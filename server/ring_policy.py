"""Decides WHEN a finish may ring the phone.

Claude Code fires a Stop hook at every main-loop turn boundary, including the
many boundaries mid-task when background subagents wake the loop; ringing each
one calls the phone a dozen times for one task. Policy:
- needs_input rings immediately (a human is blocking) and cancels any pending
  finish ring for that session (the input request supersedes it).
- finish waits a quiet window (8s by default, VOXA_RING_QUIET_SECONDS); any
  same-session hook activity inside it means the session is still working, so
  the pending ring is cancelled (a later Stop reschedules with the fresher
  summary).
- Busy panes (running background tasks visible in the driven pane) suppress
  the finish outright; the final Stop after the last agent has no marker.
Fail-open: errors inside a delayed ring are logged, never raised into /hook.

Instant mode (the DEFAULT; set VOXA_RING_INSTANT=0 to disable) trades the
quiet window for latency: finish rings the phone right away instead of
waiting to see if the session keeps going. To cover for the false positives
that trade-off invites, any same-session hook activity that arrives within a
short cancel window (VOXA_RING_CANCEL_WINDOW, default 15s) after that ring
fires a cancel push that stops the still-ringing (or, harmlessly,
already-answered) phone.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time

DEFAULT_BUSY_MARKERS = ("background task", "esc to interrupt", "tasks running",
                        "still running")


def pane_is_busy(pane_text: str, markers: tuple[str, ...] | None = None) -> bool:
    """True when the pane text shows work still running (so a Stop is a turn
    boundary, not a task end). Markers overridable via VOXA_BUSY_MARKERS."""
    if markers is None:
        raw = os.environ.get("VOXA_BUSY_MARKERS", "")
        parsed = tuple(m.strip().lower() for m in raw.split(",") if m.strip())
        markers = parsed or DEFAULT_BUSY_MARKERS
    low = (pane_text or "").lower()
    return any(m in low for m in markers)


class RingScheduler:
    """Gates finish rings behind a quiet window so one task rings once, not once
    per turn boundary. needs_input stays immediate. Public API: note_activity,
    finish, needs_input."""

    def __init__(self, report, quiet_seconds: float | None = None, cancel=None):
        self._report = report
        if quiet_seconds is None:
            # Hook traffic (UserPromptSubmit/PreToolUse) arrives within a couple
            # of seconds when the session keeps working, and note_activity
            # already cancels a pending ring the instant it does; a long window
            # only delayed genuine finishes, so 8s is plenty of margin.
            quiet_seconds = float(os.environ.get("VOXA_RING_QUIET_SECONDS", "8"))
        self._quiet = quiet_seconds
        self._pending: dict[str, asyncio.Task] = {}
        # Instant mode: an async callable (no args) that stops a still-ringing
        # phone; wired to Notifier.cancel_ring in production. None (the
        # default) keeps instant mode's cancel step a no-op, e.g. in tests
        # that only care about the immediate-ring half of the behavior.
        self._cancel = cancel
        # Instant mode is the DEFAULT (set VOXA_RING_INSTANT=0 to restore the
        # quiet-window behavior): a finish rings the phone immediately, and the
        # cancel window below covers the false positives (same-session activity
        # right after the ring stops the still-ringing phone). ~8s faster
        # finish-to-ring at the cost of an occasional self-cancelling ring.
        self._instant = (os.environ.get("VOXA_RING_INSTANT", "1").strip().lower()
                         not in ("0", "false", ""))
        self._cancel_window = float(os.environ.get("VOXA_RING_CANCEL_WINDOW", "15"))
        # session_id -> monotonic time of an instant-mode finish ring, so a
        # LATER note_activity can tell "this session just rang, and might not
        # really be done" from "this session rang ages ago, unrelated."
        self._recent_rings: dict[str, float] = {}

    def note_activity(self, session_id: str) -> None:
        """More hook traffic for this session means it is still working: drop any
        pending finish ring so it does not fire mid-task. In instant mode, ALSO
        check whether this session rang recently; if so the finish that rang it
        was premature, so fire the fire-and-forget cancel push."""
        task = self._pending.pop(session_id, None)
        if task is not None:
            task.cancel()
        if not self._instant or self._cancel is None:
            return
        rang_at = self._recent_rings.get(session_id)
        if rang_at is None or (time.monotonic() - rang_at) > self._cancel_window:
            return
        self._recent_rings.pop(session_id, None)
        # Fire-and-forget: note_activity is called synchronously from the hook
        # request path and must not block it on a cancel push round trip.
        asyncio.ensure_future(self._fire_cancel())

    async def _fire_cancel(self) -> None:
        # Fail-open: a cancel push failing must never surface anywhere but the
        # log, since it runs detached from any request/response cycle.
        try:
            await self._cancel()
        except Exception:
            logging.exception("instant-ring cancel push failed")

    async def finish(self, session_id: str, msg: str, cwd: str) -> None:
        """Ring for a task end, but only once the session has been quiet for the
        window; a fresh finish replaces any earlier pending one for the session.
        In instant mode, skip the wait entirely: ring right away, and remember
        WHEN so a same-session note_activity soon after can cancel it."""
        if self._instant:
            # A finish that supersedes an earlier one (the session kept going
            # and produced a fresher summary) must not cancel ITSELF: drop any
            # prior record before ringing and recording our own.
            self._recent_rings.pop(session_id, None)
            await self._report(msg, kind="finish", cwd=cwd)
            self._recent_rings[session_id] = time.monotonic()
            return
        if self._quiet <= 0:
            await self._report(msg, kind="finish", cwd=cwd)
            return
        self.note_activity(session_id)

        async def _later():
            try:
                await asyncio.sleep(self._quiet)
                # Commit the ring: leave the cancellable set BEFORE dialing, so a
                # note_activity arriving the instant we start reporting (the
                # session resuming right as the window elapses) cannot cancel a
                # ring already in flight and silently drop a real call. Past this
                # point only a still-sleeping task is cancellable.
                if self._pending.get(session_id) is task:
                    self._pending.pop(session_id, None)
                await self._report(msg, kind="finish", cwd=cwd)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("delayed finish ring failed")
            finally:
                if self._pending.get(session_id) is task:
                    self._pending.pop(session_id, None)

        task = asyncio.ensure_future(_later())
        self._pending[session_id] = task

    async def needs_input(self, session_id: str, msg: str, cwd: str,
                          approval: dict | None = None) -> None:
        """A human is blocking: ring now, and cancel any pending finish for the
        session (the input request supersedes a turn-end summary). Pop any
        instant-mode recent-ring record for the session FIRST, silently (no
        cancel push): note_activity's own supersede check must never race a
        cancel against the needs_input ring that is about to fire for this
        very session."""
        self._recent_rings.pop(session_id, None)
        self.note_activity(session_id)
        await self._report(msg, kind="needs_input", cwd=cwd, approval=approval)


class ErrorBurst:
    """Counts recent error-ish events within a rolling window and reports ONCE
    when a threshold is crossed, so a burst of failures rings once, not on
    every single error. Pure (no tmux/asyncio): timestamps are supplied by the
    caller to ``note_error``, or read from ``now_fn`` (defaults to
    ``time.monotonic``) when omitted, so it is trivially unit-testable with a
    fake clock.

    API: ``note_error(ts=None)`` records an event; ``triggered()`` prunes
    events outside ``window_seconds``, then reports ``True`` once
    ``threshold`` (or more) events remain inside the window. A cooldown (by
    default equal to ``window_seconds``) suppresses repeat reports for the
    SAME burst: once ``triggered()`` has fired, it will not fire again until
    either the burst ends (events drop below threshold, e.g. they age out of
    the window) or the cooldown elapses, whichever comes first.
    """

    def __init__(self, threshold: int = 3, window_seconds: float = 60.0,
                 now_fn=None, cooldown_seconds: float | None = None):
        self._threshold = threshold
        self._window = window_seconds
        self._now = now_fn or time.monotonic
        self._cooldown = window_seconds if cooldown_seconds is None else cooldown_seconds
        self._events: list[float] = []
        self._last_reported: float | None = None

    def note_error(self, ts: float | None = None) -> None:
        """Record an error-ish event at ``ts`` (defaults to ``now_fn()``)."""
        self._events.append(self._now() if ts is None else ts)

    def triggered(self) -> bool:
        """True once >= threshold errors fall inside the trailing window.
        Reports at most once per burst: a burst that ended (dropped below
        threshold) clears the cooldown, so a fresh burst can report right
        away; a burst that persists must wait out the cooldown before it can
        report again."""
        now = self._now()
        cutoff = now - self._window
        self._events = [t for t in self._events if t >= cutoff]
        if len(self._events) < self._threshold:
            self._last_reported = None   # burst over: a new one can report immediately
            return False
        if self._last_reported is not None and (now - self._last_reported) < self._cooldown:
            return False
        self._last_reported = now
        return True


# Conservative failure signatures on a CLEANED pane (after clean_pane / clean
# screen text has stripped TUI chrome). Each pattern requires more than the
# bare word "error" so ordinary prose ("there was an error in my thinking")
# does not trip a false alarm; a real traceback, an "Xyz­Error:"-style
# exception line, a tool's own "ERR"/"fatal:" prefix, or an explicit non-zero
# exit/FAILED marker is required.
_ERROR_PATTERNS = (
    re.compile(r"traceback \(most recent call last\)", re.IGNORECASE),
    re.compile(r"\b\w*error\b\s*:", re.IGNORECASE),   # "error:", "ValueError:", ...
    re.compile(r"^\s*fatal:\s", re.IGNORECASE | re.MULTILINE),
    re.compile(r"npm err!", re.IGNORECASE),
    re.compile(r"command not found"),
    re.compile(r"\bexit(?:ed)?\s+(?:with\s+)?(?:status|code)\s+[1-9]\d*", re.IGNORECASE),
    re.compile(r"\bnon-zero exit\b", re.IGNORECASE),
    re.compile(r"\bFAILED\b"),
)


def looks_like_error(screen_text: str) -> bool:
    """True when a cleaned pane shows a common failure signature: a Python
    traceback header, an "error:"/"SomeError:"-style exception line, a shell
    "fatal:" prefix (git), "npm ERR!", "command not found", a non-zero exit
    mention, or an ALL-CAPS "FAILED" marker. Conservative by design: plain
    prose that merely uses the word "error" (no colon, no known prefix) does
    not match."""
    text = screen_text or ""
    return any(p.search(text) for p in _ERROR_PATTERNS)
