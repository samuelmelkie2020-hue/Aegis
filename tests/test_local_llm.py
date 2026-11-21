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
