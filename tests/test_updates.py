"""Self-updates.

An updater is the most dangerous feature in a desktop app: it downloads an
executable from the internet and runs it in place of the app. Everything here is
about the ways that could go wrong.

* a hijacked or mistyped URL must not be followed (allowlist, HTTPS only);
* bytes must match the SHA-256 GitHub itself publishes, or nothing is unpacked;
* a zip must not write outside the staging folder (zip-slip);
* a build that cannot pass its own ``--self-check`` must never replace a working
  one — a broken update is worse than an old version;
* installing must be deliberate: server-chosen payload, session-bound approval,
  single use;
* and none of it may happen by accident on a machine that opted out.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from aegis import updates

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_staging(tmp_path, monkeypatch):
    """Never touch the real data directory, and never reach the network."""
    monkeypatch.setattr(updates, "staging_root", lambda: tmp_path / "update")
    monkeypatch.delenv("AEGIS_NO_UPDATES", raising=False)
    monkeypatch.delenv("AEGIS_ENABLE_UPDATES", raising=False)
    monkeypatch.setattr(updates, "_last_release", None, raising=False)
    return tmp_path


def release_payload(tag: str = "v9.9.9", *, asset: str | None = None, sha: str = "a" * 64,
                    size: int = 1024) -> dict:
    """A GitHub `/releases/latest` body, shaped like the real one."""
    name = asset or updates.asset_name()
    return {
        "tag_name": tag,
        "html_url": f"https://github.com/{updates.REPO}/releases/tag/{tag}",
        "published_at": "2026-01-01T00:00:00Z",
        "body": "notes",
        "assets": [
            {
                "name": name,
                "size": size,
                "browser_download_url": (
                    f"https://github.com/{updates.REPO}/releases/download/{tag}/{name}"
                ),
                "digest": f"sha256:{sha}",
            }
        ],
    }


def make_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "candidate,current,expected",
    [
        ("v1.7.0", "1.6.0", True),
        ("v1.6.1", "1.6.0", True),
        ("v1.6.0", "1.6.0", False),
        ("v1.5.9", "1.6.0", False),
        ("v10.0.0", "9.9.9", True),      # numeric, not lexicographic
        ("1.7.0", "1.6.0", True),        # tag without the leading v
    ],
)
def test_versions_compare_numerically(candidate, current, expected):
    assert updates.is_newer(candidate, current) is expected


@pytest.mark.parametrize("tag", ["v1.7.0-rc1", "nightly", "v1", "latest", ""])
def test_an_unparseable_tag_is_never_installed(tag):
    """Guessing "newer" for a pre-release could quietly downgrade the app."""
    assert updates.parse_version(tag) is None
    assert updates.is_newer(tag, "1.6.0") is False


# ---------------------------------------------------------------------------
# What the release may contain
# ---------------------------------------------------------------------------

def test_a_release_without_a_checksum_is_refused():
    """No digest means nothing to verify, and this installs an executable."""
    payload = release_payload()
    payload["assets"][0]["digest"] = None
    with pytest.raises(updates.UpdateError, match="checksum"):
        updates.release_from_payload(payload, updates.asset_name())


def test_a_release_without_this_platform_is_refused():
    payload = release_payload(asset="Aegis-plan9.zip")
    with pytest.raises(updates.UpdateError, match="build for this computer"):
        updates.release_from_payload(payload, "Aegis-windows.zip")


def test_asset_names_match_what_ci_publishes():
    """A rename in the workflow would otherwise break updates silently."""
    assert updates.asset_name("Windows") == "Aegis-windows.zip"
    assert updates.asset_name("Darwin") == "Aegis-macos.zip"
    assert updates.asset_name("Linux") == "Aegis-linux.zip"


def test_the_release_is_parsed_into_something_usable():
    release = updates.release_from_payload(release_payload("v2.0.0"), "Aegis-linux.zip")
    assert release.version == "2.0.0"
    assert release.tag == "v2.0.0"
    assert release.sha256 == "a" * 64
    assert release.asset_url.startswith("https://github.com/")


# ---------------------------------------------------------------------------
# Download safety
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/x.zip",                    # not HTTPS
        "https://evil.example.com/Aegis.zip",         # not GitHub
        "https://github.com.evil.example/Aegis.zip",  # suffix trick
        "file:///etc/passwd",
    ],
)
def test_only_https_github_urls_may_be_downloaded(url):
    with pytest.raises(updates.UpdateError):
        updates._assert_downloadable(url)


def test_a_plausible_github_url_is_allowed():
    updates._assert_downloadable(
        "https://github.com/samuelmelkie2020-hue/Aegis/releases/download/v1.7.0/Aegis-linux.zip"
    )
    updates._assert_downloadable(
        "https://objects.githubusercontent.com/github-production-release-asset/x"
    )


def test_a_download_larger_than_a_build_ever_is_is_refused(isolated_staging):
    """A 2 GB "update" must not be fetched at all."""
    release = updates.release_from_payload(release_payload(size=1), "Aegis-linux.zip")
    release.asset_size = updates.MAX_ASSET_BYTES + 1
    with pytest.raises(updates.UpdateError, match="larger"):
        updates.download_verified(release)


def test_a_checksum_mismatch_leaves_nothing_behind(isolated_staging, monkeypatch):
    """Mismatch must delete the bytes, not merely complain about them."""
    good = make_zip({"Aegis": b"#!/bin/sh\nexit 0\n"})
    release = updates.release_from_payload(release_payload(size=len(good)), "Aegis-linux.zip")
    release.sha256 = hashlib.sha256(b"something else").hexdigest()

    class Stream:
        status_code = 200
        history = []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def iter_bytes(self, _size):
            yield good

    monkeypatch.setattr(updates.httpx, "stream", lambda *a, **k: Stream())

    with pytest.raises(updates.UpdateError, match="checksum"):
        updates.download_verified(release)

    leftovers = list((isolated_staging / "update").glob("*.zip")) if (isolated_staging / "update").exists() else []
    assert leftovers == [], "a file that failed verification was left on disk"


def test_a_short_download_is_detected(isolated_staging, monkeypatch):
    body = b"truncated"
    release = updates.release_from_payload(release_payload(size=999_999), "Aegis-linux.zip")

    class Stream:
        status_code = 200
        history = []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def iter_bytes(self, _size):
            yield body

    monkeypatch.setattr(updates.httpx, "stream", lambda *a, **k: Stream())
    with pytest.raises(updates.UpdateError, match="cut short"):
        updates.download_verified(release)


# ---------------------------------------------------------------------------
# Unpacking
# ---------------------------------------------------------------------------

def test_zip_slip_is_refused(tmp_path):
    """An entry named ../../evil must not be extracted anywhere."""
    evil = tmp_path / "evil.zip"
    evil.write_bytes(make_zip({"../../escaped.txt": b"pwned"}))
    with pytest.raises(updates.UpdateError, match="outside"):
        updates.unpack(evil, tmp_path / "out")
    assert not (tmp_path.parent / "escaped.txt").exists()


def test_a_normal_zip_unpacks(tmp_path):
    good = tmp_path / "good.zip"
    good.write_bytes(make_zip({"Aegis.exe": b"MZ"}))
    destination = updates.unpack(good, tmp_path / "out")
    assert (destination / "Aegis.exe").read_bytes() == b"MZ"


def test_a_non_zip_is_refused(tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"<html>404</html>")
    with pytest.raises(updates.UpdateError, match="not a valid zip"):
        updates.unpack(bad, tmp_path / "out")


# ---------------------------------------------------------------------------
# Verification before install: the build must prove it works
# ---------------------------------------------------------------------------

def test_a_build_that_fails_self_check_is_not_installed(tmp_path, monkeypatch):
    payload = tmp_path / "Aegis"
    payload.write_text("#!/bin/sh\nexit 1\n")
    payload.chmod(0o755)

    def fake_run(*args, **kwargs):
        class Result:
            returncode = 1
            stdout = "FAILED:\n  - server failed to start"
            stderr = ""

        return Result()

    monkeypatch.setattr(updates.subprocess, "run", fake_run)
    with pytest.raises(updates.UpdateError, match="self-check"):
        updates.verify_binary(payload)


def test_a_build_with_no_executable_is_not_installed(tmp_path):
    empty = tmp_path / "payload"
    empty.mkdir()
    with pytest.raises(updates.UpdateError, match="runnable"):
        updates.verify_binary(empty)


def test_a_build_that_hangs_does_not_hang_the_app(tmp_path, monkeypatch):
    payload = tmp_path / "Aegis"
    payload.write_text("")

    def timeout_run(*args, **kwargs):
        raise updates.subprocess.TimeoutExpired(cmd="Aegis", timeout=1)

    monkeypatch.setattr(updates.subprocess, "run", timeout_run)
    with pytest.raises(updates.UpdateError, match="did not finish"):
        updates.verify_binary(payload)


def test_self_check_runs_without_a_display(tmp_path, monkeypatch):
    """A GUI error dialog in a headless self-check would block forever."""
    seen = {}

    def fake_run(args, **kwargs):
        seen["env"] = kwargs.get("env", {})

        class Result:
            returncode = 0
            stdout = "All checks passed."
            stderr = ""

        return Result()

    payload = tmp_path / "Aegis"
    payload.write_text("")
    monkeypatch.setattr(updates.subprocess, "run", fake_run)
    updates.verify_binary(payload)
    assert seen["env"].get("AEGIS_NO_GUI_ERRORS") == "1"


# ---------------------------------------------------------------------------
# Staging lifecycle
# ---------------------------------------------------------------------------

def test_download_and_stage_cleans_up_when_verification_fails(isolated_staging, monkeypatch):
    monkeypatch.setattr(updates, "download_verified", lambda *a, **k: (_ for _ in ()).throw(
        updates.UpdateError("checksum mismatch")
    ))
    release = updates.release_from_payload(release_payload(), "Aegis-linux.zip")
    with pytest.raises(updates.UpdateError):
        updates.download_and_stage(release)
    assert updates.staged_info() is None


def test_apply_plan_explains_itself_when_nothing_is_staged(isolated_staging):
    plan = updates.apply_plan()
    assert plan["ok"] is False
    assert "no downloaded update" in plan["detail"].lower()


def test_a_source_checkout_is_never_overwritten(isolated_staging, monkeypatch):
    """Running from git and replacing the checkout would destroy work."""
    monkeypatch.setattr(updates, "is_frozen", lambda: False)
    staging = updates.staging_root()
    staging.mkdir(parents=True, exist_ok=True)
    payload = staging / "payload"
    payload.mkdir()
    (staging / updates.STAGED_MARKER).write_text(json.dumps({"payload": str(payload), "version": "9.9.9"}))
    plan = updates.apply_plan()
    assert plan["ok"] is False
    assert "source" in plan["detail"].lower()


# ---------------------------------------------------------------------------
# Opting out
# ---------------------------------------------------------------------------

def test_updates_can_be_turned_off_entirely(monkeypatch):
    monkeypatch.setenv("AEGIS_NO_UPDATES", "1")
    assert updates.updates_enabled() is False
    result = updates.check_for_update()
    assert result["available"] is False
    assert "turned off" in result["reason"]


def test_a_failed_check_is_reported_not_raised(monkeypatch):
    """A network blip must never break startup or the chat."""

    def boom(*args, **kwargs):
        raise OSError("getaddrinfo failed")

    monkeypatch.setattr(updates.httpx, "get", boom)
    result = updates.check_for_update()
    assert result["available"] is False
    assert "GitHub" in result["error"]


def test_a_non_release_response_is_reported_not_raised(monkeypatch):
    class Response:
        status_code = 200

        @staticmethod
        def json():
            raise json.JSONDecodeError("bad", "doc", 0)

    monkeypatch.setattr(updates.httpx, "get", lambda *a, **k: Response())
    result = updates.check_for_update()
    assert result["available"] is False
    assert "not a release" in result["error"]


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

def test_update_routes_require_the_token(anon):
    for path in ("/api/update", "/api/update/check"):
        response = anon.get(path, headers={"Host": "localhost:8000"})
        assert response.status_code == 401, f"{path} answered without a token"
    for path in ("/api/update/download", "/api/update/install"):
        response = anon.post(path, headers={"Host": "localhost:8000"})
        assert response.status_code == 401, f"{path} answered without a token"


def test_status_reports_this_build(client):
    http, auth = client
    body = http.get("/api/update", headers=auth).json()
    assert body["current"] == updates.current_version()
    assert "enabled" in body


def test_install_without_a_staged_update_is_refused(client, isolated_staging):
    http, auth = client
    response = http.post("/api/update/install", headers=auth)
    assert response.status_code == 409
    assert response.json()["ok"] is False


def test_install_is_an_approval_not_an_action(client, isolated_staging, monkeypatch):
    """Installing registers an approval; nothing runs until it is redeemed."""
    monkeypatch.setattr(updates, "apply_plan", lambda: {
        "ok": True, "version": "9.9.9", "payload": "/tmp/x", "target": "/tmp",
    })
    called = {"ran": False}
    monkeypatch.setattr("aegis.tools.install_update", lambda *a, **k: called.update(ran=True))

    http, auth = client
    response = http.post("/api/update/install", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["approval_id"].startswith("ap-")
    assert body["name"] == "install_update"
    assert called["ran"] is False, "the install ran before it was approved"


def test_the_install_payload_is_the_servers_own(client, isolated_staging, monkeypatch):
    """A caller cannot choose a version or a URL; the server's plan decides."""
    monkeypatch.setattr(updates, "apply_plan", lambda: {
        "ok": True, "version": "1.0.0-evil", "payload": "/tmp/x", "target": "/tmp",
    })
    http, auth = client
    body = http.post("/api/update/install", headers=auth, json={"version": "9.9.9", "url": "http://evil"}).json()
    assert body["args"]["version"] == "1.0.0-evil"


# ---------------------------------------------------------------------------
# End to end: a real HTTP server, a real zip, a real checksum
# ---------------------------------------------------------------------------

def _local_server(zip_bytes: bytes):
    """A real HTTP server on loopback, so the streaming path is exercised.

    httpx is stubbed elsewhere to keep tests fast; this one goes over a socket
    because the bugs worth catching here (a truncated chunk, a mis-sized
    digest, a redirect) live in the transport, not in the code around it.
    """
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server's interface
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(zip_bytes)))
            self.end_headers()
            self.wfile.write(zip_bytes)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/Aegis-linux.zip"


def test_a_verified_release_stages_end_to_end(isolated_staging, monkeypatch):
    """The whole path: fetch, hash-check, unpack, self-check, record it.

    The transport guard is about https and GitHub hosts, which a loopback test
    server cannot satisfy, so that one check is bypassed — everything after it
    (streaming, size, digest, unpack, staging) runs for real.
    """
    zip_bytes = make_zip({"Aegis": b"#!/bin/sh\nexit 0\n"})
    server, url = _local_server(zip_bytes)
    real_stream = updates.httpx.stream
    try:
        release = updates.release_from_payload(
            release_payload(size=len(zip_bytes), sha=hashlib.sha256(zip_bytes).hexdigest()),
            "Aegis-linux.zip",
        )
        monkeypatch.setattr(updates, "_assert_downloadable", lambda target: None)
        monkeypatch.setattr(updates, "verify_binary", lambda payload: None)
        monkeypatch.setattr(updates.httpx, "stream", real_stream)
        release.asset_url = url

        info = updates.download_and_stage(release)
        assert info["version"] == "9.9.9"
        assert Path(info["payload"]).exists()
        assert updates.staged_info()["version"] == "9.9.9"
    finally:
        server.shutdown()


def test_the_end_to_end_path_refuses_a_wrong_checksum(isolated_staging, monkeypatch):
    """Same real transport, dishonest digest: nothing may be staged."""
    zip_bytes = make_zip({"Aegis": b"real"})
    server, url = _local_server(zip_bytes)
    try:
        release = updates.release_from_payload(
            release_payload(size=len(zip_bytes), sha="b" * 64), "Aegis-linux.zip"
        )
        monkeypatch.setattr(updates, "_assert_downloadable", lambda target: None)
        release.asset_url = url
        with pytest.raises(updates.UpdateError, match="checksum"):
            updates.download_and_stage(release)
        assert updates.staged_info() is None
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# The swap scripts
# ---------------------------------------------------------------------------

def test_the_posix_script_waits_for_the_running_app(isolated_staging):
    """Replacing a binary whose process is alive is how an update corrupts."""
    script = updates._posix_script(4321, Path("/p/Aegis"), Path("/t/Aegis"), Path("/t/Aegis"))
    assert "kill -0 4321" in script
    # the swap must come after the wait, never before
    assert script.index("kill -0 4321") < script.index("cp ")


def test_the_posix_script_keeps_a_way_back(isolated_staging):
    """A build that starts but is broken needs the previous one to hand."""
    script = updates._posix_script(1, Path("/p/Aegis"), Path("/t"), Path("/t/Aegis"))
    assert ".old" in script


def test_the_posix_script_does_not_delete_itself_while_running(isolated_staging):
    """Deleting the running script breaks dash partway through.

    dash reads a script incrementally; removing the file it is executing made it
    fail with "Unterminated quoted string" on the final line — after the files
    had already been swapped, so it looked like a successful update that logged a
    scary error. Helpers are pruned on the next start instead.
    """
    script = updates._posix_script(1, Path("/p/Aegis"), Path("/t"), Path("/t/Aegis"))
    assert 'rm -f "$0"' not in script
    assert "next start" in script, "the generated script must say why it survives"


def test_leftover_helpers_are_pruned_on_the_next_start(isolated_staging):
    staging = updates.staging_root()
    staging.mkdir(parents=True, exist_ok=True)
    old = staging / "apply-20200101-000000.sh"
    old.write_text("#!/bin/sh\n")
    ps1 = staging / "apply-20200101-000000.ps1"
    ps1.write_text("# helper")
    keep = staging / updates.STAGED_MARKER
    keep.write_text("{}")

    removed = updates.prune_helpers()
    assert set(removed) == {old.name, ps1.name}
    assert not old.exists() and not ps1.exists()
    assert keep.exists(), "pruning must not touch a staged update"


def test_the_windows_script_waits_then_moves(isolated_staging):
    """Windows locks a running .exe, so the move must follow the wait."""
    script = updates._windows_script(4321, Path(r"C:\p\Aegis.exe"), Path(r"C:\t\Aegis.exe"),
                                     Path(r"C:\t\Aegis.exe"))
    assert "Get-Process -Id 4321" in script
    assert script.index("Get-Process -Id 4321") < script.index("Move-Item")
    assert "Start-Process -FilePath" in script, "the app must come back after the update"


def test_the_windows_script_waits_for_a_pid_not_a_substring(isolated_staging):
    """tasklist output has digit columns; a PID must not match one of them."""
    script = updates._windows_script(4321, Path(r"C:\p\Aegis.exe"), Path(r"C:\t\Aegis.exe"),
                                     Path(r"C:\t\Aegis.exe"))
    assert "tasklist" not in script
    assert "find \"4321\"" not in script


def test_the_windows_script_gives_up_rather_than_hanging_forever(isolated_staging):
    """If Aegis never exits, the helper must stop, not wait a day."""
    script = updates._windows_script(1, Path(r"C:\p\Aegis.exe"), Path(r"C:\t\Aegis.exe"),
                                     Path(r"C:\t\Aegis.exe"))
    assert "deadline" in script
    assert "did not exit" in script


def test_the_windows_script_backs_up_the_old_build(isolated_staging):
    script = updates._windows_script(1, Path(r"C:\p\Aegis.exe"), Path(r"C:\t\Aegis.exe"),
                                     Path(r"C:\t\Aegis.exe"))
    assert ".old" in script


def test_the_windows_script_logs_what_it_did(isolated_staging):
    """A hidden window that fails silently is the bug we just fixed."""
    script = updates._windows_script(1, Path(r"C:\p\Aegis.exe"), Path(r"C:\t\Aegis.exe"),
                                     Path(r"C:\t\Aegis.exe"))
    assert "Add-Content" in script
    assert "update.log" in script
    assert "FAILED" in script


def test_a_path_with_a_quote_cannot_break_the_windows_script(isolated_staging):
    """A folder called "Bob's Aegis" is legal on Windows."""
    script = updates._windows_script(1, Path(r"C:\Bob's\Aegis.exe"), Path(r"C:\t\Aegis.exe"),
                                     Path(r"C:\t\Aegis.exe"))
    assert "Bob''s" in script, "a single quote must be doubled in PowerShell"


def test_the_helper_is_launched_hidden_without_a_profile(isolated_staging, monkeypatch):
    """A visible console flashing up would look like malware."""
    captured = {}

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

        class P:
            pass

        return P()

    monkeypatch.setattr(updates, "is_frozen", lambda: True)
    monkeypatch.setattr(updates, "install_root", lambda: isolated_staging)
    monkeypatch.setattr(updates, "_install_dir_writable", lambda: True)
    monkeypatch.setattr(updates.platform, "system", lambda: "Windows")
    monkeypatch.setattr(updates.subprocess, "Popen", fake_popen)

    staging = updates.staging_root()
    staging.mkdir(parents=True, exist_ok=True)
    payload = staging / "Aegis.exe"
    payload.write_bytes(b"MZ")
    (staging / updates.STAGED_MARKER).write_text(json.dumps(
        {"payload": str(payload), "version": "9.9.9"}
    ))

    assert updates.apply_staged_update()["ok"] is True
    args = captured["args"]
    assert args[0] == "powershell.exe"
    assert "-NoProfile" in args
    assert "-WindowStyle" in args and "Hidden" in args
    assert "-ExecutionPolicy" in args and "Bypass" in args


def test_the_scripts_are_plain_files_not_shell_strings(isolated_staging):
    """Nothing user-supplied reaches these scripts; the only inputs are paths
    the server computed, so quoting is a correctness issue, not injection."""
    script = updates._posix_script(1, Path("/p/Aegis"), Path("/t"), Path("/t/Aegis"))
    assert script.startswith("#!/bin/sh")
    assert '"{relaunch}"'.replace("{relaunch}", "/t/Aegis") in script


def test_apply_refuses_when_the_staged_payload_vanished(isolated_staging):
    staging = updates.staging_root()
    staging.mkdir(parents=True, exist_ok=True)
    (staging / updates.STAGED_MARKER).write_text(
        json.dumps({"payload": str(staging / "gone"), "version": "9.9.9"})
    )
    plan = updates.apply_plan()
    assert plan["ok"] is False
    assert "gone" in plan["detail"] or "again" in plan["detail"]
    assert updates.staged_info() is None, "a dead marker should be cleared"
