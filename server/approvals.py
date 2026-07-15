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
# SGR/CSI escapes, stripped HERE rather than relying on clean_pane: the hook
# path scrapes the pane RAW (colour kept for the phone's terminal view), and
# colour codes in front of the digits made every menu line unparseable, so the
# call said "Claude needs your permission" with no options. clean_pane is no
# fix either: it strips the SELECTED row ("❯ 1. Yes") as composer chrome.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def parse_options(pane_text: str) -> list[dict]:
    plain = _ANSI.sub("", pane_text or "")
    lines = plain.splitlines()
    # With a live footer at the bottom, only the block directly ABOVE it is the
    # menu; numbered lines higher in the scrollback (an earlier answered menu,
    # output that quotes one) must not leak into the options.
    footer = _footer_line_index(lines)
    if footer is not None:
        lines = lines[max(0, footer - _MENU_WINDOW_LINES):footer]
        plain = "\n".join(lines)
    opts: list[dict] = []
    for line in lines:
        m = _MENU_LINE.match(line)
        if m:
            opts.append({"key": m.group(1), "label": m.group(2).strip()})
    if len(opts) >= 2:
        return opts
    if _YESNO.search(plain):
        return [{"key": "y", "label": "Yes"}, {"key": "n", "label": "No"}]
    return []


# Footer hints Claude Code shows ONLY under a live interactive prompt (the
# permission menu, AskUserQuestion, trust dialog). Fallback approval builders
# gate on these so a numbered list inside ordinary OUTPUT (a plan, a summary)
# can never become a phantom approval card with buttons that press digits
# into an idle composer.
_PROMPT_FOOTERS = ("esc to cancel", "enter to select", "enter to confirm",
                   "tab to amend", "arrow keys to navigate")

# How close to the pane's bottom the footer must sit (in non-empty lines), and
# how many lines directly above it the menu may span. A session whose OUTPUT
# merely QUOTES footer text (someone developing these prompts, a pasted doc)
# has more content below the quote; a real prompt's footer is the last chrome
# on screen.
_FOOTER_TAIL_LINES = 3
_MENU_WINDOW_LINES = 30


def _footer_line_index(lines: list[str]) -> int | None:
    """Index of the footer line, ONLY if it sits within the last
    _FOOTER_TAIL_LINES non-empty lines of the pane; None otherwise."""
    non_empty = [i for i, l in enumerate(lines) if l.strip()]
    if not non_empty:
        return None
    tail = non_empty[-_FOOTER_TAIL_LINES:]
    for i in reversed(tail):
        low = lines[i].lower()
        if any(m in low for m in _PROMPT_FOOTERS):
            return i
    return None


def pane_shows_live_prompt(pane_text: str) -> bool:
    plain = _ANSI.sub("", pane_text or "")
    return _footer_line_index(plain.splitlines()) is not None


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

    def drop_for(self, cwd: str) -> list[str]:
        """Drop every active approval for ``cwd`` (its terminal is gone, e.g.
        just closed from the phone), matching by cwd the same rstrip-normalized
        way active_for does. Returns the dropped approval_ids so the caller can
        tell the phone each one is now stale."""
        target = (cwd or "").rstrip("/")
        stale = [k for k, v in self._by_id.items() if v["cwd"].rstrip("/") == target]
        for k in stale:
            self._by_id.pop(k, None)
        return stale
