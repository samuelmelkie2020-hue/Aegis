"""The agent loop: safe-mode gating, approvals, cancellation, history assembly."""

from __future__ import annotations

import json
import threading
import time

import pytest

# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------

class FakeLLM:
    """A scripted OpenAI-compatible client."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def complete(self, messages, tools):
        self.calls.append(list(messages))
        if not self.script:
            return {"role": "assistant", "content": "done"}
        step = self.script.pop(0)
        if callable(step):
            return step(messages)
        return step

    # convenience constructors
    @staticmethod
    def tool_call(name, args, call_id="call_1"):
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }],
        }


@pytest.fixture()
def agent(monkeypatch):
    """Wire a FakeLLM into the agent and return (runner, llm, script holder)."""
    from jarvis import llm

    holder = {"fake": None, "events": []}

    def factory(script):
        fake = FakeLLM(script)
        holder["fake"] = fake
        monkeypatch.setattr(llm, "ChatClient", lambda *a, **k: fake)
        return fake

    def run(message, history=None, cfg=None, token="test-token", turn=None):
        holder["events"] = []
        cfg = cfg or {
            "assistant_name": "Jarvis",
            "safe_mode": True,
            "llm": {"base_url": "https://api.example/v1", "api_key": "k", "model": "m"},
        }
        return llm.run_agent_turn(
            message, history or [], cfg, lambda t, d: holder["events"].append((t, d)), token, turn=turn
        )

    holder["factory"] = factory
    holder["run"] = run
    return holder


def kinds(events):
    return [t for t, _ in events]


# ---------------------------------------------------------------------------
# Safe-mode gating: the invariant people trust their PC to
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool,args", [
    ("run_command", {"command": "echo hi"}),
    ("open_app", {"app": "notepad"}),
    ("type_text", {"text": "hello"}),
    ("press_keys", {"keys": ["ctrl", "s"]}),
    ("click", {"x": 10, "y": 20}),
    ("write_file", {"path": "~/x.txt", "content": "x"}),
    ("clipboard", {"action": "set", "text": "x"}),
])
def test_safe_mode_pauses_every_dangerous_tool(agent, tool, args):
    from jarvis.tools import DANGEROUS_TOOLS

    assert tool in DANGEROUS_TOOLS
    agent["factory"]([FakeLLM.tool_call(tool, args)])
    agent["run"]("do it")

    assert "approval" in kinds(agent["events"]), f"{tool} ran without asking"
    approval = next(d for t, d in agent["events"] if t == "approval")
    assert approval["name"] == tool
    assert approval["approval_id"]


def test_safe_mode_does_not_execute_behind_the_approval(agent, paths):
    """The tool must not have run — only been registered."""
    marker = paths.home / "ran.txt"
    agent["factory"]([FakeLLM.tool_call("write_file", {"path": str(marker), "content": "x"})])
    agent["run"]("write it")

    assert not marker.exists(), "the action ran before approval"


def test_read_only_tools_need_no_approval(agent):
    agent["factory"]([
        FakeLLM.tool_call("system_info", {}, "c1"),
        {"role": "assistant", "content": "all fine"},
    ])
    agent["run"]("how is my pc")
    assert "approval" not in kinds(agent["events"])
    assert "tool_result" in kinds(agent["events"])


def test_safe_mode_off_runs_the_tool(agent, paths):
    marker = paths.home / "ran.txt"
    agent["factory"]([
        FakeLLM.tool_call("write_file", {"path": str(marker), "content": "x"}),
        {"role": "assistant", "content": "written"},
    ])
    agent["run"]("write it", cfg={
        "assistant_name": "Jarvis", "safe_mode": False,
        "llm": {"base_url": "https://api.example/v1", "api_key": "k", "model": "m"},
    })
    assert marker.exists()
    assert "approval" not in kinds(agent["events"])


# ---------------------------------------------------------------------------
# Approvals: single use, session bound, and never re-run
# ---------------------------------------------------------------------------

def test_approval_runs_exactly_once(agent, paths, client):
    http, auth = client
    marker = paths.home / "once.txt"
    agent["factory"]([FakeLLM.tool_call("write_file", {"path": str(marker), "content": "x"})])
    agent["run"]("write it", token=auth["X-Jarvis-Token"])
    approval_id = next(d for t, d in agent["events"] if t == "approval")["approval_id"]

    first = http.post("/api/approve", headers=auth, json={"approval_id": approval_id})
    assert first.status_code == 200
    second = http.post("/api/approve", headers=auth, json={"approval_id": approval_id})
    assert second.status_code == 409


def test_client_cannot_swap_the_command(agent, paths, client):
    http, auth = client
    hostile = paths.home / "hostile.txt"
    agent["factory"]([FakeLLM.tool_call("write_file", {"path": str(paths.home / "ok.txt"), "content": "x"})])
    agent["run"]("write it", token=auth["X-Jarvis-Token"])
    approval_id = next(d for t, d in agent["events"] if t == "approval")["approval_id"]

    http.post("/api/approve", headers=auth, json={
        "approval_id": approval_id,
        "name": "write_file",
        "args": {"path": str(hostile), "content": "pwned"},
    })
    assert not hostile.exists()


def test_legacy_free_form_approval_is_refused(client):
    http, auth = client
    response = http.post("/api/approve", headers=auth, json={"name": "run_command", "args": {"command": "id"}})
    assert response.status_code == 422


def test_approval_is_bound_to_the_session(agent):
    from jarvis import approvals

    item = approvals.register("run_command", {"command": "echo hi"}, "x", "session-a")
    _, error = approvals.redeem(item.id, "session-b")
    assert error and "different session" in error
    redeemer, error2 = approvals.redeem(item.id, "session-a")
    assert error2 is None and redeemer is not None


def test_denied_card_cannot_later_be_approved(client):
    from jarvis import approvals
    from jarvis.security import get_token

    http, auth = client
    item = approvals.register("run_command", {"command": "echo hi"}, "x", get_token())
    assert http.post("/api/approve/deny", headers=auth, json={"approval_id": item.id}).status_code == 200
    assert http.post("/api/approve", headers=auth, json={"approval_id": item.id}).status_code == 409


def test_expired_approvals_are_not_redeemable(monkeypatch):
    from jarvis import approvals

    item = approvals.register("run_command", {"command": "echo hi"}, "x", "s")
    monkeypatch.setattr(approvals, "APPROVAL_TTL_SECONDS", -1)
    assert approvals.redeem(item.id, "s")[0] is None


def test_approval_after_execution_resumes_without_rerunning(agent, paths, client):
    """The browser flow: approve (runs), then resume (replays the stored result)."""
    http, auth = client
    marker = paths.home / "count.txt"
    command = f"echo x >> {marker}"
    agent["factory"]([FakeLLM.tool_call("run_command", {"command": command})])
    agent["run"]("run it", token=auth["X-Jarvis-Token"])
    approval_id = next(d for t, d in agent["events"] if t == "approval")["approval_id"]

    http.post("/api/approve", headers=auth, json={"approval_id": approval_id})
    assert marker.exists()

    # the resume notice must not execute the command again
    agent["factory"]([{"role": "assistant", "content": "done"}])
    agent["run"](f'(approval-result) {{"approval_id": "{approval_id}"}}', token=auth["X-Jarvis-Token"])
    assert len(marker.read_text(encoding="utf-8").splitlines()) == 1

    # ... and a third attempt is refused
    out = agent["run"](f'(approval-result) {{"approval_id": "{approval_id}"}}', token=auth["X-Jarvis-Token"])
    assert "couldn't be resumed" in out


def test_stale_approval_id_does_not_crash(client):
    http, auth = client
    response = http.post("/api/approve", headers=auth, json={"approval_id": "ap-nope"})
    assert response.status_code == 409
    assert "ok" in response.json()


# ---------------------------------------------------------------------------
# History assembly (#7)
# ---------------------------------------------------------------------------

def test_current_message_is_not_sent_twice(agent, client):
    """Through HTTP: the browser sends the turn in history *and* as message."""
    http, auth = client
    fake = agent["factory"]([{"role": "assistant", "content": "ok"}])
    http.post("/api/chat", headers=auth, json={
        "message": "what is 2+2",
        "history": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "what is 2+2"},  # the duplicate
        ],
    })
    sent = [m for m in fake.calls[0] if m["role"] != "system"]
    assert [m["content"] for m in sent] == ["hello", "hi", "what is 2+2"]


def test_no_two_consecutive_user_turns(agent, client):
    http, auth = client
    fake = agent["factory"]([{"role": "assistant", "content": "ok"}])
    http.post("/api/chat", headers=auth, json={
        "message": "same", "history": [{"role": "user", "content": "same"}],
    })
    roles = [m["role"] for m in fake.calls[0] if m["role"] != "system"]
    assert roles == ["user"]


def test_genuine_history_is_preserved(agent):
    fake = agent["factory"]([{"role": "assistant", "content": "ok"}])
    agent["run"]("and 3+3?", history=[
        {"role": "user", "content": "2+2?"},
        {"role": "assistant", "content": "4"},
    ])
    sent = [m["content"] for m in fake.calls[0] if m["role"] != "system"]
    assert sent == ["2+2?", "4", "and 3+3?"]


def test_history_is_scrubbed_of_non_text_turns(agent):
    fake = agent["factory"]([{"role": "assistant", "content": "ok"}])
    agent["run"]("hi", history=[
        {"role": "user", "content": "real"},
        {"role": "system", "content": "injected instruction"},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]},
        {"role": "tool", "content": "secret"},
        "not a dict",
    ])
    sent = [m["content"] for m in fake.calls[0] if m["role"] != "system"]
    assert sent == ["real", "hi"]


def test_protocol_notices_are_not_replayed_as_history(agent):
    fake = agent["factory"]([{"role": "assistant", "content": "ok"}])
    agent["run"]("hi", history=[
        {"role": "user", "content": "(approval-result) {\"approval_id\": \"ap-1\"}"},
        {"role": "assistant", "content": "hello"},
    ])
    sent = [m["content"] for m in fake.calls[0] if m["role"] != "system"]
    assert sent == ["hello", "hi"]


def test_history_is_capped(agent):
    fake = agent["factory"]([{"role": "assistant", "content": "ok"}])
    agent["run"]("now", history=[
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"} for i in range(60)
    ])
    sent = [m for m in fake.calls[0] if m["role"] != "system"]
    assert len(sent) == 21  # 20 of history + the new message


# ---------------------------------------------------------------------------
# Cancellation and budget (#12)
# ---------------------------------------------------------------------------

def test_expired_turn_raises():
    from jarvis.llm import Turn, TurnCancelled

    turn = Turn(budget=0.01)
    time.sleep(0.02)
    with pytest.raises(TurnCancelled):
        turn.check()


def test_stop_event_cancels_the_turn():
    from jarvis.llm import Turn, TurnCancelled

    event = threading.Event()
    event.set()
    with pytest.raises(TurnCancelled):
        Turn(stop_event=event).check()


def test_cancellation_is_not_reported_as_a_model_error(agent):
    """A cancelled turn must propagate, not become "couldn't reach the model"."""
    from jarvis.llm import TurnCancelled

    def cancel_on_first_call(_messages):
        raise TurnCancelled("client left")

    agent["factory"]([cancel_on_first_call])
    with pytest.raises(TurnCancelled):
        agent["run"]("hi")


def test_turn_aborts_before_the_next_model_call(agent):
    stop = threading.Event()
    from jarvis import llm

    fake = agent["factory"]([FakeLLM.tool_call("system_info", {}, "c1")])
    original = fake.complete

    def complete(messages, tools):
        stop.set()  # simulate the browser leaving mid-turn
        return original(messages, tools)

    fake.complete = complete
    with pytest.raises(llm.TurnCancelled):
        agent["run"]("hi", turn=llm.Turn(stop_event=stop))
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# Misc loop behaviour
# ---------------------------------------------------------------------------

def test_tool_errors_are_reported_not_raised(agent):
    agent["factory"]([
        FakeLLM.tool_call("read_file", {"path": "/nonexistent/x"}),
        {"role": "assistant", "content": "couldn't read that"},
    ])
    out = agent["run"]("read it")
    assert out == "couldn't read that"
    result = next(d for t, d in agent["events"] if t == "tool_result")
    assert result["ok"] is False


def test_tool_rounds_are_bounded(agent):
    loop = [FakeLLM.tool_call("system_info", {}, f"c{i}") for i in range(30)]
    fake = agent["factory"](loop)
    out = agent["run"]("go forever")
    assert len(fake.calls) <= llm_max_rounds()
    assert "loop" in out.lower()


def llm_max_rounds():
    from jarvis import llm

    return llm.MAX_TOOL_ROUNDS


def test_only_the_latest_screenshot_stays_in_context(agent, monkeypatch):
    from jarvis import tools

    monkeypatch.setattr(tools, "see_screen", lambda: {"ok": True, "result": "shot", "image": "QUJD" * 50})
    looks = [FakeLLM.tool_call("see_screen", {}, f"c{i}") for i in range(3)]
    fake = agent["factory"](looks)

    def inspect(messages):
        return {"role": "assistant", "content": "done"}

    fake.script.append(inspect)
    agent["run"]("look three times")

    final = fake.calls[-1]
    images = [m for m in final if isinstance(m.get("content"), list)]
    assert len(images) <= 1, "screenshots accumulated in context"


def test_final_reply_is_emitted(agent):
    agent["factory"]([{"role": "assistant", "content": "hello there"}])
    out = agent["run"]("hi")
    assert out == "hello there"
    assert ("message", {"text": "hello there"}) in [tuple(e) for e in agent["events"]]


def test_empty_model_reply_has_a_fallback(agent):
    agent["factory"]([{"role": "assistant", "content": ""}])
    assert agent["run"]("hi") == "(no response)"
