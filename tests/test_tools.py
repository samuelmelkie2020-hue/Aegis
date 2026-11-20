"""Tool behaviour: path policy, redaction, coordinate mapping, arg coercion."""

from __future__ import annotations

import os

import pytest

# ---------------------------------------------------------------------------
# Path policy (#4)
# ---------------------------------------------------------------------------

def test_reads_inside_home_are_allowed(paths):
    from jarvis.tools import execute_tool

    target = paths.home / "notes.txt"
    target.write_text("hello", encoding="utf-8")
    result = execute_tool("read_file", {"path": str(target)})
    assert result["ok"] is True
    assert "hello" in result["result"]


@pytest.mark.parametrize(
    "relative",
    [".ssh/id_rsa", ".aws/credentials", ".docker/config.json", ".config/gh/hosts.yml", ".env"],
)
def test_credential_locations_are_refused(paths, relative):
    from jarvis.tools import execute_tool

    target = paths.home / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("SECRET", encoding="utf-8")
    result = execute_tool("read_file", {"path": str(target)})
    assert result["ok"] is False
    assert "refused" in result["result"]
    assert "SECRET" not in result["result"]


def test_a_normal_config_folder_is_still_readable(paths):
    """Over-blocking ~/.config would break reading ordinary app settings."""
    from jarvis.tools import execute_tool

    target = paths.home / ".config/Code/User/settings.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"editor.fontSize": 14}', encoding="utf-8")
    assert execute_tool("read_file", {"path": str(target)})["ok"] is True


def test_reads_outside_home_are_refused(paths):
    from jarvis.tools import execute_tool

    result = execute_tool("read_file", {"path": "/etc/hostname"})
    assert result["ok"] is False
    assert "outside your home" in result["result"]


def test_read_outside_home_can_be_opted_into(paths, monkeypatch):
    from jarvis.tools import execute_tool

    target = _tmp_file("opt-in.txt", "opted in")
    monkeypatch.setenv("JARVIS_ALLOW_READ_OUTSIDE_HOME", "1")
    result = execute_tool("read_file", {"path": target})
    assert result["ok"] is True
    assert "opted in" in result["result"]


def test_symlink_cannot_hop_into_a_protected_folder(paths):
    from jarvis.tools import execute_tool

    secret = paths.home / ".ssh" / "id_rsa"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("PRIVATE", encoding="utf-8")
    link = paths.home / "innocent.txt"
    os.symlink(secret, link)

    result = execute_tool("read_file", {"path": str(link)})
    assert result["ok"] is False
    assert "PRIVATE" not in result["result"]


def test_jarvis_own_config_and_token_are_unreadable(paths):
    from jarvis import config
    from jarvis.tools import execute_tool

    config.save_config({"llm": {"api_key": "sk-must-not-be-read"}})
    for target in (paths.config, paths.token):
        result = execute_tool("read_file", {"path": str(target)})
        assert result["ok"] is False, f"{target.name} was readable by the agent"
        assert "sk-must-not-be-read" not in result["result"]


def test_writes_outside_home_are_refused(paths):
    from jarvis.tools import execute_tool

    result = execute_tool("write_file", {"path": "/etc/jarvis-test.conf", "content": "x"})
    assert result["ok"] is False
    assert "outside your home" in result["result"]


def test_writes_inside_home_work(paths):
    from jarvis.tools import execute_tool

    target = paths.home / "todo.txt"
    assert execute_tool("write_file", {"path": str(target), "content": "milk"})["ok"] is True
    assert target.read_text(encoding="utf-8") == "milk"


def test_writing_a_shell_rc_file_is_allowed_but_config_is_not(paths):
    """Persistence via ~/.ssh is blocked; ordinary files are not."""
    from jarvis.tools import execute_tool

    assert execute_tool("write_file", {"path": str(paths.home / ".bashrc"), "content": "x"})["ok"]
    denied = execute_tool("write_file", {"path": str(paths.config), "content": "{}"})
    assert denied["ok"] is False and "credentials" in denied["result"]


def test_listing_a_protected_folder_is_refused(paths):
    from jarvis.tools import execute_tool

    (paths.home / ".ssh").mkdir(parents=True, exist_ok=True)
    (paths.home / ".ssh" / "id_rsa").write_text("PRIVATE", encoding="utf-8")
    result = execute_tool("list_files", {"path": str(paths.home / ".ssh")})
    assert result["ok"] is False
    assert "id_rsa" not in result["result"]


def test_search_never_returns_credential_paths(paths):
    from jarvis.tools import execute_tool

    secret = paths.home / ".ssh" / "id_rsa"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("PRIVATE", encoding="utf-8")
    (paths.home / "id_rsa.txt").write_text("public notes", encoding="utf-8")

    result = execute_tool("search_files", {"query": "id_rsa"})
    assert result["ok"] is True
    assert ".ssh" not in result["result"]


def test_large_files_are_read_in_bounded_chunks(paths):
    from jarvis.tools import MAX_FILE_READ, execute_tool

    target = paths.home / "big.txt"
    target.write_text("A" * 2_000_000, encoding="utf-8")
    result = execute_tool("read_file", {"path": str(target)})
    # capped, and it says so rather than pretending that was the whole file
    assert len(result["result"]) < MAX_FILE_READ + 200
    assert "truncated" in result["result"]


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,secret",
    [
        ("key=sk-abcdefghijklmnopqrstuvwxyz", "abcdefghijklmnopqrstuvwxyz"),
        ("AKIAIOSFODNN7EXAMPLE", "IOSFODNN7EXAMPLE"),
        ("ghp_abcdefghijklmnopqrstuvwxyz0123456789", "ghp_abcdefghijklmnopqrstuvwxyz0123456789"),
        # Built by concatenation on purpose: a literal token-shaped string in a
        # test file trips GitHub push protection, which cannot know it is fake.
        ("xox" + "b-FAKE0000-zzzzzzzzzzzzzzzz", "zzzzzzzzzzzzzzzz"),
        ("postgres://admin:hunter2secret@db/app", "hunter2secret"),
        ("AIzaSyA1234567890abcdefghijklmnop", "1234567890abcdefghijklmnop"),
        ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz", "abcdefghijklmnopqrstuvwxyz"),
    ],
)
def test_secrets_are_redacted(raw, secret):
    from jarvis.safety import redact

    assert secret not in redact(raw)
    assert "redacted" in redact(raw).lower()


def test_pem_blocks_are_redacted():
    from jarvis.safety import redact

    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"
    out = redact(pem)
    assert "MIIEow" not in out
    assert "BEGIN RSA PRIVATE KEY" in out  # the shape stays recognisable


def test_ordinary_text_is_untouched():
    from jarvis.safety import redact

    text = "Build finished in 4.2s; 12 tests passed on port 8000."
    assert redact(text) == text


def test_tool_output_is_redacted_before_reaching_the_model(paths):
    """The dispatcher, not just the helper, must scrub output."""
    from jarvis.tools import execute_tool

    result = execute_tool("run_command", {"command": "echo key=sk-live-abcdefghijklmnopqrstu"})
    assert "abcdefghijklmnopqrstu" not in result["result"]
    assert "redacted" in result["result"]


# ---------------------------------------------------------------------------
# Coordinate mapping (#8)
# ---------------------------------------------------------------------------

@pytest.fixture()
def fake_screen(monkeypatch, paths):
    """Pretend the display is 2560x1440 and record clicks."""
    from jarvis import tools

    state = {"clicked": None, "pressed": None}

    class Shot:
        size = (2560, 1440)
        rgb = bytes(2560 * 1440 * 3)

    class Sct:
        monitors = [{}, {"width": 2560, "height": 1440}]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def grab(self, monitor):
            return Shot()

    class Pg:
        FAILSAFE = True

        def click(self, x, y, button="left"):
            state["clicked"] = (x, y, button)

        def press(self, key):
            state["pressed"] = [key]

        def hotkey(self, *keys):
            state["pressed"] = list(keys)

    class Mss:
        @staticmethod
        def mss():
            return Sct()

    monkeypatch.setitem(__import__("sys").modules, "mss", Mss)
    monkeypatch.setattr(tools, "_pyautogui", lambda: Pg())
    return state


def test_screenshot_reports_downscale(fake_screen):
    from jarvis.tools import MAX_IMAGE_WIDTH, execute_tool

    result = execute_tool("see_screen", {})
    assert result["image_width"] == MAX_IMAGE_WIDTH
    assert result["screen_width"] == 2560
    assert "scaled" in result["result"]


def test_click_maps_image_coordinates_back_to_the_screen(fake_screen):
    from jarvis.tools import execute_tool

    execute_tool("see_screen", {})
    execute_tool("click", {"x": 640, "y": 360})
    assert fake_screen["clicked"] == (1280, 720, "left")


def test_click_refuses_points_off_screen(fake_screen):
    from jarvis.tools import execute_tool

    execute_tool("see_screen", {})
    result = execute_tool("click", {"x": 5000, "y": 5000})
    assert result["ok"] is False
    assert fake_screen["clicked"] is None


def test_click_rejects_negative_coordinates(fake_screen):
    from jarvis.tools import execute_tool

    assert execute_tool("click", {"x": -5, "y": 10})["ok"] is False
    assert fake_screen["clicked"] is None


def test_click_rejects_non_numeric_coordinates(fake_screen):
    from jarvis.tools import execute_tool

    result = execute_tool("click", {"x": "middle", "y": 10})
    assert result["ok"] is False


# ---------------------------------------------------------------------------
# Dispatcher robustness
# ---------------------------------------------------------------------------

def test_unknown_tool_is_an_error_not_a_crash():
    from jarvis.tools import execute_tool

    result = execute_tool("delete_the_internet", {})
    assert result["ok"] is False
    assert "unknown tool" in result["result"]


@pytest.mark.parametrize("name,args", [
    ("read_file", {}),
    ("write_file", {"path": "/tmp/x"}),
    ("run_command", {}),
    ("click", {}),
    ("list_files", {"path": 12345}),
])
def test_bad_arguments_never_raise(name, args):
    from jarvis.tools import execute_tool

    result = execute_tool(name, args)
    assert isinstance(result, dict) and "result" in result


def test_press_keys_tolerates_a_string(fake_screen):
    """Models often send "enter" instead of ["enter"].

    A bare string used to be iterated into five separate key presses.
    """
    from jarvis.tools import press_keys

    result = press_keys("enter")
    assert result["ok"] is True
    assert fake_screen["pressed"] == ["enter"]


def test_press_keys_handles_a_chord(fake_screen):
    from jarvis.tools import press_keys

    assert press_keys(["ctrl", "s"])["ok"] is True
    assert fake_screen["pressed"] == ["ctrl", "s"]


def test_press_keys_rejects_an_empty_list(fake_screen):
    from jarvis.tools import press_keys

    result = press_keys([])
    assert result["ok"] is False
    assert "no keys" in result["result"]


def _tmp_file(name: str, content: str) -> str:
    import tempfile

    path = os.path.join(tempfile.mkdtemp(prefix="jarvis-optin-"), name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    return path
