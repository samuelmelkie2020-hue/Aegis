"""The Aegis agent loop: talks to any OpenAI-compatible LLM with tool calling,
or falls back to a built-in demo brain when no key is configured."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from . import approvals
from .config import is_demo_mode
from .tools import DANGEROUS_TOOLS, TOOLS, execute_tool

MAX_TOOL_ROUNDS = 8
TURN_BUDGET_SECONDS = 300   # give up on one request after this long
EmitFn = Callable[[str, dict], None]


class TurnCancelled(RuntimeError):
    """Raised when the turn must stop: the client left, or the budget ran out."""


class Turn:
    """Per-request stop conditions.

    The queue bridge in :mod:`aegis.server` flips ``stop`` when the browser
    goes away, and the deadline keeps one slow tool from pinning a worker (and
    blocking every other request that needs the same config) forever.
    """

    def __init__(self, stop_event: Any = None, budget: float = TURN_BUDGET_SECONDS):
        self.stop_event = stop_event
        self.deadline = time.monotonic() + budget

    def check(self) -> None:
        if self.stop_event is not None and self.stop_event.is_set():
            raise TurnCancelled("the browser left before this turn finished")
        if time.monotonic() > self.deadline:
            raise TurnCancelled("this request took too long and was stopped")

SYSTEM_PROMPT = """You are {name}, a personal AI assistant that runs on the user's computer — like Aegis.
You can SEE the screen and CONTROL the computer with tools: run commands, open apps, type, click, manage files, read the clipboard and report system status.

Personality: calm, capable, a little witty — but always concise. Speak like a trusted aide.

Rules:
- When the user asks anything about what is on their screen, what they are doing, what a window/error says — call see_screen FIRST and then answer from the image.
- To operate the UI: see_screen to find coordinates -> click -> type_text / press_keys.
- Prefer safe, reversible actions. Never run destructive commands (rm -rf, formatting disks, deleting system files, registry wipes) unless the user explicitly and clearly asks for exactly that.
- Summarize what you did in one or two sentences; don't dump raw tool output unless asked.
- Answer in the same language the user writes in.
- If a tool fails, explain briefly and suggest the fix.
"""


# ---------------------------------------------------------------------------
# OpenAI-compatible client
# ---------------------------------------------------------------------------

class ChatClient:
    def __init__(self, base_url: str, api_key: str, model: str):
        self.url = (base_url or "").rstrip("/") + "/chat/completions"
        self.api_key = api_key or ""
        self.model = model

    def complete(self, messages: list[dict], tools: list[dict]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.4,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        resp = httpx.post(self.url, json=payload, headers=headers, timeout=240)
        if resp.status_code != 200:
            raise RuntimeError(f"LLM error {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"Unexpected LLM response: {str(data)[:500]}") from exc
        # normalize
        out: dict[str, Any] = {"role": "assistant", "content": msg.get("content") or ""}
        if msg.get("tool_calls"):
            out["tool_calls"] = [
                {
                    "id": tc.get("id") or f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"].get("arguments") or "{}",
                    },
                }
                for i, tc in enumerate(msg["tool_calls"])
            ]
        return out


def _parse_args(raw: str) -> dict:
    try:
        args = json.loads(raw or "{}")
        return args if isinstance(args, dict) else {}
    except Exception:
        return {}


def _tool_message(call_id: str, name: str, result: dict) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "name": name, "content": result.get("result", "")}


def _is_image_message(message: dict) -> bool:
    content = message.get("content")
    return isinstance(content, list) and any(
        isinstance(part, dict) and part.get("type") == "image_url" for part in content
    )


def _image_message(image_b64: str) -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "[Aegis captured this screenshot of the user's screen]"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
        ],
    }


# ---------------------------------------------------------------------------
# The agent turn
# ---------------------------------------------------------------------------

def run_agent_turn(
    user_message: str,
    history: list[dict],
    cfg: dict,
    emit: EmitFn,
    session_token: str = "",
    turn: Turn | None = None,
) -> str:
    """Run one user turn (with tool loops) and return the final text reply.

    ``session_token`` binds any pending approvals to the caller, so only the
    session that was shown an approval card can redeem it.
    """
    turn = turn or Turn()
    turn.check()

    approval = _take_approval(user_message, session_token)
    if approval is not None:
        return _resume_after_approval(approval, history, cfg, emit, turn)

    name = cfg.get("assistant_name") or "Aegis"
    safe_mode = bool(cfg.get("safe_mode", True))

    if is_demo_mode(cfg):
        return _demo_turn(user_message, cfg, emit, session_token)

    llm = cfg.get("llm", {})
    client = ChatClient(llm.get("base_url", ""), llm.get("api_key", ""), llm.get("model", ""))

    # clean history: only role/content strings (and no protocol notices)
    clean_history = _clean_history(history)

    messages: list[dict] = (
        [{"role": "system", "content": SYSTEM_PROMPT.format(name=name)}]
        + clean_history[-20:]
        + [{"role": "user", "content": user_message}]
    )

    pending_approval = False
    screenshots_sent = 0
    for _ in range(MAX_TOOL_ROUNDS):
        turn.check()
        emit("status", {"message": "Thinking…"})
        try:
            msg = client.complete(messages, TOOLS)
        except TurnCancelled:
            raise  # abandoning the turn is not a model error
        except Exception as exc:
            return _friendly_model_error(exc, llm)

        if not msg.get("tool_calls"):
            text = msg.get("content") or "(no response)"
            emit("message", {"text": text})
            return text

        messages.append(
            {
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": msg["tool_calls"],
            }
        )

        for call in msg["tool_calls"]:
            turn.check()
            fn = call.get("function", {})
            tool_name = fn.get("name", "")
            args = _parse_args(fn.get("arguments", "{}"))
            emit("tool_start", {"name": tool_name, "args": args})

            if safe_mode and tool_name in DANGEROUS_TOOLS:
                description = _describe_action(tool_name, args)
                # Register what we proposed; the browser only gets the id.
                item = approvals.register(tool_name, args, description, session_token)
                emit(
                    "approval",
                    {
                        "approval_id": item.id,
                        "name": tool_name,
                        "args": args,
                        "description": description,
                    },
                )
                messages.append(_tool_message(call.get("id", ""), tool_name, {
                    "ok": False,
                    "result": "Action paused — waiting for the user to approve it in the interface.",
                }))
                pending_approval = True
                continue

            result = execute_tool(tool_name, args)
            emit("tool_result", {"name": tool_name, "result": result.get("result", ""), "ok": result.get("ok", True)})
            if result.get("image"):
                emit("screenshot", {"data": result["image"]})
            messages.append(_tool_message(call.get("id", ""), tool_name, result))
            if result.get("image"):
                # Only the freshest screenshot stays in context: each one is a
                # large base64 PNG that would otherwise be re-sent on every
                # subsequent round. Older ones are dropped, not accumulated.
                messages = [m for m in messages if not _is_image_message(m)]
                messages.append(_image_message(result["image"]))
                screenshots_sent += 1

        if pending_approval:
            break

    if pending_approval:
        text = "I've paused — I need your approval before acting on the computer. Use the approval card above."
        emit("message", {"text": text})
        return text
    text = "I ran into a loop while working on that — could you rephrase the request?"
    emit("message", {"text": text})
    return text


# ---------------------------------------------------------------------------
# Resuming a turn after the user approved (or denied) an action
# ---------------------------------------------------------------------------

RESUME_PREFIX = "(approval-result)"


def _take_approval(user_message: str, session_token: str):
    """If this turn is the redeem notice for an approval, consume it.

    Returns the redeemed :class:`approvals.PendingApproval`, an
    ``("error", reason)`` tuple, or None when the turn is a normal chat turn.
    """
    text = (user_message or "").strip()
    if not text.startswith(RESUME_PREFIX):
        return None
    payload = text[len(RESUME_PREFIX):].strip()
    if payload.lower().startswith("retry"):
        return None  # a stale notice; treat as ordinary text rather than failing
    try:
        data = json.loads(payload)
    except Exception:
        return None
    approval_id = str(data.get("approval_id") or "")
    if not approval_id:
        return None
    executed, error = approvals.claim_result(approval_id, session_token)
    if error:
        return ("error", error)
    if executed is not None:
        return executed
    item, error = approvals.redeem(approval_id, session_token)
    if item is None:
        return ("error", error or "That approval could not be redeemed.")
    return item


def _result_for(approval: Any) -> dict:
    """The outcome of an approved action, running it only if it has not run.

    ``ExecutedApproval`` means /api/approve already executed it, so its stored
    result is replayed. Re-running here is what used to make an approved
    command fire twice.
    """
    if isinstance(approval, approvals.ExecutedApproval):
        return approval.result
    return execute_tool(approval.name, approval.args)


def _resume_after_approval(
    approval: Any,
    history: list[dict],
    cfg: dict,
    emit: EmitFn,
    turn: Turn | None = None,
) -> str:
    turn = turn or Turn()
    """Run the approved action exactly once and let the model react to it.

    The action is executed here, server-side, with the arguments the server
    itself proposed. The browser never says what to run.
    """
    if isinstance(approval, tuple):  # ("error", reason)
        _, reason = approval
        text = f"That action couldn't be resumed — {reason} Ask me again and I'll re-request it."
        emit("message", {"text": text})
        return text

    name, description = approval.name, approval.description
    result = _result_for(approval)

    emit("tool_start", {"name": name, "args": approval.args})
    emit("tool_result", {"name": name, "result": result.get("result", ""), "ok": result.get("ok", True)})
    if result.get("image"):
        emit("screenshot", {"data": result["image"]})

    if is_demo_mode(cfg):
        verdict = "✅" if result.get("ok") else "⚠"
        text = f"{verdict} {description} — {result.get('result', '(no output)')}"
        emit("message", {"text": text})
        return text

    llm = cfg.get("llm", {})
    client = ChatClient(llm.get("base_url", ""), llm.get("api_key", ""), llm.get("model", ""))
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT.format(name=cfg.get("assistant_name") or "Aegis")},
    ]
    for m in _clean_history(history):
        messages.append(m)
    messages.append({
        "role": "user",
        "content": (
            f"I approved your proposed action ({description}) and it has now run. "
            f"Result:\n{result.get('result', '(no output)')}\n\n"
            "Tell me the outcome in one or two sentences. Do not run it again."
        ),
    })
    if result.get("image"):
        messages.append(_image_message(result["image"]))

    turn.check()
    emit("status", {"message": "Thinking…"})
    try:
        msg = client.complete(messages, TOOLS)
    except TurnCancelled:
        raise
    except Exception as exc:
        return _friendly_model_error(exc, llm)
    text = msg.get("content") or "Done."
    emit("message", {"text": text})
    return text


def _clean_history(history: list[dict]) -> list[dict]:
    """Keep only plain user/assistant text turns from the client."""
    clean = []
    for m in history or []:
        if (
            isinstance(m, dict)
            and m.get("role") in ("user", "assistant")
            and isinstance(m.get("content"), str)
            and not m["content"].startswith(RESUME_PREFIX)
        ):
            clean.append({"role": m["role"], "content": m["content"]})
    return clean


def _friendly_model_error(exc: Exception, llm_cfg: dict) -> str:
    """Turn raw network/API errors into actionable, human guidance."""
    base = (llm_cfg.get("base_url") or "").strip()
    host = base.split("//")[-1].split("/")[0] if base else "the AI provider"
    msg = str(exc)
    low = msg.lower()

    m = re.search(r"LLM error (\d{3})", msg)
    if m:
        code = int(m.group(1))
        if code in (401, 403):
            return (
                f"⚠ I reached {host}, but the **API key was rejected** (HTTP {code}). "
                "Open ⚙️ Settings and re-copy your key, then press **🔌 Test connection**."
            )
        if code == 402:
            return f"⚠ Your {host} account is out of credit (HTTP 402). Top it up and try again."
        if code == 429:
            return f"⚠ {host} is rate-limiting us (HTTP 429). Good news: your setup works — just wait a moment and retry."
        if code == 404:
            return (
                f"⚠ {host} didn't recognize the model or URL (HTTP 404). "
                "Check the **model name** and **API base URL** in ⚙️ Settings."
            )
        return f"⚠ {host} returned an error: {msg[:300]}"

    if any(k in low for k in ("ssl", "tls", "eof", "connection", "timed out", "timeout", "resolve", "refused", "reset", "proxy", "network")):
        return (
            f"⚠ **Can't reach {host} at all** — something on the network is blocking the connection. "
            "Your API key is fine; this is a network problem.\n\n"
            "Fixes to try:\n"
            "1. Press **🔌 Test connection** in ⚙️ Settings to confirm.\n"
            "2. Try a different **Provider** in Settings — some networks block only some sites "
            "(if you're using the chat preview inside Arena instead of the app on your own PC, "
            "that preview is *always* blocked from AI sites — run Aegis on your PC instead).\n"
            "3. Use a VPN if your ISP blocks AI sites.\n"
            "4. Or pick **Ollama (local)** — a free AI that runs offline, no internet needed."
        )
    return f"⚠ I couldn't reach the AI model: {msg[:300]}"


def _describe_action(tool_name: str, args: dict) -> str:
    if tool_name == "run_command":
        return f"Run command: `{args.get('command', '')}`"
    if tool_name == "open_app":
        return f"Open app: **{args.get('app', '')}**"
    if tool_name == "type_text":
        t = args.get("text", "")
        return f"Type: “{t[:80]}{'…' if len(t) > 80 else ''}”"
    if tool_name == "press_keys":
        return f"Press keys: {'+'.join(args.get('keys', []))}"
    if tool_name == "click":
        return f"Click at ({args.get('x')}, {args.get('y')})"
    if tool_name == "write_file":
        return f"Write file: {args.get('path', '')}"
    if tool_name == "clipboard":
        return "Change the clipboard"
    return tool_name


# ---------------------------------------------------------------------------
# Demo brain intent matching — works with no API key at all
# ---------------------------------------------------------------------------

# Stops an "open X" capture from swallowing a trailing clause or the second half
# of a sentence: "open chrome and go to youtube.com" -> "chrome".
_APP_STOP = r"(?=\s+(?:and|then|to|so|because|with|for)\b|[,.!?;]|$)"


def _match_app(text: str) -> str | None:
    match = re.search(r"\bopen\s+['\"]?([\w .\-]+?)" + _APP_STOP, text, re.IGNORECASE)
    if not match:
        return None
    app = match.group(1).strip(" .\"'")
    return app or None


def _match_command(text: str) -> str | None:
    """Return the command after "run"/"execute", if that is what was asked.

    A trailing "and tell me ..." is dropped; the command itself keeps its
    arguments and flags ("git status --short").
    """
    match = re.search(
        r"\b(?:run|execute)\s+(.+?)(?=\s+(?:and then|and tell|then tell|then)\b|[.!?]\s*$|$)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    return match.group(1).strip(" .\"'") or None


def _match_folder(text: str) -> str | None:
    low = text.lower()
    if not re.search(r"\b(list|show)\b.*\b(files|folder|directory|documents|downloads|desktop)\b", low):
        return None
    from pathlib import Path

    for key, sub in (("desktop", "Desktop"), ("downloads", "Downloads"), ("documents", "Documents")):
        if key in low:
            return str(Path.home() / sub)
    return None

def _demo_turn(user_message: str, cfg: dict, emit: EmitFn, session_token: str = "") -> str:
    name = cfg.get("assistant_name") or "Aegis"
    text = (user_message or "").strip()
    low = text.lower()

    def run_safe_tool(tool: str, args: dict, phrase: str) -> str:
        if cfg.get("safe_mode", True) and tool in DANGEROUS_TOOLS:
            description = _describe_action(tool, args)
            item = approvals.register(tool, args, description, session_token)
            emit("approval", {
                "approval_id": item.id,
                "name": tool,
                "args": args,
                "description": description,
            })
            return f"I'd like to {phrase}, but I need your approval first — use the approval card above."
        emit("tool_start", {"name": tool, "args": args})
        result = execute_tool(tool, args)
        emit("tool_result", {"name": tool, "result": result.get("result", ""), "ok": result.get("ok", True)})
        if result.get("image"):
            emit("screenshot", {"data": result["image"]})
        return result.get("result", "")

    # Intent order matters: an explicit action verb ("run", "open") wins over a
    # keyword that merely appears in the object ("git status", "open chrome and
    # go to youtube.com"). Keywords only decide when no verb is present.
    app = _match_app(text)
    command = _match_command(text)
    folder = _match_folder(text)
    asks_screen = re.search(r"\b(screen|what am i|what'?s open|what is open|see my|looking at)\b", low)
    asks_system = re.search(
        r"\b(system|status|cpu|memory|ram|disk|battery|uptime|how is my (pc|computer))\b", low
    )
    asks_help = re.search(r"\b(help|what can you do|capabilities|who are you|hello|hi|hey)\b", low)

    reply: str
    if asks_screen:
        reply = run_safe_tool("see_screen", {}, "look at your screen")
        if "attached" in reply or "captured" in reply.lower():
            reply = (
                "I've captured your screen — you can see it in the screen panel. "
                "(Demo brain: connect a vision model in Settings and I'll describe and use what I see.)"
            )
    elif command is not None:
        reply = run_safe_tool("run_command", {"command": command}, f"run `{command}`")
    elif app is not None:
        reply = run_safe_tool("open_app", {"app": app}, f"open **{app}**")
    elif asks_system:
        info = run_safe_tool("system_info", {}, "check system status")
        reply = f"Here's your system status:\n\n{info}"
    elif folder is not None:
        reply = run_safe_tool("list_files", {"path": folder}, "list your files")
    elif asks_help:
        reply = (
            f"At your service — I'm {name}. Right now I'm running my built-in **demo brain**, "
            "but I can already:\n\n"
            "• 👀 **See your screen** — ask “what's on my screen?”\n"
            "• 💻 **Run commands** — “run ipconfig” or “execute ls”\n"
            "• 🚀 **Open apps** — “open notepad”\n"
            "• 📂 **Browse files** — “list my downloads”\n"
            "• 🖥 **System status** — “how is my pc?”\n\n"
            "Connect an AI model in ⚙️ Settings (OpenAI, Gemini, Groq, OpenRouter or local Ollama) "
            "and I become a full assistant that can reason about your screen and chain actions together."
        )
    else:
        reply = (
            "That's a bit beyond my built-in demo brain. Try “what's on my screen?”, "
            "“run <command>”, “open <app>” or “help” — or connect an AI model in ⚙️ Settings "
            "so I can think for real."
        )

    emit("message", {"text": reply})
    return reply
