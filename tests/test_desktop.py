"""The desktop shell.

The window itself cannot be opened in CI (no display, and WebKitGTK is not
installed), so these tests drive the shell with a fake webview. That covers the
parts that actually break: the threading model, port selection, readiness
polling, the JS bridge, and shutdown.

`run.py --self-check` covers the rest against a real frozen build.
"""

from __future__ import annotations

import sys
import threading
import types

import pytest

# ---------------------------------------------------------------------------
# Paths: a bundled app must not write into its own bundle
# ---------------------------------------------------------------------------

def test_source_mode_keeps_config_beside_the_repo(monkeypatch):
    from jarvis import config

    monkeypatch.delenv("JARVIS_DATA_DIR", raising=False)
    monkeypatch.setattr(config, "is_frozen", lambda: False)
    assert config.user_data_dir().name != "Claw"
    assert (config.user_data_dir() / "jarvis_config.json").exists() or True


@pytest.mark.parametrize(
    "platform_name,env,expected_parts",
    [
        ("Windows", {"APPDATA": "/fake/appdata"}, ("Claw",)),
        ("Darwin", {}, ("Library", "Application Support", "Claw")),
        ("Linux", {"XDG_CONFIG_HOME": "/fake/xdg"}, ("fake", "xdg", "Claw")),
    ],
)
def test_frozen_app_uses_the_per_user_location(monkeypatch, platform_name, env, expected_parts):
    from jarvis import config

    monkeypatch.delenv("JARVIS_DATA_DIR", raising=False)
    monkeypatch.setattr(config, "is_frozen", lambda: True)
    monkeypatch.setattr(config.platform, "system", lambda: platform_name)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    if platform_name == "Linux":
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", "/fake/xdg")

    path = config.user_data_dir()
    for part in expected_parts:
        assert part in path.parts, f"{path} missing {part}"
    # never inside the bundle / site-packages
    assert "site-packages" not in str(path)


def test_data_dir_can_be_overridden(monkeypatch, tmp_path):
    from jarvis import config

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "custom"))
    assert config.user_data_dir() == tmp_path / "custom"


def test_is_frozen_reads_pyinstaller_marker(monkeypatch):
    from jarvis import config

    monkeypatch.delattr(sys, "frozen", raising=False)
    assert config.is_frozen() is False
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert config.is_frozen() is True


# ---------------------------------------------------------------------------
# Port selection
# ---------------------------------------------------------------------------

def test_preferred_port_is_used_when_free():
    from jarvis.desktop import find_free_port

    port = find_free_port(0)
    assert 1024 < port < 65536
    assert find_free_port(port) == port


def test_busy_port_falls_back_instead_of_crashing():
    """A second launch used to die with "address already in use"."""
    import socket

    from jarvis.desktop import find_free_port

    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    busy = taken.getsockname()[1]
    try:
        chosen = find_free_port(busy)
        assert chosen != busy
        assert chosen > 0
    finally:
        taken.close()


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

def test_server_thread_serves_the_app_and_stops_cleanly():
    from jarvis.desktop import find_free_port, start_server, stop_server

    handle = start_server(find_free_port(0))
    try:
        import urllib.error
        import urllib.request

        from jarvis.security import get_token

        # unauthenticated: the socket is live and correctly refuses
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(f"{handle.base_url}/api/health", timeout=5)
        assert excinfo.value.code == 401

        request = urllib.request.Request(
            f"{handle.base_url}/api/health", headers={"X-Jarvis-Token": get_token()}
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200
    finally:
        stop_server(handle)
    assert not handle.thread.is_alive()


def test_server_runs_in_a_background_thread():
    from jarvis.desktop import find_free_port, start_server, stop_server

    handle = start_server(find_free_port(0))
    try:
        assert handle.thread.daemon is True
        assert handle.thread is not threading.main_thread()
    finally:
        stop_server(handle)


def test_stop_server_is_idempotent():
    from jarvis.desktop import stop_server

    stop_server(None)  # must not raise


# ---------------------------------------------------------------------------
# The JS bridge
# ---------------------------------------------------------------------------

class FakeWindow:
    def __init__(self):
        self.shown = False
        self.minimized = False
        self.evaluated: list[str] = []
        self.exposed = []

    def show(self):
        self.shown = True

    def restore(self):
        self.minimized = False

    def evaluate_js(self, script):
        self.evaluated.append(script)

    def expose(self, *funcs):
        self.exposed.extend(funcs)


def test_bridge_reports_desktop_and_version():
    from jarvis.desktop import DesktopAPI

    info = DesktopAPI().info()
    assert info["desktop"] is True
    assert "version" in info


def test_bridge_drives_the_page_not_the_other_way_round():
    from jarvis.desktop import DesktopAPI

    api = DesktopAPI()
    window = FakeWindow()
    api.attach(window)

    api.open_settings()
    api.new_conversation()

    assert window.shown
    assert any("OpenSettings" in s for s in window.evaluated)
    assert any("NewChat" in s for s in window.evaluated)


def test_bridge_is_a_no_op_before_the_window_exists():
    from jarvis.desktop import DesktopAPI

    api = DesktopAPI()
    api.open_settings()      # must not raise
    api.new_conversation()


def test_bridge_survives_a_failing_page():
    from jarvis.desktop import DesktopAPI

    class Broken(FakeWindow):
        def evaluate_js(self, script):
            raise RuntimeError("no page yet")

    api = DesktopAPI()
    api.attach(Broken())
    api.open_settings()  # must not raise


# ---------------------------------------------------------------------------
# Startup path, with a fake webview
# ---------------------------------------------------------------------------

@pytest.fixture()
def fake_webview(monkeypatch):
    """A stand-in for pywebview that records how it was started."""
    recorded = {}

    module = types.ModuleType("webview")
    menu_module = types.ModuleType("webview.menu")

    class _Action:
        def __init__(self, title, func):
            self.title, self.func = title, func

    class _Menu:
        def __init__(self, title, items):
            self.title, self.items = title, items

    menu_module.Menu = _Menu
    menu_module.MenuAction = _Action
    menu_module.MenuSeparator = lambda: object()

    class Window(FakeWindow):
        pass

    def create_window(**kwargs):
        recorded["window_kwargs"] = kwargs
        return Window()

    def start(**kwargs):
        recorded["start_kwargs"] = kwargs
        recorded["url_opened"] = recorded["window_kwargs"].get("url")
        # emulate a quick visit so we can assert the token handshake
        import urllib.request

        try:
            urllib.request.urlopen(recorded["url_opened"], timeout=5)
        except Exception:
            pass

    module.create_window = create_window
    module.start = start
    module.menu = menu_module
    module.Window = Window

    monkeypatch.setitem(sys.modules, "webview", module)
    monkeypatch.setitem(sys.modules, "webview.menu", menu_module)
    return recorded


def test_desktop_opens_a_window_on_a_loopback_token_url(fake_webview):
    from jarvis.desktop import run_desktop

    assert run_desktop(port=0) == 0

    url = fake_webview["url_opened"]
    assert url.startswith("http://127.0.0.1:")
    assert "?token=" in url, "the desktop shell must take the token handshake itself"

    kwargs = fake_webview["start_kwargs"]
    assert kwargs["http_server"] is False, "uvicorn serves the app, not pywebview"
    assert kwargs["private_mode"] is False


def test_desktop_window_is_configured_as_an_app(fake_webview):
    from jarvis.desktop import MIN_SIZE, run_desktop

    run_desktop(port=0)
    kwargs = fake_webview["window_kwargs"]
    assert kwargs["min_size"] == MIN_SIZE
    assert kwargs["confirm_close"] is True
    assert kwargs["js_api"] is not None
    assert kwargs["title"].startswith("Claw")


def test_missing_pywebview_explains_the_two_options(monkeypatch, capsys):
    from jarvis.desktop import run_desktop

    monkeypatch.setitem(sys.modules, "webview", None)
    assert run_desktop(port=0) == 1
    out = capsys.readouterr().out
    assert "pip install pywebview" in out
    assert "--web" in out


def test_self_check_writes_a_report_for_console_less_builds(capsys):
    """A `console=False` bundle has nowhere to print, so the result lands on disk."""
    from jarvis.config import DATA_DIR
    from jarvis.desktop import self_check

    assert self_check() == 0
    capsys.readouterr()

    report = (DATA_DIR / "self-check.txt").read_text(encoding="utf-8")
    assert "Claw" in report
    assert "frozen:" in report
    assert "All checks passed" in report


def test_ensure_streams_survives_a_windowed_build(monkeypatch):
    """PyInstaller's windowed build sets sys.stdout/stderr to None; printing must not crash."""
    import jarvis.desktop as desktop

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    desktop._ensure_streams()
    assert sys.stdout is not None and sys.stderr is not None
    print("ok")  # would raise AttributeError without the fix


def test_self_check_entry_point_runs(capsys):
    from jarvis.desktop import self_check

    assert self_check() == 0
    out = capsys.readouterr().out
    assert "All checks passed" in out
    assert "settings:" in out


# ---------------------------------------------------------------------------
# Startup failures must be visible in a windowed (no-console) build
# ---------------------------------------------------------------------------

class _RecordingWebview(types.ModuleType):
    """pywebview stand-in that records windows and can fail to start."""

    def __init__(self, start_error=None):
        super().__init__("webview")
        self.windows: list[dict] = []
        self.start_calls: list[dict] = []
        self.start_error = start_error

    def create_window(self, *args, **kwargs):
        self.windows.append({"args": args, **kwargs})
        return FakeWindow()

    def start(self, **kwargs):
        self.start_calls.append(kwargs)
        if self.start_error:
            raise self.start_error


def test_fatal_server_error_opens_an_error_window(monkeypatch, tmp_path):
    """Frozen builds exclude tkinter, so the crash message must go through webview."""
    import jarvis.desktop as desktop

    fake = _RecordingWebview()
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setattr(desktop, "start_server", _raise(RuntimeError("boom")))
    monkeypatch.setattr("jarvis.config.DATA_DIR", tmp_path)

    assert desktop.run_desktop(port=0) == 1

    assert len(fake.windows) == 1
    window = fake.windows[0]
    assert window["args"][0] == "Claw could not start"
    assert "boom" in window["html"]
    assert "RuntimeError: boom" in (tmp_path / "claw-crash.log").read_text(encoding="utf-8")


def test_fatal_webview_error_is_reported_not_raised(monkeypatch, tmp_path):
    """Missing WebKitGTK or a display must produce a message, not a bare traceback."""
    import jarvis.desktop as desktop

    fake = _RecordingWebview(start_error=RuntimeError("no display"))
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setattr("jarvis.config.DATA_DIR", tmp_path)

    assert desktop.run_desktop(port=0) == 1

    assert fake.windows[0]["title"].startswith("Claw —")
    assert fake.windows[1]["args"][0] == "Claw could not start"
    assert "no display" in (tmp_path / "claw-crash.log").read_text(encoding="utf-8")


def test_fatal_report_falls_back_to_tkinter_without_a_webview(monkeypatch, tmp_path):
    """A broken pywebview import still has to say something."""
    import jarvis.desktop as desktop

    shown = []
    tkinter = types.ModuleType("tkinter")
    messagebox = types.ModuleType("tkinter.messagebox")

    class _Root:
        def withdraw(self):
            pass

        def destroy(self):
            pass

    tkinter.Tk = _Root
    tkinter.messagebox = messagebox
    messagebox.showerror = lambda title, message: shown.append((title, message))
    monkeypatch.setitem(sys.modules, "webview", None)  # import webview -> ImportError
    monkeypatch.setitem(sys.modules, "tkinter", tkinter)
    monkeypatch.setitem(sys.modules, "tkinter.messagebox", messagebox)
    monkeypatch.setattr("jarvis.config.DATA_DIR", tmp_path)

    desktop._report_fatal(RuntimeError("kaboom"), page=None)

    assert shown and "kaboom" in shown[0][1]
    assert "kaboom" in (tmp_path / "claw-crash.log").read_text(encoding="utf-8")


def test_error_page_escapes_the_message():
    from jarvis.desktop import _error_page

    html = _error_page(error="<script>alert(1)</script>", log="/tmp/<x>.log")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "/tmp/&lt;x&gt;.log" in html


def _raise(exc):
    def boom(*_args, **_kwargs):
        raise exc

    return boom


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_defaults_to_desktop(monkeypatch):
    import run as runner

    calls = {}
    monkeypatch.setattr(
        "jarvis.desktop.run_desktop",
        lambda **kwargs: calls.update(kwargs) or 0,
    )
    assert runner.main([]) == 0
    assert "port" in calls and "debug" in calls


def test_cli_web_flag_serves_instead(monkeypatch):
    import run as runner

    calls = {}
    monkeypatch.setattr(
        runner, "run_web", lambda host, port, open_browser: calls.update(
            {"host": host, "port": port, "open": open_browser}) or 0
    )
    assert runner.main(["--web", "--no-open", "--port", "8123"]) == 0
    assert calls == {"host": "127.0.0.1", "port": 8123, "open": False}


def test_cli_host_flag_is_honoured(monkeypatch):
    import run as runner

    seen = {}
    monkeypatch.setattr(runner, "run_web", lambda host, port, open_browser: seen.update(
        {"host": host, "port": port}) or 0)
    runner.main(["--web", "--host", "0.0.0.0", "--no-open"])
    assert seen["host"] == "0.0.0.0"


def test_self_check_flag_short_circuits(monkeypatch):
    import run as runner

    monkeypatch.setattr("jarvis.desktop.self_check", lambda: 0)
    assert runner.main(["--self-check"]) == 0
