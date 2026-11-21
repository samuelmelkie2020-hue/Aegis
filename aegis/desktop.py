"""The desktop shell.

Aegis started life as a web page. This module gives it a native window without
duplicating the app: uvicorn runs in a background thread on loopback, and
pywebview shows the same UI in the OS webview (WebView2 on Windows, WKWebView on
macOS, WebKitGTK on Linux).

Threading rules that matter here, because getting them wrong hangs the app:

* uvicorn's own signal handlers are disabled — in a thread they would fight the
  webview's event loop, and Ctrl+C would not close the window.
* the window is the main thread; the server is the child. When the window
  closes we shut the server down explicitly.
* readiness is a real HTTP poll of ``/api/health``, never a fixed sleep.

Security is unchanged from the browser build: the server is bound to 127.0.0.1,
and the token handshake still applies — except that in the desktop shell it is
invisible, because the first navigation goes to ``/?token=…`` on loopback, which
the server exchanges for an HttpOnly session cookie.
"""

from __future__ import annotations

import logging
import os
import platform
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("aegis.desktop")

APP_NAME = "Aegis"
WINDOW_WIDTH = 1240
WINDOW_HEIGHT = 840
MIN_SIZE = (900, 620)
BROWSER_COLOR = "#060b16"
STARTUP_TIMEOUT = 30.0
ERROR_WINDOW_TIMEOUT = 20.0  # never block forever on a crash dialog


def find_free_port(preferred: int) -> int:
    """Return ``preferred`` if it is free, otherwise an ephemeral port.

    A second launch used to die with "address already in use"; now it simply
    picks a different port, which also lets two windows coexist.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", preferred))
        if preferred == 0:
            # "Any port" must resolve to the real one, or callers poll port 0.
            return probe.getsockname()[1]
        return preferred
    except OSError:
        probe.close()
        fallback = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            fallback.bind(("127.0.0.1", 0))
            return fallback.getsockname()[1]
        finally:
            fallback.close()
    finally:
        try:
            probe.close()
        except OSError:
            pass


@dataclass
class ServerHandle:
    port: int
    thread: threading.Thread
    server: object

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def start_server(port: int, *, log_level: str = "warning") -> ServerHandle:
    """Run uvicorn on loopback in a daemon thread and wait until it answers."""
    import uvicorn

    from .security import get_token
    from .server import app

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level=log_level,
        access_log=False,
        lifespan="off",
    )
    server = uvicorn.Server(config)
    # In a thread these would try to own SIGINT/SIGTERM from the webview loop.
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    thread = threading.Thread(target=server.run, name="aegis-uvicorn", daemon=True)
    thread.start()

    handle = ServerHandle(port=port, thread=thread, server=server)
    _await_ready(handle, get_token())
    return handle


def _await_ready(handle: ServerHandle, token: str) -> None:
    """Poll the health endpoint until the app answers (or give up loudly)."""
    deadline = time.monotonic() + STARTUP_TIMEOUT
    url = f"{handle.base_url}/api/health"
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if not handle.thread.is_alive():
            raise RuntimeError("The Aegis server exited during startup.")
        try:
            request = urllib.request.Request(url, headers={"X-Aegis-Token": token})
            with urllib.request.urlopen(request, timeout=2):
                return
        except urllib.error.HTTPError as exc:
            # Any real HTTP answer (even 401) means the socket is serving.
            if exc.code in (200, 401, 403):
                return
            last_error = exc
        except Exception as exc:  # connection refused while it boots
            last_error = exc
        time.sleep(0.1)

    hint = ""
    try:
        from .config import CONFIG_PATH

        hint = f" Settings file: {CONFIG_PATH}"
    except Exception:  # pragma: no cover - diagnostics must never raise
        pass
    raise RuntimeError(
        f"Aegis did not start within {STARTUP_TIMEOUT:.0f}s ({last_error}).{hint}"
    )


def stop_server(handle: ServerHandle | None) -> None:
    if handle is None:
        return
    try:
        handle.server.should_exit = True  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover
        log.warning("Could not ask the server to stop", exc_info=True)
    handle.thread.join(timeout=5)


# ---------------------------------------------------------------------------
# JS bridge (window controls from the page)
# ---------------------------------------------------------------------------

class DesktopAPI:
    """Exposed to the page as ``window.pywebview.api`` in the desktop shell.

    Deliberately tiny: the page keeps using HTTP for everything else, so the
    browser build and the desktop build stay one codebase.
    """

    def __init__(self) -> None:
        self._window = None
        self._server: ServerHandle | None = None

    def attach(self, window, server: ServerHandle | None = None) -> None:
        self._window = window
        self._server = server

    def info(self) -> dict:
        from . import __version__

        return {
            "desktop": True,
            "version": __version__,
            "port": self._server.port if self._server else None,
            "platform": os.name,
        }

    def open_settings(self) -> None:
        """Focus the window and open the settings dialog (menu accelerator)."""
        if self._window is None:
            return
        self._window.restore() if self._window.minimized else self._window.show()
        try:
            self._window.evaluate_js("window.__aegisOpenSettings && window.__aegisOpenSettings()")
        except Exception:  # pragma: no cover - best effort
            log.debug("Could not open settings from the menu", exc_info=True)

    def new_conversation(self) -> None:
        if self._window is None:
            return
        try:
            self._window.evaluate_js("window.__aegisNewChat && window.__aegisNewChat()")
        except Exception:  # pragma: no cover
            log.debug("Could not start a new conversation", exc_info=True)

    def min_size(self) -> dict:
        return {"width": MIN_SIZE[0], "height": MIN_SIZE[1]}


def _menu(api: DesktopAPI):
    """A minimal native menu: settings, new conversation, quit."""
    try:
        from webview.menu import Menu, MenuAction, MenuSeparator
    except Exception:  # pragma: no cover - older pywebview
        return []

    items = [
        MenuAction("Settings…", api.open_settings),
        MenuAction("New conversation", api.new_conversation),
        MenuSeparator(),
    ]
    return [Menu("Aegis", items)]


def window_icon_path() -> Path | None:
    """The window icon, wherever it lives in this layout (source or bundle).

    Windows and macOS take the icon from the executable itself, so this only
    matters on Linux — but returning ``None`` cleanly is what keeps a missing
    asset from breaking startup anywhere.
    """
    from .server import STATIC_DIR

    candidate = Path(STATIC_DIR) / "aegis.png"
    return candidate if candidate.exists() else None


def _ensure_streams() -> None:
    """Give a windowed bundle somewhere to print.

    PyInstaller's ``console=False`` build leaves ``sys.stdout`` and ``sys.stderr``
    as ``None``, so a bare ``print`` raises ``AttributeError`` and takes the
    process down. Diagnostics must survive that.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115
            except OSError:  # pragma: no cover - devnull always opens
                pass


def self_check() -> int:
    """Verify a build can start, without opening a window.

    Used by CI against the frozen binary and by users who report "it does not
    open" — it checks the pieces that actually break in a bundle (static
    assets, imports, a writable settings location) and prints where the
    settings file lives.
    """
    _ensure_streams()
    problems: list[str] = []
    report: list[str] = []

    def say(line: str = "") -> None:
        report.append(line)
        print(line)

    try:
        from . import __version__

        say(f"Aegis {__version__} self-check")
    except Exception as exc:  # pragma: no cover - only if the bundle is broken
        problems.append(f"version import failed: {exc}")

    try:
        from .config import CONFIG_PATH, DATA_DIR, is_frozen, user_data_dir

        say(f"  frozen:      {is_frozen()}")
        say(f"  data dir:    {DATA_DIR}")
        say(f"  settings:    {CONFIG_PATH}")
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        probe = user_data_dir() / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        say("  writable:    yes")
    except Exception as exc:
        problems.append(f"settings location unusable: {exc}")

    try:
        from pathlib import Path

        from .server import STATIC_DIR

        for asset in ("index.html", "app.js", "style.css"):
            if not (Path(STATIC_DIR) / asset).exists():
                problems.append(f"missing bundled asset: aegis/static/{asset}")
        if window_icon_path() is None:
            # Cosmetic, so it is reported but does not fail the check.
            print("  window icon: missing (aegis.png not bundled)")
        else:
            print("  window icon: ok")
        say(f"  web UI:      {STATIC_DIR}")
    except Exception as exc:
        problems.append(f"web UI missing: {exc}")

    try:
        handle = start_server(find_free_port(0))
        say(f"  server:      ok ({handle.base_url})")
        stop_server(handle)
        say("  shutdown:    ok")
    except Exception as exc:
        problems.append(f"server failed to start: {exc}")

    try:
        import webview  # noqa: F401

        say("  webview:     installed")
    except Exception as exc:
        problems.append(f"pywebview not available: {exc}")

    if problems:
        say("\nFAILED:")
        for problem in problems:
            say(f"  - {problem}")
    else:
        say("\nAll checks passed.")
    _write_report(report)
    return 1 if problems else 0


def _webview2_version() -> str | None:
    """The installed Edge WebView2 runtime version, or ``None`` when absent.

    Duplicated from the frozen bootstrap (packaging/entry.py) on purpose: that
    file must run before the application imports, so it cannot import this one.
    """
    if not sys.platform.startswith("win"):
        return None
    try:
        import winreg
    except ImportError:  # pragma: no cover - only reachable on Windows
        return None

    guide = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for root, prefix in (
        (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\"),
        (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\"),
        (winreg.HKEY_CURRENT_USER, "SOFTWARE\\"),
    ):
        try:
            with winreg.OpenKey(root, prefix + guide) as key:
                version = winreg.QueryValueEx(key, "pv")[0]
                if version and version != "0.0.0.0":
                    return version
        except OSError:
            continue
    return None


def diagnose() -> int:
    """A shareable report for "it does not open".

    Deliberately not a pass/fail check like ``--self-check``: the question here
    is *why* nothing appeared, so it prints the facts a maintainer needs — the
    webview backend, whether WebView2 is present, whether another copy already
    holds the port, and whether a crash or startup log exists — and says which
    of them is the likely culprit.
    """
    _ensure_streams()
    report: list[str] = []

    def say(line: str = "") -> None:
        report.append(line)
        print(line)

    from . import __version__

    say(f"Aegis {__version__} — startup diagnosis")
    say(f"  python:      {sys.version.split()[0]}")
    say(f"  platform:    {platform_description()}")
    say(f"  frozen:      {is_frozen_bundle()}")
    say(f"  executable:  {sys.executable}")

    try:
        from .config import CONFIG_PATH, user_data_dir

        say(f"  data dir:    {user_data_dir()}")
        say(f"  settings:    {CONFIG_PATH}")
        writable = "yes"
        try:
            probe = user_data_dir() / ".write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except Exception as exc:
            writable = f"NO ({exc})"
        say(f"  writable:    {writable}")
    except Exception as exc:
        say(f"  data dir:    unavailable ({exc})")

    try:
        import webview

        say(f"  pywebview:   {getattr(webview, '__version__', 'installed')}")
    except Exception as exc:
        say(f"  pywebview:   MISSING ({exc})")
        say("")
        say("→ Install it: pip install pywebview")

    if sys.platform.startswith("win"):
        version = _webview2_version()
        say(f"  WebView2:    {version or 'NOT FOUND'}")
        if not version:
            say("")
            say("→ This is the usual cause of 'nothing happens' on Windows.")
            say("  Install it once (free, ~2 MB):")
            say("  https://go.microsoft.com/fwlink/p/?LinkId=2124703")

    port = int(os.environ.get("AEGIS_PORT", "8000"))
    busy = _port_in_use(port)
    say(f"  port {port}:   {'IN USE — another copy is probably running' if busy else 'free'}")
    if busy:
        say("")
        say("→ Close the other Aegis window, or start this one with a different port:")
        say(f"  Aegis.exe --web --port {port + 1}")

    try:
        from .config import user_data_dir as dir_for_logs

        for name in ("aegis-crash.log", "aegis-startup.log", "self-check.txt"):
            path = dir_for_logs() / name
            if path.exists():
                say(f"  {name}: {path}")
                if name.endswith(("crash.log", "startup.log")):
                    say("")
                    say(f"  --- last lines of {name} ---")
                    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]:
                        say(f"  {line}")
                    say(f"  --- end of {name} ---")
                    say("")
    except Exception:  # pragma: no cover - diagnostics must never raise
        pass

    say("")
    say("Copy this report when asking for help. It contains no API key and no token.")
    try:
        from .config import user_data_dir as dir_for_logs

        (dir_for_logs() / "aegis-diagnose.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    except Exception:  # pragma: no cover
        pass
    return 0


def platform_description() -> str:
    return f"{platform.system()} {platform.release()} ({platform.machine()})"


def is_frozen_bundle() -> bool:
    return bool(getattr(sys, "frozen", False))


def _port_in_use(port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.4)
    try:
        return probe.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False
    finally:
        probe.close()


def _write_report(report: list[str]) -> None:
    """Persist the self-check output: windowed builds have no console to read."""
    try:
        from .config import DATA_DIR

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "self-check.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    except Exception:  # pragma: no cover - diagnostics must never raise
        pass


def _report_fatal(exc: Exception, *, page: str | None = None) -> None:
    """Tell the user something useful even when there is no terminal.

    A windowed build (`console=False`) swallows stdout, so a startup failure
    would otherwise look like "nothing happened".

    Order matters, and one option is deliberately missing: a **modal** dialog
    (tkinter) is not used. In CI it blocks forever on a click that never comes,
    and on a desktop it can end up hidden behind the project window while the
    user waits for something that already failed. So: always write the log,
    then show the error in a real window if one can be opened, and otherwise
    just leave a message the user can find.
    """
    import traceback

    detail = "".join(traceback.format_exception(exc))
    log.error("Fatal startup error", exc_info=exc)
    location = ""
    try:
        from .config import DATA_DIR

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = DATA_DIR / "aegis-crash.log"
        path.write_text(detail, encoding="utf-8")
        location = str(path)
    except Exception:  # pragma: no cover
        pass

    print(f"\n  Could not start Aegis: {exc}\n")
    if location:
        print(f"  Details written to {location}\n")

    if not page:
        return
    if os.environ.get("AEGIS_NO_GUI_ERRORS"):
        # Headless (CI, a service): a window here would block with nobody to
        # close it. The log above is the record.
        return
    try:  # the webview is already imported: we only get here after it was
        import webview

        webview.create_window("Aegis could not start", html=page(error=exc, log=location))
        _show_without_blocking(webview)
    except Exception:  # pragma: no cover - no display, no backend, or headless
        log.warning("Could not show the error window", exc_info=True)


def _show_without_blocking(webview, timeout: float | None = None) -> None:
    """Open a window, but never wait on it forever.

    ``webview.start()`` can block without raising when there is no usable
    display, and a message nobody can dismiss is worse than a log line: it
    hides the failure. Runs it on a daemon thread and gives up quietly.
    """
    thread = threading.Thread(
        target=_swallow, args=(webview.start,), name="aegis-error-window", daemon=True
    )
    thread.start()
    thread.join(ERROR_WINDOW_TIMEOUT if timeout is None else timeout)


def _swallow(func) -> None:
    try:
        func()
    except Exception:  # pragma: no cover - the point is to ignore it
        log.warning("Error window failed", exc_info=True)


def _start_webview(webview, **options) -> None:
    """Run the webview event loop, and never return silently without a window.

    pywebview reports a missing or broken backend in two ways: it raises, or —
    on Windows when WebView2 cannot initialise — it logs a warning and returns
    *without ever opening a window*. The second case is the bug report we kept
    getting: the process is alive, the server is on port 8000 and answering,
    and the user sees nothing at all. So if ``start()`` returns and no window
    was ever created, that is treated as a failure with a real message.
    """
    webview.start(**options)
    try:
        open_windows = len(webview.windows)
    except Exception:  # pragma: no cover - older pywebview
        return
    if open_windows == 0:
        raise RuntimeError(
            "The desktop window could not be created. This is almost always a "
            "missing WebView2 runtime on Windows, or a missing WebKitGTK on "
            "Linux — see the startup log for the backend's own message."
        )


def run_desktop(*, port: int | None = None, debug: bool = False) -> int:
    """Start the server, open the desktop window, and block until it closes."""
    _ensure_streams()
    try:
        import webview
    except ImportError:
        print(
            "The desktop window needs pywebview, which is not installed.\n"
            "  pip install pywebview\n"
            "or run the browser version:  python run.py --web"
        )
        return 1

    from .security import get_token

    preferred = port or int(os.environ.get("AEGIS_PORT", "8000"))
    chosen = find_free_port(preferred)
    if chosen != preferred:
        log.warning("Port %s was busy; using %s instead", preferred, chosen)

    try:
        handle = start_server(chosen)
    except Exception as exc:
        _report_fatal(exc, page=_error_page)
        return 1

    api = DesktopAPI()
    try:
        window = webview.create_window(
            title=APP_NAME,
            url=f"{handle.base_url}/?token={get_token()}",
            js_api=api,
            width=WINDOW_WIDTH,
            height=WINDOW_HEIGHT,
            min_size=MIN_SIZE,
            background_color=BROWSER_COLOR,
            text_select=True,
            zoomable=True,
            confirm_close=True,
            **({"icon": str(icon)} if (icon := window_icon_path()) else {}),
        )
        api.attach(window, handle)

        # The page also asks for these via JS so the browser build stays unaware.
        try:
            window.expose(api.info, api.open_settings, api.new_conversation, api.min_size)
        except Exception:  # pragma: no cover - optional in older versions
            pass

        print(f"\n  🛡 Aegis is running as a desktop app (port {chosen}).")
        print("     Close the window to quit.\n")

        _start_webview(
            webview,
            debug=debug,
            private_mode=False,          # keep localStorage (voice prefs) between runs
            menu=_menu(api),
            http_server=False,           # we serve from uvicorn, not pywebview
        )
    except Exception as exc:  # e.g. no WebKitGTK, no display
        _report_fatal(exc, page=_error_page)
        return 1
    finally:
        stop_server(handle)
    return 0


_ERROR_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Aegis could not start</title>
<style>
  body {{ margin:0; min-height:100vh; display:grid; place-items:center;
         background:#060b16; color:#e6edf7;
         font:16px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif; }}
  main {{ max-width:38rem; padding:2rem; }}
  h1 {{ font-size:1.35rem; margin:0 0 .75rem; }}
  code {{ display:block; white-space:pre-wrap; word-break:break-word;
         background:#0f172a; border:1px solid #24314a; border-radius:.5rem;
         padding:.75rem; margin-top:.75rem; color:#ffb4b4; }}
  p {{ margin:.5rem 0; color:#9fb0c9; }}
</style></head>
<body><main>
  <h1>Aegis could not start</h1>
  <code>{error}</code>
  <p>Full details: {log}</p>
  <p>On Linux the window needs WebKitGTK
     (<code style="display:inline">libwebkit2gtk-4.1-0</code> + <code style="display:inline">python3-gi</code>).
     Run <code style="display:inline">python run.py --self-check</code> to see what is missing,
     or <code style="display:inline">python run.py --web</code> to use the browser instead —
     it is the same app.</p>
</main></body></html>"""


def _error_page(*, error: object, log: str) -> str:
    """Render the crash page with everything HTML-escaped."""
    from html import escape

    return _ERROR_PAGE.format(error=escape(str(error)), log=escape(log or "not written"))
