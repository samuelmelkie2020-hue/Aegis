"""The SSE contract (#11) and the queue bridge (#12).

These tests pin the wire format, because a second client (or a rewrite of the
web UI) depends on it and nothing else documents it.
"""

from __future__ import annotations

import json
import time

import pytest


def parse_sse(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: "):]))
    return events


@pytest.fixture()
def scripted(monkeypatch):
    """Replace the agent with a scripted emitter."""
    from jarvis import server

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
    from jarvis import server

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
    from jarvis import server

    assert server.MAX_QUEUE_ITEMS > 0
    assert server.QUEUE_PUT_TIMEOUT > 0


def test_a_worker_that_dies_still_terminates_the_stream(client, monkeypatch):
    """A broken agent must close the stream, not hang the browser forever."""
    from jarvis import server

    def explode(*args, **kwargs):
        raise ValueError("internal")

    monkeypatch.setattr(server, "run_agent_turn", explode)
    http, auth = client
    events = parse_sse(http.post("/api/chat", headers=auth, json={"message": "hi"}).text)
    assert events[-1]["type"] == "done"
    assert any(e["type"] == "error" for e in events)
