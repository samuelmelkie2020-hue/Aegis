"""Access control for the local Aegis server.

Aegis can run shell commands and read files, so the HTTP surface is only
reachable by someone who is meant to use it. Three cheap, independent checks
do that:

1. **Bind localhost by default.** ``run.py`` listens on 127.0.0.1 unless
   ``AEGIS_HOST`` says otherwise, so the LAN cannot reach the port at all.
2. **A shared secret.** A random token (``.aegis_token`` next to the config,
   or ``AEGIS_TOKEN``) is required on every ``/api`` call, either as a
   session cookie or as the ``X-Aegis-Token`` header. Local programs and
   other users on the machine cannot guess it.
3. **Host + Origin validation.** A page on ``evil.com`` whose DNS is rebound to
   127.0.0.1 still sends ``Host: evil.com``; that is rejected. State-changing
   requests whose ``Origin`` is not this server are rejected too.

Deliberately *not* set: ``X-Frame-Options`` / CSP ``frame-ancestors``. The app
is designed to be previewed inside an embedded pane, and blocking framing would
break that; the token is what protects the API, not the frame policy.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status

from .config import CONFIG_PATH

log = logging.getLogger("aegis.security")

SESSION_COOKIE = "aegis_session"
TOKEN_HEADER = "x-aegis-token"
TOKEN_FILE = Path(os.environ.get("AEGIS_TOKEN_FILE") or CONFIG_PATH.parent / ".aegis_token")

LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}
LOOPBACK_BINDS = {"127.0.0.1", "localhost", "::1"}

# Hosts a *hosted preview pane* serves the app from. Empty by default, on
# purpose: this is software the user installs on their own PC, and a shipped
# allowlist for somebody else's domain would trust that domain with the access
# token on every machine that installs it. A sandbox that needs it exports the
# suffix instead — e.g. AEGIS_PREVIEW_HOST_SUFFIXES=.e2b.app.
PREVIEW_HOST_SUFFIXES: tuple[str, ...] = tuple(
    s.strip() for s in os.environ.get("AEGIS_PREVIEW_HOST_SUFFIXES", "").split(",") if s.strip()
)

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------

def get_token() -> str:
    """The shared secret, created on first use and persisted next to the config."""
    env = os.environ.get("AEGIS_TOKEN")
    if env:
        return env
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if token:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    try:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(TOKEN_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        # Mode bits are not access control on Windows; an ACL is.
        from .config import _restrict

        _restrict(TOKEN_FILE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
    except OSError as exc:  # read-only checkout, etc. — keep running, memory only
        log.warning("Could not persist the access token to %s: %s", TOKEN_FILE, exc)
    return token


def token_matches(candidate: str | None) -> bool:
    if not candidate:
        return False
    return secrets.compare_digest(candidate, get_token())


# ---------------------------------------------------------------------------
# Bind address / where to expose the token
# ---------------------------------------------------------------------------

def is_loopback_bind(host: str | None = None) -> bool:
    return (host or os.environ.get("AEGIS_HOST", "127.0.0.1")).strip() in LOOPBACK_BINDS


def embed_token() -> bool:
    """Whether ``/`` may put the token in the page for the browser to pick up.

    Only when the server is bound to loopback: then the only possible callers
    are processes on this machine, and asking the user to copy a token would be
    pure friction. Explicitly overridable for unusual setups.
    """
    forced = os.environ.get("AEGIS_EMBED_TOKEN")
    if forced is not None:
        return forced.strip().lower() in {"1", "true", "yes", "on"}
    return is_loopback_bind()


def is_preview_host(host_header: str) -> bool:
    """True for a hosted preview pane's host name (see PREVIEW_HOST_SUFFIXES)."""
    name = _hostname(host_header)
    return bool(name) and any(
        suffix and name.endswith(suffix.strip().lower())
        for suffix in PREVIEW_HOST_SUFFIXES
    )


def embed_token_for(host_header: str) -> bool:
    """Whether the page served *for this host* may hold the access token.

    This keys off the host the request arrived on, not the bind address, so a
    LAN-exposed server still refuses to hand its token to an unknown host name.

    * loopback host  — the normal local case; the caller is this machine.
    * preview host   — only when the operator opted in with
      AEGIS_PREVIEW_HOST_SUFFIXES (empty by default: this is installed software,
      not a hosted demo, so no third-party domain is trusted out of the box).
    * anything else  — no token in the page. Use the URL the terminal printed.
    """
    forced = os.environ.get("AEGIS_EMBED_TOKEN")
    if forced is not None:
        return forced.strip().lower() in {"1", "true", "yes", "on"}
    if _hostname(host_header) in LOOPBACK_NAMES:
        return True
    return is_preview_host(host_header)


# ---------------------------------------------------------------------------
# Host / Origin checks
# ---------------------------------------------------------------------------

def _hostname(host_header: str) -> str:
    host = (host_header or "").strip().lower()
    if host.startswith("["):  # [::1]:8000
        end = host.find("]")
        return host[1:end] if end > 0 else host
    return host.rsplit(":", 1)[0] if ":" in host else host


def host_allowed(host_header: str) -> bool:
    """Reject Host headers that a DNS-rebinding attack would have to use."""
    if os.environ.get("AEGIS_ALLOWED_HOSTS", "").strip() == "*":
        return True
    name = _hostname(host_header)
    if not name:
        return True  # HTTP/1.0 clients without a Host header
    if name in LOOPBACK_NAMES:
        return True
    extra = {
        h.strip().lower()
        for h in os.environ.get("AEGIS_ALLOWED_HOSTS", "").split(",")
        if h.strip()
    }
    if name in extra:
        return True
    if any(suffix and name.endswith(suffix.strip().lower()) for suffix in PREVIEW_HOST_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def origin_allowed(request: Request) -> bool:
    """True when the request did not come from another origin's page."""
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return True  # curl / test client: already holds the token
    target = urlsplit(origin).netloc.lower()
    if not target:
        return True  # e.g. "null" from a sandboxed frame
    return target == (request.headers.get("host") or "").strip().lower()


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------

def require_session(request: Request) -> None:
    """Gate for every ``/api`` route."""
    if not host_allowed(request.headers.get("host", "")):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Request rejected: unexpected Host header. Add this host name to "
                "AEGIS_ALLOWED_HOSTS if you really serve the app from it."
            ),
        )

    if request.method not in SAFE_METHODS and not origin_allowed(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Request rejected: cross-origin request to a state-changing endpoint.",
        )

    cookie_ok = token_matches(request.cookies.get(SESSION_COOKIE))
    header_ok = token_matches(request.headers.get(TOKEN_HEADER))
    if cookie_ok or header_ok:
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=(
            "Locked. Open the URL printed in the Aegis terminal (it contains your "
            f"access token), or paste the contents of {TOKEN_FILE}."
        ),
    )


# ---------------------------------------------------------------------------
# Page rendering
# ---------------------------------------------------------------------------

PAGE_TOKEN_PLACEHOLDER = "%%AEGIS_TOKEN%%"


def embed_token_for_request(request: Any) -> bool:
    """As :func:`embed_token_for`, plus a guard against cross-site subresources.

    A page on another site can include ``<script src="http://127.0.0.1:8000/
    config.js">`` and read the token out of its own window. ``Sec-Fetch-Site``
    tells us that is what is happening, so a secret is only ever handed to a
    top-level navigation or a same-origin fetch.
    """
    site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if site and site not in {"same-origin", "none"}:
        return False
    return embed_token_for(request.headers.get("host", ""))


def render_index(html: str, host_header: str = "") -> str:
    """Fill the token placeholder in index.html (empty when it must not leak)."""
    return html.replace(
        PAGE_TOKEN_PLACEHOLDER, get_token() if embed_token_for(host_header) else ""
    )


def describe_exposure(host: str) -> str:
    """A one-line, human warning for the startup banner."""
    if is_loopback_bind(host):
        return "bound to this machine only (127.0.0.1)"
    return (
        f"⚠ bound to {host} — reachable from your network. Only devices that know "
        f"the access token in {TOKEN_FILE} can control this PC."
    )


def unlock_url(port: int, host: str) -> str:
    """URL the browser should open, carrying the token once."""
    shown = "localhost" if is_loopback_bind(host) else host
    return f"http://{shown}:{port}/?token={get_token()}"


def auth_debug_state() -> dict[str, Any]:
    """Non-secret view of the access settings, handy in logs and tests."""
    return {
        "loopback_bind": is_loopback_bind(),
        "token_embedded_in_page": embed_token(),
        "token_file": str(TOKEN_FILE),
    }
