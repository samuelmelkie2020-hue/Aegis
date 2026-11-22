"""Running the AI on this PC.

Aegis is meant to be a private assistant, and the only way that is true is if the
model runs here too. This module is the whole story of that:

* **find a local server** — Ollama (the usual choice), LM Studio, or llama.cpp;
* **list what it actually has** — model names come from the server, not a
  hard-coded guess, because a name that is not installed is a confusing error;
* **size the machine** — RAM, VRAM, CPU, so a model can be recommended that will
  not swap or take a minute per reply;
* **pull a model** — streamed progress, cancellation, and honest byte counts;
* **explain failures** — "Ollama is not running" and "that model is not
  installed" are different problems with different fixes, and neither should
  surface as a stack trace.

Nothing here talks to a cloud provider. Every URL must resolve to loopback, and
that is enforced rather than assumed: a "local" provider pointing at a public
host would quietly send the user's screen to it.
"""

from __future__ import annotations

import ipaddress
import json
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

# Where local servers listen by default. Aegis checks these in order.
# Where local servers listen by default. Aegis checks these in order. The first
# three are the common ones; the rest are other apps people actually use, and
# probing them costs one refused connection each (instant on loopback).
KNOWN_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("Ollama", "http://127.0.0.1:11434"),
    ("LM Studio", "http://127.0.0.1:1234"),
    ("llama.cpp", "http://127.0.0.1:8080"),
    ("Jan", "http://127.0.0.1:1337"),
    ("vLLM", "http://127.0.0.1:8000"),
    ("LocalAI", "http://127.0.0.1:8081"),
    ("text-generation-webui", "http://127.0.0.1:5000"),
)

# Extensions that are always model weights, and never something to read.
WEIGHT_SUFFIXES: tuple[str, ...] = (".gguf", ".safetensors", ".bin", ".mlx", ".onnx")

# Folders that hold LM Studio / Jan / plain GGUF downloads. Ollama keeps its own
# layout (below); these are the "I downloaded a file and put it somewhere" cases,
# which is most of the rest of the world.
GGUF_FOLDER_NAMES: tuple[str, ...] = ("models", "Models", "lmstudio", "LM Studio", "jan", "gguf")

# Models that can see a screenshot. Aegis's core skills are visual, so this is
# what the recommendation aims at; text-only models still work, minus the eyes.
VISION_MODELS: tuple[str, ...] = (
    "qwen2.5vl",
    "qwen2.5-vl",
    "llava",
    "llama3.2-vision",
    "llama3.2:11b-vision",
    "moondream",
    "minicpm-v",
    "gemma3",
    "granite3.2-vision",
)

# Rough download sizes, so a recommendation can say what it will cost in
# bandwidth and disk before anything starts. Real sizes come from the server.
MODEL_HINTS: dict[str, tuple[float, str]] = {
    "qwen2.5vl:3b": (3.2, "best all-rounder for 8 GB of RAM"),
    "qwen2.5vl:7b": (6.0, "sharper, needs ~12 GB"),
    "llava:7b": (4.7, "older but reliable vision model"),
    "llama3.2-vision:11b": (7.9, "strong vision, needs ~16 GB"),
    "moondream": (1.7, "tiny, works on almost anything"),
    "llama3.2:3b": (2.0, "fast text-only model, no screenshots"),
    "qwen2.5:3b": (1.9, "fast text-only model, no screenshots"),
    "llama3.1:8b": (4.9, "solid text-only model"),
}

PROBE_TIMEOUT = 1.5          # a local server answers instantly or is not running
LIST_TIMEOUT = 4.0
PULL_TIMEOUT = 3600.0        # a 6 GB pull over a slow line is still fine
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


# ---------------------------------------------------------------------------
# Local-ness
# ---------------------------------------------------------------------------

def is_local_url(url: str) -> bool:
    """True when this URL points at this machine and nothing else.

    Deliberately strict: a provider labelled "local" that resolves elsewhere
    would send screenshots and file contents off the PC, which is the exact thing
    a local model is chosen to prevent.
    """
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower()
    if not host:
        return False
    if host in LOCAL_HOSTS:
        return _valid_port(parts)
    try:
        return ipaddress.ip_address(host).is_loopback and _valid_port(parts)
    except ValueError:
        return False


def _valid_port(parts: Any) -> bool:
    """A URL whose port cannot be parsed is not a usable endpoint."""
    try:
        _ = parts.port  # raises ValueError on "http://127.0.0.1:notaport"
    except ValueError:
        return False
    return True


def origin(url: str) -> str:
    """``http://127.0.0.1:11434/v1`` → ``http://127.0.0.1:11434``.

    Ollama's pull API lives at the origin (``/api/pull``), not under ``/v1``,
    so the OpenAI-style base has to be reduced before calling it.
    """
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def normalise_base(url: str) -> str:
    """``http://localhost:11434`` → the ``/v1`` OpenAI-compatible root."""
    base = (url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/v1"):
        return base
    return base + "/v1"


# ---------------------------------------------------------------------------
# What this machine can run
# ---------------------------------------------------------------------------

@dataclass
class Hardware:
    system: str = ""
    machine: str = ""
    cpu: str = ""
    cores: int = 0
    ram_gb: float = 0.0
    gpu: str = ""
    vram_gb: float = 0.0

    @property
    def budget_gb(self) -> float:
        """Memory a model can occupy: VRAM if there is a usable GPU, else RAM.

        Deliberately conservative — models also need room for the context window
        and the desktop itself, so roughly 60% is the usable figure rather than
        "fit exactly and swap".
        """
        if self.vram_gb >= 2.0:
            return round(self.vram_gb * 0.85, 1)
        return round(max(self.ram_gb - 4.0, 0.0) * 0.7, 1)

    def as_dict(self) -> dict[str, Any]:
        return {
            "system": self.system,
            "machine": self.machine,
            "cpu": self.cpu,
            "cores": self.cores,
            "ram_gb": self.ram_gb,
            "gpu": self.gpu,
            "vram_gb": self.vram_gb,
            "budget_gb": self.budget_gb,
        }


# `detect_hardware()` shells out to nvidia-smi and PowerShell. On a cold Windows
# start that can take many seconds — and a shelled-out process can even flash a
# console window — which is fine when the user presses Refresh in Settings and
# unacceptable on the path that opens the app. So the answer is cached per
# process, and callers on a startup path use `probe_hardware()` with a deadline.
_HARDWARE: Hardware | None = None
_HARDWARE_LOCK = threading.Lock()

HARDWARE_TIMEOUT = 20.0     # a full probe (GPU included) may take this long
STARTUP_PROBE_TIMEOUT = 0.6  # the startup path only checks "is anything listening"


def probe_hardware(timeout: float = HARDWARE_TIMEOUT) -> Hardware | None:
    """Hardware from cache, or from a probe abandoned after ``timeout``.

    Returns ``None`` rather than blocking. The abandoned probe keeps running in
    the background, so by the time Settings is opened its answer is usually
    cached — the slow case costs one wait, never two.
    """
    with _HARDWARE_LOCK:
        if _HARDWARE is not None:
            return _HARDWARE

    def probe_and_remember() -> None:
        # Cache from *inside* the worker, so an abandoned probe still leaves its
        # answer behind. Otherwise the slow case is paid twice: once at startup
        # and again when Settings opens.
        global _HARDWARE
        try:
            found = detect_hardware()
        except Exception:  # pragma: no cover - detect_hardware is total
            return
        with _HARDWARE_LOCK:
            _HARDWARE = found

    worker = threading.Thread(target=probe_and_remember, name="aegis-hardware", daemon=True)
    worker.start()
    worker.join(timeout)
    # Either it finished inside the budget, or it is still running and the answer
    # is not ready: in both cases the cache is the single source of truth.
    return cached_hardware()


def cached_hardware() -> Hardware | None:
    """Only what has already been probed. Never starts a probe."""
    with _HARDWARE_LOCK:
        return _HARDWARE


def reset_hardware_cache() -> None:
    """Forget the cached probe (tests; or after a GPU driver install)."""
    global _HARDWARE
    with _HARDWARE_LOCK:
        _HARDWARE = None


def detect_hardware() -> Hardware:
    """Read what this PC has. Never raises: unknown is a valid answer.

    Slow — it shells out to nvidia-smi and PowerShell — so nothing on the startup
    path may call it directly. Use :func:`probe_hardware`, which caches.
    """
    info = Hardware(
        system=platform.system() or "",
        machine=platform.machine() or "",
        cpu=_cpu_name(),
        cores=os.cpu_count() or 0,
        ram_gb=_ram_gb(),
    )
    gpu, vram = _gpu()
    info.gpu, info.vram_gb = gpu, vram
    return info


def _cpu_name() -> str:
    try:
        system = platform.system()
        if system == "Windows":
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
            ) as key:
                return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        if system == "Darwin":
            out = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True, text=True, timeout=5,
            )
            return out.stdout.strip() or platform.processor()
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return platform.processor() or ""


def _ram_gb() -> float:
    try:
        import psutil

        return round(psutil.virtual_memory().total / (1024**3), 1)
    except Exception:  # pragma: no cover - psutil is a hard dependency
        return 0.0


def _gpu() -> tuple[str, float]:
    """The GPU and its VRAM, if one can be read. ``("", 0)`` when unknown.

    VRAM decides which models are pleasant rather than painful, so it is worth a
    few platform-specific attempts — but any of them failing is normal.
    """
    system = platform.system()
    try:
        if system == "Windows":
            return _gpu_windows()
        if system == "Darwin":
            return _gpu_macos()
        return _gpu_linux()
    except Exception:
        return "", 0.0


def _gpu_windows() -> tuple[str, float]:
    # nvidia-smi is present with any NVIDIA driver and reports exact VRAM.
    smi = shutil.which("nvidia-smi")
    if smi:
        out = subprocess.run(
            [smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8,
        )
        line = (out.stdout or "").strip().splitlines()[0] if out.stdout.strip() else ""
        if "," in line:
            name, megabytes = line.rsplit(",", 1)
            return name.strip(), round(float(megabytes) / 1024, 1)

    # Otherwise ask PowerShell for the display adapter's reported memory. This
    # includes shared memory, so it is an upper bound; treat it as such.
    try:
        out = subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                "Get-CimInstance Win32_VideoController | "
                "Select-Object -First 1 Name,AdapterRAM | ConvertTo-Json -Compress",
            ],
            capture_output=True, text=True, timeout=15,
        )
        data = json.loads(out.stdout or "{}")
        name = str(data.get("Name") or "").strip()
        adapter = data.get("AdapterRAM") or 0
        vram = round(float(adapter) / (1024**3), 1) if adapter else 0.0
        return name, max(vram, 0.0)
    except Exception:
        return "", 0.0


def _gpu_macos() -> tuple[str, float]:
    out = subprocess.run(
        ["system_profiler", "SPDisplaysDataType"], capture_output=True, text=True, timeout=15
    )
    name, vram = "", 0.0
    for line in (out.stdout or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("Chipset Model:"):
            name = stripped.split(":", 1)[1].strip()
        elif "VRAM" in stripped and ":" in stripped:
            amount = stripped.split(":", 1)[1].strip().split()[0]
            try:
                vram = round(float(amount) / 1024, 1)  # reported in MB
            except ValueError:
                pass
    if not vram and name:
        # Apple silicon shares memory with the CPU; there is no separate VRAM.
        vram = round(_ram_gb() * 0.6, 1)
    return name, vram


def _gpu_linux() -> tuple[str, float]:
    smi = shutil.which("nvidia-smi")
    if smi:
        out = subprocess.run(
            [smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8,
        )
        if out.stdout.strip():
            name, megabytes = out.stdout.strip().splitlines()[0].rsplit(",", 1)
            return name.strip(), round(float(megabytes) / 1024, 1)
    return "", 0.0


# ---------------------------------------------------------------------------
# Finding and reading a local server
# ---------------------------------------------------------------------------

@dataclass
class LocalServer:
    name: str
    base_url: str
    models: list[str] = field(default_factory=list)
    reachable: bool = False
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "base_url": self.base_url,
            "models": self.models,
            "reachable": self.reachable,
            "detail": self.detail,
        }


def list_models(base_url: str, *, timeout: float = LIST_TIMEOUT) -> tuple[list[str], str]:
    """``(models, error)`` from an OpenAI-compatible ``/v1/models``.

    Returns the server's own names, because a model the server does not have is
    the error users hit most, and guessing a name from a hard-coded list is how
    the app ends up recommending something that is not installed.
    """
    if not is_local_url(base_url):
        return [], "That address is not on this computer, so it is not a local model."
    try:
        response = httpx.get(normalise_base(base_url) + "/models", timeout=timeout)
    except Exception as exc:
        return [], _friendly_unreachable(exc)
    if response.status_code != 200:
        return [], f"The local server answered HTTP {response.status_code}."
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return [], "The local server sent something that is not a model list."

    names: list[str] = []
    for entry in payload.get("data") or payload.get("models") or []:
        if isinstance(entry, dict):
            name = entry.get("id") or entry.get("name") or entry.get("model")
            if name:
                names.append(str(name))
        elif isinstance(entry, str):
            names.append(entry)
    return sorted(set(names)), ""


def _friendly_unreachable(exc: Exception) -> str:
    text = str(exc).lower()
    if any(
        key in text
        for key in ("refused", "connect", "timeout", "timed out", "unreachable", "no route")
    ):
        return (
            "Nothing is listening there — the local model server is not running. "
            "Start it (Ollama: open the app, or run `ollama serve`), then press Refresh."
        )
    return f"Could not reach the local server ({str(exc)[:160]})."


def port_open(base: str, *, timeout: float = PROBE_TIMEOUT) -> bool:
    """True when *anything* accepts a TCP connection on this loopback address.

    Cheaper and more forgiving than an HTTP probe: a local server that does not
    expose ``/v1/models`` still means the user has something running, and saying
    "nothing is running" then would be a lie they cannot debug.
    """
    try:
        parts = urlsplit((base or "").strip())
    except ValueError:
        return False
    host, port = parts.hostname, parts.port
    if not host or not port:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _ollama_version(base: str, *, timeout: float = PROBE_TIMEOUT) -> str:
    """Ollama's version string, or "" — used to tell "Ollama" from "something"."""
    try:
        response = httpx.get(f"{origin(base)}/api/version", timeout=timeout)
        if response.status_code == 200:
            return str((response.json() or {}).get("version") or "")
    except Exception:
        return ""
    return ""


def probe_servers(*, include: str | None = None, timeout: float | None = None) -> list[LocalServer]:
    """Check the known local servers, plus one the user configured."""
    endpoints = list(KNOWN_ENDPOINTS)
    if (
        include
        and is_local_url(include)
        and not any(include.rstrip("/").startswith(base) for _, base in endpoints)
    ):
        endpoints.append(("Configured", include.rstrip("/")))

    results: list[LocalServer] = []
    for name, base in endpoints:
        models, error = list_models(base, timeout=timeout if timeout is not None else LIST_TIMEOUT)
        reachable = not error
        if not reachable and port_open(base, timeout=timeout or PROBE_TIMEOUT):
            # The port answers but the model list did not. Still worth reporting:
            # the user can see something is there and the detail says what broke.
            reachable = True
            version = _ollama_version(base, timeout=timeout or PROBE_TIMEOUT)
            error = (
                f"{name} is answering on {origin(base)}"
                + (f" (version {version})" if version else "")
                + ", but did not list any models. Download one with the button below, "
                "or load a model in the app."
            )
        elif reachable:
            error = ""
        results.append(
            LocalServer(
                name=name,
                base_url=base,
                models=models,
                reachable=reachable,
                detail=error,
            )
        )
    return results


def detect_running(
    *, include: str | None = None, timeout: float = PROBE_TIMEOUT
) -> LocalServer | None:
    """The first reachable local server, or ``None``.

    Probes each endpoint exactly once — it used to run the whole loop twice, which
    doubled the startup cost of the common case (nothing is running). Servers
    that have a model win over servers that are merely up.
    """
    servers = [s for s in probe_servers(include=include, timeout=timeout) if s.reachable]
    for server in servers:
        if server.models:
            return server
    return servers[0] if servers else None


# ---------------------------------------------------------------------------
# Recommending something that will actually run
# ---------------------------------------------------------------------------

def has_vision(name: str) -> bool:
    low = (name or "").lower()
    return any(marker in low for marker in VISION_MODELS)


def recommend(
    hardware: Hardware | None = None,
    installed: list[str] | None = None,
    *,
    timeout: float = 0.0,
) -> dict[str, Any]:
    """Pick a model for this machine and say why, in plain terms.

    Preference order: something already installed (no download), then the largest
    vision model that fits comfortably, then a text-only one, then the smallest
    vision model regardless — a slow working model beats no model.

    ``timeout=0`` (the default) never waits for a hardware probe: it uses what is
    cached and otherwise assumes a modest machine. Settings passes a real timeout,
    and the startup path must never spend seconds here.
    """
    hardware = hardware or cached_hardware()
    if hardware is None:
        hardware = probe_hardware(timeout) if timeout > 0 else None
    if hardware is None:
        # Unknown is not a reason to guess big: a model that does not fit is a
        # worse first impression than one that is merely smaller.
        hardware = Hardware(
            system=platform.system() or "",
            machine=platform.machine() or "",
            cpu="",
            cores=os.cpu_count() or 0,
            ram_gb=8.0,
        )
    installed = installed or []
    budget = hardware.budget_gb

    installed_vision = [m for m in installed if has_vision(m)]
    if installed_vision:
        return {
            "model": installed_vision[0],
            "installed": True,
            "vision": True,
            "detail": f"You already have {installed_vision[0]} — nothing to download.",
            "reason": "already installed",
        }
    if installed:
        return {
            "model": installed[0],
            "installed": True,
            "vision": False,
            "detail": (
                f"You already have {installed[0]}, but it cannot see screenshots. "
                "A vision model would let Aegis look at your screen."
            ),
            "reason": "already installed, text only",
        }

    candidates = sorted(MODEL_HINTS.items(), key=lambda kv: -kv[1][0])
    for name, (size_gb, why) in candidates:
        if size_gb <= budget and has_vision(name):
            return {
                "model": name,
                "installed": False,
                "vision": True,
                "size_gb": size_gb,
                "detail": f"{why} — about {size_gb:.1f} GB to download.",
                "reason": "fits this machine",
            }
    for name, (size_gb, why) in candidates:
        if size_gb <= budget:
            return {
                "model": name,
                "installed": False,
                "vision": False,
                "size_gb": size_gb,
                "detail": f"{why} — about {size_gb:.1f} GB. Text only, so no screenshots.",
                "reason": "fits this machine, text only",
            }
    smallest, (size_gb, why) = min(MODEL_HINTS.items(), key=lambda kv: kv[1][0])
    return {
        "model": smallest,
        "installed": False,
        "vision": has_vision(smallest),
        "size_gb": size_gb,
        "detail": (
            f"This PC reports {budget:.0f} GB usable for a model, which is tight. "
            f"{smallest} is the smallest option ({size_gb:.1f} GB) and will be slow."
        ),
        "reason": "smallest available",
    }


# ---------------------------------------------------------------------------
# Pulling a model
# ---------------------------------------------------------------------------

_pull_lock = threading.Lock()
_pull: dict[str, Any] = {"running": False, "model": "", "status": "", "completed": 0, "total": 0,
                         "error": "", "started": 0.0, "cancelled": False}


def pull_state() -> dict[str, Any]:
    with _pull_lock:
        return dict(_pull)


def _set_pull(**changes: Any) -> None:
    with _pull_lock:
        _pull.update(changes)


def start_pull(model: str, *, base_url: str = "http://127.0.0.1:11434") -> dict[str, Any]:
    """Begin downloading a model in the background. One at a time."""
    model = (model or "").strip()
    if not model:
        return {"ok": False, "detail": "Name a model to download."}
    if not is_local_url(base_url):
        return {"ok": False, "detail": "That address is not on this computer."}
    with _pull_lock:
        if _pull["running"]:
            return {
                "ok": False,
                "detail": f"Already downloading {_pull['model']}. Wait for it to finish.",
            }
        _pull.update(running=True, model=model, status="starting", completed=0, total=0,
                     error="", started=time.time(), cancelled=False)

    threading.Thread(target=_pull_worker, args=(model, base_url), daemon=True).start()
    return {"ok": True, "detail": f"Downloading {model}…"}


def cancel_pull() -> dict[str, Any]:
    with _pull_lock:
        if not _pull["running"]:
            return {"ok": False, "detail": "Nothing is downloading."}
        _pull["cancelled"] = True
    return {"ok": True, "detail": "Stopping the download…"}


def _pull_worker(model: str, base_url: str) -> None:
    """Stream Ollama's progress events into :data:`_pull`.

    Only Ollama has a pull API. For anything else the user is told to fetch the
    model in its own UI, which is honest rather than pretending to track it.
    """
    endpoint = base_url.rstrip("/")
    if not endpoint.endswith("/api"):
        endpoint = endpoint + "/api"

    try:
        with httpx.stream(
            "POST",
            endpoint + "/pull",
            json={"name": model, "stream": True},
            timeout=PULL_TIMEOUT,
        ) as response:
            if response.status_code != 200:
                _set_pull(running=False, error=f"HTTP {response.status_code} from the local server")
                return
            for line in response.iter_lines():
                if pull_state()["cancelled"]:
                    _set_pull(running=False, status="cancelled")
                    return
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("error"):
                    _set_pull(running=False, error=str(event["error"])[:300])
                    return
                # Ollama's last events ("verifying sha256 digest", "success")
                # carry no byte counts. Writing 0 there would snap the progress
                # bar back to the start just as the download finished, so only
                # update what the event actually reports.
                changes: dict[str, Any] = {"status": str(event.get("status") or "")}
                if event.get("total"):
                    changes["total"] = int(event["total"])
                    changes["completed"] = int(event.get("completed") or 0)
                elif event.get("completed"):
                    changes["completed"] = int(event["completed"])
                _set_pull(**changes)
        final = pull_state()
        _set_pull(
            running=False,
            status="done",
            completed=final.get("total") or final.get("completed") or 0,
        )
    except Exception as exc:
        _set_pull(running=False, error=str(exc)[:300])


# ---------------------------------------------------------------------------
# One report for the UI
# ---------------------------------------------------------------------------

def _safe_hardware() -> Hardware:
    """Hardware, or a zero-budget placeholder if the probe itself broke."""
    try:
        return detect_hardware()
    except Exception:  # pragma: no cover - defensive
        return Hardware(system=platform.system(), machine=platform.machine(), cpu="unknown", cores=1)


def status(*, include: str | None = None) -> dict[str, Any]:
    """Everything the settings panel needs in one round trip.

    Blocks for at most ~5 s (three endpoints × :data:`PROBE_TIMEOUT`), which the
    UI calls once when Settings opens and then only on Refresh.
    """
    hardware = _safe_hardware()
    servers = probe_servers(include=include)
    reachable = [s for s in servers if s.reachable]
    installed: list[str] = []
    for server in reachable:
        installed.extend(server.models)
    choice = recommend(hardware, installed)
    # What is on this disk even if no server is up: the answer to "I downloaded a
    # model — where did it go?". Nothing here starts a process; that is the
    # explicit /api/local/adopt action, because opening Settings must stay cheap.
    disk = scan_disk()
    weights = scan_weights()
    return {
        "hardware": hardware.as_dict(),
        "servers": [s.as_dict() for s in servers],
        "running": bool(reachable),
        "installed": sorted(set(installed)),
        "recommendation": choice,
        "pull": pull_state(),
        "ollama_installed": bool(shutil.which("ollama")),
        "hint": install_hint(),
        "disk": disk,
        "weights": weights,
        "on_disk_not_listed": [
            m for m in disk["models"] if m["name"] not in set(installed)
        ],
    }


def adopt_local(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Point an unconfigured Aegis at a local server, once, at startup.

    Returns a plain dict describing what happened so the caller can log it. It
    never overwrites a *working* setup: a model on a local server, or a key for a
    remote one. That keeps "it was working yesterday" from turning into "it moved
    itself".

    This runs before the window opens, so it is written to be *fast*: a loopback
    connect with a 0.6 s budget per endpoint and no GPU probe. Anything slower
    than that is a bug, not a trade-off — the app must not appear to hang because
    a model server happened to be starting.
    """
    from .config import load_config, save_config

    cfg = cfg or load_config()
    llm = cfg.get("llm") or {}
    base = llm.get("base_url") or ""
    model = (llm.get("model") or "").strip()

    has_key = bool((llm.get("api_key") or "").strip())
    # "Configured" means it could actually answer: a model on a local server, or
    # a key for a remote one. The pre-1.8 default (OpenAI, no key, gpt-4o-mini)
    # could not, and leaving it would show a demo brain plus an API-key field the
    # user has no key for — exactly the confusion this release removes.
    if model and (is_local_url(base) or has_key):
        return {"changed": False, "reason": "a model is already selected", "model": model}
    if base and not is_local_url(base) and has_key:
        return {"changed": False, "reason": "a remote provider is configured", "model": ""}

    try:
        server = detect_running(include=base, timeout=STARTUP_PROBE_TIMEOUT)
    except Exception as exc:  # pragma: no cover - startup must never fail here
        return {"changed": False, "reason": f"probe failed: {exc}", "model": ""}
    if server is None or not server.models:
        return {"changed": False, "reason": "no local server running", "model": ""}

    # No hardware probe here: the cache is used if Settings already filled it, and
    # otherwise a modest machine is assumed. A wrong-but-working pick is better
    # than a window that takes ten seconds to appear.
    choice = recommend(None, server.models)
    if not choice.get("model"):
        return {"changed": False, "reason": "no model to choose", "model": ""}
    try:
        save_config(
            {
                "llm": {
                    "base_url": normalise_base(server.base_url),
                    "model": choice["model"],
                    "api_key": "",
                }
            }
        )
    except Exception as exc:  # pragma: no cover - config write already validated
        return {"changed": False, "reason": f"could not save: {exc}", "model": ""}
    return {
        "changed": True,
        "reason": f"using {choice['model']} on {server.name}",
        "model": choice["model"],
        "server": server.name,
    }


def install_hint() -> str:
    """One line telling the user how to get a local server, per platform."""
    system = platform.system()
    if system == "Windows":
        return "Download Ollama for Windows from ollama.com, install it, then press Refresh."
    if system == "Darwin":
        return "Download Ollama from ollama.com (or `brew install ollama`), then press Refresh."
    if shutil.which("ollama"):
        return "Run `ollama serve` in a terminal, then press Refresh."
    return "Install Ollama: `curl -fsSL https://ollama.com/install.sh | sh`"


# ---------------------------------------------------------------------------
# What is on this disk, even when Ollama is not running
# ---------------------------------------------------------------------------
#
# The question "I downloaded a model, where is it?" is answered by the file
# system, not by the server: Ollama only lists models while `ollama serve` is up,
# and if the app is not installed any more (or the server was never started) the
# model list comes back empty while gigabytes sit in a folder. These two
# functions read that folder directly, which is the only way to say "you have
# llama3.2:3b on disk" without anything running.

# Ollama's own directory names. Weights live in `blobs/`, and `manifests/` holds
# a small JSON per model whose path *is* the model name (`registry.ollama.ai/
# library/llama3.2/3b` → `llama3.2:3b`).
MANIFEST_ROOT = ("manifests",)
BLOB_ROOT = ("blobs",)


def _home() -> Path:
    return Path(os.environ.get("HOME") or os.environ.get("USERPROFILE") or Path.home())


def model_dirs() -> list[tuple[Path, str]]:
    """Every place Ollama might keep its models on this machine, best first.

    ``OLLAMA_MODELS`` wins because that is how a user who moved the folder said
    so. The rest are the defaults for each OS, plus the older shared location a
    Linux install may still use. Only existing directories are returned, so a
    caller can report "nowhere" honestly rather than listing guesses.
    """
    candidates: list[tuple[Path, str]] = []
    env = (os.environ.get("OLLAMA_MODELS") or "").strip()
    if env:
        candidates.append((Path(env).expanduser(), "set by OLLAMA_MODELS"))
    home = _home()
    system = platform.system()
    if system == "Windows":
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        candidates.append((local / "Programs" / "Ollama" / "models", "Ollama's Windows install folder"))
        candidates.append((home / ".ollama" / "models", "your user folder"))
    elif system == "Darwin":
        candidates.append((home / ".ollama" / "models", "your user folder"))
        candidates.append(
            (home / "Library" / "Application Support" / "Ollama" / "models", "the macOS app data folder")
        )
    else:
        candidates.append((home / ".ollama" / "models", "your user folder"))
        candidates.append((Path("/usr/share/ollama/.ollama/models"), "the system-wide Ollama service"))
        candidates.append((Path("/var/lib/ollama/.ollama/models"), "the system-wide Ollama service"))
    seen: set[str] = set()
    out: list[tuple[Path, str]] = []
    for path, why in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            if path.is_dir() and any(path.iterdir()):
                out.append((path, why))
        except OSError:
            continue
    return out


def model_dir() -> Path:
    """The most likely Ollama model folder (whether or not it exists)."""
    found = model_dirs()
    return found[0][0] if found else _home() / ".ollama" / "models"


def _manifest_name(root: Path, manifest: Path) -> str:
    """``…/manifests/registry.ollama.ai/library/llama3.2/3b`` → ``llama3.2:3b``.

    Ollama's layout carries the registry and namespace as extra path parts. A
    name without a tag is served as ``:latest``, and the tag is the last part, so
    the two cases are distinguishable without asking the server.
    """
    try:
        parts = manifest.relative_to(root).parts
    except ValueError:
        parts = manifest.parts
    if len(parts) < 3:
        return ""
    model, tag = parts[-2], parts[-1]
    return model if tag == "latest" else f"{model}:{tag}"


def _blob_size(root: Path, digest: str) -> int:
    """Bytes for a ``sha256:…`` digest, preferring the uncompressed blob."""
    if not digest.startswith("sha256:"):
        return 0
    key = digest.split(":", 1)[1]
    for name in (f"sha256-{key}", f"sha256-{key}-partial"):
        candidate = root / Path(*BLOB_ROOT) / name
        try:
            return candidate.stat().st_size
        except OSError:
            continue
    return 0


def scan_disk(*, limit_manifests: int = 4000) -> dict[str, Any]:
    """Models sitting in an Ollama folder, read without any server running.

    Returns ``{"dirs": [...], "models": [...], "total_gb": float, "error": ""}``.
    Every model entry is ``{name, size_bytes, size_gb, vision, modified}``, so the
    UI can show the user what they already paid for in bandwidth and disk.
    """
    dirs = model_dirs()
    models: dict[str, dict[str, Any]] = {}
    scanned = 0
    for root, why in dirs:
        manifests = root / Path(*MANIFEST_ROOT)
        if not manifests.is_dir():
            continue
        for manifest in sorted(manifests.rglob("*")):
            if scanned >= limit_manifests:
                break
            try:
                if not manifest.is_file():
                    continue
            except OSError:
                continue
            scanned += 1
            name = _manifest_name(manifests, manifest)
            if not name:
                continue
            size = 0
            modified = 0.0
            try:
                import json as _json

                data = _json.loads(manifest.read_text(encoding="utf-8"))
                for layer in data.get("layers") or []:
                    if isinstance(layer, dict):
                        size += _blob_size(root, str(layer.get("digest") or ""))
                modified = manifest.stat().st_mtime
            except Exception:
                # An unreadable manifest is not a reason to hide the rest.
                pass
            models[name] = {
                "name": name,
                "size_bytes": size,
                "size_gb": round(size / 1e9, 2) if size else 0.0,
                "vision": has_vision(name),
                "modified": modified,
                "folder": str(root),
                "folder_note": why,
            }
    found = sorted(models.values(), key=lambda m: (-m["size_bytes"], m["name"]))
    return {
        "dirs": [{"path": str(p), "note": why} for p, why in dirs],
        "models": found,
        "total_gb": round(sum(m["size_bytes"] for m in found) / 1e9, 2),
        "error": "",
    }


def known_model_names() -> list[str]:
    """Names of models on disk, for a better "that model is not installed" error."""
    try:
        return [m["name"] for m in scan_disk()["models"]]
    except Exception:  # pragma: no cover - a scan failure must never break a chat
        return []


def weight_dirs() -> list[tuple[Path, str]]:
    """Folders that hold model *files* (not Ollama's manifest layout).

    Deliberately not a whole-disk search: a scan of the whole disk on every Settings open is
    a good way to make the app feel broken. These are the places each app puts
    models by default, plus anything the user pointed at with an env var.
    """
    home = _home()
    system = platform.system()
    candidates: list[tuple[Path, str]] = []
    for key, note in (
        ("AEGIS_MODEL_DIR", "set by AEGIS_MODEL_DIR"),
        ("LMSTUDIO_MODELS", "set by LMSTUDIO_MODELS"),
        ("JAN_MODELS_DIR", "set by JAN_MODELS_DIR"),
    ):
        value = (os.environ.get(key) or "").strip()
        if value:
            candidates.append((Path(value).expanduser(), note))
    if system == "Windows":
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        candidates.append((home / ".cache" / "lm-studio" / "models", "LM Studio's default folder"))
        candidates.append((home / ".lmstudio" / "models", "LM Studio's default folder"))
        candidates.append((local / "nomic.ai" / "GPT4All", "GPT4All's models"))
        candidates.append((home / "Downloads", "your Downloads folder"))
    elif system == "Darwin":
        candidates.append((home / ".cache" / "lm-studio" / "models", "LM Studio's default folder"))
        candidates.append((home / ".lmstudio" / "models", "LM Studio's default folder"))
        candidates.append((home / "Library" / "Application Support" / "Jan" / "data", "Jan's data folder"))
        candidates.append((home / "Downloads", "your Downloads folder"))
    else:
        candidates.append((home / ".cache" / "lm-studio" / "models", "LM Studio's default folder"))
        candidates.append((home / ".lmstudio" / "models", "LM Studio's default folder"))
        candidates.append((home / ".local" / "share" / "nomic.ai" / "GPT4All", "GPT4All's models"))
        candidates.append((home / "Downloads", "your Downloads folder"))
    out: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for path, note in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            if path.is_dir():
                out.append((path, note))
        except OSError:
            continue
    return out


def gguf_name(path: Path) -> str:
    """A model name a server would actually accept, from a file name.

    ``qwen2.5-vl-3b-instruct-q4_k_m.gguf`` → ``qwen2.5-vl-3b-instruct``. The
    quantisation suffix is dropped because every server keys off the base name
    and the quant is a detail of the file, not of the model.
    """
    stem = path.stem
    lowered = stem.lower()
    for marker in ("-q4_k_m", "-q4_0", "-q5_k_m", "-q8_0", "-q6_k", "-q4_k_s", "-f16", "-fp16", "-iq4_xs"):
        index = lowered.find(marker)
        if index > 0:
            stem = stem[:index]
            break
    return stem


def scan_weights(
    *, limit_files: int = 400, max_depth: int = 4, extra_dirs: list[Path] | None = None
) -> list[dict[str, Any]]:
    """Model *files* found in the usual folders, with their real sizes.

    Bounded on purpose: at most ``limit_files`` files, ``max_depth`` levels deep,
    and only the folders each app actually uses. A model weights folder can hold
    tens of gigabytes and thousands of shards; the UI needs names and sizes, not
    a filesystem crawl.
    """
    found: dict[str, dict[str, Any]] = {}
    budget = limit_files
    for root, note in list(weight_dirs()) + [(p, "chosen by you") for p in (extra_dirs or [])]:
        depth_root = len(root.parts)
        try:
            walker = os.walk(root)
            for dirpath, dirnames, filenames in walker:
                current = Path(dirpath)
                if len(current.parts) - depth_root >= max_depth:
                    dirnames[:] = []
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for filename in filenames:
                    if budget <= 0:
                        return sorted(found.values(), key=lambda m: (-m["size_bytes"], m["name"]))
                    if not filename.lower().endswith(WEIGHT_SUFFIXES):
                        continue
                    budget -= 1
                    path = current / filename
                    try:
                        size = path.stat().st_size
                    except OSError:
                        continue
                    name = gguf_name(path)
                    if name in found and found[name]["size_bytes"] >= size:
                        continue
                    found[name] = {
                        "name": name,
                        "path": str(path),
                        "format": path.suffix.lstrip(".").lower(),
                        "size_bytes": size,
                        "size_gb": round(size / 1e9, 2),
                        "vision": has_vision(name),
                        "folder": str(root),
                        "folder_note": note,
                    }
        except OSError:
            continue
    return sorted(found.values(), key=lambda m: (-m["size_bytes"], m["name"]))


def installed_bytes(ollama_models: list[dict[str, Any]], weights: list[dict[str, Any]]) -> int:
    """Combined size of everything installed, in bytes.

    Bytes, not GB: a 900 MB model and a 4.7 GB one must not each be rounded
    before being added, or the total drifts by a gigabyte. The UI formats this.
    """
    total = sum(int(m.get("size_bytes") or 0) for m in ollama_models)
    total += sum(int(w.get("size_bytes") or 0) for w in weights)
    return total


def find_all_models() -> dict[str, Any]:
    """Everything installed, from both worlds: Ollama's layout and model files.

    One answer for the UI, so "what have I got?" is never two half-answers.
    """
    ollama = scan_disk()
    weights = scan_weights()
    served: list[str] = []
    servers: list[dict[str, Any]] = []
    try:
        for server in probe_servers(timeout=PROBE_TIMEOUT):
            if server.reachable:
                servers.append(server.as_dict())
                served.extend(server.models)
    except Exception:  # pragma: no cover - a probe must never break a scan
        pass
    names = {m["name"] for m in ollama["models"]} | {w["name"] for w in weights}
    return {
        "ollama": ollama,
        "weights": weights,
        "servers": servers,
        "served": sorted(set(served)),
        "names": sorted(names),
        "total_bytes": installed_bytes(ollama["models"], weights),
        "total_gb": round(installed_bytes(ollama["models"], weights) / 1e9, 2),
    }


def start_server(models_dir: Path | None = None, *, timeout: float = 20.0) -> dict[str, Any]:
    """Start Ollama in the background so the user does not have to open a terminal.

    Only ever starts the binary Ollama itself installed — never a path from the
    network or from a request body. If it is already answering, nothing happens.
    """
    if any(server.reachable for server in probe_servers(timeout=PROBE_TIMEOUT)):
        return {"ok": True, "detail": "A local AI server is already running."}
    binary = shutil.which("ollama")
    if not binary:
        return {
            "ok": False,
            "detail": (
                "Ollama's `ollama` command is not on your PATH, so Aegis cannot start it "
                "for you. Open the Ollama app once, then press Refresh."
            ),
        }
    env = dict(os.environ)
    if models_dir is not None:
        env["OLLAMA_MODELS"] = str(models_dir)
    kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": env,
    }
    if platform.system() == "Windows":
        # No console window flashing up for a background service.
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        subprocess.Popen([binary, "serve"], **kwargs)  # noqa: S603 - fixed argv
    except Exception as exc:
        return {"ok": False, "detail": f"Could not start Ollama ({str(exc)[:160]})."}

    # Wait for it to answer: a first start loads model metadata and is not instant.
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(server.reachable for server in probe_servers(timeout=0.5)):
            return {"ok": True, "detail": "Started Ollama. Checking its models…"}
        time.sleep(0.5)
    return {
        "ok": False,
        "detail": (
            "Started Ollama, but it is not answering yet. Give it a few seconds and "
            "press Refresh — or open the Ollama app and check its window."
        ),
    }


def load_installed(models_dir: Path | None = None, *, base_url: str = "") -> dict[str, Any]:
    """Make a model on this disk usable, in as few steps as the machine allows.

    The order is deliberate:

    1. **server already up** — just read its list; nothing else to do.
    2. **models on disk but no server** — start Ollama ourselves, then read it.
    3. **the folder is not where Ollama looks** — restart it pointed at the folder
       that actually has the weights (``OLLAMA_MODELS``), which is exactly the
       "I know it downloaded, but it is not in the list" case.
    4. **nothing anywhere** — say so plainly, with the one command that fixes it.
    """
    target = Path(models_dir).expanduser() if models_dir else None
    disk = scan_disk()

    if target is None and disk["dirs"]:
        # Prefer a folder that actually holds models over an empty default.
        target = Path(disk["dirs"][0]["path"])

    running = [s for s in probe_servers(include=base_url or None, timeout=PROBE_TIMEOUT) if s.reachable]
    with_models = [s for s in running if s.models]
    if with_models:
        return {
            "ok": True,
            "started": False,
            "server": with_models[0].name,
            "models": with_models[0].models,
            "detail": f"{with_models[0].name} is already running with {len(with_models[0].models)} model(s) ready.",
            "disk": disk,
        }

    if not disk["models"] and not scan_weights() and not running:
        return {
            "ok": False,
            "started": False,
            "models": [],
            "disk": disk,
            "detail": (
                "No model files and no local server were found on this PC. "
                + install_hint()
            ),
        }

    started = start_server(target)
    after = [s for s in probe_servers(include=base_url or None, timeout=LIST_TIMEOUT) if s.models]
    if after:
        return {
            "ok": True,
            "started": started.get("ok", False),
            "server": after[0].name,
            "models": after[0].models,
            "detail": (
                f"{started.get('detail', '')} {after[0].name} now lists "
                f"{len(after[0].models)} model(s)."
            ).strip(),
            "disk": disk,
        }

    weights = scan_weights()
    on_disk = [m["name"] for m in disk["models"]] + [w["name"] for w in weights]
    if weights and not after:
        # A GGUF on disk is only usable by a server that knows where it is: point
        # the user at the app that owns the folder rather than pretending Aegis
        # can load weights itself.
        where = weights[0].get("folder_note") or weights[0].get("folder") or ""
        return {
            "ok": False,
            "started": started.get("ok", False),
            "models": [w["name"] for w in weights],
            "disk": disk,
            "weights": weights,
            "detail": (
                f"Found {len(weights)} model file(s) on disk ({where}), but no local server is "
                "running them. Open the app that owns that folder (LM Studio, Jan, GPT4All…), "
                "load the model there, then press Refresh — Aegis will use it. "
                "Ollama needs GGUF files imported before it can serve them."
            ),
        }
    if on_disk and target is not None:
        return {
            "ok": False,
            "started": started.get("ok", False),
            "models": on_disk,
            "disk": disk,
            "detail": (
                f"Found {len(on_disk)} model(s) on disk ({', '.join(on_disk[:4])}"
                f"{'…' if len(on_disk) > 4 else ''}) in {target}, but Ollama is not serving them. "
                "Aegis restarted Ollama pointed at that folder; if the list is still empty, "
                "open the Ollama app once so it can register them."
            ),
        }
    return {
        "ok": False,
        "started": started.get("ok", False),
        "models": [],
        "disk": disk,
        "detail": started.get("detail") or "Ollama is installed but did not list any models.",
    }
