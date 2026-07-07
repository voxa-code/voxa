"""Structured approvals: turn an on-screen Claude Code prompt into an object the
phone can render as buttons. Parsing is best-effort by design: when the pane
does not look like a menu or a y/n question we return nothing and the caller
falls back to today's plain spoken summary. Voxa observes prompts; it never
blocks tools (the hook always exits 0), so injecting the chosen key into the
terminal is the ONLY actuation path.
"""
from __future__ import annotations

import os
import re
import time
import uuid

_MENU_LINE = re.compile(r"^\s*[>❯]?\s*(\d+)[.)]\s+(\S.*)$")
_YESNO = re.compile(r"\((y)/(n)\)|\[(y)/(n)\]|\((yes)/(no)\)", re.IGNORECASE)


def parse_options(pane_text: str) -> list[dict]:
    opts: list[dict] = []
    for line in (pane_text or "").splitlines():
        m = _MENU_LINE.match(line)
        if m:
            opts.append({"key": m.group(1), "label": m.group(2).strip()})
    if len(opts) >= 2:
        return opts
    if _YESNO.search(pane_text or ""):
        return [{"key": "y", "label": "Yes"}, {"key": "n", "label": "No"}]
    return []


def build_approval(cwd: str, summary: str, pane_text: str, tool: str = "") -> dict | None:
    options = parse_options(pane_text)
    if not options:
        return None
    return {
        "approval_id": uuid.uuid4().hex[:12],
        "cwd": cwd or "",
        "project": os.path.basename((cwd or "").rstrip("/")) or cwd or "",
        "summary": summary or "",
        "tool": tool or "",
        "options": options,
        "created_at": time.time(),
    }


def build_action_approval(cwd: str, summary: str, tool: str, action: dict,
                          options: list[dict] | None = None) -> dict:
    """A SYNTHETIC approval for a server-side action (e.g. a git commit) that
    has no on-screen prompt behind it, so build_approval (which parses pane
    text) cannot produce it. Same shape, so the store, the phone card, and the
    queue/push wiring treat it identically; the extra ``action`` dict is what
    the resolution paths dispatch INSTEAD of pressing a key into a pane."""
    return {
        "approval_id": uuid.uuid4().hex[:12],
        "cwd": cwd or "",
        "project": os.path.basename((cwd or "").rstrip("/")) or cwd or "",
        "summary": summary or "",
        "tool": tool or "",
        "options": options or [{"key": "y", "label": "Approve"},
                               {"key": "n", "label": "Cancel"}],
        "created_at": time.time(),
        "action": dict(action or {}),
    }


class ApprovalStore:
    """Active approvals, one per cwd (a fresh prompt supersedes a stale one)."""

    def __init__(self):
        self._by_id: dict[str, dict] = {}

    def put(self, approval: dict) -> None:
        stale = [k for k, v in self._by_id.items() if v["cwd"] == approval["cwd"]]
        for k in stale:
            self._by_id.pop(k, None)
        self._by_id[approval["approval_id"]] = approval

    def get(self, approval_id: str) -> dict | None:
        return self._by_id.get(approval_id)

    def active_for(self, cwd: str) -> dict | None:
        """Find the active approval for ``cwd``, matching by cwd with a trailing
        slash tolerated on either side. Kept in sync with the exact comparison
        ws_session.py's approval_decision handler uses, so the tap path (phone
        button) and the voice path (resolve_approval) agree on which prompt a
        decision is allowed to actuate."""
        target = (cwd or "").rstrip("/")
        for v in self._by_id.values():
            if v["cwd"].rstrip("/") == target:
                return v
        return None

    def latest(self) -> dict | None:
        vals = sorted(self._by_id.values(), key=lambda v: v["created_at"])
        return vals[-1] if vals else None

    def resolve(self, approval_id: str) -> dict | None:
        return self._by_id.pop(approval_id, None)
