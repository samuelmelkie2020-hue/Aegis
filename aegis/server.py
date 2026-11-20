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
import json
import logging
import os
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

from . import __version__, approvals
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
from .tools import execute_tool, see_screen

STATIC_DIR = Path(__file__).resolve().parent / "static"

HEARTBEAT_SECONDS = 15      # SSE keepalive while the agent is thinking
QUEUE_PUT_TIMEOUT = 5       # how long a tool may block on a stalled client
MAX_QUEUE_ITEMS = 64        # bound the memory one abandoned turn can hold
log = logging.getLogger("aegis.server")

app = FastAPI(title="Aegis — Aegis", version=__version__)
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


@app.get("/api/screenshot", dependencies=API)
def get_screenshot() -> Response:
    result = see_screen()
    data = base64.b64decode(result.get("image", ""))
    return Response(content=data, media_type="image/png")


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
    * client gone — a bounded, non-blocking drain, so an abandoned turn cannot
      keep appending screenshots to a queue nobody reads.
    """
    import queue as queue_mod
    import threading

    q: queue_mod.Queue = queue_mod.Queue(maxsize=MAX_QUEUE_ITEMS)
    stop = threading.Event()
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
                turn=Turn(stop_event=stop),
            )
            outcome["text"] = text
        except TurnCancelled:
            outcome["cancelled"] = True
        except Exception as exc:
            log.exception("agent turn failed")
            outcome["error"] = str(exc) or type(exc).__name__
        finally:
            q.put(None)  # never blocks: the sentinel is what ends the loop

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

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
            stop.set()  # tell the agent to abandon its next emit
            log.info("Client disconnected; turn abandoned")

    if outcome.get("cancelled"):
        return
    if outcome.get("error"):
        message = f"Something went wrong: {outcome['error']}"
        yield _sse("error", {"message": message})
        yield _sse("done", {"ok": False})
        return
    yield _sse("done", {"ok": True, "text": outcome.get("text", "")})
