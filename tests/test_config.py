"""Config: validation, atomic writes, secret hygiene, corruption recovery."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

from aegis import config


def test_defaults_when_no_file(paths):
    paths.config.unlink(missing_ok=True)
    cfg = config.load_config()
    assert cfg["assistant_name"] == "Aegis"
    assert cfg["safe_mode"] is True
    assert cfg["llm"]["api_key"] == ""


def test_save_then_load_roundtrip(paths):
    config.save_config({"assistant_name": "Friday", "llm": {"model": "gpt-4o"}})
    cfg = config.load_config()
    assert cfg["assistant_name"] == "Friday"
    assert cfg["llm"]["model"] == "gpt-4o"
    # untouched keys survive a partial patch
    assert cfg["llm"]["base_url"].startswith("https://")


def test_unknown_keys_are_ignored(paths):
    config.save_config({"assistant_name": "Friday", "not_a_real_setting": {"x": 1}})
    assert "not_a_real_setting" not in config.load_config()


@pytest.mark.parametrize(
    "patch",
    [
        {"llm": "oops"},
        {"llm": 5},
        {"safe_mode": "yes"},
        {"assistant_name": 12},
        {"llm": {"api_key": 99}},
    ],
)
def test_malformed_payloads_are_rejected(paths, patch):
    with pytest.raises(config.SettingsError):
        config.save_config(patch)


def test_a_rejected_payload_changes_nothing(paths):
    config.save_config({"assistant_name": "Friday"})
    with pytest.raises(config.SettingsError):
        config.save_config({"llm": "oops"})
    assert config.load_config()["assistant_name"] == "Friday"


def test_poisoned_file_does_not_500_the_app(paths, client):
    """A bad value on disk is repaired to its default, not an exception."""
    http, auth = client
    paths.config.write_text(json.dumps({"llm": "oops", "safe_mode": "nope"}), encoding="utf-8")
    assert http.get("/api/health", headers=auth).status_code == 200
    cfg = config.load_config()
    assert cfg["llm"]["api_key"] == ""
    assert cfg["safe_mode"] is True


@pytest.mark.skipif(sys.platform.startswith("win"), reason="windows uses an ACL, not mode bits")
def test_settings_file_is_owner_only(paths):
    import os
    import stat

    config.save_config({"assistant_name": "Friday"})
    assert stat.S_IMODE(os.stat(paths.config).st_mode) == 0o600


def test_saving_restricts_the_file_on_every_platform(paths, monkeypatch):
    """The API key must be owner-only: mode bits on POSIX, an ACL on Windows."""
    calls = []
    monkeypatch.setattr(config, "_restrict", lambda path: calls.append(str(path)))

    config.save_config({"assistant_name": "Friday"})

    assert calls, "the config was written without restricting permissions"
    assert calls[0].endswith(".tmp"), "restrict before the key lands in the file"


def test_save_is_atomic_under_concurrency(paths):
    errors: list[str] = []

    def hammer(tag: str) -> None:
        for i in range(20):
            try:
                config.save_config({"assistant_name": f"{tag}{i}"})
            except Exception as exc:  # noqa: BLE001 - the assertion is "no errors"
                errors.append(str(exc))

    threads = [threading.Thread(target=hammer, args=(t,)) for t in "abcd"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert config.load_config()["assistant_name"][0] in "abcd"


def test_no_temp_files_left_behind(paths):
    config.save_config({"assistant_name": "Friday"})
    leftovers = [p.name for p in paths.config.parent.iterdir() if p.name.startswith(".aegis_config")]
    assert leftovers == []


def test_corrupt_file_is_quarantined_not_destroyed(paths):
    paths.config.write_text('{"llm": {"api_key": "sk-REAL-KEY"},', encoding="utf-8")
    cfg = config.load_config()

    assert cfg["llm"]["api_key"] == ""  # falls back to defaults
    problem = config.config_problem()
    assert problem is not None and problem["kind"] == "unreadable"
    # the user's key is still recoverable by hand
    backup = problem["backup"]
    assert backup and "sk-REAL-KEY" in Path(backup).read_text(encoding="utf-8")
    assert not paths.config.exists()


def test_healthy_file_reports_no_problem(paths):
    config.save_config({"assistant_name": "Friday"})
    config.load_config()
    assert config.config_problem() is None


# ---------------------------------------------------------------------------
# Secret hygiene: the API key must never be handed to a browser
# ---------------------------------------------------------------------------

def test_public_config_never_includes_the_key(paths):
    cfg = config.save_config({"llm": {"api_key": "sk-very-secret-value-1234"}})
    public = config.public_config(cfg)

    assert "sk-very-secret-value-1234" not in json.dumps(public)
    assert public["llm"]["api_key_set"] is True
    assert public["llm"]["api_key_masked"] == "sk-…1234"


def test_masking_edges():
    assert config.mask_key("") == ""
    assert config.mask_key("short") == "•••••"
    assert config.mask_key("sk-abcdefghijklmnop") == "sk-…mnop"


def test_is_demo_mode_matches_a_missing_key():
    assert config.is_demo_mode({"llm": {"base_url": "https://api.openai.com/v1", "api_key": ""}})
    assert not config.is_demo_mode({"llm": {"base_url": "https://api.openai.com/v1", "api_key": "k"}})
    # a local model needs no key
    assert not config.is_demo_mode({"llm": {"base_url": "http://localhost:11434/v1", "api_key": ""}})


def test_is_demo_mode_survives_a_poisoned_mapping():
    assert config.is_demo_mode({"llm": "oops"}) is True


def test_health_demo_flag_agrees_with_is_demo_mode(client):
    http, auth = client
    body = http.get("/api/health", headers=auth).json()
    assert body["demo"] is config.is_demo_mode(config.load_config())


def test_get_settings_never_leaks_the_key(client, paths):
    http, auth = client
    http.post("/api/settings", headers=auth, json={"settings": {"llm": {"api_key": "sk-leak-me-9876"}}})
    for path in ("/api/settings", "/api/bootstrap"):
        assert "sk-leak-me-9876" not in http.get(path, headers=auth).text


def test_omitting_the_key_keeps_it_and_empty_clears_it(client):
    http, auth = client
    http.post("/api/settings", headers=auth, json={"settings": {"llm": {"api_key": "sk-keep-123456"}}})
    http.post("/api/settings", headers=auth, json={"settings": {"llm": {"model": "gpt-4o"}}})
    assert http.get("/api/settings", headers=auth).json()["llm"]["api_key_set"] is True

    http.post("/api/settings", headers=auth, json={"settings": {"llm": {"api_key": ""}}})
    assert http.get("/api/settings", headers=auth).json()["llm"]["api_key_set"] is False


def test_bad_settings_payload_returns_400(client):
    http, auth = client
    response = http.post("/api/settings", headers=auth, json={"settings": {"llm": "oops"}})
    assert response.status_code == 400
    assert "llm" in response.json()["detail"]
    # and the server is still healthy afterwards
    assert http.get("/api/health", headers=auth).status_code == 200
