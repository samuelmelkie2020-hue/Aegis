"""Update Aegis in place, without downloading a zip again.

The app ships as a zip from a GitHub release. Asking someone to notice a new
version, download 27 MB, unzip it, and replace a running executable by hand is
how a desktop app ends up years out of date — so this does the whole thing:

1. **check** — read ``/releases/latest`` and compare its tag with our own;
2. **download** — stream the platform asset, and *verify* it before trusting it;
3. **stage** — unpack next to the settings, never over the running install;
4. **swap on restart** — a tiny helper waits for this process to exit (Windows
   locks a running ``.exe``) and only then replaces the binary and reopens it.

Two properties matter more than the feature itself.

**Only genuine Aegis releases are acceptable.** The URL comes from GitHub's API,
so it is checked against an allowlist of GitHub hosts, refused unless HTTPS, and
the downloaded bytes must match the SHA-256 *GitHub itself reports* for the
asset. A zip that does not match is deleted, not unpacked. A staged build is
also required to run its own ``--self-check`` before it may be installed, so a
truncated or blocked download can never replace a working app.

**Nothing happens by surprise.** Checking is read-only. Downloading and
installing are separate, explicit steps, and installing never happens mid-answer:
the swap only runs when the user restarts.

Set ``AEGIS_NO_UPDATES=1`` to turn the whole thing off, including the check.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from . import __version__

REPO = "samuelmelkie2020-hue/Aegis"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"

# Only these hosts may serve an update. The API decides the URL, but the URL is
# attacker-controlled the moment an account is compromised, so it is re-checked
# here rather than trusted: this download replaces an executable.
ALLOWED_DOWNLOAD_HOSTS = (
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "api.github.com",
)

MAX_ASSET_BYTES = 250 * 1024 * 1024   # a 250 MB ceiling; real assets are ~30 MB
CHECK_TIMEOUT = 10.0
DOWNLOAD_TIMEOUT = 60.0
STAGED_MARKER = "staged.json"
SELF_CHECK_TIMEOUT = 90.0             # a PyInstaller exe is slow to boot

# One check at a time: two concurrent downloads into the same staging directory
# would interleave and could install a half-written build.
_lock = threading.Lock()

# The release the last check found. Kept so /api/update/download can use the
# server's own copy of the URL and digest: rebuilding one from a summary dict
# (or, worse, accepting one from the client) is how a caller gets to choose what
# gets installed.
_last_release: Release | None = None

# Set by the desktop shell so an install can close the window first. Without a
# window (`--web`) the helper still waits for the process and relaunches.
_restart_hook: Callable[[], None] | None = None


def _ca_bundles() -> list[str | bool]:
    """Certificate stores to try, in order, best first.

    httpx verifies against ``certifi``, a snapshot bundled with the wheel. On a
    fresh Windows or macOS install that is almost always right; on a machine
    behind a TLS-inspecting proxy (a corporate antivirus, some VPNs) it is not,
    and the failure reads as "could not check for updates", which is useless.

    So both candidates are returned and the caller retries on a *certificate*
    error only: certifi first, then whatever the OS trusts. Verification is
    never turned off — this path downloads and runs an executable.
    """
    candidates: list[str | bool] = []
    try:
        import certifi

        path = certifi.where()
        if path and os.path.isfile(path):
            candidates.append(path)
    except Exception:  # pragma: no cover - certifi ships with httpx
        pass

    for candidate in (
        os.environ.get("SSL_CERT_FILE"),
        os.environ.get("REQUESTS_CA_BUNDLE"),
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/pki/tls/certs/ca-bundle.crt",
        "/usr/local/etc/openssl/cert.pem",
    ):
        if candidate and os.path.isfile(candidate) and candidate not in candidates:
            candidates.append(candidate)

    # `True` = let the ssl module use its own default (the OS store on Windows).
    candidates.append(True)
    return candidates


def _is_certificate_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "certificate" in text or "ssl" in text


class UpdateError(RuntimeError):
    """Something about an update is unacceptable. Always shown to the user."""


# ---------------------------------------------------------------------------
# What this build is
# ---------------------------------------------------------------------------

def updates_enabled() -> bool:
    if os.environ.get("AEGIS_NO_UPDATES"):
        return False
    return os.environ.get("AEGIS_ENABLE_UPDATES", "1") != "0"


def is_frozen() -> bool:
    """True in the packaged app. Updates replace a binary, so this must be."""
    return bool(getattr(sys, "frozen", False))


def current_version() -> str:
    return __version__


def asset_name(system: str | None = None) -> str:
    """The release asset for this platform. Names match the CI artifact names."""
    system = (system or platform.system()).lower()
    if system.startswith("win"):
        return "Aegis-windows.zip"
    if system == "darwin":
        return "Aegis-macos.zip"
    return "Aegis-linux.zip"


def install_root() -> Path:
    """The directory holding the running build (the exe's folder).

    For a macOS ``.app`` this walks up to the bundle itself, because replacing a
    bundle means replacing that whole directory, not the binary inside it.
    """
    exe = Path(sys.executable).resolve()
    if platform.system() == "Darwin":
        for parent in exe.parents:
            if parent.suffix == ".app":
                return parent
        return exe.parent
    return exe.parent


def staging_root() -> Path:
    """Where downloads are unpacked — never inside the install or the checkout.

    In a source checkout ``user_data_dir()`` is the repository root, and dropping
    a 30 MB download plus an unpacked build into a git working tree is how people
    end up committing a binary. A packaged app uses its data folder.
    """
    from .config import user_data_dir

    if is_frozen():
        return user_data_dir() / "update"
    return Path(tempfile.gettempdir()) / "aegis-update"


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

def parse_version(text: str) -> tuple[int, int, int] | None:
    """``v1.6.0`` → ``(1, 6, 0)``. Anything else is not comparable, so ``None``.

    Deliberately strict: a tag like ``v1.6.0-rc1`` or ``nightly`` must not be
    installed automatically, because "newer than what I have" is not knowable
    for those and a wrong guess would quietly downgrade the app.
    """
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", (text or "").strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def is_newer(candidate: str, current: str) -> bool:
    theirs, ours = parse_version(candidate), parse_version(current)
    if theirs is None or ours is None:
        return False
    return theirs > ours


@dataclass
class Release:
    version: str                 # "1.7.0", already stripped of the tag's "v"
    tag: str                     # "v1.7.0"
    asset_url: str
    asset_name: str
    asset_size: int
    sha256: str                  # from GitHub's asset digest, lower-case hex
    page_url: str
    notes: str = ""
    published: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "tag": self.tag,
            "asset": self.asset_name,
            "size": self.asset_size,
            "sha256": self.sha256,
            "url": self.page_url,
            "notes": self.notes[:4000],
            "published": self.published,
        }


def _digest_of(asset: dict[str, Any]) -> str:
    """GitHub reports ``digest: "sha256:<hex>"`` (or null on some releases).

    Some releases carry ``null``; when that happens the asset has to be
    downloaded to be checked, which is exactly what we refuse to do for an
    executable. Newer GitHub releases always publish it.
    """
    digest = (asset.get("digest") or "").strip().lower()
    if digest.startswith("sha256:"):
        return digest.split(":", 1)[1]
    return ""


def choose_asset(payload: dict[str, Any], wanted: str) -> dict[str, Any] | None:
    for asset in payload.get("assets") or []:
        if asset.get("name") == wanted and asset.get("browser_download_url"):
            return asset
    return None


def last_release() -> Release | None:
    """The release found by the most recent check, if any."""
    return _last_release


def release_from_payload(payload: dict[str, Any], wanted: str) -> Release:
    """Turn a GitHub release payload into a :class:`Release`, or refuse it."""
    tag = str(payload.get("tag_name") or "")
    version = tag[1:] if tag.startswith("v") else tag
    if parse_version(version) is None:
        raise UpdateError(f"The newest release ({tag or 'unknown'}) is not a version I can install.")

    asset = choose_asset(payload, wanted)
    if asset is None:
        raise UpdateError(f"The release {tag} has no {wanted} build for this computer.")

    sha = _digest_of(asset)
    if not sha:
        # Refusing is the safe direction: without a published digest there is
        # nothing to check the download against, and this installs an executable.
        raise UpdateError(
            f"The release {tag} does not publish a checksum for {wanted}, so it "
            "cannot be verified. Download it manually if you trust it."
        )

    return Release(
        version=version,
        tag=tag,
        asset_url=str(asset["browser_download_url"]),
        asset_name=wanted,
        asset_size=int(asset.get("size") or 0),
        sha256=sha,
        page_url=str(payload.get("html_url") or ""),
        notes=str(payload.get("body") or ""),
        published=str(payload.get("published_at") or ""),
    )


def check_for_update(*, timeout: float = CHECK_TIMEOUT) -> dict[str, Any]:
    """Read the latest release and say whether it beats this build.

    Read-only: nothing is downloaded, so this is safe to call at startup.
    """
    result: dict[str, Any] = {
        "current": current_version(),
        "updates_enabled": updates_enabled(),
        "checked_at": time.time(),
    }
    if not updates_enabled():
        result.update(available=False, reason="Updates are turned off (AEGIS_NO_UPDATES).")
        return result

    response = None
    failure: Exception | None = None
    for verify in _ca_bundles():
        try:
            response = httpx.get(
                API_LATEST,
                timeout=timeout,
                follow_redirects=True,
                verify=verify,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": f"Aegis/{__version__}",
                },
            )
            break
        except Exception as exc:
            failure = exc
            if not _is_certificate_error(exc):
                break          # a real network problem: retrying a CA will not help
    if response is None:
        result.update(
            available=False, error=_friendly_network_error(failure or Exception("no attempt")),
            reachable=False,
        )
        return result

    if response.status_code != 200:
        result.update(
            available=False,
            error=f"GitHub answered HTTP {response.status_code} — no update information right now.",
        )
        return result

    try:
        payload = response.json()
    except json.JSONDecodeError:
        result.update(available=False, error="GitHub sent something that is not a release.")
        return result

    try:
        release = release_from_payload(payload, asset_name())
    except UpdateError as exc:
        result.update(available=False, error=str(exc))
        return result

    global _last_release
    _last_release = release
    result["latest"] = release.as_dict()
    result["available"] = is_newer(release.version, current_version())
    result["installable"] = is_frozen()
    if not result["available"]:
        result["reason"] = f"You are on the newest version ({current_version()})."
    elif not result["installable"]:
        result["reason"] = (
            "You are running from source, so Aegis will not replace its own files. "
            "Pull the new version with git instead."
        )
    return result


def _friendly_network_error(exc: Exception) -> str:
    text = str(exc)
    if "Name or service not known" in text or "getaddrinfo" in text:
        return "Could not reach GitHub — the network looks offline."
    if "timed out" in text.lower():
        return "GitHub did not answer in time."
    return f"Could not check for updates ({text[:160]})."


# ---------------------------------------------------------------------------
# Downloading and staging
# ---------------------------------------------------------------------------

def _assert_downloadable(url: str) -> None:
    """HTTPS and a known GitHub host, or nothing. The asset becomes an exe."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme != "https":
        raise UpdateError("Refusing to download an update over a connection that is not HTTPS.")
    host = (parts.hostname or "").lower()
    if host not in ALLOWED_DOWNLOAD_HOSTS:
        raise UpdateError(f"Refusing to download an update from {host or 'an unknown host'}.")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_verified(
    release: Release,
    *,
    timeout: float = DOWNLOAD_TIMEOUT,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Fetch the asset, checking its size limit and SHA-256. Returns the zip."""
    _assert_downloadable(release.asset_url)
    if release.asset_size > MAX_ASSET_BYTES:
        raise UpdateError(
            f"That release is {release.asset_size // (1024 * 1024)} MB, which is larger "
            "than an Aegis build should be. Not downloading it."
        )

    staging_root().mkdir(parents=True, exist_ok=True)
    # delete=False: the file is checked, then unpacked, by code that is not in
    # this `with` block, so its lifetime is explicit rather than tied to a scope.
    with tempfile.NamedTemporaryFile(
        prefix="aegis-update-", suffix=".zip", dir=staging_root(), delete=False
    ) as handle:
        target = Path(handle.name)
    digest = hashlib.sha256()
    written = 0

    try:
        # Same certificate-store retry as the check: a TLS-inspecting proxy must
        # not be able to make updates impossible.
        last: Exception | None = None
        for verify in _ca_bundles():
            written, digest = 0, hashlib.sha256()
            try:
                with open(target, "wb") as handle, httpx.stream(
                    "GET",
                    release.asset_url,
                    timeout=timeout,
                    follow_redirects=True,
                    verify=verify,
                    headers={"User-Agent": f"Aegis/{__version__}"},
                ) as response:
                    if response.status_code != 200:
                        raise UpdateError(f"The download failed (HTTP {response.status_code}).")
                    # Redirects land on GitHub's asset CDN; every hop must be GitHub.
                    for hop in response.history:
                        _assert_downloadable(str(hop.next_request.url if hop.next_request else ""))
                    for chunk in response.iter_bytes(1024 * 128):
                        written += len(chunk)
                        if written > MAX_ASSET_BYTES:
                            raise UpdateError("The download exceeded the size limit. Stopped.")
                        digest.update(chunk)
                        handle.write(chunk)
                        if progress:
                            progress(written, release.asset_size)
                last = None
                break
            except Exception as exc:
                last = exc
                if isinstance(exc, UpdateError) or not _is_certificate_error(exc):
                    raise
        if last is not None:
            raise last

        if release.asset_size and written != release.asset_size:
            raise UpdateError(
                f"The download is {written} bytes but the release says {release.asset_size}. "
                "The connection was cut short."
            )
        actual = digest.hexdigest()
        if actual != release.sha256.lower():
            raise UpdateError(
                "The downloaded file does not match the checksum GitHub publishes for it. "
                "It was NOT installed — try again, or download it from the release page."
            )
        return target
    except Exception:
        target.unlink(missing_ok=True)
        raise


def unpack(zip_path: Path, destination: Path) -> Path:
    """Unpack a release zip into ``destination``, refusing paths that escape it.

    Permissions are restored by hand. ``ZipFile.extractall`` ignores the Unix
    mode stored in each entry, so a staged ``Aegis`` comes out non-executable and
    the very next step — its ``--self-check`` — dies with "Permission denied".
    That is a total failure of every Linux and macOS update, and it is invisible
    on Windows, where execution does not depend on the mode bits.
    """
    if not zipfile.is_zipfile(zip_path):
        raise UpdateError("That download is not a valid zip file.")
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            resolved = (destination / member.filename).resolve()
            if not str(resolved).startswith(str(root)):
                raise UpdateError("That zip tries to write outside the staging folder. Refused.")
        archive.extractall(destination)

        if os.name == "posix":
            for member in archive.infolist():
                mode = (member.external_attr >> 16) & 0o777
                if not mode or member.is_dir():
                    continue
                try:
                    os.chmod(root / member.filename, mode)
                except OSError:  # pragma: no cover - odd filesystems
                    pass
    return destination


def _ensure_executable(path: Path) -> None:
    """Make sure a payload binary can actually be run.

    Belt and braces after :func:`unpack`: a zip built on Windows carries no Unix
    mode at all, so the bit has to come from somewhere. Only applies to real
    binaries (ELF/Mach-O or a ``#!`` line), never to data files.
    """
    if os.name != "posix" or not path.is_file():
        return
    if os.access(path, os.X_OK):
        return
    try:
        with open(path, "rb") as handle:
            head = handle.read(4)
    except OSError:  # pragma: no cover
        return
    looks_runnable = head.startswith((b"\x7fELF", b"#!", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"))
    if looks_runnable:
        try:
            path.chmod(path.stat().st_mode | 0o755)
        except OSError:  # pragma: no cover
            pass


def staged_payload() -> Path | None:
    """The staged files: the exe, a directory, or a macOS bundle."""
    marker = staging_root() / STAGED_MARKER
    if not marker.exists():
        return None
    try:
        info = json.loads(marker.read_text(encoding="utf-8"))
        payload = Path(info["payload"])
        return payload if payload.exists() else None
    except Exception:
        return None


def staged_info() -> dict[str, Any] | None:
    marker = staging_root() / STAGED_MARKER
    if not marker.exists():
        return None
    try:
        return json.loads(marker.read_text(encoding="utf-8"))
    except Exception:
        return None


def clear_staging() -> None:
    shutil.rmtree(staging_root(), ignore_errors=True)


def _payload_entry(extracted: Path) -> Path:
    """What was actually shipped: one file, or a directory (bundle/onedir)."""
    entries = [p for p in extracted.iterdir() if not p.name.startswith(".")]
    if len(entries) == 1:
        return entries[0]
    # A one-dir build unpacks to several files; the folder itself is the payload.
    return extracted


def verify_binary(payload: Path) -> None:
    """Make the staged build prove it runs before it may replace this one.

    ``--self-check`` is the same command CI uses on the frozen app: it checks the
    bundled assets, a writable settings folder, and that the server starts and
    stops. A build that fails it would not have opened either, so installing it
    would be strictly worse than waiting.
    """
    exe = _payload_executable(payload)
    if exe is None:
        raise UpdateError("The update does not contain a runnable Aegis binary.")
    _ensure_executable(exe)
    try:
        completed = subprocess.run(
            [str(exe), "--self-check"],
            capture_output=True,
            text=True,
            timeout=SELF_CHECK_TIMEOUT,
            cwd=str(exe.parent),
            env={**os.environ, "AEGIS_NO_GUI_ERRORS": "1", "AEGIS_DATA_DIR": str(staging_root() / "probe")},
        )
    except subprocess.TimeoutExpired:
        raise UpdateError("The new build did not finish starting, so it was not installed.") from None
    except OSError as exc:
        raise UpdateError(f"The new build could not be started ({exc}), so it was not installed.") from None

    if completed.returncode != 0:
        detail = (completed.stdout or completed.stderr or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit code {completed.returncode}"
        raise UpdateError(f"The new build failed its own self-check ({tail}), so it was not installed.")


def _payload_executable(payload: Path) -> Path | None:
    """The binary to run/install: bundle-aware, platform-aware."""
    if payload.is_file():
        return payload
    system = platform.system()
    if system == "Darwin":
        candidate = payload / "Contents" / "MacOS" / "Aegis"
        if candidate.is_file():
            return candidate
        for item in payload.rglob("Aegis.app"):
            inner = item / "Contents" / "MacOS" / "Aegis"
            if inner.is_file():
                return inner
        return None
    name = "Aegis.exe" if system == "Windows" else "Aegis"
    for candidate in [payload / name, *payload.rglob(name)]:
        if candidate.is_file():
            return candidate
    return None


def download_and_stage(
    release: Release, *, progress: Callable[[int, int], None] | None = None
) -> dict[str, Any]:
    """Download, verify, unpack and self-check a release. Nothing is installed."""
    with _lock:
        clear_staging()
        staging = staging_root()
        try:
            zip_path = download_verified(release, progress=progress)
            extracted = unpack(zip_path, staging / "payload")
            payload = _payload_entry(extracted)
            verify_binary(payload)
        except Exception:
            clear_staging()
            raise

        info = {
            "version": release.version,
            "tag": release.tag,
            "sha256": release.sha256,
            "payload": str(payload),
            "staged_at": time.time(),
            "installable": is_frozen() and _install_dir_writable(),
        }
        (staging / STAGED_MARKER).write_text(json.dumps(info, indent=2), encoding="utf-8")
        zip_path.unlink(missing_ok=True)
        return info


def _install_dir_writable() -> bool:
    """Can this process actually replace its own files?

    A zip unpacked into ``Program Files`` or ``/Applications`` needs elevation,
    and finding that out *after* announcing a restart is a bad experience.
    """
    root = install_root()
    try:
        probe = root / ".aegis-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Installing: hand off to a helper that waits for this process to die
# ---------------------------------------------------------------------------

def register_restart_hook(hook: Callable[[], None] | None) -> None:
    """Let the desktop shell close its window when an install is ready."""
    global _restart_hook
    _restart_hook = hook


def apply_plan() -> dict[str, Any]:
    """What applying the staged update would replace, and whether it can."""
    info = staged_info()
    if info is None:
        return {"ok": False, "detail": "There is no downloaded update waiting to be installed."}
    payload = Path(info["payload"])
    if not payload.exists():
        clear_staging()
        return {"ok": False, "detail": "The downloaded update is gone; download it again."}
    if not is_frozen():
        return {
            "ok": False,
            "detail": (
                "This copy runs from source, so Aegis will not overwrite it. "
                "Update with git pull, or download the release."
            ),
        }
    if not _install_dir_writable():
        return {
            "ok": False,
            "detail": (
                f"Aegis cannot write to {install_root()}. Move the folder somewhere you "
                "own (Downloads, Desktop) and update again, or download the new release."
            ),
        }
    return {
        "ok": True,
        "version": info.get("version"),
        "payload": str(payload),
        "target": str(install_root()),
    }


def _windows_script(pid: int, payload: Path, target: Path, relaunch: Path) -> str:
    """A PowerShell helper: wait for our exit, swap, relaunch, report.

    PowerShell rather than batch for three reasons that all matter here: batch
    has no reliable "wait for this PID" (``tasklist`` output has to be grepped,
    and its memory column contains digits that look like the PID), no robust
    directory replace, and no way to report a failure. It runs hidden with no
    profile, so the user's own PowerShell setup cannot slow or alter it.
    """
    log = staging_root() / "update.log"
    if payload.is_dir():
        replace = """    if (Test-Path -LiteralPath $target) {{
        Move-Item -LiteralPath $target -Destination $backup -Force
    }}
    Copy-Item -LiteralPath $payload -Destination $target -Recurse -Force"""
    else:
        replace = """    if (Test-Path -LiteralPath $target) {{
        Move-Item -LiteralPath $target -Destination $backup -Force
    }}
    Copy-Item -LiteralPath $payload -Destination $target -Force"""
    return f"""# Aegis update helper. Generated by aegis/updates.py; deletes itself.
$ErrorActionPreference = 'Stop'
$log = '{_ps_quote(log)}'
function Note($message) {{
    Add-Content -Path $log -Value ("{{0:HH:mm:ss}}  {{1}}" -f (Get-Date), $message)
}}

try {{
    Note 'waiting for Aegis to exit'
    # Windows locks a running .exe. Waiting on the PID is the only reliable
    # signal; a fixed sleep would swap under a still-running app.
    $deadline = (Get-Date).AddMinutes(5)
    while ((Get-Date) -lt $deadline) {{
        if (-not (Get-Process -Id {pid} -ErrorAction SilentlyContinue)) {{ break }}
        Start-Sleep -Milliseconds 400
    }}
    if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{
        Note 'Aegis did not exit within five minutes; nothing was changed'
        exit 1
    }}

    $payload = '{_ps_quote(payload)}'
    $target = '{_ps_quote(target)}'
    $backup = '{_ps_quote(target)}.old'
    $relaunch = '{_ps_quote(relaunch)}'

    Note "replacing $target"
    if (Test-Path -LiteralPath $backup) {{
        Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue
    }}
{replace}

    Note 'starting the new build'
    if (Test-Path -LiteralPath $relaunch) {{
        $started = Start-Process -FilePath $relaunch -PassThru
        Start-Sleep -Seconds 3
        if ($started.HasExited) {{
            Note ("the new build exited immediately with code {{0}}" -f $started.ExitCode)
        }}
    }} else {{
        Note "the new build is missing: $relaunch"
    }}
    Note 'done'
}} catch {{
    Note "FAILED: $($_.Exception.Message)"
    exit 1
}} finally {{
    # The helper prunes itself on the next launch rather than deleting the file
    # it is still executing (see prune_helpers in aegis/updates.py).
    Note 'helper finished'
}}
"""


def _ps_quote(path: Path | str) -> str:
    """Single-quote a path for PowerShell ('' escapes a literal quote)."""
    return str(path).replace("'", "''")


def install_target(payload: Path, root: Path | None = None) -> Path:
    """The exact path the payload replaces.

    Two shapes exist, and confusing them replaces a folder with a file — which is
    how a staged update once left a 92-byte "directory" where the app used to be:

    * **a single binary** (a one-file build) replaces that file inside the
      install folder, so nothing else in the folder is touched;
    * **a directory** (a one-dir build, or a macOS ``.app``) replaces the install
      folder itself, whole.
    """
    root = root or install_root()
    return root / payload.name if payload.is_file() else root


def relaunch_path(payload: Path, target: Path) -> Path:
    """Where the new build is started from once the swap is done."""
    if payload.is_file():
        return target
    exe = _payload_executable(payload)
    if exe is None:
        return target
    try:
        return target / exe.relative_to(payload)
    except ValueError:  # pragma: no cover - guard for an odd bundle layout
        return target / exe.name


def _posix_script(pid: int, payload: Path, target: Path, relaunch: Path) -> str:
    """The POSIX helper: wait for our exit, swap, relaunch, keep the log.

    The swap has one form or the other depending on whether the payload is a
    single binary or a directory — decided here, while writing the script, rather
    than by a runtime test. A runtime test once replaced the whole install
    directory with a 92-byte file.
    """
    relaunch_log = staging_root() / "relaunch.log"
    quote = chr(34)

    def q(path) -> str:
        return f"{quote}{path}{quote}"

    if payload.is_dir():
        swap = (
            f"  rm -rf {q(str(target) + '.old')}\n"
            f"  mv {q(target)} {q(str(target) + '.old')} 2>/dev/null\n"
            f"  cp -R {q(payload)} {q(target)}"
        )
    else:
        swap = (
            f"  rm -f {q(str(target) + '.old')}\n"
            f"  mv {q(target)} {q(str(target) + '.old')} 2>/dev/null\n"
            f"  cp {q(payload)} {q(target)}"
        )
    return (
        "#!/bin/sh\n"
        "# Wait for the running Aegis to exit. On POSIX replacing a running image is\n"
        "# legal, but the old process would keep executing deleted code, so wait anyway.\n"
        f"while kill -0 {pid} 2>/dev/null; do sleep 0.3; done\n"
        "\n"
        f"{swap}\n"
        "\n"
        f"chmod +x {q(relaunch)} 2>/dev/null\n"
        "if command -v xattr >/dev/null 2>&1; then\n"
        f"  xattr -dr com.apple.quarantine {q(relaunch)} 2>/dev/null\n"
        "fi\n"
        "\n"
        "# Relaunch detached, but keep its first output: a build that dies on start\n"
        "# otherwise vanishes without a trace anywhere.\n"
        f"nohup {q(relaunch)} >{q(relaunch_log)} 2>&1 &\n"
        "child=$!\n"
        "sleep 3\n"
        'if kill -0 "$child" 2>/dev/null; then\n'
        "  :\n"
        f'elif [ ! -s {q(relaunch_log)} ]; then\n'
        f'  echo "the new build exited immediately and printed nothing" >>{q(relaunch_log)}\n'
        "fi\n"
        "\n"
        "# This script deliberately does not delete itself: dash reads a script\n"
        "# incrementally, so removing it mid-run breaks the interpreter. Aegis\n"
        "# prunes it on the next start instead.\n"
    )


def apply_staged_update(*, relaunch: bool = True) -> dict[str, Any]:
    """Write the helper script and start it. The caller then closes the app.

    Returns immediately: the script waits for this process to exit, so nothing
    can be replaced while it is still running.
    """
    plan = apply_plan()
    if not plan["ok"]:
        return plan

    payload = Path(plan["payload"])
    target = install_target(payload)
    restart_into = relaunch_path(payload, target) if relaunch else target

    staging_root().mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if platform.system() == "Windows":
        script = staging_root() / f"apply-{stamp}.ps1"
        script.write_text(
            _windows_script(os.getpid(), payload, target, restart_into), encoding="utf-8-sig"
        )
        flags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
        # -ExecutionPolicy Bypass for this one script: the alternative is asking
        # the user to change a machine-wide policy to receive an update.
        subprocess.Popen(  # noqa: S603 - fixed arguments, no shell
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle", "Hidden",
                "-ExecutionPolicy", "Bypass",
                "-File", str(script),
            ],
            creationflags=flags,
            close_fds=True,
            cwd=str(script.parent),
        )
    else:
        script = staging_root() / f"apply-{stamp}.sh"
        script.write_text(
            _posix_script(os.getpid(), payload, target, restart_into), encoding="utf-8"
        )
        script.chmod(0o700)
        subprocess.Popen(  # noqa: S603 - fixed arguments, no shell
            ["/bin/sh", str(script)],
            start_new_session=True,
            close_fds=True,
            cwd=str(script.parent),
        )

    if _restart_hook is not None:
        # The window closes; run_desktop returns; the process exits; the script
        # proceeds. Without this the user would have to quit by hand.
        threading.Thread(target=_restart_hook, name="aegis-restart", daemon=True).start()

    return {
        "ok": True,
        "detail": "Installing now — Aegis will close and reopen on the new version.",
        "script": str(script),
    }


def prune_helpers() -> list[str]:
    """Delete helper scripts left by previous updates.

    They cannot delete themselves: on POSIX, ``dash`` reads a script
    incrementally, so a script that removes itself fails partway through with
    "Unterminated quoted string" — after it has already swapped the files. So
    they are cleaned up here, on the next run, when nothing is executing them.
    """
    removed: list[str] = []
    root = staging_root()
    if not root.is_dir():
        return removed
    for pattern in ("apply-*.sh", "apply-*.ps1", "apply-*.bat"):
        for script in root.glob(pattern):
            try:
                script.unlink()
                removed.append(script.name)
            except OSError:  # pragma: no cover - still running, or locked
                pass
    return removed


def update_status() -> dict[str, Any]:
    """Everything the UI needs in one cheap call."""
    info = staged_info()
    return {
        "current": current_version(),
        "enabled": updates_enabled(),
        "frozen": is_frozen(),
        "staged": info,
        "installable": bool(info) and apply_plan().get("ok", False),
        "updates_download": str(staging_root()),
    }
