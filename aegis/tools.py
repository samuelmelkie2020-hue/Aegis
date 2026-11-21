"""Tools that let Aegis see and control the PC.

Everything is guarded so the server also runs on machines without a display
(e.g. during development) — in that case tools return friendly errors or a
placeholder screenshot instead of crashing.
"""

from __future__ import annotations

import base64
import io
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .safety import check_readable, check_writable, redact, redact_result

if TYPE_CHECKING:  # Pillow is imported lazily: --self-check must not need it
    from PIL.Image import Image as _Image

MAX_IMAGE_WIDTH = 1280   # screenshots are downscaled to this for the model
MAX_OUTPUT = 4000        # max characters of tool output fed back to the model
MAX_FILE_READ = 8000     # max characters of a file read (also the read cap)
MAX_WALK_DIRS = 2000     # folders inspected by a single search_files call
MAX_SEARCH_ENTRIES = 60000  # files inspected by a single search_files call
MAX_SEARCH_HITS = 30     # matches returned by search_files

# The scale factor of the most recent screenshot, so `click` can map the
# coordinates the model read off the (downscaled) image back to real pixels.
_last_scale: tuple[float, int, int] = (1.0, 0, 0)
# The same screenshot in full colour, kept for the HUD. DOWNSCALED is what the
# model sees (cheap in tokens); this is what the user sees (readable text).
_last_full: _Image | None = None

# Frames for the live HUD. Deliberately conservative: a screen recording is a
# continuous read of everything the user sees, so it is opt-in, capped in rate,
# and abandoned the moment nobody is looking at it.
LIVE_MAX_FPS = 4.0          # hard ceiling, whatever the client asks for
LIVE_IDLE_STOP = 120.0      # seconds: no caller has asked for a frame
LIVE_MAX_SECONDS = 1800.0   # one run cannot be left on indefinitely
LIVE_MAX_WIDTH = 1600       # the browser panel is ~600px wide
_live_lock = threading.Lock()
_live_requests = 0
_live_opened = 0.0
_live_frame = ""            # last JPEG, so a fast poll re-sends it
_live_frame_info: dict[str, Any] = {}
_live_tick = 0              # increments per new frame: proof the view is live


# Tools that modify the system or control the keyboard/mouse.
DANGEROUS_TOOLS = {
    "run_command",
    "open_app",
    "type_text",
    "press_keys",
    "click",
    "write_file",
    "clipboard",
}

# ---------------------------------------------------------------------------
# Tool schemas (OpenAI function-calling format)
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "see_screen",
            "description": (
                "Capture the user's screen and look at it. Use this whenever the "
                "user asks what is on their screen, what they are doing, what an "
                "error says, or anything that requires seeing their PC. The "
                "screenshot is attached to the conversation as an image."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run a shell command on the user's computer and return its output. "
                "Use for anything that can be done from a terminal. Never run "
                "destructive commands (rm -rf, format, del /s, registry edits) "
                "unless the user explicitly asked for exactly that."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run."},
                    "cwd": {"type": "string", "description": "Optional working directory."},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_app",
            "description": "Open an application on the user's computer (e.g. 'notepad', 'chrome', 'Spotify', 'Calculator').",
            "parameters": {
                "type": "object",
                "properties": {
                    "app": {"type": "string", "description": "Name of the app to open."}
                },
                "required": ["app"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Type text with the keyboard at the current cursor position. Use after opening an app or after click.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The text to type."}
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_keys",
            "description": "Press keyboard shortcuts, e.g. ['ctrl','s'] to save, ['enter'], ['alt','tab'].",
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Keys to press together as a shortcut.",
                    }
                },
                "required": ["keys"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Click the mouse at screen coordinates (pixels). Combine with see_screen to find the coordinates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system_info",
            "description": "Report the computer's status: OS, CPU, memory, disk, battery, uptime and top processes.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and folders in a directory (defaults to the user's home folder).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path. Defaults to home."}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file from the computer.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a text file with the given content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search for files by name under a folder (defaults to the user's home folder).",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Substring of the file name."},
                    "path": {"type": "string", "description": "Folder to search in. Defaults to home."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clipboard",
            "description": "Get the current clipboard text, or set it to new text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["get", "set"]},
                    "text": {"type": "string", "description": "Text to put on the clipboard when action is 'set'."},
                },
                "required": ["action"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [truncated, {len(text) - limit} more characters]"


def _fail(message: str) -> dict[str, Any]:
    return {"ok": False, "result": f"Error: {message}"}


def _placeholder_image(message: str) -> str:
    """Generate a placeholder PNG (base64) when the screen can't be captured."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (960, 540), (10, 16, 30))
    draw = ImageDraw.Draw(img)
    draw.rectangle([40, 40, 920, 500], outline=(40, 200, 230), width=3)
    # simple wrapped message
    words, lines, line = message.split(), [], ""
    for w in words:
        if len(line) + len(w) > 60:
            lines.append(line)
            line = w
        else:
            line = (line + " " + w).strip()
    if line:
        lines.append(line)
    y = 230
    for ln in lines[:8]:
        draw.text((80, y), ln, fill=(160, 220, 245))
        y += 28
    draw.text((80, 470), "Aegis — screen capture", fill=(90, 130, 160))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _pyautogui():
    import pyautogui  # noqa: PLC0415  (lazy: needs a display)

    pyautogui.FAILSAFE = True
    return pyautogui


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def capture_screen() -> tuple[_Image, _Image, float, tuple[int, int]]:
    """Grab the primary display once.

    Returns ``(for_the_model, for_the_human, scale, (display_w, display_h))``:
    two differently-scaled copies of a single grab, so one capture feeds both the
    LLM (small — it costs tokens) and the HUD (large — the user has to read it).
    """
    import mss
    from PIL import Image

    with mss.mss() as sct:
        monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
        shot = sct.grab(monitor)
        img = Image.frombytes("RGB", shot.size, shot.rgb)

    full_size = (img.width, img.height)
    scale = 1.0
    for_the_model = img
    if img.width > MAX_IMAGE_WIDTH:
        scale = MAX_IMAGE_WIDTH / img.width
        for_the_model = img.resize((MAX_IMAGE_WIDTH, max(1, int(img.height * scale))))

    # The HUD panel is a fraction of the screen; sending it a 4K frame wastes
    # bandwidth for detail nobody can see.
    for_the_human = img
    if img.width > LIVE_MAX_WIDTH:
        human_scale = LIVE_MAX_WIDTH / img.width
        for_the_human = img.resize((LIVE_MAX_WIDTH, max(1, int(img.height * human_scale))))

    global _last_full
    _last_full = for_the_human
    return for_the_model, for_the_human, scale, full_size


def see_screen() -> dict[str, Any]:
    try:
        img, _human, scale, (full_w, full_h) = capture_screen()
        buf = io.BytesIO()
        img.save(buf, "PNG")
        data = base64.b64encode(buf.getvalue()).decode()
        global _last_scale
        _last_scale = (scale, full_w, full_h)
        how = (
            f"shown at {img.width}x{img.height}, scaled {1 / scale:.2f}x from your "
            f"{full_w}x{full_h} display"
            if scale != 1.0
            else f"shown 1:1 at {full_w}x{full_h}"
        )
        return {
            "ok": True,
            "result": (
                f"Screenshot captured and attached below ({how}). "
                "Click coordinates are converted back to screen pixels automatically, "
                "so use the pixel positions you see in this image."
            ),
            "image": data,
            "image_width": img.width,
            "image_height": img.height,
            "screen_width": full_w,
            "screen_height": full_h,
        }
    except Exception as exc:  # no display, permissions, etc.
        return {
            "ok": True,
            "result": f"Screen capture unavailable here ({exc}). If you are running this on your PC, make sure a display is attached and the app has screen-recording permission.",
            "image": _placeholder_image(f"Screen capture unavailable: {exc}"),
            "image_width": 960,
            "image_height": 540,
            "screen_width": 960,
            "screen_height": 540,
        }


def live_frame(fps: float = 2.0) -> dict[str, Any]:
    """One frame of the live view, as JPEG.

    ``reused`` tells the browser whether this is a fresh capture or the previous
    frame re-sent because it asked faster than the limit, and ``tick`` increments
    once per new frame so the UI can prove the view is actually live.

    Aegis cannot make this read-only once it is on: anything holding the view open
    is watching the screen. So the server enforces its own limits instead of
    trusting the client — the rate is clamped, a run ends by itself after
    LIVE_MAX_SECONDS, and after LIVE_IDLE_STOP with no callers the next frame
    starts a new run the UI has to re-authorise. Frames go to the caller only;
    nothing is written to disk.
    """
    global _live_requests, _live_opened, _live_frame, _live_frame_info, _live_tick

    now = time.monotonic()
    with _live_lock:
        if now - _live_requests > LIVE_IDLE_STOP:
            _live_opened, _live_frame = now, ""      # idle: this is a new run
        elif now - _live_opened > LIVE_MAX_SECONDS:
            return {
                "ok": False,
                "reason": "time limit",
                "detail": (
                    f"Live view stopped after {LIVE_MAX_SECONDS / 60:.0f} minutes "
                    "so it cannot be left running by accident. Turn it back on if "
                    "you still need it."
                ),
            }
        interval = 1.0 / max(1.0, min(LIVE_MAX_FPS, fps))
        since_last = now - _live_requests
        cached, cached_info = _live_frame, _live_frame_info
        _live_requests = now

    # A client polling faster than its cap gets the frame it already has: the cap
    # has to be enforced here, not by well-behaved JavaScript.
    if cached and since_last < interval:
        return {**cached_info, "image": cached, "reused": True}

    try:
        _model, human, _scale, (full_w, full_h) = capture_screen()
        buf = io.BytesIO()
        # JPEG, not PNG: this is a video frame refreshed several times a second,
        # and a PNG of a busy screen is roughly 5x the bytes for no gain.
        human.save(buf, "JPEG", quality=70, optimize=True)
        payload = base64.b64encode(buf.getvalue()).decode()
    except Exception as exc:
        return {
            "ok": False,
            "reason": "capture",
            "detail": (
                f"Screen capture unavailable ({exc}). Aegis needs a display and "
                "screen-recording permission."
            ),
        }

    with _live_lock:
        _live_tick += 1
        info = {
            "ok": True,
            "format": "jpeg",
            "tick": _live_tick,
            "width": human.width,
            "height": human.height,
            "screen_width": full_w,
            "screen_height": full_h,
            "fps": round(1.0 / interval, 1),
            "reused": False,
        }
        _live_frame, _live_frame_info = payload, info
    return {**info, "image": payload}


def run_command(command: str, cwd: str | None = None) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=cwd or None,
        )
    except subprocess.TimeoutExpired:
        return _fail("the command timed out after 120 seconds")
    except Exception as exc:
        return _fail(str(exc))
    out = proc.stdout or ""
    if proc.stderr:
        out += ("\n[stderr]\n" if out else "[stderr]\n") + (proc.stderr or "")
    out = out.strip() or "(no output)"
    status = f"exit code {proc.returncode}"
    return {"ok": proc.returncode == 0, "result": _truncate(f"{status}\n{out}")}


def open_app(app: str) -> dict[str, Any]:
    app = (app or "").strip()
    if not app:
        return _fail("no app name given")
    try:
        if sys.platform.startswith("win"):
            os.startfile(app) if Path(app).exists() else subprocess.Popen(  # noqa: S606
                f'start "" "{app}"', shell=True
            )
        elif sys.platform == "darwin":
            target = app if Path(app).exists() else "-a"
            subprocess.Popen(["open", target, app] if target == "-a" else ["open", app])
        else:
            exe = shutil.which(app)
            if exe:
                subprocess.Popen([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif Path(app).exists():
                subprocess.Popen(["xdg-open", app], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                # try common desktop launchers
                for launcher in (["gtk-launch", app], [app]):
                    try:
                        subprocess.Popen(launcher, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        return {"ok": True, "result": f"Opened {app}."}
                    except Exception:
                        continue
                return _fail(f"couldn't find an app or file named '{app}'")
        return {"ok": True, "result": f"Opened {app}."}
    except Exception as exc:
        return _fail(str(exc))


def type_text(text: str) -> dict[str, Any]:
    try:
        import pyperclip

        pyperclip.copy(text)
        pg = _pyautogui()
        modifier = "command" if sys.platform == "darwin" else "ctrl"
        pg.hotkey(modifier, "v")
        return {"ok": True, "result": f"Typed {len(text)} characters."}
    except Exception as exc:
        return _fail(f"could not type: {exc}")


def press_keys(keys: list[str] | str) -> dict[str, Any]:
    """Press one key, or a combination.

    Models routinely send ``"enter"`` where the schema says ``["enter"]``; a
    bare string used to be iterated into five single-key presses, which is why
    this normalises first.
    """
    try:
        if isinstance(keys, str):
            keys = [keys]
        keys = [str(k).strip() for k in (keys or []) if str(k).strip()]
        if not keys:
            return _fail("no keys given (expected something like [\"ctrl\", \"s\"])")
        pg = _pyautogui()
        if len(keys) == 1:
            pg.press(keys[0])
        else:
            pg.hotkey(*keys)
        return {"ok": True, "result": f"Pressed {'+'.join(keys)}."}
    except Exception as exc:
        return _fail(str(exc))


def click(x: int, y: int, button: str = "left") -> dict[str, Any]:
    """Click at coordinates read off the most recent screenshot.

    ``see_screen`` may downscale the image before the model sees it, so the
    numbers the model produces are image pixels, not screen pixels. Mapping them
    back here is what makes "click the blue button" land on the button.
    """
    try:
        x, y = int(x), int(y)
    except (TypeError, ValueError):
        return _fail(f"coordinates must be numbers, got x={x!r} y={y!r}")
    if x < 0 or y < 0:
        return _fail(f"coordinates must not be negative, got ({x}, {y})")

    scale, screen_w, screen_h = _last_scale
    if scale and scale != 1.0:
        mapped_x, mapped_y = int(round(x / scale)), int(round(y / scale))
    else:
        mapped_x, mapped_y = x, y
    if screen_w and screen_h and not (0 <= mapped_x < screen_w and 0 <= mapped_y < screen_h):
        return _fail(
            f"({x}, {y}) maps to ({mapped_x}, {mapped_y}), which is outside your "
            f"{screen_w}x{screen_h} screen. Take a fresh screenshot and pick a "
            "point inside it."
        )
    try:
        pg = _pyautogui()
        pg.click(mapped_x, mapped_y, button=button or "left")
    except Exception as exc:
        return _fail(str(exc))
    note = f" (from image point {x}, {y})" if (mapped_x, mapped_y) != (x, y) else ""
    return {"ok": True, "result": f"Clicked at ({mapped_x}, {mapped_y}){note} with {button} button."}


def system_info() -> dict[str, Any]:
    lines = [f"Platform: {platform.system()} {platform.release()} ({platform.machine()})"]
    try:
        import psutil

        boot = time.time() - psutil.boot_time()
        lines.append(f"Boot time: {int(boot // 3600)}h {int((boot % 3600) // 60)}m ago")
        lines.append(f"CPU: {platform.processor() or 'unknown'}, {psutil.cpu_count(logical=True)} cores, load {psutil.getloadavg()[0]:.2f}" if hasattr(psutil, "getloadavg") else f"CPU: {platform.processor()}, {psutil.cpu_count(logical=True)} cores")
        vm = psutil.virtual_memory()
        lines.append(f"Memory: {vm.used / 1e9:.1f} GB used of {vm.total / 1e9:.1f} GB ({vm.percent}%)")
        for part in psutil.disk_partitions()[:3]:
            try:
                usage = psutil.disk_usage(part.mountpoint)
                lines.append(f"Disk {part.mountpoint}: {usage.used / 1e9:.0f} GB used of {usage.total / 1e9:.0f} GB ({usage.percent}%)")
            except Exception:
                continue
        if hasattr(psutil, "sensors_battery"):
            bat = psutil.sensors_battery()
            if bat:
                lines.append(f"Battery: {bat.percent}%{' (charging)' if bat.power_plugged else ''}")
        procs = []
        for p in psutil.process_iter(["name", "cpu_percent", "memory_percent"]):
            try:
                procs.append((p.info["memory_percent"] or 0, p.info["name"]))
            except Exception:
                continue
        top = " , ".join(n for _, n in sorted(procs, reverse=True)[:5])
        lines.append(f"Top processes by memory: {top}")
    except Exception as exc:
        lines.append(f"(detailed stats unavailable: {exc})")
    return {"ok": True, "result": _truncate("\n".join(lines))}


def list_files(path: str | None = None) -> dict[str, Any]:
    target, denied = check_readable(path) if path else (Path.home().resolve(), None)
    if denied:
        return _fail(denied)
    if not target.exists() or not target.is_dir():
        return _fail(f"'{target}' is not a folder")
    rows = []
    try:
        entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        for e in entries[:100]:
            kind = "dir " if e.is_dir() else "file"
            size = ""
            if e.is_file():
                try:
                    s = e.stat().st_size
                    size = f"  {s / 1e6:.1f} MB" if s > 1e6 else f"  {s / 1e3:.1f} KB"
                except Exception:
                    pass
            rows.append(f"{kind}  {e.name}{size}")
        if len(entries) > 100:
            rows.append(f"… and {len(entries) - 100} more")
    except Exception as exc:
        return _fail(str(exc))
    return {"ok": True, "result": _truncate(f"{target}:\n" + ("\n".join(rows) or "(empty folder)"))}


def read_file(path: str) -> dict[str, Any]:
    p, denied = check_readable(path)
    if denied:
        return _fail(denied)
    if not p.exists():
        return _fail(f"'{path}' does not exist")
    if not p.is_file():
        return _fail(f"'{path}' is not a file")
    try:
        # Never materialise a whole (possibly huge or binary) file: read one
        # character past the cap so we can still say that it was truncated.
        with p.open("r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(MAX_FILE_READ + 1)
    except Exception as exc:
        return _fail(str(exc))
    return {"ok": True, "result": _truncate(text, MAX_FILE_READ)}


def write_file(path: str, content: str) -> dict[str, Any]:
    p, denied = check_writable(path)
    if denied:
        return _fail(denied)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content or "", encoding="utf-8")
    except Exception as exc:
        return _fail(str(exc))
    return {"ok": True, "result": f"Wrote {len(content or '')} characters to {p}."}


def search_files(query: str, path: str | None = None) -> dict[str, Any]:
    needle = (query or "").strip().lower()
    if not needle:
        return _fail("give me something to search for")
    root, denied = check_readable(path) if path else (Path.home().resolve(), None)
    if denied:
        return _fail(denied)
    if not root.exists():
        return _fail(f"'{root}' does not exist")

    skip = {"node_modules", "__pycache__", "venv", ".venv", "site-packages", "Library"}
    hits: list[str] = []
    folders = 0
    entries = 0
    stopped = None
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d.lower() not in skip]
        for fn in filenames:
            entries += 1
            if entries > MAX_SEARCH_ENTRIES:
                stopped = f"after {MAX_SEARCH_ENTRIES} files"
                break
            if needle in fn.lower():
                candidate = Path(dirpath) / fn
                if check_readable(candidate)[1]:
                    continue  # never surface the path of a credential file
                hits.append(str(candidate))
                if len(hits) >= MAX_SEARCH_HITS:
                    stopped = f"after {MAX_SEARCH_HITS} matches"
                    break
        if stopped:
            break
        folders += 1
        if folders > MAX_WALK_DIRS:
            # Bounded work per call: a search of a huge tree must not pin the
            # request for minutes. The model is told to narrow the path.
            stopped = f"after {MAX_WALK_DIRS} folders"
            break
    if stopped:
        hits.append(f"… stopped {stopped} — narrow the search path to go deeper")
    return {"ok": True, "result": _truncate("\n".join(hits) or f"No files matching '{query}' under {root}")}


def clipboard(action: str, text: str | None = None) -> dict[str, Any]:
    try:
        import pyperclip

        if action == "get":
            return {"ok": True, "result": _truncate(redact(f"Clipboard: {pyperclip.paste()!r}"))}
        pyperclip.copy(text or "")
        return {"ok": True, "result": "Clipboard updated."}
    except Exception as exc:
        return _fail(str(exc))


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def execute_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run a tool by name and return ``{ok, result, [image, …]}``.

    Every result passes through :func:`aegis.safety.redact_result` on the way
    out, so credential-shaped text can never reach the model (or the browser)
    just because a command happened to print it.
    """
    args = args or {}
    try:
        result = _dispatch(name, args)
    except Exception as exc:  # never let a tool crash the agent
        return _fail(f"{type(exc).__name__}: {exc}")
    return redact_result(result)


def install_update(args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Install the staged update. Reached only by redeeming an approval.

    Not part of the model's tool list on purpose: the agent must not be able to
    replace its own executable. It exists here because the approval reducer
    dispatches through the same toolbox, and the two-step redeem is what makes
    the install deliberate (see aegis.updates).
    """
    from . import updates

    result = updates.apply_staged_update()
    if not result.get("ok"):
        return _fail(str(result.get("detail") or "The update could not be installed."))
    return {"ok": True, "result": str(result.get("detail") or "Installing now.")}


def _dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "install_update":
        return install_update(args)
    if name == "see_screen":
        return see_screen()
    if name == "run_command":
        return run_command(args.get("command", ""), args.get("cwd"))
    if name == "open_app":
        return open_app(args.get("app", ""))
    if name == "type_text":
        return type_text(args.get("text", ""))
    if name == "press_keys":
        return press_keys(args.get("keys", []))
    if name == "click":
        return click(args.get("x", 0), args.get("y", 0), args.get("button", "left"))
    if name == "system_info":
        return system_info()
    if name == "list_files":
        return list_files(args.get("path"))
    if name == "read_file":
        return read_file(args.get("path", ""))
    if name == "write_file":
        return write_file(args.get("path", ""), args.get("content", ""))
    if name == "search_files":
        return search_files(args.get("query", ""), args.get("path"))
    if name == "clipboard":
        return clipboard(args.get("action", "get"), args.get("text"))
    return _fail(f"unknown tool '{name}'")
