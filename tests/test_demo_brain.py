"""The demo brain: intent parsing with no API key.

Pure-function tests over the phrasings the README advertises, so the "works
instantly with no key" promise keeps being true.
"""

from __future__ import annotations

import pytest

DEMO_CFG = {"assistant_name": "Jarvis", "safe_mode": False, "llm": {"api_key": ""}}


@pytest.fixture()
def demo(monkeypatch):
    from jarvis import llm, tools

    calls = []

    def fake_execute(name, args):
        calls.append((name, args))
        if name == "see_screen":
            return {"ok": True, "result": "Screenshot captured", "image": "QUJD"}
        if name == "system_info":
            return {"ok": True, "result": "CPU: fine"}
        if name == "list_files":
            return {"ok": True, "result": "(empty folder)"}
        return {"ok": True, "result": f"ran {name}"}

    monkeypatch.setattr(llm, "execute_tool", fake_execute)
    monkeypatch.setattr(tools, "execute_tool", fake_execute)

    def run(message, cfg=None):
        events = []
        reply = llm._demo_turn(message, cfg or DEMO_CFG, lambda t, d: events.append((t, d)))
        return reply, calls, events

    return run


@pytest.mark.parametrize("message", [
    "what's on my screen?",
    "what am I looking at",
    "what is open?",
])
def test_screen_intent_captures_the_screen(demo, message):
    _, calls, events = demo(message)
    assert calls and calls[0][0] == "see_screen"
    assert "screenshot" in [t for t, _ in events]


@pytest.mark.parametrize("message", [
    "how is my pc doing?",
    "system status",
    "how much memory do I have",
    "battery",
])
def test_system_intent_reports_status(demo, message):
    reply, calls, _ = demo(message)
    assert calls and calls[0][0] == "system_info"
    assert "CPU" in reply


def test_open_intent_passes_a_clean_app_name(demo):
    _, calls, _ = demo("open notepad")
    assert calls[0] == ("open_app", {"app": "notepad"})


def test_open_intent_does_not_swallow_the_rest_of_the_sentence(demo):
    """README promises "Open Chrome and go to youtube.com"."""
    _, calls, _ = demo("open chrome and go to youtube.com")
    app = calls[0][1]["app"]
    assert "and go to" not in app, f"app name was over-captured: {app!r}"
    assert app.startswith("chrome")


def test_run_intent_keeps_the_command_verbatim(demo):
    _, calls, _ = demo("run git status")
    assert calls[0] == ("run_command", {"command": "git status"})


def test_list_intent_maps_friendly_folders(demo):
    _, calls, _ = demo("list my downloads")
    assert calls[0][0] == "list_files"
    assert calls[0][1]["path"].endswith("Downloads")


@pytest.mark.parametrize("message", ["help", "what can you do?", "hello"])
def test_help_intent_explains_capabilities(demo, message):
    reply, calls, _ = demo(message)
    assert not calls
    assert "demo brain" in reply.lower()


def test_unknown_intent_suggests_examples(demo):
    reply, calls, _ = demo("compose a symphony")
    assert not calls
    assert "demo brain" in reply.lower()


def test_demo_mode_asks_for_approval_when_safe_mode_is_on(demo):
    """The demo brain must honour safe mode exactly like the real one."""
    cfg = {**DEMO_CFG, "safe_mode": True}
    reply, calls, events = demo("run echo hi", cfg)
    assert "approval" in [t for t, _ in events]
    assert not calls  # nothing executed
    assert "approval" in reply.lower()


def test_demo_mode_matches_the_api_decision(paths):
    from jarvis import config, llm

    config.save_config({"llm": {"api_key": "", "base_url": "https://api.openai.com/v1"}})
    assert config.is_demo_mode(config.load_config()) is True
    assert llm.is_demo_mode(config.load_config()) is True
