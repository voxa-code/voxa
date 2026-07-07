"""Decides WHEN a finish may ring the phone.

Claude Code fires a Stop hook at every main-loop turn boundary, including the
many boundaries mid-task when background subagents wake the loop; ringing each
one calls the phone a dozen times for one task. Policy:
- needs_input rings immediately (a human is blocking) and cancels any pending
  finish ring for that session (the input request supersedes it).
- finish waits a quiet window; any same-session hook activity inside it means
  the session is still working, so the pending ring is cancelled (a later Stop
  reschedules with the fresher summary).
- Busy panes (running background tasks visible in the driven pane) suppress
  the finish outright; the final Stop after the last agent has no marker.
Fail-open: errors inside a delayed ring are logged, never raised into /hook.
"""
from __future__ import annotations

import asyncio
import logging
import os

DEFAULT_BUSY_MARKERS = ("background task", "esc to interrupt", "tasks running")


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

    def __init__(self, report, quiet_seconds: float | None = None):
        self._report = report
        if quiet_seconds is None:
            quiet_seconds = float(os.environ.get("VOXA_RING_QUIET_SECONDS", "25"))
        self._quiet = quiet_seconds
        self._pending: dict[str, asyncio.Task] = {}

    def note_activity(self, session_id: str) -> None:
        """More hook traffic for this session means it is still working: drop any
        pending finish ring so it does not fire mid-task."""
        task = self._pending.pop(session_id, None)
        if task is not None:
            task.cancel()

    async def finish(self, session_id: str, msg: str, cwd: str) -> None:
        """Ring for a task end, but only once the session has been quiet for the
        window; a fresh finish replaces any earlier pending one for the session."""
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
        session (the input request supersedes a turn-end summary)."""
        self.note_activity(session_id)
        await self._report(msg, kind="needs_input", cwd=cwd, approval=approval)
