"""Path policy and secret redaction for the tools.

Two ideas, both cheap and both load-bearing:

* **Sensitive locations are off-limits to the read-only tools.** Whoever is
  driving Jarvis is not always the account owner — a prompt injection in a web
  page, a file, or a screenshot can ask for ``~/.ssh/id_rsa``. Reads outside the
  home folder and writes outside it are refused unless the user opts out.
* **Tool output is scrubbed before it reaches the model.** If a dangerous
  command dumps an env var or a config file, the key must not travel to the
  LLM provider just because it happened to appear in stdout.

Both are deliberately *policy in code, not prompts*: a system prompt asking a
model to be careful is not a control.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Any

from .config import CONFIG_PATH

# Names that are never readable through the tools, anywhere on disk.
BLOCKED_DIR_NAMES = {
    ".ssh", ".aws", ".gnupg", ".kube", ".docker", ".password-store",
}
# Credential stores that live inside an otherwise ordinary config folder.
BLOCKED_SUBPATHS = (
    ".config/gh",        # GitHub CLI token
    ".config/gcloud",    # Google Cloud credentials
    ".config/hub",       # hub token
    ".config/rclone",    # cloud storage remotes
    ".config/sops",
    ".vscode/settings.json",  # often has inline tokens for extensions
)
BLOCKED_FILE_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", ".netrc", ".npmrc", ".pypirc",
    ".git-credentials", "credentials", "credentials.*", "*.pgpass", "*.kdbx",
    "secring.*", "*.ovpn", "known_hosts", "authorized_keys",
)
# Jarvis's own secrets: the access token must never be readable by the agent.
OWN_SECRET_NAMES = {CONFIG_PATH.name, (CONFIG_PATH.parent / ".jarvis_token").name}

# Paths an opt-out may re-enable (still never the crypto/credential material).
READ_ESCAPE_ENV = "JARVIS_ALLOW_READ_OUTSIDE_HOME"
WRITE_ESCAPE_ENV = "JARVIS_ALLOW_WRITE_OUTSIDE_HOME"


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _sensitive_reason(p: Path) -> str | None:
    """Why this path is off-limits, or None if it is fine."""
    name = p.name
    if name in OWN_SECRET_NAMES or ".jarvis_token" in name:
        return "it stores Jarvis's own API key or access token"
    if name in (".git",) or name.startswith(".env"):
        return "it may contain credentials or repository internals"
    as_posix = p.as_posix()
    for sub in BLOCKED_SUBPATHS:
        if f"/{sub}" in as_posix:
            return f"it is inside a protected folder ({sub})"
    for part in p.parts:
        if part in BLOCKED_DIR_NAMES:
            return (
                f"it is inside a protected folder ({part})"
                if part != p.name
                else f"{part} is a protected folder (it holds credentials)"
            )
    lowered = name.lower()
    for pattern in BLOCKED_FILE_PATTERNS:
        if fnmatch.fnmatch(lowered, pattern) or fnmatch.fnmatch(name, pattern):
            return "the file name matches a known credential pattern"
    return None


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def check_readable(path: str | Path) -> tuple[Path | None, str | None]:
    """Resolve ``path`` and decide whether the read-only tools may touch it.

    Returns ``(resolved_path, None)`` or ``(None, reason)``.
    """
    try:
        # resolves symlinks, so a link cannot be used to hop into ~/.ssh
        p = Path(path).expanduser().resolve()
    except Exception as exc:
        return None, f"could not resolve that path ({exc})"

    reason = _sensitive_reason(p)
    if reason:
        return None, f"refused to read {p} — {reason}. Ask the user to open it themselves."

    home = Path.home().resolve()
    if not _within(p, home) and not _flag(READ_ESCAPE_ENV):
        return None, (
            f"refused to read {p} — it is outside your home folder. "
            f"Set {READ_ESCAPE_ENV}=1 if that is really wanted."
        )
    return p, None


def check_writable(path: str | Path) -> tuple[Path | None, str | None]:
    """Resolve ``path`` and decide whether ``write_file`` may create it."""
    try:
        p = Path(path).expanduser().resolve()
    except Exception as exc:
        return None, f"could not resolve that path ({exc})"

    name = p.name
    if name in OWN_SECRET_NAMES or ".jarvis_token" in name:
        return None, f"refused to write {p} — it holds Jarvis's own credentials."
    reason = _sensitive_reason(p)
    if reason:
        return None, f"refused to write {p} — {reason}."

    home = Path.home().resolve()
    if not _within(p, home) and not _flag(WRITE_ESCAPE_ENV):
        return None, (
            f"refused to write {p} — it is outside your home folder. "
            f"Set {WRITE_ESCAPE_ENV}=1 if that is really wanted."
        )
    return p, None


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

# Values that look like credentials, wherever they appear in tool output.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(sk-[A-Za-z0-9_\-]{8})[A-Za-z0-9_\-]{6,}"), r"\1…[redacted]"),
    (re.compile(r"\b(sk_(?:live|test)_[A-Za-z0-9]{4})[A-Za-z0-9]{6,}"), r"\1…[redacted]"),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{4})[A-Za-z0-9]{20,}"), r"\1…[redacted]"),
    (re.compile(r"\b(xox[baprs]-[A-Za-z0-9-]{4})[A-Za-z0-9-]{10,}"), r"\1…[redacted]"),
    (re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{12,}"), r"\1…[redacted]"),
    (re.compile(r"\b(AIza[0-9A-Za-z\-_]{6})[0-9A-Za-z\-_]{10,}"), r"\1…[redacted]"),
    (re.compile(r"(-----BEGIN [A-Z ]*PRIVATE KEY-----)[\s\S]*?(-----END [A-Z ]*PRIVATE KEY-----)"),
     r"\1…[redacted]\2"),
    (re.compile(r"(?i)\b(bearer\s+|token[=:]\s*|api[_-]?key[\"']?\s*[=:]\s*[\"']?)([A-Za-z0-9._\-]{12,})"),
     r"\1[redacted]"),
    (re.compile(r"(?i)(://[^:/\s]+:)([^@/\s]{3,})(@)"), r"\1[redacted]\3"),
)

MAX_REDACTION_SOURCE = 200_000  # never scan an unbounded blob


def redact(text: str) -> str:
    """Strip credential-shaped substrings out of tool output."""
    if not text:
        return text
    out = text
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out


def redact_result(result: dict[str, Any]) -> dict[str, Any]:
    """Redact the human-facing ``result`` field of a tool result, in place-safe way."""
    if isinstance(result.get("result"), str):
        result = {**result, "result": redact(result["result"][:MAX_REDACTION_SOURCE])}
    return result
