"""Configuration for Jarvis. Settings are stored in jarvis_config.json (git-ignored).

The file can hold an API key, so this module is careful about it:

* :func:`public_config` is the only shape ever handed to a browser — it reports
  *whether* a key is set plus a masked hint, never the key itself.
* Every value is validated both on load and on save. A malformed value falls
  back to its default instead of breaking the app, and a malformed payload is
  rejected before anything touches the disk.
* Writes are atomic (temp file + ``os.replace``), so a crash mid-save cannot
  leave a half-written file behind.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import sys
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger("jarvis.config")

# Set when the stored file could not be used, so the UI can say why instead of
# silently looking like a fresh install. A dict: {"kind", "detail", "backup"}.
LAST_PROBLEM: dict[str, str] | None = None


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle (the desktop app)."""
    return bool(getattr(sys, "frozen", False))


def user_data_dir() -> Path:
    """Where the app keeps its settings.

    * **Frozen/desktop:** the per-user location the OS expects, so the config
      is not inside the bundle (which may be read-only, and is wiped on
      upgrade) and does not land in whatever folder the app was launched from.
    * **From source:** the repository root, as documented — keeps `python
      run.py` behaving exactly as before.
    """
    override = os.environ.get("JARVIS_DATA_DIR")
    if override:
        return Path(override).expanduser()

    if is_frozen():
        home = Path.home()
        if platform.system() == "Windows":
            base = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        elif platform.system() == "Darwin":
            base = home / "Library" / "Application Support"
        else:  # Linux and friends, XDG
            base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
        return base / "Claw"

    return Path(__file__).resolve().parent.parent


DATA_DIR = user_data_dir()

CONFIG_PATH = Path(
    os.environ.get("JARVIS_CONFIG", str(DATA_DIR / "jarvis_config.json"))
)

DEFAULTS: dict[str, Any] = {
    "assistant_name": "Jarvis",
    "llm": {
        # Any OpenAI-compatible endpoint works:
        #   OpenAI     -> https://api.openai.com/v1        (model: gpt-4o-mini / gpt-4o)
        #   OpenRouter -> https://openrouter.ai/api/v1
        #   Groq       -> https://api.groq.com/openai/v1
        #   Gemini     -> https://generativelanguage.googleapis.com/v1beta/openai/
        #   Ollama     -> http://localhost:11434/v1        (no key needed, model: llama3.2-vision)
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-4o-mini",
    },
    # When True, Jarvis asks for your approval before acting on the PC
    # (running commands, typing, clicking, opening apps, writing files).
    "safe_mode": True,
    # When True, replies are spoken out loud in the browser.
    "voice_replies": True,
}

# Fields a browser may change. Anything else is ignored on input, so a client
# can never smuggle in its own value for a field it does not own.
CLIENT_WRITABLE = ("assistant_name", "safe_mode", "voice_replies")
CLIENT_WRITABLE_LLM = ("base_url", "api_key", "model")


class SettingsError(ValueError):
    """Raised when a settings payload is malformed. Maps to HTTP 400."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _as_str(value: Any, name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SettingsError(f"{name} must be a string")
    return value.strip()


def _as_bool(value: Any, name: str, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise SettingsError(f"{name} must be true or false")


def _validate_llm(raw: Any, base: dict[str, Any]) -> dict[str, Any]:
    if raw is None:
        return dict(base)
    if not isinstance(raw, dict):
        raise SettingsError("llm must be an object with base_url / api_key / model")
    out = dict(base)
    for key in CLIENT_WRITABLE_LLM:
        if key in raw:
            out[key] = _as_str(raw[key], f"llm.{key}")
    return out


def _validate(raw: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    """Return ``base`` with the recognised fields of ``raw`` validated on top."""
    out = dict(base)
    if "assistant_name" in raw:
        out["assistant_name"] = _as_str(raw["assistant_name"], "assistant_name") or "Jarvis"
    for key in ("safe_mode", "voice_replies"):
        if key in raw:
            out[key] = _as_bool(raw[key], key, default=bool(base.get(key, True)))
    out["llm"] = _validate_llm(raw.get("llm"), base.get("llm") or {})
    return out


def _fresh_defaults() -> dict[str, Any]:
    return json.loads(json.dumps(DEFAULTS))  # deep copy


def _quarantine(original: str) -> str:
    """Keep an unusable config next to the real one instead of destroying it.

    A truncated write or a hand-edit typo used to mean the next save silently
    overwrote the only copy of the user's API key. Now the bad file is moved
    aside (recoverable by hand) before anything new is written.
    """
    import time as _time

    backup = CONFIG_PATH.with_name(f"{CONFIG_PATH.name}.corrupt-{int(_time.time())}")
    try:
        CONFIG_PATH.replace(backup)
        log.warning("Moved the unreadable config to %s", backup)
        return str(backup)
    except OSError as exc:
        log.warning("Could not move the unreadable config aside: %s", exc)
        return ""


def config_problem() -> dict[str, str] | None:
    """The problem found at load time (if any), for /api/bootstrap."""
    return LAST_PROBLEM


def _sanitize(raw: Any) -> dict[str, Any]:
    """Best-effort repair of a stored config: bad field -> its default.

    Never raises. One corrupt field must not cost the user every other setting.
    """
    cfg = _fresh_defaults()
    if not isinstance(raw, dict):
        log.warning("%s does not contain a settings object; using defaults", CONFIG_PATH)
        return cfg
    for key in ("assistant_name", "safe_mode", "voice_replies"):
        if key in raw:
            try:
                cfg[key] = _validate({key: raw[key]}, cfg)[key]
            except SettingsError as exc:
                log.warning("Ignoring saved setting %s: %s", key, exc)
    try:
        cfg["llm"] = _validate_llm(raw.get("llm"), cfg["llm"])
    except SettingsError as exc:
        log.warning("Ignoring saved llm settings: %s", exc)
    return cfg


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def load_config() -> dict[str, Any]:
    """Read the stored config. Always returns a usable, fully-typed dict.

    A broken file is quarantined and reported through :func:`config_problem`
    rather than silently pretending the user never configured anything.
    """
    global LAST_PROBLEM
    try:
        if not CONFIG_PATH.exists():
            LAST_PROBLEM = None
            return _fresh_defaults()
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # unreadable or not JSON
        log.warning("Could not read %s (%s); quarantining it", CONFIG_PATH, exc)
        LAST_PROBLEM = {
            "kind": "unreadable",
            "detail": f"{CONFIG_PATH.name} is not valid JSON ({exc}).",
            "backup": _quarantine(str(exc)),
        }
        return _fresh_defaults()
    cfg = _sanitize(raw)
    LAST_PROBLEM = None
    return cfg


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".jarvis_config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, 0o600)  # only the owner can read the API key
        except OSError:
            pass  # e.g. Windows, where chmod is a no-op
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_config(patch: dict[str, Any] | None) -> dict[str, Any]:
    """Validate ``patch``, merge it over the stored config and persist it.

    Raises :class:`SettingsError` (before writing anything) if the payload is
    malformed. Unknown keys are ignored. Returns the full stored config.
    """
    if patch is None:
        patch = {}
    if not isinstance(patch, dict):
        raise SettingsError("settings must be an object")
    updated = _validate(patch, load_config())
    _atomic_write(CONFIG_PATH, json.dumps(updated, indent=2))
    return updated


def preview_config(patch: dict[str, Any] | None) -> dict[str, Any]:
    """Merge and validate ``patch`` over the stored config WITHOUT persisting.

    Used by "Test connection", which checks unsaved form values.
    """
    current = load_config()
    if not patch:
        return current
    if not isinstance(patch, dict):
        raise SettingsError("settings must be an object")
    return _validate(patch, current)


# ---------------------------------------------------------------------------
# Browser-facing view
# ---------------------------------------------------------------------------

def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "•" * len(key)
    return f"{key[:3]}…{key[-4:]}"


def public_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """The only representation of the config that may leave the server."""
    llm = cfg.get("llm") if isinstance(cfg.get("llm"), dict) else {}
    key = llm.get("api_key") or ""
    return {
        "assistant_name": cfg.get("assistant_name") or "Jarvis",
        "safe_mode": bool(cfg.get("safe_mode", True)),
        "voice_replies": bool(cfg.get("voice_replies", True)),
        "llm": {
            "base_url": llm.get("base_url") or "",
            "model": llm.get("model") or "",
            "api_key_set": bool(key),
            "api_key_masked": mask_key(key),
        },
    }


def has_api_key(cfg: dict[str, Any]) -> bool:
    llm = cfg.get("llm")
    return isinstance(llm, dict) and bool(llm.get("api_key"))


def is_demo_mode(cfg: dict[str, Any]) -> bool:
    """True when no real LLM is reachable: no key on a remote endpoint."""
    llm = cfg.get("llm")
    if not isinstance(llm, dict):
        return True
    base = (llm.get("base_url") or "").lower()
    local = "localhost" in base or "127.0.0.1" in base
    return not llm.get("api_key") and not local
