"""The SSE contract (#11) and the queue bridge (#12).

These tests pin the wire format, because a second client (or a rewrite of the
web UI) depends on it and nothing else documents it.
"""

from __future__ import annotations

import json
import time

import pytest


def parse_sse(text: str) -> list[dict]:
    """Every ``data:`` event except the leading ``turn`` handshake.

    The handshake names the turn so the UI can stop it (see /api/chat/stop); it
    is not part of the reply contract, so the tests below describe the reply.
    """
    events = []
    for line in text.splitlines():
        if line.startswith("data: "):
            event = json.loads(line[len("data: "):])
            if event.get("type") == "turn":
                continue
            events.append(event)
    return events


@pytest.fixture()
def scripted(monkeypatch):
    """Replace the agent with a scripted emitter."""
    from aegis import server

    state = {"emit": None, "outcome": None}

    def install(events, *, error=None, text="the answer", result="the answer"):
        def fake(message, history, cfg, emit, token="", turn=None):
            for etype, data in events:
                emit(etype, data)
            if error:
                raise RuntimeError(error)
            return text

        monkeypatch.setattr(server, "run_agent_turn", fake)
        return state

    return install


def test_success_stream_ends_with_done(client, scripted):
    http, auth = client
    scripted([("status", {"message": "Thinking…"}), ("message", {"text": "hi there"})], text="hi there")
    events = parse_sse(http.post("/api/chat", headers=auth, json={"message": "hi"}).text)

    assert [e["type"] for e in events] == ["status", "message", "done"]
    assert events[-1]["ok"] is True
    assert events[-1]["text"] == "hi there"


def test_done_carries_the_turn_result_even_without_a_message_event(client, scripted):
    """The UI uses done.text when no message event arrived (e.g. a truncated stream)."""
    http, auth = client
    scripted([], text="only in done")
    events = parse_sse(http.post("/api/chat", headers=auth, json={"message": "hi"}).text)
    assert events == [{"type": "done", "ok": True, "text": "only in done"}]


def test_failure_emits_exactly_one_error(client, scripted):
    """The original bug: error text arrived twice, so the UI showed it twice."""
    http, auth = client
    scripted([], error="kaboom")
    text = http.post("/api/chat", headers=auth, json={"message": "hi"}).text
    events = parse_sse(text)

    assert [e["type"] for e in events] == ["error", "done"]
    assert events[0]["message"].endswith("kaboom")
    # done must not repeat the text that error already carried
    assert "text" not in events[-1]
    assert events[-1]["ok"] is False
    assert text.count("kaboom") == 1


def test_exactly_one_terminal_event_even_with_many_events(client, scripted):
    http, auth = client
    scripted([
        ("status", {"message": "Thinking…"}),
        ("tool_start", {"name": "system_info", "args": {}}),
        ("tool_result", {"name": "system_info", "result": "ok", "ok": True}),
        ("message", {"text": "fine"}),
    ])
    events = parse_sse(http.post("/api/chat", headers=auth, json={"message": "hi"}).text)
    assert [e["type"] for e in events].count("done") == 1
    assert events[-1]["type"] == "done"


def test_events_are_json_objects_on_one_line(client, scripted):
    http, auth = client
    scripted([("message", {"text": "line one\nline two"})])
    text = http.post("/api/chat", headers=auth, json={"message": "hi"}).text
    for line in text.splitlines():
        if line.startswith("data: "):
            json.loads(line[6:])  # must not raise
            assert "\n" not in line


def test_streaming_response_headers(client, scripted):
    http, auth = client
    scripted([("message", {"text": "x"})])
    response = http.post("/api/chat", headers=auth, json={"message": "hi"})
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"


def test_heartbeat_keeps_a_slow_turn_alive(client, monkeypatch):
    """No bytes for minutes is exactly what proxies drop."""
    from aegis import server

    monkeypatch.setattr(server, "HEARTBEAT_SECONDS", 0.1)

    def slow(*args, **kwargs):
        time.sleep(0.35)
        return "finally"

    monkeypatch.setattr(server, "run_agent_turn", slow)
    http, auth = client
    text = http.post("/api/chat", headers=auth, json={"message": "hi"}).text

    assert ": ping" in text
    assert parse_sse(text)[-1]["text"] == "finally"


def test_queue_is_bounded(monkeypatch):
    """An abandoned turn must not grow memory without limit."""
    from aegis import server

    assert server.MAX_QUEUE_ITEMS > 0
    assert server.QUEUE_PUT_TIMEOUT > 0


def test_a_worker_that_dies_still_terminates_the_stream(client, monkeypatch):
    """A broken agent must close the stream, not hang the browser forever."""
    from aegis import server

    def explode(*args, **kwargs):
        raise ValueError("internal")

    monkeypatch.setattr(server, "run_agent_turn", explode)
    http, auth = client
    events = parse_sse(http.post("/api/chat", headers=auth, json={"message": "hi"}).text)
    assert events[-1]["type"] == "done"
    assert any(e["type"] == "error" for e in events)


# ---------------------------------------------------------------------------
# Stopping a turn (#stop)
# ---------------------------------------------------------------------------

def test_the_stream_announces_a_turn_id_first(client, scripted):
    """The Stop button needs something to name before it can stop anything."""
    http, auth = client
    scripted([("message", {"text": "hi"})], text="hi")
    events = [json.loads(line[6:]) for line in
              http.post("/api/chat", headers=auth, json={"message": "hi"}).text.splitlines()
              if line.startswith("data: ")]
    assert events[0]["type"] == "turn"
    assert events[0]["turn_id"].startswith("turn-")


def test_stop_is_idempotent_and_never_errors_on_a_finished_turn(client):
    http, auth = client
    first = http.post("/api/chat/stop", headers=auth, json={"turn_id": "turn-999999"})
    second = http.post("/api/chat/stop", headers=auth, json={"turn_id": "turn-999999"})
    assert first.status_code == second.status_code == 200
    assert second.json()["stopped"] is False
    assert "already finished" in second.json()["detail"]


def test_stop_requires_a_turn_id(client):
    http, auth = client
    response = http.post("/api/chat/stop", headers=auth, json={})
    assert response.status_code == 400


def test_stop_requires_the_access_token(anon):
    """Otherwise any page on this machine could cancel the user's work."""
    response = anon.post(
        "/api/chat/stop", headers={"Host": "localhost:8000"}, json={"turn_id": "turn-1"}
    )
    assert response.status_code == 401


def test_a_stopped_turn_reports_stopped_not_failed(client, monkeypatch):
    """End to end: press Stop mid-stream and read the last two events."""
    import threading

    from aegis import server
    from aegis.llm import TurnCancelled

    started = threading.Event()

    def slow(message, history, cfg, emit, token="", turn=None):
        emit("message", {"text": "let me think"})
        started.set()
        for _ in range(500):
            turn.check()          # raises once /api/chat/stop is called
            time.sleep(0.01)
        raise AssertionError("the turn was never stopped")

    monkeypatch.setattr(server, "run_agent_turn", slow)
    http, auth = client

    def stop_once_the_turn_is_visible():
        started.wait(5)
        turn_id = next(iter(server._active_turns))
        http.post("/api/chat/stop", headers=auth, json={"turn_id": turn_id})

    stopper = threading.Thread(target=stop_once_the_turn_is_visible, daemon=True)
    stopper.start()
    text = http.post("/api/chat", headers=auth, json={"message": "hi"}).text
    stopper.join(5)

    events = parse_sse(text)
    assert [e["type"] for e in events] == ["message", "done"]
    assert events[-1]["stopped"] is True
    assert events[-1]["ok"] is True
    assert "error" not in [e["type"] for e in events]
    assert TurnCancelled  # the machinery under test is the cancelling path
