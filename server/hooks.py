"""Claude Code hook integration.

The reliable, terminal-agnostic way to know a Claude session finished or needs
input is Claude Code's own lifecycle hooks (Stop / Notification), not screen
scraping. We install a global hook in ~/.claude/settings.json that POSTs the
hook's stdin JSON to the local Voxa server's /hook endpoint; the server then
rings the phone (or speaks, if a line is attached) exactly like the watcher did.

This module is pure/host-agnostic so it is fully unit-testable:
  - last_assistant_text: pull a spoken summary from a transcript JSONL
  - route_hook: decide what (if anything) to announce for a hook event
  - merge_hook / install_claude_hook / uninstall_claude_hook: edit settings.json
"""

from __future__ import annotations

import json
import os
import shlex

# Identifies OUR hook entries inside settings.json so install is idempotent and
# uninstall is precise (it is a shell comment on the command, ignored at runtime).
MARKER = "voxa-hook"

# Events we register globally. Stop = finished a turn; Notification = needs
# permission / input; UserPromptSubmit = turn start (lets us time the turn so a
# quick interactive exchange doesn't ring the phone); PreToolUse = records the
# pending tool call's context (session-scoped) so a later Notification can be
# turned into a structured approval prompt.
HOOK_EVENTS = ("Stop", "Notification", "UserPromptSubmit", "PreToolUse")


def last_assistant_text(transcript_path: str, max_len: int = 240) -> str:
    """Return the last assistant message's text from a Claude transcript JSONL, as a
    short spoken summary. Empty string if unreadable or none found."""
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    last = ""
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                msg = obj.get("message") or {}
                content = msg.get("content")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = " ".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                else:
                    text = ""
                text = " ".join(text.split())
                if text:
                    last = text
    except OSError:
        return ""
    return last[:max_len]


def _label_for(cwd: str) -> str:
    return os.path.basename((cwd or "").rstrip("/"))


def route_hook(body: dict, *, turn_start: dict, hook_last: dict, now: float,
               min_seconds: float = 30.0, cooldown: float = 8.0,
               read_transcript=last_assistant_text,
               pre_tool: dict | None = None) -> tuple[str | None, str]:
    """Decide the announcement for a Claude Code hook event, or None to stay silent.

    Mutates ``turn_start`` (per-session turn start time) and ``hook_last`` (per-session
    last-announced time, for debounce). Pure otherwise, so it is easy to unit test.

    Returns ``(msg, kind)``: ``kind`` is ``"finish"`` for Stop, ``"needs_input"`` for
    Notification, and ``""`` otherwise (including every silent/None case).

    - UserPromptSubmit -> record turn start, announce nothing.
    - PreToolUse -> record the pending tool's name and a capped input summary in
      ``pre_tool`` (keyed by session_id), announce nothing. Never debounces, never
      rings; it is pure bookkeeping for a later Notification to draw context from.
    - Stop -> announce "finished" UNLESS the turn was shorter than ``min_seconds`` (a
      quick interactive exchange) or within the debounce ``cooldown``.
    - Notification -> announce "needs input" (debounced).
    """
    event = body.get("hook_event_name") or body.get("hook_event") or ""
    session = body.get("session_id") or ""
    label = _label_for(body.get("cwd") or "")

    if event == "UserPromptSubmit":
        turn_start[session] = now
        return None, ""

    if event == "PreToolUse":
        if pre_tool is not None:
            pre_tool[session] = {
                "tool_name": body.get("tool_name") or "",
                "input_summary": str(body.get("tool_input") or "")[:200],
            }
        return None, ""

    if event == "Stop":
        start = turn_start.pop(session, None)
        if start is not None and (now - start) < min_seconds:
            return None, ""  # quick interactive turn: don't call
        if now - hook_last.get(session, float("-inf")) < cooldown:
            return None, ""  # debounce repeated stops
        hook_last[session] = now
        summary = read_transcript(body.get("transcript_path", ""))
        msg = f"{label or 'a session'} finished" + (f": {summary}" if summary else "")
        return msg, "finish"

    if event == "Notification":
        if now - hook_last.get(session, float("-inf")) < cooldown:
            return None, ""
        hook_last[session] = now
        summary = (body.get("message") or "").strip()
        msg = f"{label or 'Claude'} needs input" + (f": {summary}" if summary else "")
        return msg, "needs_input"

    return None, ""


def others_mid_turn(turn_start: dict, session_id: str, now: float,
                    max_age: float | None = None) -> bool:
    """True when ANOTHER Claude session has an open turn (its UserPromptSubmit
    arrived, no Stop yet): a finish ring should be HELD until everything is
    done, so one long-running fleet doesn't call the phone once per session
    saying "finished" while work is clearly still going.

    Entries older than ``max_age`` seconds (VOXA_OPEN_TURN_MAX_SECONDS, default
    3600) are pruned as stale, so a session killed mid-turn cannot silence
    finish rings forever. Mutates ``turn_start`` (the prune), like route_hook."""
    if max_age is None:
        try:
            max_age = float(os.environ.get("VOXA_OPEN_TURN_MAX_SECONDS", "3600"))
        except (TypeError, ValueError):
            max_age = 3600.0
    for sid in [s for s, t0 in turn_start.items() if now - t0 >= max_age]:
        turn_start.pop(sid, None)
    return any(s != session_id for s in turn_start)


def drain_held_finishes(held: dict, msg: str, max_len: int = 500) -> str:
    """Fold finish messages held while other sessions were mid-turn into the
    final ring's message, oldest first, capped for speech. Mutates ``held``
    (clears it). With nothing held, returns ``msg`` unchanged."""
    earlier = [m for m, _cwd in held.values() if m and m.strip()]
    held.clear()
    if not earlier:
        return msg
    combined = f"All tasks are done. {msg}. Earlier: " + "; ".join(earlier)
    return combined[:max_len]


# --------------------------------------------------------------------------
# settings.json install / uninstall
# --------------------------------------------------------------------------


def hook_command(url: str, event: str | None = None) -> str:
    """The shell command Claude Code runs for the hook: POST the event's stdin JSON to
    the Voxa server. `; true` keeps the hook exit code 0 (a down server never blocks
    Claude); the trailing comment is our MARKER for idempotent install/uninstall.

    ``event`` is None or one of Stop/Notification/UserPromptSubmit -> the original
    fire-and-forget form: stdout AND stderr swallowed (>/dev/null 2>&1). This is load
    bearing for UserPromptSubmit specifically, since Claude Code injects that hook's
    stdout straight into the conversation context; leaking anything there is a bug.

    ``event == "PreToolUse"`` -> stdout is left connected so the server's JSON body
    (a deny decision, when server.danger flags the pending command) reaches Claude
    Code; stderr is still suppressed and the command still ends in `; true` so a down
    server never blocks the tool call -- the decision comes from the JSON body, not
    the exit code."""
    quoted = shlex.quote(url)
    if event == "PreToolUse":
        return (f"curl -s -m 5 -X POST {quoted} "
                f"-H 'Content-Type: application/json' --data-binary @- 2>/dev/null "
                f"; true  # {MARKER}")
    return (f"curl -s -m 5 -X POST {quoted} "
            f"-H 'Content-Type: application/json' --data-binary @- >/dev/null 2>&1 "
            f"; true  # {MARKER}")


def _is_ours(entry: dict) -> bool:
    try:
        return any(MARKER in (h.get("command") or "") for h in entry.get("hooks", []))
    except (AttributeError, TypeError):
        return False


def merge_hook(settings: dict, url: str) -> dict:
    """Return settings with our Stop/Notification/UserPromptSubmit/PreToolUse hooks
    merged in, replacing any prior Voxa entries and preserving every other hook. Each
    event gets its own command (see hook_command): PreToolUse keeps stdout connected
    so a deny decision can reach Claude Code, the rest keep the original fire-and-
    forget form. Pure."""
    out = dict(settings or {})
    hooks = {k: list(v) for k, v in (out.get("hooks") or {}).items()}
    for event in HOOK_EVENTS:
        entry = {"hooks": [{"type": "command", "command": hook_command(url, event)}]}
        kept = [e for e in hooks.get(event, []) if not _is_ours(e)]
        kept.append(entry)
        hooks[event] = kept
    out["hooks"] = hooks
    return out


def remove_hook(settings: dict) -> dict:
    """Return settings with all Voxa hook entries removed; other hooks untouched."""
    out = dict(settings or {})
    hooks = {k: [e for e in v if not _is_ours(e)] for k, v in (out.get("hooks") or {}).items()}
    hooks = {k: v for k, v in hooks.items() if v}  # drop now-empty event lists
    if hooks:
        out["hooks"] = hooks
    else:
        out.pop("hooks", None)
    return out


def _load(settings_path: str) -> dict:
    """Load settings.json. Missing/empty -> {}. A malformed EXISTING file RAISES rather
    than returning {}, so callers never overwrite (and destroy) a real config they
    couldn't parse."""
    if not os.path.exists(settings_path):
        return {}
    with open(settings_path, "r", encoding="utf-8") as f:
        raw = f.read()
    if not raw.strip():
        return {}
    data = json.loads(raw)  # raises ValueError on malformed -> do NOT clobber
    if not isinstance(data, dict):
        raise ValueError("settings.json is not a JSON object")
    return data


def _save(settings_path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(settings_path) or ".", exist_ok=True)
    tmp = settings_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, settings_path)


def install_claude_hook(settings_path: str, url: str) -> None:
    """Idempotently install the Voxa hooks into a Claude Code settings.json."""
    _save(settings_path, merge_hook(_load(settings_path), url))


def uninstall_claude_hook(settings_path: str) -> None:
    """Remove the Voxa hooks from a Claude Code settings.json (leaves others)."""
    if os.path.exists(settings_path):
        _save(settings_path, remove_hook(_load(settings_path)))


def default_settings_path() -> str:
    return os.path.expanduser("~/.claude/settings.json")


def hook_url(host: str, port: int, token: str) -> str:
    """The local /hook URL the installed hook POSTs to (always loopback)."""
    from urllib.parse import quote
    return f"http://127.0.0.1:{port}/hook?token={quote(token, safe='')}"


# --------------------------------------------------------------------------
# PreToolUse danger gate: catch Claude Code autonomously about to run something
# destructive, and deny it so it stops and asks the user instead. This is
# separate from server.danger's existing use gating what the USER dictates;
# here we inspect the pending tool call Claude itself is about to make.
# --------------------------------------------------------------------------


def pre_tool_command_text(body: dict) -> str:
    """Extract the pending command text from a PreToolUse hook body's tool_input, for
    danger classification. tool_input is usually a dict (e.g. {"command": "..."} for
    Bash) but may also arrive as a plain string; handle both, pure/host-agnostic."""
    tool_input = body.get("tool_input")
    if isinstance(tool_input, dict):
        return str(tool_input.get("command") or tool_input)
    if isinstance(tool_input, str):
        return tool_input
    return ""


def pre_tool_deny_response(body: dict) -> dict | None:
    """For a PreToolUse hook body, return Claude Code's PreToolUse deny JSON if
    server.danger.classify() flags the pending command, else None. Kept pure (no
    FastAPI) so it is unit-testable without spinning up the app."""
    from server import danger
    reason = danger.classify(pre_tool_command_text(body))
    if not reason:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"Voxa blocked this: {reason}. It {reason}; "
                f"ask the user to confirm before running it."
            ),
        }
    }
