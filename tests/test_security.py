"""Access control: the token, the Host check, and the session cookie.

These are the tests that matter most. Aegis can run shell commands, so every
one of these represents "a stranger on the network can control this PC".
"""

from __future__ import annotations

import os
import stat
import sys

import pytest

API_ROUTES = [
    ("get", "/api/health", None),
    ("get", "/api/settings", None),
    ("get", "/api/bootstrap", None),
    ("get", "/api/screenshot", None),
    ("get", "/api/stats", None),
    ("post", "/api/settings", {"settings": {}}),
    ("post", "/api/chat", {"message": "hi"}),
    ("post", "/api/approve", {"approval_id": "ap-1-x"}),
    ("post", "/api/approve/deny", {"approval_id": "ap-1-x"}),
    ("post", "/api/test-connection", {"settings": {}}),
]


@pytest.mark.parametrize("method,path,body", API_ROUTES)
def test_every_api_route_requires_the_token(anon, method, path, body):
    kwargs = {"headers": {"Host": "localhost:8000"}}
    if body is not None:
        kwargs["json"] = body
    response = getattr(anon, method)(path, **kwargs)
    assert response.status_code == 401, f"{path} answered without a token"
    # the 401 must not hint at whether a command would have worked
    assert "PWNED" not in response.text


def test_unauthenticated_approve_cannot_run_a_command(anon, paths):
    """The original critical bug: remote code execution with no auth at all."""
    marker = paths.home / "pwned"
    response = anon.post(
        "/api/approve",
        headers={"Host": "localhost:8000"},
        json={"approval_id": "ap-1-x", "name": "run_command", "args": {"command": f"touch {marker}"}},
    )
    assert response.status_code == 401
    assert not marker.exists()


def test_token_works_as_a_header(client):
    http, auth = client
    assert http.get("/api/health", headers=auth).status_code == 200


@pytest.mark.skipif(
    sys.platform.startswith("win"), reason="windows protects the token with an ACL, not mode bits"
)
def test_token_file_is_owner_only(client, paths):
    from aegis.security import get_token

    get_token()
    mode = stat.S_IMODE(os.stat(paths.token).st_mode)
    assert mode == 0o600, f"token file is {oct(mode)}, expected 0600"


def test_token_file_is_restricted_on_creation(paths, monkeypatch):
    """Every platform must lock the token down, by whichever mechanism it has."""
    from aegis import config, security

    restricted = []
    monkeypatch.setattr(config, "_restrict", lambda path: restricted.append(str(path)))
    monkeypatch.setattr(security, "TOKEN_FILE", paths.token.parent / "fresh_token")
    monkeypatch.delenv("AEGIS_TOKEN", raising=False)

    token = security.get_token()

    assert token and restricted == [str(security.TOKEN_FILE)]


def test_token_is_stable_across_calls(paths):
    from aegis.security import get_token

    first = get_token()
    assert get_token() == first
    assert paths.token.read_text(encoding="utf-8").strip() == first


def test_env_token_override_is_honoured(sandbox, monkeypatch):
    from aegis.security import get_token

    monkeypatch.setenv("AEGIS_TOKEN", "fixed-token-for-tests")
    assert get_token() == "fixed-token-for-tests"


def test_access_link_sets_an_httponly_cookie(anon, auth):
    token = auth["X-Aegis-Token"]
    response = anon.get(f"/?token={token}", headers={"Host": "localhost:8000"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookie = response.headers["set-cookie"].lower()
    assert "aegis_session=" in cookie
    assert "httponly" in cookie
    assert "samesite=strict" in cookie


def test_bad_access_link_shows_a_banner(anon):
    response = anon.get("/?token=wrong", headers={"Host": "localhost:8000"})
    assert "no longer valid" in response.text


def test_loopback_visit_is_trusted_enough_to_mint_a_session(anon):
    """On loopback the caller is this machine, so no copy-paste is required.

    Even a stale ?token= link still gets a session, because the request already
    came from localhost — the link only matters for a non-loopback host.
    """
    response = anon.get("/", headers={"Host": "localhost:8000"})
    assert "aegis_session" in response.cookies


def test_non_loopback_visit_does_not_mint_a_session(anon):
    """A remote caller gets no cookie unless it presents the real token."""
    response = anon.get("/", headers={"Host": "192.168.1.50:8000"})
    assert "aegis_session" not in response.cookies
    # ... and its screenshot/API calls stay locked
    assert anon.get("/api/health", headers={"Host": "192.168.1.50:8000"}).status_code == 401


@pytest.mark.parametrize(
    "host",
    ["evil.example.com", "attacker.test:8000", "aegis.evil-mirror.io"],
)
def test_dns_rebinding_hosts_are_rejected(client, host):
    http, auth = client
    response = http.get("/api/settings", headers={**auth, "Host": host})
    assert response.status_code == 403


@pytest.mark.parametrize("host", ["localhost:8000", "127.0.0.1:8000", "[::1]:8000", "192.168.1.20:8000"])
def test_local_and_private_hosts_are_accepted(client, host):
    http, auth = client
    assert http.get("/api/health", headers={**auth, "Host": host}).status_code == 200


def test_allowed_hosts_env_extends_the_list(client, monkeypatch):
    http, auth = client
    monkeypatch.setenv("AEGIS_ALLOWED_HOSTS", "aegis.lan")
    assert http.get("/api/health", headers={**auth, "Host": "aegis.lan:8000"}).status_code == 200


def test_cross_origin_post_is_rejected(client):
    http, auth = client
    response = http.post(
        "/api/settings", headers={**auth, "Origin": "http://evil.example.com"}, json={"settings": {}}
    )
    assert response.status_code == 403


def test_same_origin_post_is_accepted(client):
    http, auth = client
    response = http.post(
        "/api/settings",
        headers={**auth, "Host": "localhost:8000", "Origin": "http://localhost:8000"},
        json={"settings": {"assistant_name": "Friday"}},
    )
    assert response.status_code == 200


def test_cross_site_subresource_cannot_read_the_token(anon):
    """A page elsewhere must not be able to <script src> the token out."""
    response = anon.get(
        "/config.js",
        headers={"Host": "localhost:8000", "Sec-Fetch-Site": "cross-site"},
    )
    assert 'window.AEGIS_TOKEN = ""' in response.text


def test_config_js_serves_the_token_to_localhost(anon):
    response = anon.get(
        "/config.js", headers={"Host": "localhost:8000", "Sec-Fetch-Site": "same-origin"}
    )
    assert 'window.AEGIS_TOKEN = ""' not in response.text
    assert "window.AEGIS_TOKEN = \"" in response.text


def test_unknown_host_never_receives_the_token(anon):
    response = anon.get("/config.js", headers={"Host": "random.example.com", "Sec-Fetch-Site": "none"})
    assert 'window.AEGIS_TOKEN = ""' in response.text


def test_no_third_party_host_is_trusted_out_of_the_box(anon):
    """Shipped software must not hand its token to somebody else's domain.

    This used to allow ``*.e2b.app`` unconditionally — an artifact of developing
    Aegis inside a hosted preview sandbox. On every machine that installed the
    app, that meant a domain we do not control was allowed to fetch the access
    token; the port is loopback-only, but the rule had no business shipping.
    """
    response = anon.get(
        "/config.js", headers={"Host": "8000-abc123.e2b.app", "Sec-Fetch-Site": "none"}
    )
    assert 'window.AEGIS_TOKEN = ""' in response.text


def test_preview_host_support_is_opt_in(anon, monkeypatch):
    """A sandbox that really needs it opts in through the environment."""
    from aegis import security

    monkeypatch.setenv("AEGIS_PREVIEW_HOST_SUFFIXES", ".sandbox.example")
    monkeypatch.setattr(
        security, "PREVIEW_HOST_SUFFIXES", (".sandbox.example",)
    )
    response = anon.get(
        "/config.js", headers={"Host": "8000-abc123.sandbox.example", "Sec-Fetch-Site": "none"}
    )
    assert 'window.AEGIS_TOKEN = ""' not in response.text


def test_static_assets_stay_public(anon):
    """The page must load before it has a session, or nothing works."""
    for path in ("/", "/static/app.js", "/static/style.css"):
        assert anon.get(path, headers={"Host": "localhost:8000"}).status_code == 200


def test_pages_carry_no_frame_denial_headers(anon):
    """The app is designed to run in an embedded preview pane."""
    response = anon.get("/", headers={"Host": "localhost:8000"})
    for header in ("x-frame-options", "content-security-policy"):
        assert header not in {k.lower() for k in response.headers}, (
            f"{header} would break the preview pane"
        )


# ---------------------------------------------------------------------------
# Outbound endpoint policy (the Test-connection probe)
# ---------------------------------------------------------------------------

def test_private_addresses_are_not_probed(anon, auth):
    """Test connection must not become a request-forger for the user's LAN."""
    response = anon.post(
        "/api/test-connection",
        headers=auth,
        json={"settings": {"llm": {
            "base_url": "http://192.168.1.1/v1", "api_key": "k", "model": "m",
        }}},
    )
    body = response.json()
    assert body["ok"] is False
    assert "private address" in body["detail"]


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.5:8000/v1",
        "http://169.254.169.254/latest/meta-data",   # cloud metadata
        "http://172.16.0.1/v1",
        "http://[fd00::1]/v1",
        "file:///etc/passwd",
        "ftp://example.com/v1",
        "gopher://example.com/v1",
    ],
)
def test_dangerous_probe_targets_are_refused(anon, auth, url):
    response = anon.post(
        "/api/test-connection",
        headers=auth,
        json={"settings": {"llm": {"base_url": url, "api_key": "k", "model": "m"}}},
    )
    assert response.json()["ok"] is False


def test_loopback_and_public_endpoints_are_still_allowed():
    """The guard must not break the two configurations the app actually uses."""
    from aegis.safety import outbound_reason

    assert outbound_reason("http://127.0.0.1:11434/v1") is None
    assert outbound_reason("http://localhost:1234/v1") is None
    assert outbound_reason("https://api.openai.com/v1") is None
    assert outbound_reason("https://openrouter.ai/api/v1") is None


def test_the_probe_guard_names_the_problem_and_the_way_out():
    from aegis.safety import outbound_reason

    reason = outbound_reason("http://192.168.1.50:8080/v1")
    assert reason and "192.168.1.50" in reason
