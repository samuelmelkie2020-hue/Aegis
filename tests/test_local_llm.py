"""Tests for the local-model brain: the default way Aegis thinks.

Nothing here talks to the internet. HTTP is monkeypatched, or a fake Ollama is
started on a loopback port — the same contract the real code uses.
"""

from __future__ import annotations

import json

import httpx
import pytest

from aegis import config, local_llm

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _response(status=200, payload=None, url="http://127.0.0.1:11434/v1/models"):
    request = httpx.Request("GET", url)
    if payload is None:
        return httpx.Response(status, request=request)
    return httpx.Response(status, json=payload, request=request)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly if a test accidentally reaches a real host."""
    real_get, real_post = httpx.get, httpx.post

    def guarded_get(url, *a, **kw):
        if not local_llm.is_local_url(str(url)):
            raise AssertionError(f"unexpected outbound request: {url}")
        return real_get(url, *a, **kw)

    def guarded_post(url, *a, **kw):
        if not local_llm.is_local_url(str(url)):
            raise AssertionError(f"unexpected outbound request: {url}")
        return real_post(url, *a, **kw)

    monkeypatch.setattr(httpx, "get", guarded_get)
    monkeypatch.setattr(httpx, "post", guarded_post)


@pytest.fixture(autouse=True)
def clean_pull():
    """Each test starts with no pull in flight."""
    local_llm._set_pull(
        running=False, model="", status="", completed=0, total=0, error="", cancelled=False
    )
    yield
    local_llm._set_pull(
        running=False, model="", status="", completed=0, total=0, error="", cancelled=False
    )


# ---------------------------------------------------------------------------
# Local-ness: the security boundary
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:11434/v1",
        "http://localhost:11434",
        "http://LOCALHOST:1234/v1",
        "http://[::1]:8080/v1",
        "https://127.0.0.1:8443/v1",
        "http://0.0.0.0:11434",
        "http://127.0.0.2:11434",
    ],
)
def test_loopback_urls_are_local(url):
    assert local_llm.is_local_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://api.openai.com/v1",
        "http://192.168.1.50:11434/v1",   # LAN is not "this computer"
        "http://127.0.0.1.nip.io:11434",  # resolves to loopback, but is not one
        "http://localhost.evil.com/v1",
        "file:///etc/passwd",
        "ftp://127.0.0.1/v1",
        "",
        "not a url",
        "http://127.0.0.1:notaport/v1",
    ],
)
def test_everything_else_is_not_local(url):
    assert local_llm.is_local_url(url) is False


def test_a_lookalike_host_cannot_claim_to_be_local():
    """The whole privacy promise rests on this: a remote host must never pass."""
    assert local_llm.is_local_url("https://localhost.attacker.example/v1") is False
    assert local_llm.is_local_url("http://127.0.0.1.evil.example/v1") is False


def test_origin_and_normalise_base():
    assert local_llm.origin("http://127.0.0.1:11434/v1") == "http://127.0.0.1:11434"
    assert local_llm.origin("http://127.0.0.1:11434") == "http://127.0.0.1:11434"
    assert local_llm.origin("nonsense") == ""
    assert local_llm.normalise_base("http://localhost:11434") == "http://localhost:11434/v1"
    assert local_llm.normalise_base("http://localhost:11434/") == "http://localhost:11434/v1"
    assert local_llm.normalise_base("http://localhost:11434/v1") == "http://localhost:11434/v1"
    assert local_llm.normalise_base("") == ""


# ---------------------------------------------------------------------------
# Configuration: local is the default, and `local` cannot be faked
# ---------------------------------------------------------------------------

def test_defaults_point_at_this_pc_and_need_no_key(sandbox):
    config.CONFIG_PATH.unlink(missing_ok=True)
    cfg = config.load_config()
    assert local_llm.is_local_url(cfg["llm"]["base_url"])
    assert cfg["llm"]["api_key"] == ""
    assert cfg["llm"]["local"] is True


def test_a_client_cannot_declare_a_remote_endpoint_local(sandbox):
    """`local` is derived from the URL, so the UI cannot be told a lie."""
    config.save_config(
        {"llm": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini", "local": True}}
    )
    assert config.load_config()["llm"]["local"] is False
    assert config.public_config(config.load_config())["llm"]["local"] is False


def test_public_config_reports_locality(sandbox):
    config.save_config(
        {"llm": {"base_url": "http://127.0.0.1:11434/v1", "model": "m", "api_key": ""}}
    )
    public = config.public_config(config.load_config())
    assert public["llm"]["local"] is True
    assert public["llm"]["api_key_set"] is False


# ---------------------------------------------------------------------------
# Hardware sizing and the recommendation
# ---------------------------------------------------------------------------

def test_hardware_budget_never_goes_negative():
    hw = local_llm.Hardware(system="Windows", machine="AMD64", cpu="x", cores=4, ram_gb=2.0)
    assert hw.budget_gb >= 0.0


def test_a_gpu_with_vram_raises_the_budget():
    cpu_only = local_llm.Hardware(system="Linux", machine="x", cpu="c", cores=8, ram_gb=16.0)
    with_gpu = local_llm.Hardware(
        system="Linux", machine="x", cpu="c", cores=8, ram_gb=16.0, gpu="RTX 3060", vram_gb=12.0
    )
    assert with_gpu.budget_gb > cpu_only.budget_gb
    assert with_gpu.budget_gb <= 12.0


def test_detect_hardware_always_answers():
    hw = local_llm.detect_hardware()
    assert hw.system and hw.machine
    assert hw.cores >= 1
    assert hw.ram_gb >= 0
    assert isinstance(hw.as_dict(), dict)


def test_recommendation_prefers_something_already_installed():
    hw = local_llm.Hardware(system="Windows", machine="AMD64", cpu="c", cores=8, ram_gb=32.0)
    choice = local_llm.recommend(hw, ["llava:7b", "llama3.2:3b"])
    assert choice["model"] == "llava:7b"  # installed AND can see
    assert choice["installed"] is True
    assert choice["vision"] is True


def test_recommendation_flags_a_text_only_install_as_blind():
    hw = local_llm.Hardware(system="Windows", machine="AMD64", cpu="c", cores=8, ram_gb=32.0)
    choice = local_llm.recommend(hw, ["llama3.2:3b"])
    assert choice["model"] == "llama3.2:3b"
    assert choice["vision"] is False
    assert "screen" in choice["detail"]


def test_recommendation_picks_a_vision_model_that_fits():
    hw = local_llm.Hardware(system="Windows", machine="AMD64", cpu="c", cores=8, ram_gb=16.0)
    choice = local_llm.recommend(hw, [])
    assert choice["vision"] is True
    assert choice["size_gb"] <= hw.budget_gb


def test_a_tiny_machine_still_gets_the_smallest_option():
    hw = local_llm.Hardware(system="Windows", machine="AMD64", cpu="c", cores=2, ram_gb=4.0)
    choice = local_llm.recommend(hw, [])
    smallest = min(size for size, _ in local_llm.MODEL_HINTS.values())
    assert choice["size_gb"] == smallest
    assert "slow" in choice["detail"]


def test_vision_detection_covers_the_common_names():
    assert local_llm.has_vision("qwen2.5vl:3b")
    assert local_llm.has_vision("llava:7b")
    assert local_llm.has_vision("moondream:latest")
    assert local_llm.has_vision("gemma3:4b")
    assert not local_llm.has_vision("llama3.2:3b")
    assert not local_llm.has_vision("qwen2.5:3b")


# ---------------------------------------------------------------------------
# Talking to a local server
# ---------------------------------------------------------------------------

def test_list_models_reads_the_openai_shape(monkeypatch):
    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: _response(200, {"data": [{"id": "a"}, {"id": "b"}]})
    )
    models, error = local_llm.list_models("http://127.0.0.1:11434")
    assert models == ["a", "b"]
    assert error == ""


def test_list_models_explains_a_refused_connection(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", boom)
    models, error = local_llm.list_models("http://127.0.0.1:11434")
    assert models == []
    assert "not running" in error
    assert "ollama serve" in error


def test_list_models_handles_a_server_with_no_models(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _response(200, {"data": []}))
    models, error = local_llm.list_models("http://127.0.0.1:11434")
    # No models is not an error: the server answered. The panel says "pick one".
    assert models == []
    assert error == ""


def test_an_open_port_that_is_not_a_model_server_is_still_reported(monkeypatch):
    """Something answering is better than "nothing is running" — say what broke."""
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _response(404))
    monkeypatch.setattr(local_llm, "port_open", lambda base, timeout=1.5: True)
    monkeypatch.setattr(local_llm, "_ollama_version", lambda base, timeout=1.5: "0.5.13")
    servers = local_llm.probe_servers()
    ollama = next(s for s in servers if s.name == "Ollama")
    assert ollama.reachable is True
    assert "answering" in ollama.detail
    assert "0.5.13" in ollama.detail


def test_status_never_raises_and_always_reports_the_hint(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _response(404))

    def boom(*a, **k):
        raise RuntimeError("hardware probe exploded")

    monkeypatch.setattr(local_llm, "detect_hardware", boom)
    report = local_llm.status()
    assert "hint" in report
    assert report["running"] is False


def test_install_hint_is_plain_and_platform_specific(monkeypatch):
    monkeypatch.setattr(local_llm.platform, "system", lambda: "Windows")
    assert "ollama.com" in local_llm.install_hint()
    monkeypatch.setattr(local_llm.platform, "system", lambda: "Darwin")
    assert "ollama.com" in local_llm.install_hint()


# ---------------------------------------------------------------------------
# Adoption at startup
# ---------------------------------------------------------------------------

def test_adopt_local_wires_up_an_installed_model(sandbox, monkeypatch):
    config.save_config({"llm": {"base_url": "http://127.0.0.1:11434/v1", "model": "", "api_key": ""}})
    monkeypatch.setattr(
        local_llm,
        "detect_running",
        lambda include=None, timeout=1.5: local_llm.LocalServer(
            name="Ollama", base_url="http://127.0.0.1:11434", models=["llava:7b"], reachable=True
        ),
    )
    result = local_llm.adopt_local()
    assert result["changed"] is True
    saved = config.load_config()["llm"]
    assert saved["model"] == "llava:7b"
    assert saved["base_url"] == "http://127.0.0.1:11434/v1"
    assert saved["api_key"] == ""


def test_adopt_local_leaves_a_saved_model_alone(sandbox, monkeypatch):
    config.save_config(
        {"llm": {"base_url": "http://127.0.0.1:11434/v1", "model": "mine:1b", "api_key": ""}}
    )
    monkeypatch.setattr(
        local_llm,
        "detect_running",
        lambda include=None, timeout=1.5: pytest.fail("must not probe when a model is saved"),
    )
    result = local_llm.adopt_local()
    assert result["changed"] is False
    assert config.load_config()["llm"]["model"] == "mine:1b"


def test_adopt_local_never_overwrites_a_remote_provider(sandbox, monkeypatch):
    config.save_config(
        {"llm": {"base_url": "https://api.openai.com/v1", "model": "", "api_key": "sk-x"}}
    )
    def must_not_probe(include=None, timeout=1.5):
        pytest.fail("a remote provider must be left alone, not probed")

    monkeypatch.setattr(local_llm, "detect_running", must_not_probe)
    result = local_llm.adopt_local()
    assert result["changed"] is False
    assert config.load_config()["llm"]["base_url"] == "https://api.openai.com/v1"


def test_adopt_local_replaces_the_old_keyless_cloud_default(sandbox, monkeypatch):
    """Pre-1.8 installs saved the OpenAI preset with no key — it cannot work.

    Leaving it would show a demo brain and an API-key field the user has no key
    for, which is exactly the confusion this release is removing.
    """
    config.save_config(
        {"llm": {"base_url": "https://api.openai.com/v1", "api_key": "", "model": "gpt-4o-mini"}}
    )
    monkeypatch.setattr(
        local_llm,
        "detect_running",
        lambda include=None, timeout=1.5: local_llm.LocalServer(
            name="Ollama", base_url="http://127.0.0.1:11434", models=["llava:7b"], reachable=True
        ),
    )
    result = local_llm.adopt_local()
    assert result["changed"] is True
    saved = config.load_config()["llm"]
    assert local_llm.is_local_url(saved["base_url"])
    assert saved["model"] == "llava:7b"


def test_adopt_local_is_quiet_when_nothing_is_running(sandbox, monkeypatch):
    config.save_config({"llm": {"base_url": "http://127.0.0.1:11434/v1", "model": ""}})
    monkeypatch.setattr(local_llm, "detect_running", lambda include=None, timeout=1.5: None)
    assert local_llm.adopt_local()["changed"] is False


def test_adopt_local_survives_a_probe_that_raises(sandbox, monkeypatch):
    config.save_config({"llm": {"base_url": "http://127.0.0.1:11434/v1", "model": ""}})

    def boom(include=None, timeout=1.5):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(local_llm, "detect_running", boom)
    assert local_llm.adopt_local()["changed"] is False


def test_startup_adoption_is_wired_into_both_entry_points():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    assert "adopt_local()" in (root / "run.py").read_text(encoding="utf-8")
    assert "adopt_local()" in (root / "aegis" / "desktop.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Pulling a model
# ---------------------------------------------------------------------------

def test_a_pull_refuses_anything_not_on_this_pc():
    result = local_llm.start_pull("llava:7b", base_url="http://192.168.1.9:11434")
    assert result["ok"] is False
    assert "this computer" in result["detail"]


def test_a_pull_needs_a_model_name():
    assert local_llm.start_pull("   ", base_url="http://127.0.0.1:11434")["ok"] is False


def test_only_one_pull_runs_at_a_time(monkeypatch):
    monkeypatch.setattr(local_llm.threading, "Thread", lambda *a, **k: _NoopThread())
    assert local_llm.start_pull("a:1b", base_url="http://127.0.0.1:11434")["ok"] is True
    second = local_llm.start_pull("b:1b", base_url="http://127.0.0.1:11434")
    assert second["ok"] is False
    assert "Already downloading" in second["detail"]


def test_cancel_with_nothing_running_is_honest():
    assert local_llm.cancel_pull()["ok"] is False


def test_the_pull_worker_streams_progress_and_keeps_the_final_count(monkeypatch):
    """A finished download must not report 0 bytes (the bar would snap to 0%)."""
    lines = [
        json.dumps({"status": "pulling manifest"}),
        json.dumps({"status": "pulling layer", "completed": 512, "total": 1024}),
        json.dumps({"status": "success"}),
    ]

    class FakeStream:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_lines(self):
            return iter(lines)

    monkeypatch.setattr(httpx, "stream", lambda *a, **k: FakeStream())
    local_llm._pull_worker("moondream", "http://127.0.0.1:11434")
    state = local_llm.pull_state()
    assert state["running"] is False
    assert state["status"] == "done"
    assert state["completed"] == 1024
    assert state["total"] == 1024


def test_the_pull_worker_reports_the_servers_own_error(monkeypatch):
    lines = [json.dumps({"error": "pull model manifest: file does not exist"})]

    class FakeStream:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_lines(self):
            return iter(lines)

    monkeypatch.setattr(httpx, "stream", lambda *a, **k: FakeStream())
    local_llm._pull_worker("nope:1b", "http://127.0.0.1:11434")
    state = local_llm.pull_state()
    assert state["running"] is False
    assert "does not exist" in state["error"]


def test_a_cancelled_pull_stops_before_finishing(monkeypatch):
    class FakeStream:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_lines(self):
            local_llm._set_pull(cancelled=True)
            for _ in range(5):
                yield json.dumps({"status": "pulling layer", "completed": 1, "total": 10})

    monkeypatch.setattr(httpx, "stream", lambda *a, **k: FakeStream())
    local_llm._pull_worker("slow:1b", "http://127.0.0.1:11434")
    assert local_llm.pull_state()["status"] == "cancelled"


def test_an_http_error_on_pull_is_surfaced(monkeypatch):
    class FakeStream:
        status_code = 500

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_lines(self):
            return iter([])

    monkeypatch.setattr(httpx, "stream", lambda *a, **k: FakeStream())
    local_llm._pull_worker("x:1b", "http://127.0.0.1:11434")
    assert "HTTP 500" in local_llm.pull_state()["error"]


class _NoopThread:
    def __init__(self, *a, **k):
        pass

    def start(self):
        pass


# ---------------------------------------------------------------------------
# The HTTP surface
# ---------------------------------------------------------------------------

def test_local_status_route_needs_the_token(anon):
    assert anon.get("/api/local/status").status_code in (401, 403)


def test_local_status_route_reports_hardware_and_a_recommendation(client, monkeypatch):
    http, auth = client
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _response(404))
    body = http.get("/api/local/status", headers=auth).json()
    assert body["ok"] is True
    assert "cpu" in body["hardware"]
    assert "recommendation" in body
    assert isinstance(body["hint"], str) and body["hint"]
    assert body["running"] is False


def test_local_status_survives_a_probe_explosion(client, monkeypatch):
    http, auth = client

    def boom(*a, **k):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(local_llm, "status", boom)
    body = http.get("/api/local/status", headers=auth).json()
    assert body["ok"] is False
    assert "Could not check" in body["detail"]
    assert body["hint"]


def test_local_pull_route_downloads_to_a_local_server(client, monkeypatch):
    http, auth = client
    config.save_config({"llm": {"base_url": "http://127.0.0.1:11434/v1", "model": "m"}})
    started = {}
    monkeypatch.setattr(
        local_llm,
        "start_pull",
        lambda model, base_url="": started.update(model=model, base_url=base_url)
        or {"ok": True, "detail": f"Downloading {model}…"},
    )
    response = http.post("/api/local/pull", headers=auth, json={"model": "llava:7b"})
    assert response.status_code == 200
    assert started == {"model": "llava:7b", "base_url": "http://127.0.0.1:11434"}


def test_local_pull_route_refuses_a_remote_provider(client, monkeypatch):
    """A pull must never be aimed at a cloud endpoint, even if asked politely."""
    http, auth = client
    config.save_config(
        {"llm": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini", "api_key": "k"}}
    )
    called = []
    monkeypatch.setattr(local_llm, "start_pull", lambda *a, **k: called.append(1) or {"ok": True})
    response = http.post("/api/local/pull", headers=auth, json={"model": "x"})
    assert response.status_code == 409
    assert called == []
    assert "this computer" in response.json()["detail"]


def test_local_pull_cancel_route(client):
    http, auth = client
    assert http.post("/api/local/pull/cancel", headers=auth).status_code == 409


def test_local_setup_saves_a_working_local_config(client, monkeypatch):
    http, auth = client
    monkeypatch.setattr(
        local_llm,
        "detect_running",
        lambda include=None, timeout=1.5: local_llm.LocalServer(
            name="Ollama", base_url="http://127.0.0.1:11434", models=["qwen2.5vl:3b"], reachable=True
        ),
    )
    body = http.post("/api/local/setup", headers=auth).json()
    assert body["ok"] is True
    assert body["model"] == "qwen2.5vl:3b"
    saved = config.load_config()["llm"]
    assert saved["base_url"] == "http://127.0.0.1:11434/v1"
    assert saved["model"] == "qwen2.5vl:3b"
    assert saved["api_key"] == ""


def test_local_setup_explains_what_to_install_when_nothing_runs(client, monkeypatch):
    http, auth = client
    monkeypatch.setattr(local_llm, "detect_running", lambda include=None, timeout=1.5: None)
    response = http.post("/api/local/setup", headers=auth)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "ollama" in detail.lower()
    assert response.json()["hint"]


def test_local_setup_does_not_save_without_a_model(client, monkeypatch):
    http, auth = client
    monkeypatch.setattr(
        local_llm,
        "detect_running",
        lambda include=None, timeout=1.5: local_llm.LocalServer(
            name="Ollama", base_url="http://127.0.0.1:11434", models=[], reachable=True
        ),
    )
    before = config.load_config()["llm"]["model"]
    response = http.post("/api/local/setup", headers=auth)
    assert response.status_code == 409
    # It may *suggest* a model, but it must not save one that is not installed.
    assert response.json()["recommendation"]["model"]
    assert config.load_config()["llm"]["model"] == before


def test_local_setup_survives_a_probe_explosion(client, monkeypatch):
    http, auth = client

    def boom(include=None, timeout=1.5):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(local_llm, "detect_running", boom)
    response = http.post("/api/local/setup", headers=auth)
    assert response.status_code == 500
    assert response.json()["ok"] is False


# ---------------------------------------------------------------------------
# Test connection, in local terms
# ---------------------------------------------------------------------------

def test_test_connection_says_nothing_is_running(client, monkeypatch):
    http, auth = client
    monkeypatch.setattr(local_llm, "port_open", lambda base, timeout=1.5: False)
    body = http.post(
        "/api/test-connection",
        headers=auth,
        json={"settings": {"llm": {"base_url": "http://127.0.0.1:11434/v1", "model": "m"}}},
    ).json()
    assert body["ok"] is False
    assert "Nothing is running" in body["detail"]
    assert "API key" not in body["detail"]


def test_test_connection_asks_for_a_model_when_the_server_is_up(client, monkeypatch):
    http, auth = client
    monkeypatch.setattr(local_llm, "list_models", lambda base, timeout=4.0: (["a:1b"], ""))
    body = http.post(
        "/api/test-connection",
        headers=auth,
        json={"settings": {"llm": {"base_url": "http://127.0.0.1:11434/v1", "model": ""}}},
    ).json()
    assert body["ok"] is False
    assert "choose a model" in body["detail"]
    assert "a:1b" in body["detail"]


def test_test_connection_names_the_missing_model(client, monkeypatch):
    http, auth = client
    monkeypatch.setattr(local_llm, "list_models", lambda base, timeout=4.0: (["a:1b"], ""))
    body = http.post(
        "/api/test-connection",
        headers=auth,
        json={"settings": {"llm": {"base_url": "http://127.0.0.1:11434/v1", "model": "zzz:9b"}}},
    ).json()
    assert body["ok"] is False
    assert "not installed" in body["detail"]
    assert "zzz:9b" in body["detail"]
    assert "ollama pull" in body["detail"]


def test_test_connection_passes_when_the_model_is_installed(client, monkeypatch):
    http, auth = client
    monkeypatch.setattr(local_llm, "list_models", lambda base, timeout=4.0: (["a:1b"], ""))
    body = http.post(
        "/api/test-connection",
        headers=auth,
        json={"settings": {"llm": {"base_url": "http://127.0.0.1:11434/v1", "model": "a:1b"}}},
    ).json()
    assert body["ok"] is True
    assert "no api key" in body["detail"].lower()
    assert "api key rejected" not in body["detail"].lower()


def test_a_cloud_endpoint_still_gets_cloud_advice(client, monkeypatch):
    """The local messages must not leak onto the cloud path."""
    http, auth = client
    response = _response(404, {"error": "nope"}, url="https://api.openai.com/v1/chat/completions")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: response)
    body = http.post(
        "/api/test-connection",
        headers=auth,
        json={"settings": {"llm": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"}}},
    ).json()
    assert body["ok"] is False
    assert "check the base URL and model name" in body["detail"]


# ---------------------------------------------------------------------------
# Finding a model that is already on the disk ("where did my download go?")
# ---------------------------------------------------------------------------

def _fake_ollama_store(root, models: dict[str, list[int]]):
    """Write a minimal but *accurately shaped* Ollama model folder.

    Layout: ``manifests/registry.ollama.ai/library/<name>/<tag>`` refers to
    ``blobs/sha256-<hex>``. Getting this wrong is how a scanner reports "no
    models" on a machine that has 20 GB of them.
    """
    import hashlib

    blobs = root / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    for name, sizes in models.items():
        model, _, tag = name.partition(":")
        tag = tag or "latest"
        layers = []
        for index, size in enumerate(sizes):
            data = (f"{name}-{index}".encode() * (size // max(len(name), 1) + 1))[:size]
            digest = hashlib.sha256(data).hexdigest()
            (blobs / f"sha256-{digest}").write_bytes(data)
            layers.append({"mediaType": "application/vnd.ollama.image.model", "digest": f"sha256:{digest}"})
        manifest = root / "manifests" / "registry.ollama.ai" / "library" / model / tag
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"layers": layers}), encoding="utf-8")
    return root


def test_models_on_disk_are_found_without_a_running_server(tmp_path, monkeypatch):
    """The whole premise: the folder is readable even when Ollama is not up."""
    store = _fake_ollama_store(tmp_path / "models", {"llama3.2:3b": [2_000_000], "qwen2.5vl:3b": [3_000_000]})
    monkeypatch.setenv("OLLAMA_MODELS", str(store))
    found = local_llm.scan_disk()

    names = [m["name"] for m in found["models"]]
    assert names == ["qwen2.5vl:3b", "llama3.2:3b"], names       # largest first
    assert found["models"][0]["vision"] is True
    assert found["models"][1]["vision"] is False
    assert found["total_gb"] > 0.004
    assert str(store) == found["dirs"][0]["path"]


def test_a_manifest_named_latest_is_reported_without_a_tag(tmp_path, monkeypatch):
    """Ollama serves ``llava/latest`` as plain ``llava``; saying ``llava:latest``
    would name a model the server does not have."""
    store = _fake_ollama_store(tmp_path / "models", {"llava": [1_000]})
    monkeypatch.setenv("OLLAMA_MODELS", str(store))
    assert [m["name"] for m in local_llm.scan_disk()["models"]] == ["llava"]


def test_an_unreadable_manifest_does_not_hide_the_others(tmp_path, monkeypatch):
    store = _fake_ollama_store(tmp_path / "models", {"llama3.2:3b": [1000]})
    broken = store / "manifests" / "registry.ollama.ai" / "library" / "broken" / "1b"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("OLLAMA_MODELS", str(store))
    names = [m["name"] for m in local_llm.scan_disk()["models"]]
    assert "llama3.2:3b" in names and "broken:1b" in names


def test_nowhere_to_look_is_reported_honestly(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path / "empty"))
    monkeypatch.setattr(local_llm, "_home", lambda: tmp_path / "nohome")
    monkeypatch.setattr(local_llm.platform, "system", lambda: "Linux")
    found = local_llm.scan_disk()
    assert found["models"] == [] and found["dirs"] == []
    assert local_llm.model_dir().name == "models"  # a sensible default, not a crash


def test_known_model_names_never_raises(monkeypatch):
    monkeypatch.setattr(local_llm, "scan_disk", lambda **kw: 1 / 0)
    assert local_llm.known_model_names() == []


def test_starting_a_server_that_is_already_running_does_nothing(monkeypatch):
    """Starting a second Ollama would fight the first one for the port."""
    from aegis.local_llm import LocalServer

    monkeypatch.setattr(
        local_llm, "probe_servers",
        lambda **kw: [LocalServer(name="Ollama", base_url="http://127.0.0.1:11434", reachable=True)],
    )
    started = []
    monkeypatch.setattr(local_llm.subprocess, "Popen", lambda *a, **k: started.append(a))
    result = local_llm.start_server()
    assert result["ok"] is True and started == []
    assert "already running" in result["detail"]


def test_starting_a_server_is_refused_when_ollama_is_not_installed(monkeypatch):
    from aegis.local_llm import LocalServer

    monkeypatch.setattr(local_llm, "probe_servers", lambda **kw: [LocalServer("x", "http://127.0.0.1:1")])
    monkeypatch.setattr(local_llm.shutil, "which", lambda name: None)
    result = local_llm.start_server()
    assert result["ok"] is False
    assert "not on your PATH" in result["detail"]


def test_load_installed_prefers_a_server_that_already_has_models(monkeypatch):
    from aegis.local_llm import LocalServer

    monkeypatch.setattr(
        local_llm, "probe_servers",
        lambda **kw: [LocalServer("Ollama", "http://127.0.0.1:11434", models=["llama3.2:3b"], reachable=True)],
    )
    called = []
    monkeypatch.setattr(local_llm, "start_server", lambda *a, **k: called.append(1) or {"ok": True})
    result = local_llm.load_installed()
    assert result["ok"] is True and result["models"] == ["llama3.2:3b"]
    assert called == [], "nothing should be started when the server is already up"


def test_load_installed_says_so_when_there_is_nothing_anywhere(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path / "nothing"))
    monkeypatch.setattr(local_llm, "_home", lambda: tmp_path / "nohome")
    monkeypatch.setattr(local_llm.platform, "system", lambda: "Linux")
    monkeypatch.setattr(local_llm, "probe_servers", lambda **kw: [])
    monkeypatch.setattr(local_llm, "start_server", lambda *a, **k: {"ok": False, "detail": "no"})
    result = local_llm.load_installed()
    assert result["ok"] is False
    assert "No model files and no local server" in result["detail"]


def test_load_installed_explains_models_that_the_server_will_not_serve(tmp_path, monkeypatch):
    store = _fake_ollama_store(tmp_path / "models", {"llama3.2:3b": [1000]})
    monkeypatch.setenv("OLLAMA_MODELS", str(store))
    monkeypatch.setattr(local_llm, "probe_servers", lambda **kw: [])
    monkeypatch.setattr(local_llm, "start_server", lambda *a, **k: {"ok": False, "detail": "did not answer"})
    result = local_llm.load_installed()
    assert result["ok"] is False
    assert "llama3.2:3b" in result["detail"] and "not serving them" in result["detail"]


def test_status_reports_disk_models_the_server_is_not_listing(tmp_path, monkeypatch):
    """Settings needs to know "it is on disk but not served" to offer the fix."""
    store = _fake_ollama_store(tmp_path / "models", {"llama3.2:3b": [1000], "llava": [2000]})
    monkeypatch.setenv("OLLAMA_MODELS", str(store))
    monkeypatch.setattr(local_llm, "probe_servers", lambda **kw: [])
    report = local_llm.status()
    assert {m["name"] for m in report["on_disk_not_listed"]} == {"llama3.2:3b", "llava"}
    assert report["disk"]["models"], "the disk scan must survive into the report"


# ---------------------------------------------------------------------------
# /api/local/adopt — the "find the model I downloaded" button
# ---------------------------------------------------------------------------

def test_adopt_route_needs_the_token(anon):
    response = anon.post("/api/local/adopt", headers={"Host": "localhost:8000"}, json={})
    assert response.status_code == 401


def test_adopt_finds_a_model_on_disk_and_selects_it(client, tmp_path, monkeypatch, sandbox):
    store = _fake_ollama_store(tmp_path / "models", {"llama3.2:3b": [1000], "qwen2.5vl:3b": [2000]})
    monkeypatch.setenv("OLLAMA_MODELS", str(store))
    monkeypatch.setattr(local_llm, "probe_servers", lambda **kw: [])
    monkeypatch.setattr(local_llm, "start_server", lambda *a, **k: {"ok": True, "detail": "started"})
    monkeypatch.setattr(local_llm, "detect_running", lambda **kw: None)

    http, auth = client
    response = http.post("/api/local/adopt", headers=auth, json={})
    body = response.json()

    assert response.status_code == 200, body
    # A vision model wins, because Aegis's core skill is looking at the screen.
    assert body["model"] == "qwen2.5vl:3b"
    assert body["vision"] is True
    assert config.load_config()["llm"]["model"] == "qwen2.5vl:3b"


def test_adopt_can_select_one_named_model(client, tmp_path, monkeypatch, sandbox):
    store = _fake_ollama_store(tmp_path / "models", {"llama3.2:3b": [1000], "llava": [2000]})
    monkeypatch.setenv("OLLAMA_MODELS", str(store))
    monkeypatch.setattr(local_llm, "probe_servers", lambda **kw: [])
    monkeypatch.setattr(local_llm, "detect_running", lambda **kw: None)

    http, auth = client
    body = http.post("/api/local/adopt", headers=auth, json={"model": "llama3.2:3b"}).json()
    assert body["model"] == "llama3.2:3b"
    assert body["vision"] is False
    assert "cannot see your screen" in body["detail"]


def test_adopt_refuses_a_model_that_is_not_on_this_pc(client, tmp_path, monkeypatch, sandbox):
    """Writing a name the server does not have would 404 on the next message."""
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path / "empty"))
    monkeypatch.setattr(local_llm, "probe_servers", lambda **kw: [])
    http, auth = client
    response = http.post("/api/local/adopt", headers=auth, json={"model": "gpt-5-imaginary"})
    assert response.status_code == 404
    assert "not on this PC" in response.json()["detail"]


def test_adopt_says_what_to_do_when_there_is_no_model(client, tmp_path, monkeypatch, sandbox):
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path / "empty"))
    monkeypatch.setattr(local_llm, "_home", lambda: tmp_path / "nohome")
    monkeypatch.setattr(local_llm.platform, "system", lambda: "Linux")
    monkeypatch.setattr(local_llm, "probe_servers", lambda **kw: [])
    monkeypatch.setattr(local_llm, "start_server", lambda *a, **k: {"ok": False, "detail": "no"})
    http, auth = client
    response = http.post("/api/local/adopt", headers=auth, json={})
    assert response.status_code == 409
    assert response.json()["detail"]


def test_status_route_exposes_what_is_on_disk(client, tmp_path, monkeypatch, sandbox):
    store = _fake_ollama_store(tmp_path / "models", {"llava": [5000]})
    monkeypatch.setenv("OLLAMA_MODELS", str(store))
    monkeypatch.setattr(local_llm, "probe_servers", lambda **kw: [])
    http, auth = client
    body = http.get("/api/local/status", headers=auth).json()
    assert [m["name"] for m in body["disk"]["models"]] == ["llava"]
    assert [m["name"] for m in body["on_disk_not_listed"]] == ["llava"]


# ---------------------------------------------------------------------------
# The local_models tool: "where is my model?" asked in the chat
# ---------------------------------------------------------------------------

def test_the_agent_can_answer_where_the_models_are(tmp_path, monkeypatch):
    from aegis.tools import execute_tool

    store = _fake_ollama_store(tmp_path / "models", {"qwen2.5vl:3b": [3_000_000]})
    monkeypatch.setenv("OLLAMA_MODELS", str(store))
    monkeypatch.setattr(local_llm, "probe_servers", lambda **kw: [])
    result = execute_tool("local_models", {})
    assert result["ok"] is True
    assert "qwen2.5vl:3b" in result["result"]
    assert str(store) in result["result"], "the folder is the answer to 'where is it'"


def test_the_agent_is_told_when_nothing_is_installed(tmp_path, monkeypatch):
    from aegis.tools import execute_tool

    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path / "empty"))
    monkeypatch.setattr(local_llm, "probe_servers", lambda **kw: [])
    result = execute_tool("local_models", {})
    assert result["ok"] is True
    assert "No AI model files" in result["result"]


def test_a_404_names_the_models_that_do_exist(tmp_path, monkeypatch):
    """The commonest local failure: the config names a model the server lacks."""
    from aegis.llm import _friendly_model_error

    store = _fake_ollama_store(tmp_path / "models", {"qwen2.5vl:3b": [1000]})
    monkeypatch.setenv("OLLAMA_MODELS", str(store))
    monkeypatch.setattr(
        local_llm, "status",
        lambda **kw: {"servers": [{"reachable": True, "models": ["llava"]}]},
    )
    message = _friendly_model_error(
        RuntimeError("LLM error 404: model not found"),
        {"base_url": "http://127.0.0.1:11434/v1", "model": "gemma3"},
    )
    assert "gemma3" in message
    assert "llava" in message                 # what the server does have
    assert "Find the model I downloaded" in message


# ---------------------------------------------------------------------------
# Model *files*: LM Studio, Jan, GPT4All, a GGUF in Downloads
# ---------------------------------------------------------------------------

def _fake_gguf(path, size=1000):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"G" * size)
    return path


def test_a_gguf_file_is_named_the_way_a_server_would_accept_it(tmp_path, monkeypatch):
    """`…-q4_k_m.gguf` must not become part of the model name."""
    downloads = tmp_path / "Downloads"
    _fake_gguf(downloads / "qwen2.5-vl-3b-instruct-q4_k_m.gguf", 3_000)
    monkeypatch.setattr(local_llm, "_home", lambda: tmp_path)
    monkeypatch.setattr(local_llm.platform, "system", lambda: "Linux")
    found = local_llm.scan_weights(extra_dirs=[downloads])

    assert [m["name"] for m in found] == ["qwen2.5-vl-3b-instruct"]
    assert found[0]["format"] == "gguf"
    assert found[0]["vision"] is True
    assert found[0]["size_bytes"] == 3_000


def test_the_file_scan_is_bounded(tmp_path):
    folder = tmp_path / "models"
    for index in range(20):
        _fake_gguf(folder / f"model-{index}.gguf", 10)
    found = local_llm.scan_weights(limit_files=5, extra_dirs=[folder])
    assert len(found) == 5, "an unbounded walk of a 20 GB folder would freeze Settings"


def test_the_file_scan_respects_its_depth_limit(tmp_path):
    deep = tmp_path / "models" / "a" / "b" / "c" / "d" / "e"
    _fake_gguf(deep / "deep.gguf")
    shallow = tmp_path / "models" / "shallow.gguf"
    _fake_gguf(shallow)
    found = local_llm.scan_weights(max_depth=2, extra_dirs=[tmp_path / "models"])
    names = [m["name"] for m in found]
    assert "shallow" in names and "deep" not in names


def test_non_weight_files_are_ignored(tmp_path):
    folder = tmp_path / "models"
    _fake_gguf(folder / "readme.txt")
    _fake_gguf(folder / "notes.json")
    _fake_gguf(folder / "real.gguf")
    found = local_llm.scan_weights(extra_dirs=[folder])
    assert [m["name"] for m in found] == ["real"]


def test_a_custom_folder_can_be_scanned(tmp_path, monkeypatch):
    monkeypatch.setenv("LMSTUDIO_MODELS", str(tmp_path / "elsewhere"))
    _fake_gguf(tmp_path / "elsewhere" / "mistral-7b.gguf", 50)
    monkeypatch.setattr(local_llm.platform, "system", lambda: "Linux")
    monkeypatch.setattr(local_llm, "_home", lambda: tmp_path / "nohome")
    found = local_llm.scan_weights()
    assert [m["name"] for m in found] == ["mistral-7b"]
    assert "LMSTUDIO_MODELS" in found[0]["folder_note"]


def test_find_all_models_merges_both_worlds(tmp_path, monkeypatch):
    store = _fake_ollama_store(tmp_path / "ollama" / "models", {"llama3.2:3b": [1000]})
    monkeypatch.setenv("OLLAMA_MODELS", str(store))
    monkeypatch.setenv("LMSTUDIO_MODELS", str(tmp_path / "lmstudio"))
    _fake_gguf(tmp_path / "lmstudio" / "mistral-7b.gguf", 2_000_000)
    monkeypatch.setattr(local_llm, "probe_servers", lambda **kw: [])

    merged = local_llm.find_all_models()
    assert set(merged["names"]) == {"llama3.2:3b", "mistral-7b"}
    # Bytes, because 2 MB of GGUF is 0.0 GB at two decimals — a test asserting on
    # the rounded figure would pass for the wrong reason or fail for no reason.
    assert merged["total_bytes"] == 1_000 + 2_000_000


def test_more_local_servers_are_probed():
    """People do not only use Ollama and LM Studio."""
    names = {name for name, _ in local_llm.KNOWN_ENDPOINTS}
    assert {"Ollama", "LM Studio", "llama.cpp"} <= names
    assert {"Jan", "vLLM"} <= names
    # Every endpoint must be loopback, or "local" would be a lie.
    for _, url in local_llm.KNOWN_ENDPOINTS:
        assert local_llm.is_local_url(url), url


def test_a_model_file_with_no_server_is_explained_not_hidden(tmp_path, monkeypatch):
    """A GGUF is not something Aegis can load itself — say whose folder it is."""
    monkeypatch.setenv("LMSTUDIO_MODELS", str(tmp_path / "lmstudio"))
    _fake_gguf(tmp_path / "lmstudio" / "mistral-7b.gguf", 2000)
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path / "empty"))
    monkeypatch.setattr(local_llm, "probe_servers", lambda **kw: [])
    monkeypatch.setattr(local_llm, "start_server", lambda *a, **k: {"ok": False, "detail": "no"})

    result = local_llm.load_installed()
    assert result["ok"] is False
    assert result["models"] == ["mistral-7b"]
    assert "LM Studio" in result["detail"] or "Jan" in result["detail"]
    assert "no local server is running" in result["detail"]


def test_status_route_reports_every_installed_model(client, tmp_path, monkeypatch, sandbox):
    monkeypatch.setenv("LMSTUDIO_MODELS", str(tmp_path / "lmstudio"))
    _fake_gguf(tmp_path / "lmstudio" / "mistral-7b.gguf", 2000)
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path / "empty"))
    monkeypatch.setattr(local_llm, "probe_servers", lambda **kw: [])
    http, auth = client
    body = http.get("/api/local/status", headers=auth).json()
    assert "mistral-7b" in body["all_models"]
    assert [w["name"] for w in body["weights"]] == ["mistral-7b"]
