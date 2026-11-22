"""FastAPI server: serves the Aegis web UI and the agent API.

Every ``/api`` route requires the local access token (see :mod:`aegis.security`).
The client never decides anything safety-relevant: ``safe_mode`` and the LLM
endpoint used for a chat turn are read from the server's own config, never from
the request body. The client may still *save* settings and probe an unsaved
candidate with "/api/test-connection" (so the Test button works before Save),
but nothing is applied to an agent turn until it is saved.
"""

from __future__ import annotations

import base64
import itertools
import json
import logging
import os
import threading
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Query, Request, status
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, approvals, updates
from .config import (
    SettingsError,
    config_problem,
    has_api_key,
    is_demo_mode,
    load_config,
    preview_config,
    public_config,
    save_config,
)
from .llm import Turn, TurnCancelled, run_agent_turn
from .security import (
    SESSION_COOKIE,
    TOKEN_HEADER,
    describe_exposure,
    embed_token_for_request,
    get_token,
    render_index,
    require_session,
    token_matches,
)
from .tools import execute_tool, live_frame, see_screen

STATIC_DIR = Path(__file__).resolve().parent / "static"

HEARTBEAT_SECONDS = 15      # SSE keepalive while the agent is thinking
QUEUE_PUT_TIMEOUT = 5       # how long a tool may block on a stalled client
MAX_QUEUE_ITEMS = 64        # bound the memory one abandoned turn can hold
MAX_ACTIVE_TURNS = 8        # more than this is a client bug, not a user
log = logging.getLogger("aegis.server")


# ---------------------------------------------------------------------------
# Running turns
# ---------------------------------------------------------------------------
#: ``turn_id`` → the live :class:`aegis.llm.Turn`. The browser gets the id in the
#: first event of the stream and POSTs it back to ``/api/chat/stop``.
_active_turns: dict[str, Turn] = {}
_active_lock = threading.Lock()
_turn_ids = itertools.count(1)


def _register_turn(stop: threading.Event) -> tuple[str, Turn]:
    """Create a cancellable turn and publish it, so the UI can stop it."""
    turn = Turn(stop_event=stop)
    turn_id = f"turn-{next(_turn_ids)}"
    with _active_lock:
        # A browser that reconnects without ending its old stream would otherwise
        # accumulate turns forever. The oldest goes first: it is the abandoned one.
        while len(_active_turns) >= MAX_ACTIVE_TURNS:
            oldest = next(iter(_active_turns))
            _active_turns[oldest].set_reason("superseded")
            _active_turns.pop(oldest, None)
        _active_turns[turn_id] = turn
    return turn_id, turn


def _release_turn(turn_id: str, turn: Turn) -> None:
    with _active_lock:
        if _active_turns.get(turn_id) is turn:
            _active_turns.pop(turn_id, None)


def _stop_turn(turn_id: str) -> bool:
    """Ask one running turn to stop. Returns False if it already finished."""
    with _active_lock:
        turn = _active_turns.get(turn_id)
    if turn is None:
        return False
    turn.set_reason("user")
    if turn.stop_event is not None:
        turn.stop_event.set()
    return True


# `docs_url` and friends are disabled deliberately. They need no token, so
# leaving them on publishes a map of every endpoint — request bodies and all —
# to anything that can reach the port. The API is documented in the README
# instead, and the UI is the client.
app = FastAPI(
    title="Aegis",
    version=__version__,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Only the /api surface is protected; "/" and "/static" stay public so the page
# can load at all (and so an embedded preview pane works).
API = [Depends(require_session)]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str
    # Policy (safe_mode, llm endpoint) is intentionally NOT accepted here.
    history: list[dict] = Field(default_factory=list)


class ApproveRequest(BaseModel):
    """Redeem a pending approval.

    ``approval_id`` is the only field that matters: the action and its arguments
    are looked up from the server's own registry (see aegis/approvals.py). The
    legacy free-form ``name``/``args`` path is gone on purpose — it let a caller
    choose what to run.
    """
    approval_id: str


class SettingsRequest(BaseModel):
    settings: dict


class TestRequest(BaseModel):
    settings: dict | None = None


class LocalPullRequest(BaseModel):
    """Only a model name: where to download from is the server's own config."""

    model: str = ""


class LocalAdoptRequest(BaseModel):
    """Which model to use, and where to look for model files.

    Declared with the other request models on purpose: a model class defined
    *after* its route is invisible to FastAPI at decoration time, so the body is
    silently dropped and the request looks like it was ignored.
    """

    model: str = Field(default="", max_length=200)
    models_dir: str = Field(default="", max_length=1024)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

def _set_session_cookie(response: Response) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        get_token(),
        httponly=True,
        samesite="strict",
        path="/",
        max_age=60 * 60 * 24 * 30,
    )


@app.get("/")
def index(request: Request, token: str | None = Query(default=None)):
    """Serve the UI, using the first visit to give the browser its session.

    ``/?token=…`` (the URL the terminal prints) exchanges the token for an
    HttpOnly cookie and redirects, so the secret does not linger in the address
    bar. A loopback-bound server also embeds the token in the page it serves, so
    the normal local flow needs no copy-paste; a remote visitor gets nothing.
    """
    host_header = request.headers.get("host", "")
    embed = embed_token_for_request(request)
    html = render_index((STATIC_DIR / "index.html").read_text(encoding="utf-8"), host_header)
    link_ok = token is not None and token_matches(token)

    if link_ok:
        # Exchange the token for an HttpOnly session so it leaves the URL bar.
        response: Response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        _set_session_cookie(response)
        return response

    if token is not None:
        # The supplied link did not open the app; say so plainly.
        html = html.replace(
            "<!--TOKEN-BANNER-->",
            '<div class="token-banner">That access link is no longer valid. '
            "Restart Aegis and open the URL it prints.</div>",
        )

    response = HTMLResponse(html)
    if embed or token_matches(request.cookies.get(SESSION_COOKIE)):
        # Local or preview request: the page is allowed to hold the session, so
        # the browser can call /api without ever seeing a token.
        _set_session_cookie(response)
    return response


@app.get("/config.js")
def config_js(request: Request) -> Response:
    """Session bootstrap for the page, deliberately NOT under /api.

    The token is only ever filled in for a loopback or preview host; from
    anywhere else the caller gets an empty token (and therefore no access).
    """
    token = get_token() if embed_token_for_request(request) else ""
    body = (
        "/* generated by Aegis — do not cache */\n"
        f"window.AEGIS_TOKEN = {json.dumps(token)};\n"
    )
    return Response(
        content=body,
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/api/health", dependencies=API)
def health() -> dict:
    cfg = load_config()
    return {
        "ok": True,
        "version": __version__,
        # Same decision the agent makes (is_demo_mode), so they cannot disagree.
        "demo": is_demo_mode(cfg),
        "exposure": describe_exposure(os.environ.get("AEGIS_HOST", "127.0.0.1")),
    }


@app.get("/api/stats", dependencies=API)
def stats() -> dict:
    """Live machine telemetry for the HUD, in the shape the UI draws.

    Read-only and cheap: the HUD polls it every couple of seconds. ``None``
    means "this machine cannot report it" (no battery, no temperature sensor),
    which the UI renders as a dash rather than a fake zero.
    """
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil is a hard dependency
        return {"ok": False, "detail": "psutil is not installed"}

    def _temperature() -> float | None:
        try:
            sensors = psutil.sensors_temperatures()
        except (AttributeError, OSError):
            return None
        for name in ("coretemp", "k10temp", "acpitz", "cpu_thermal", "cpu-thermal"):
            if sensors.get(name):
                return round(sensors[name][0].current, 1)
        for entries in sensors.values():
            if entries:
                return round(entries[0].current, 1)
        return None

    try:
        load_one = psutil.getloadavg()[0]
    except (AttributeError, OSError):
        load_one = None

    try:
        battery = psutil.sensors_battery()
    except (AttributeError, OSError):
        battery = None

    disk = None
    try:
        usage = psutil.disk_usage(str(Path.home()))
        disk = {"percent": usage.percent, "free_gb": round(usage.free / 1e9, 1)}
    except OSError:
        pass

    virtual = psutil.virtual_memory()
    return {
        "ok": True,
        "cpu": {
            "percent": psutil.cpu_percent(interval=None),
            "cores": psutil.cpu_count(logical=True),
            "load1": load_one,
        },
        "memory": {
            "percent": virtual.percent,
            "used_gb": round(virtual.used / 1e9, 1),
            "total_gb": round(virtual.total / 1e9, 1),
        },
        "temperature": _temperature(),
        "battery": None
        if battery is None
        else {"percent": round(battery.percent), "plugged": bool(battery.power_plugged)},
        "disk": disk,
        "uptime_seconds": int(time.time() - psutil.boot_time()),
    }


@app.get("/api/bootstrap", dependencies=API)
def bootstrap() -> dict:
    """Everything the UI needs on first paint, in one round trip (no secrets)."""
    cfg = load_config()
    return {
        "settings": public_config(cfg),
        "demo": is_demo_mode(cfg),
        "has_key": has_api_key(cfg),
        "version": __version__,
        "config_problem": config_problem(),
    }


@app.get("/api/settings", dependencies=API)
def get_settings() -> dict:
    """Public view of the config. The API key is never included."""
    return public_config(load_config())


@app.post("/api/settings", dependencies=API)
def post_settings(body: SettingsRequest) -> JSONResponse:
    try:
        save_config(body.settings)
    except SettingsError as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=status.HTTP_400_BAD_REQUEST)
    return JSONResponse(public_config(load_config()))


@app.post("/api/test-connection", dependencies=API)
def test_connection(body: TestRequest) -> JSONResponse:
    """Check network + API key + model in one shot with a 1-token chat call.

    Probes the *unsaved* form values so the button works before Save. Supplying
    a candidate URL is a deliberate action by whoever holds the local access
    token, not something a random web page can do.
    """
    import httpx

    try:
        cfg = preview_config(body.settings)
    except SettingsError as exc:
        return JSONResponse({"ok": False, "detail": str(exc)})

    llm = cfg.get("llm") or {}
    base = (llm.get("base_url") or "").rstrip("/")
    key = llm.get("api_key") or ""
    model = llm.get("model") or ""
    if not base:
        return JSONResponse({"ok": False, "detail": "No API base URL set — pick a provider first."})
    host = base.split("//")[-1].split("/")[0]

    # This route opens a socket to whatever the form contains, before it is saved.
    # Allowing arbitrary addresses would make it a request-forger pointed at the
    # user's own network (routers, NAS boxes, cloud metadata endpoints), so it is
    # restricted to this machine and the public internet. See safety.outbound_reason.
    from .safety import outbound_reason

    blocked = outbound_reason(base)
    if blocked:
        return JSONResponse({"ok": False, "detail": (
            f"🚫 {blocked}.\n"
            "Aegis only talks to a model on this PC (http://127.0.0.1:…) or a public "
            "provider (https://…). If your model really lives on another machine, "
            "reach it through an SSH tunnel so it appears on 127.0.0.1."
        )})

    # A local model has no key and no network: every cloud-flavoured message here
    # would send the user looking for the wrong thing.
    from . import local_llm

    if local_llm.is_local_url(base):
        return _test_local(base, host, model)

    try:
        r = httpx.post(
            base + "/chat/completions",
            headers={"Authorization": f"Bearer {key}"} if key else {},
            json={"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1},
            timeout=30,
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "detail": (
            f"❌ Could not reach {host} at all: {str(exc)[:160]}\n"
            "This is a NETWORK block (firewall / ISP / antivirus), not your key. "
            "If you are using the Arena chat preview: it is always blocked from AI sites — "
            "run Aegis on your own PC instead. Otherwise try another provider, a VPN, or Ollama (local)."
        )})

    if r.status_code == 200:
        return JSONResponse({"ok": True, "detail": f"✅ Connected to {host} — network, key and model all work. Aegis is ready!"})
    if r.status_code in (401, 403):
        return JSONResponse({"ok": False, "detail": f"🌐 Reached {host}, but the API key was rejected (HTTP {r.status_code}). Re-copy your key."})
    if r.status_code == 402:
        return JSONResponse({"ok": False, "detail": f"💳 Reached {host}, but the account is out of credit (HTTP 402)."})
    if r.status_code == 429:
        return JSONResponse({"ok": False, "detail": f"⏳ Reached {host} — everything works, but rate-limited right now (HTTP 429). Try again in a moment."})
    return JSONResponse({"ok": False, "detail": f"⚠ Reached {host} but got HTTP {r.status_code}: {r.text[:160]} — check the base URL and model name."})


def _test_local(base: str, host: str, model: str) -> JSONResponse:
    """Test a local endpoint in local terms: is the server up, does it have the model."""
    from . import local_llm

    origin = local_llm.origin(base) or base
    models, error = local_llm.list_models(origin)
    if error and not models:
        if not local_llm.port_open(origin):
            return JSONResponse(
                {
                    "ok": False,
                    "detail": (
                        f"❌ Nothing is running on {host}, so there is no local model to talk to.\n"
                        f"{local_llm.install_hint()}"
                    ),
                }
            )
        return JSONResponse(
            {"ok": False, "detail": f"⚠ {host} is answering but did not list any models. {error}"}
        )

    if not model:
        listing = ", ".join(models[:6]) if models else "none"
        return JSONResponse(
            {
                "ok": False,
                "detail": (
                    f"⚠ {host} is running. Now choose a model — installed here: {listing}.\n"
                    "Press ↻ Refresh in 💻 This computer to pick one."
                ),
            }
        )
    if model not in models:
        listing = ", ".join(models[:6]) if models else "none"
        return JSONResponse(
            {
                "ok": False,
                "detail": (
                    f"⚠ {host} is running, but <b>{model}</b> is not installed (installed: {listing}).\n"
                    f"Download it from 💻 This computer, or run `ollama pull {model}`."
                ),
            }
        )
    return JSONResponse(
        {
            "ok": True,
            "detail": (
                f"✅ {model} is ready on this PC via {host} — no API key, nothing leaves your computer. "
                "Aegis is ready!"
            ),
        }
    )


@app.get("/api/screenshot", dependencies=API)
def get_screenshot() -> Response:
    result = see_screen()
    data = base64.b64decode(result.get("image", ""))
    return Response(content=data, media_type="image/png")


@app.get("/api/live/frame", dependencies=API)
def get_live_frame(fps: float = Query(default=2.0, ge=0.1, le=30)) -> Response:
    """One frame of the live screen view, as JPEG.

    Token-gated like every other ``/api`` route. Returning an image instead of
    JSON keeps the hot path cheap — the client assigns the response straight to
    an ``<img>``, with no base64 decode and no per-frame object URL to leak.

    ``no-store`` on the frame itself: a screen recording is the last thing that
    should survive in a browser cache after the tab closes.
    """
    frame = live_frame(fps)
    if not frame.get("ok"):
        return JSONResponse(
            {"ok": False, "reason": frame.get("reason"), "detail": frame.get("detail")},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={"Cache-Control": "no-store"},
        )
    data = base64.b64decode(frame["image"])
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-store, max-age=0",
            "X-Aegis-Capture": "reused" if frame.get("reused") else "fresh",
            "X-Aegis-Fps": str(frame.get("fps", "")),
            "X-Aegis-Tick": str(frame.get("tick", "")),
        },
    )


# ---------------------------------------------------------------------------
# Local model (the default brain: it runs on this PC)
#
# Nothing here touches the PC or an outside network, so it needs no approval:
# probing a loopback port and reading a model list are read-only, and a pull
# only ever goes to a server on this machine (enforced by is_local_url).
# ---------------------------------------------------------------------------

@app.get("/api/local/status", dependencies=API)
def local_status() -> dict:
    """Hardware, any local server that is running, its models, and what to get."""
    from . import local_llm

    cfg = load_config()
    base = (cfg.get("llm") or {}).get("base_url") or ""
    try:
        report = local_llm.status(include=base)
    except Exception as exc:  # a probe must never take the app down
        log.warning("Local model probe failed", exc_info=True)
        return {
            "ok": False,
            "detail": f"Could not check for a local model server ({str(exc)[:160]}).",
            "running": False,
            "installed": [],
            "hint": local_llm.install_hint(),
        }
    report["ok"] = True
    report.setdefault("hint", local_llm.install_hint())
    report["active_model"] = (cfg.get("llm") or {}).get("model") or ""
    report["servers"] = report.get("servers") or []
    # One list for the UI: Ollama's models and loose model files, together.
    report["all_models"] = sorted(
        {m["name"] for m in (report.get("disk") or {}).get("models", [])}
        | {w["name"] for w in report.get("weights") or []}
        | set(report.get("installed") or [])
    )
    return report


@app.post("/api/local/setup", dependencies=API)
def local_setup() -> JSONResponse:
    """One press: point Aegis at a local server and pick a model it can run.

    This is the whole "no API key" story in one call — the user should never have
    to type a URL, a model name, or find a key. If nothing is running, nothing is
    saved and they get instructions instead.
    """
    from . import local_llm

    cfg = load_config()
    configured = (cfg.get("llm") or {}).get("base_url") or ""
    try:
        server = local_llm.detect_running(include=configured)
    except Exception as exc:
        log.warning("Local model detection failed", exc_info=True)
        return JSONResponse(
            {"ok": False, "detail": f"Could not check for a local model server ({str(exc)[:160]})."},
            status_code=500,
        )
    if server is None:
        return JSONResponse(
            {
                "ok": False,
                "detail": (
                    "No local AI server is running on this PC yet. "
                    + local_llm.install_hint()
                ),
                "hint": local_llm.install_hint(),
            },
            status_code=409,
        )

    if not server.models:
        # Recommending here would save a model name that is not downloaded yet,
        # and the first chat would fail with a 404 the user cannot explain.
        choice = local_llm.recommend(local_llm.detect_hardware(), [])
        return JSONResponse(
            {
                "ok": False,
                "detail": (
                    f"{server.name} is running but has no models yet. "
                    f"Recommended for this PC: {choice.get('model')} — "
                    f"{choice.get('detail') or ''}"
                ),
                "recommendation": choice,
                "server": server.name,
            },
            status_code=409,
        )
    choice = local_llm.recommend(local_llm.detect_hardware(), server.models)

    try:
        save_config(
            {
                "llm": {
                    "base_url": local_llm.normalise_base(server.base_url),
                    "model": choice["model"],
                    "api_key": "",
                }
            }
        )
    except SettingsError as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)

    return JSONResponse(
        {
            "ok": True,
            "detail": f"Using {choice['model']} on {server.name} — running on this PC.",
            "settings": public_config(load_config()),
            "model": choice["model"],
            "server": server.name,
        }
    )


@app.post("/api/local/adopt", dependencies=API)
def local_adopt(body: LocalAdoptRequest | None = None) -> JSONResponse:
    """Find the model files on this PC, make them usable, and select one.

    This is the button for "I know I downloaded a model and I do not know where
    it is": it reads Ollama's folders directly (no server needed), starts Ollama
    if it is installed but not running, and points Aegis at the best model it
    found — preferring one that can see the screen. Picking a model by name is
    the same call with ``model`` set.
    """
    from . import local_llm

    requested = (body.model if body else "") or ""
    folder = (body.models_dir if body else "") or ""

    if requested:
        # Only a name the server or the disk already knows may be selected. This
        # keeps a stray request from writing a model that cannot answer into the
        # config, which would turn into a 404 on the user's next message.
        known = set(local_llm.known_model_names())
        known.update(w["name"] for w in local_llm.scan_weights())
        for server in local_llm.probe_servers(timeout=local_llm.PROBE_TIMEOUT):
            known.update(server.models)
        if requested not in known:
            return JSONResponse(
                {"ok": False, "detail": f"{requested} is not on this PC. {local_llm.install_hint()}"},
                status_code=status.HTTP_404_NOT_FOUND,
            )

    try:
        result = (
            {"ok": True, "models": [requested], "detail": ""}
            if requested
            else local_llm.load_installed(folder or None)
        )
    except Exception as exc:  # a scan must never take the app down
        log.warning("Local model discovery failed", exc_info=True)
        return JSONResponse(
            {"ok": False, "detail": f"Could not look for local models ({str(exc)[:160]})."},
            status_code=500,
        )

    models = result.get("models") or []
    if not models:
        return JSONResponse(
            {"ok": False, "detail": result.get("detail") or local_llm.install_hint(),
             "disk": result.get("disk")},
            status_code=status.HTTP_409_CONFLICT,
        )

    server = local_llm.detect_running(timeout=local_llm.PROBE_TIMEOUT)
    base = local_llm.normalise_base(server.base_url) if server else "http://127.0.0.1:11434/v1"
    choice = local_llm.recommend(local_llm.cached_hardware(), models)
    model = requested or choice.get("model") or models[0]
    try:
        save_config({"llm": {"base_url": base, "model": model, "api_key": ""}})
    except SettingsError as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)
    return JSONResponse(
        {
            "ok": True,
            "model": model,
            "base_url": base,
            "models": models,
            "vision": local_llm.has_vision(model),
            "started": bool(result.get("started")),
            "detail": (
                f"Using {model} — it runs on this PC, so no key and nothing leaves it."
                if local_llm.has_vision(model)
                else f"Using {model}. It is a text model, so it cannot see your screen yet."
            ),
        }
    )


@app.post("/api/local/pull", dependencies=API)
def local_pull(body: LocalPullRequest) -> JSONResponse:
    """Download a model onto this PC, in the background. Progress is polled."""
    from . import local_llm

    base = (load_config().get("llm") or {}).get("base_url") or ""
    if not local_llm.is_local_url(base):
        return JSONResponse(
            {
                "ok": False,
                "detail": (
                    "Aegis can only download models onto this computer, and the "
                    "current provider is not a local one. Set the provider to "
                    "“Run on this PC” first."
                ),
            },
            status_code=409,
        )
    url = local_llm.origin(base)
    if not url:
        return JSONResponse(
            {"ok": False, "detail": "Set the provider to “Run on this PC” first."},
            status_code=409,
        )
    result = local_llm.start_pull(body.model, base_url=url)
    return JSONResponse(result, status_code=200 if result.get("ok") else 409)


@app.post("/api/local/pull/cancel", dependencies=API)
def local_pull_cancel() -> JSONResponse:
    from . import local_llm

    result = local_llm.cancel_pull()
    return JSONResponse(result, status_code=200 if result.get("ok") else 409)


# ---------------------------------------------------------------------------
# Updates
#
# Checking is read-only and safe to call at startup. Downloading is explicit.
# *Installing* replaces the executable, so it is a deliberate two-step: the
# request registers an approval bound to this session, and the same
# /api/approve route that guards "run this command" redeems it. A page cannot
# swap the payload in between, and it cannot be replayed.
# ---------------------------------------------------------------------------

@app.get("/api/update", dependencies=API)
def update_status() -> dict:
    """What version this is, and whether an update is downloaded and waiting."""
    return updates.update_status()


@app.get("/api/update/check", dependencies=API)
def update_check() -> dict:
    """Ask GitHub for the newest release. Nothing is downloaded."""
    return updates.check_for_update()


@app.post("/api/update/download", dependencies=API)
def update_download(request: Request) -> JSONResponse:
    """Download, verify (size + SHA-256) and self-check the newest release.

    Which build to fetch comes from the *server's* last check, never from the
    request body: a caller who could name the URL could name any executable.
    """
    if not updates.updates_enabled():
        return JSONResponse({"ok": False, "detail": "Updates are turned off."}, status_code=409)

    status = updates.check_for_update()
    if not status.get("available"):
        return JSONResponse(
            {"ok": False, "detail": status.get("reason") or status.get("error") or "No update available."},
            status_code=409,
        )
    if not status.get("installable"):
        return JSONResponse({"ok": False, "detail": status.get("reason")}, status_code=409)

    release = updates.last_release()
    if release is None:  # pragma: no cover - check_for_update just set it
        return JSONResponse({"ok": False, "detail": "No release to download."}, status_code=409)

    try:
        info = updates.download_and_stage(release)
    except updates.UpdateError as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=422)
    except Exception as exc:  # never leave the UI without a reason
        log.warning("Update download failed", exc_info=True)
        return JSONResponse(
            {"ok": False, "detail": f"The update could not be downloaded ({exc})."},
            status_code=500,
        )
    return JSONResponse(
        {
            "ok": True,
            "detail": f"Aegis {info['version']} is downloaded and verified. Restart to install it.",
            "staged": info,
        }
    )


@app.post("/api/update/install", dependencies=API)
def update_install(request: Request) -> JSONResponse:
    """Register the install as an approval. Redeem it via /api/approve."""
    plan = updates.apply_plan()
    if not plan.get("ok"):
        return JSONResponse({"ok": False, "detail": plan.get("detail")}, status_code=409)

    version = plan.get("version") or "the downloaded version"
    item = approvals.register(
        "install_update",
        {"version": version},
        f"Install Aegis {version} and restart. This replaces the app you are running.",
        _session_token(request),
    )
    return JSONResponse(
        {
            "ok": True,
            "approval_id": item.id,
            "name": item.name,
            "args": item.args,
            "description": item.description,
            "detail": "Confirm to install and restart Aegis.",
        }
    )


@app.post("/api/approve", dependencies=API)
def approve(body: ApproveRequest, request: Request) -> JSONResponse:
    """Redeem an approval. Single use, bound to this session, TTL-limited."""
    token = _session_token(request)
    item, error = approvals.redeem(body.approval_id, token)
    if item is None:
        return JSONResponse(
            {"ok": False, "result": error or "That approval could not be redeemed."},
            status_code=status.HTTP_409_CONFLICT,
        )
    result = execute_tool(item.name, item.args)
    # Remember the outcome so the browser can resume the turn without the
    # action being run a second time (the result is claimed, never re-executed).
    approvals.complete(item.id, token, item.name, item.args, item.description, result)
    return JSONResponse({**result, "name": item.name, "description": item.description})


@app.post("/api/approve/deny", dependencies=API)
def deny(body: ApproveRequest, request: Request) -> JSONResponse:
    """Discard an approval so its card cannot be redeemed later."""
    item, error = approvals.redeem(body.approval_id, _session_token(request))
    if item is None:
        return JSONResponse({"ok": False, "result": error}, status_code=status.HTTP_409_CONFLICT)
    return JSONResponse({"ok": True, "result": f"Dropped the request to {item.description}."})


@app.post("/api/chat", dependencies=API)
def chat(body: ChatRequest, request: Request) -> StreamingResponse:
    """Run one agent turn and stream Server-Sent Events to the UI."""
    cfg = load_config()  # policy comes from disk, never from the client
    history = _dedupe_history(body.history, body.message)

    return StreamingResponse(
        _streaming_chat(body, cfg, history, _session_token(request)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


class StopRequest(BaseModel):
    turn_id: str = Field(default="")


@app.post("/api/chat/stop", dependencies=API)
def stop_chat(body: StopRequest) -> JSONResponse:
    """Stop a running turn. Idempotent: stopping a finished turn is not an error.

    This is the only way a long turn ends early on purpose. Before it existed a
    turn could only be abandoned by closing the tab, which also threw away the
    half-written reply and any approval still pending in it.
    """
    if not body.turn_id:
        return JSONResponse(
            {"ok": False, "detail": "No turn to stop."}, status_code=status.HTTP_400_BAD_REQUEST
        )
    stopped = _stop_turn(body.turn_id)
    return JSONResponse(
        {
            "ok": True,
            "stopped": stopped,
            "detail": "Stopping." if stopped else "That turn had already finished.",
        }
    )


def _session_token(request: Request) -> str:
    """The token this request authenticated with, used to bind approvals."""
    return request.cookies.get(SESSION_COOKIE) or request.headers.get(TOKEN_HEADER, "")


def _sse(etype: str, data: dict) -> str:
    return f"data: {json.dumps({'type': etype, **data})}\n\n"


def _dedupe_history(history: list[dict], message: str) -> list[dict]:
    """Drop a trailing copy of the current message from the client's history.

    The browser pushes the user's message into its history *before* posting, so
    the same turn would otherwise arrive twice (history + message) and be sent
    to the model as two consecutive user messages. Only an exact-match trailing
    user turn that is identical to ``message`` is removed.
    """
    cleaned = [m for m in (history or []) if isinstance(m, dict)]
    wanted = (message or "").strip()
    while cleaned and cleaned[-1].get("role") == "user" and isinstance(cleaned[-1].get("content"), str) \
            and cleaned[-1]["content"].strip() == wanted:
        cleaned.pop()
    return cleaned


def _streaming_chat(body: ChatRequest, cfg: dict, history: list[dict], session_token: str):
    """Stream one turn's events to the browser.

    The worker only *wakes* this generator; it never decides what the browser
    sees. That matters because there is exactly one terminal event per stream:

    * success — ``message`` already carried the text, ``done`` confirms;
    * failure — a single ``error`` carrying the text to show, then ``done``
      with ``ok: false`` and no text (it used to repeat the error, so the UI
      rendered every failure twice);
    * stopped by the user — ``done`` with ``stopped: true``, and whatever the
      model had already said stays on screen (it is not an error);
    * client gone — a bounded, non-blocking drain, so an abandoned turn cannot
      keep appending screenshots to a queue nobody reads.

    The first event always carries the ``turn_id`` the Stop button posts back.
    """
    import queue as queue_mod

    q: queue_mod.Queue = queue_mod.Queue(maxsize=MAX_QUEUE_ITEMS)
    stop = threading.Event()
    turn_id, turn = _register_turn(stop)
    outcome: dict[str, object] = {}

    def emit(etype: str, data: dict) -> None:
        """Never block the agent: if the browser is gone, drop the event."""
        if stop.is_set():
            raise TurnCancelled("the browser left before this turn finished")
        try:
            q.put((etype, data), timeout=QUEUE_PUT_TIMEOUT)
        except queue_mod.Full:
            log.warning("Dropping a %s event for a stalled client", etype)

    def worker() -> None:
        try:
            text = run_agent_turn(
                body.message.strip(), history, cfg, emit, session_token,
                turn=turn,
            )
            outcome["text"] = text
        except TurnCancelled:
            outcome["cancelled"] = True
            outcome["stopped"] = turn.cancelled_by_user
            outcome["timed_out"] = turn.reason == "timeout"
        except Exception as exc:
            log.exception("agent turn failed")
            outcome["error"] = str(exc) or type(exc).__name__
        finally:
            q.put(None)  # never blocks: the sentinel is what ends the loop

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    # Tell the UI what to send back to /api/chat/stop before anything else, so a
    # turn can be stopped even while the first model call is still in flight.
    yield _sse("turn", {"turn_id": turn_id})

    try:
        while True:
            try:
                item = q.get(timeout=HEARTBEAT_SECONDS)
            except queue_mod.Empty:
                if not thread.is_alive():
                    break
                yield ": ping\n\n"  # keeps proxies/keepalive from timing out
                continue
            if item is None:
                break
            etype, data = item
            yield _sse(etype, data)
    finally:
        if thread.is_alive():
            # Nobody is reading this stream any more, so this is not the user
            # pressing Stop: end silently rather than report a cancel.
            turn.set_reason("disconnected")
            stop.set()
            log.info("Client disconnected; turn abandoned")
        _release_turn(turn_id, turn)

    if outcome.get("cancelled"):
        if outcome.get("stopped"):
            # A deliberate stop is not a failure: keep what was already said and
            # say so, so the bubble does not look like a crash.
            yield _sse("done", {"ok": True, "stopped": True, "text": ""})
        elif outcome.get("timed_out"):
            yield _sse("done", {"ok": True, "stopped": True, "timed_out": True, "text": ""})
        return
    if outcome.get("error"):
        message = f"Something went wrong: {outcome['error']}"
        yield _sse("error", {"message": message})
        yield _sse("done", {"ok": False})
        return
    yield _sse("done", {"ok": True, "text": outcome.get("text", "")})
