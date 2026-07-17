"""Background watcher: ring the phone when ANY open Claude terminal finishes.

Voxa's main loop only follows the one terminal you're attached to. This watcher
runs alongside it and watches EVERY open Claude session it can discover (tmux
sessions and iTerm2 windows, including ones you started yourself). When any of
them goes working -> done, it reports a one-line summary.

It reuses the same completion detector as the controllers (``monitor_loop``):
a session is "done" when its screen stops changing after having been active.

Routing of the report (speak on the line if a phone is connected, else fire a
CallKit ring) is the caller's job, passed in as ``on_done``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Awaitable, Callable, Optional

from .terminals import _osa, _shell, discover_claude_sessions, _iterm_capture_script
from .tmux_controller import monitor_loop, looks_actionable, _ACTIVE_MARKERS
from .transcript_monitor import TranscriptMonitor
from .transcripts import PROJECTS_DIR

logger = logging.getLogger(__name__)

# on_done(label, cwd, summary) -> awaitable | None
DoneCallback = Callable[[str, str, str], object]


class _PassiveWatch:
    """A read-only stand-in a session that exposes exactly what monitor_loop needs.

    Unlike the real controllers it never sends input, it only captures the pane
    and emits when the screen stabilises after activity."""

    # Claude shows these only while actively working; their ABSENCE (plus a stable
    # screen) is what tells us a task truly finished, not just paused mid-step.
    _WORKING_MARKERS = ("esc to interrupt", "esc to cancel", "tokens", "thinking…",
                        "thinking...", "running…", "running...")

    def __init__(self, session: dict, on_emit, run=_shell, osa=_osa,
                 poll_interval: float = 2.0, idle_polls: int = 5):
        self._session = session
        self._on_emit = on_emit
        self._run = run
        self._osa = osa
        self._poll = poll_interval
        self._idle_polls = idle_polls
        self.status = "idle"
        self._started = True
        # True once we've actually seen this session WORKING since the last report. A
        # fresh session that merely booted to its idle prompt never shows the working
        # markers, so it won't ring the phone on startup.
        self._saw_work = False

    def _capture(self) -> str:
        backend = self._session.get("backend")
        raw = self._session.get("raw_id", "")
        if backend == "tmux":
            screen = self._run(["tmux", "capture-pane", "-p", "-t", raw])
        elif backend == "iterm":
            screen = self._osa(_iterm_capture_script(raw))
        elif backend == "terminal_app":
            wid, _, tab = raw.partition(":")
            screen = self._osa(
                f'tell application "Terminal" to return history of '
                f'tab {tab or "1"} of window id {wid}'
            )
        else:
            screen = ""
        if any(m in screen.lower() for m in _ACTIVE_MARKERS):
            self._saw_work = True
        return screen

    async def _emit(self, text: str) -> None:
        # Only report once this session actually worked (then went quiet), or when it
        # is showing a real prompt waiting on the user. A fresh session that just booted
        # to its idle prompt must not ring (that would call the user on session start).
        if not (self._saw_work or looks_actionable(text)):
            return
        # Don't report "done" if Claude is still working (stable screen but a
        # spinner/"esc to interrupt" is showing) -> avoids calling before the task
        # actually finishes. In a thread: this is an osascript for the iTerm/
        # Terminal.app backends, which on the event loop froze the process.
        screen = (await asyncio.to_thread(self._capture)).lower()
        if any(m in screen for m in self._WORKING_MARKERS):
            return
        self._saw_work = False
        result = self._on_emit(self._session, text)
        if inspect.isawaitable(result):
            await result


class TerminalWatcher:
    """Polls for open Claude sessions and watches each for completion."""

    def __init__(self, on_done: DoneCallback, *,
                 on_resumed=None,
                 discover=discover_claude_sessions, run=_shell, osa=_osa,
                 should_skip: Optional[Callable[[dict], bool]] = None,
                 rediscover_interval: float = 5.0,
                 poll_interval: float = 2.0, idle_polls: int = 5,
                 dedupe_window: float = 90.0, clock=None,
                 resume_window: float = 30.0, resume_poll: float = 2.0,
                 monitor=monitor_loop, projects_dir: str | None = None):
        self._on_done = on_done
        self._on_resumed = on_resumed
        self._resume_window = resume_window
        self._resume_poll = resume_poll
        self._discover = discover
        self._run = run
        self._osa = osa
        self._should_skip = should_skip or (lambda s: False)
        self._rediscover = rediscover_interval
        self._poll = poll_interval
        self._idle_polls = idle_polls
        self._monitor = monitor
        self._dedupe_window = dedupe_window
        self._projects = projects_dir
        self._quiet = 5.0
        import time as _t
        self._clock = clock or _t.monotonic
        self._last_fired: dict[str, float] = {}   # cwd -> last report time
        self._watchers: dict[str, asyncio.Task] = {}

    async def _handle_emit(self, session: dict, text: str) -> None:
        # A session we're actively driving is reported by the main loop already.
        if self._should_skip(session):
            return
        # Dedupe: don't report the same terminal again within the window (the
        # monitor can re-fire on minor screen changes -> avoids ringing twice).
        cwd = session.get("cwd", "") or session.get("id", "")
        now = self._clock()
        if now - self._last_fired.get(cwd, -1e9) < self._dedupe_window:
            return
        self._last_fired[cwd] = now
        kind, summary = classify_screen(text)
        if kind == "needs_input" and summary:
            summary = f"needs input: {summary}"
        result = self._on_done(session.get("label", ""), cwd, summary)
        if inspect.isawaitable(result):
            await result
        if self._on_resumed is not None:
            asyncio.ensure_future(self._watch_resume(session))

    def _spawn(self, session: dict) -> None:
        if session.get("backend") == "ax":
            mon = TranscriptMonitor(
                session.get("cwd", ""),
                lambda text, s=session: self._handle_emit(s, text),
                poll_interval=self._poll, quiet_secs=self._quiet,
                projects_dir=self._projects or PROJECTS_DIR)
            mon._started = True
            self._watchers[session["id"]] = asyncio.ensure_future(mon.run())
            return
        watch = _PassiveWatch(session, self._handle_emit, run=self._run, osa=self._osa,
                              poll_interval=self._poll, idle_polls=self._idle_polls)
        self._watchers[session["id"]] = asyncio.ensure_future(self._monitor(watch))

    def _capture_session(self, session: dict) -> str:
        backend = session.get("backend")
        raw = session.get("raw_id", "")
        if backend == "tmux":
            return self._run(["tmux", "capture-pane", "-p", "-t", raw])
        if backend == "iterm":
            return self._osa(_iterm_capture_script(raw))
        if backend == "terminal_app":
            wid, _, tab = raw.partition(":")
            return self._osa(
                f'tell application "Terminal" to return history of '
                f'tab {tab or "1"} of window id {wid}'
            )
        return ""

    async def _watch_resume(self, session: dict) -> None:
        """After a ring fires, poll this session briefly. If working markers reappear
        (the user acted on the laptop), tell the caller to cancel the ring."""
        if self._on_resumed is None:
            return
        elapsed = 0.0
        while elapsed < self._resume_window:
            screen = ((await asyncio.to_thread(self._capture_session, session))
                      or "").lower()
            if any(m in screen for m in _PassiveWatch._WORKING_MARKERS):
                result = self._on_resumed(session.get("label", ""), session.get("cwd", ""))
                if inspect.isawaitable(result):
                    await result
                return
            await asyncio.sleep(self._resume_poll)
            elapsed += self._resume_poll

    async def reconcile_once(self) -> None:
        """One discovery pass: start watchers for new sessions, drop gone ones."""
        try:
            sessions = await asyncio.to_thread(self._discover, self._run, self._osa)
        except Exception:
            logger.exception("terminal discovery failed")
            return
        live_ids = set()
        for s in sessions:
            sid = s.get("id")
            if not sid:
                continue
            live_ids.add(sid)
            task = self._watchers.get(sid)
            if task is None or task.done():
                self._spawn(s)
        for sid in [s for s in self._watchers if s not in live_ids]:
            self._watchers.pop(sid).cancel()

    async def run(self) -> None:
        try:
            while True:
                await self.reconcile_once()
                await asyncio.sleep(self._rediscover)
        finally:
            for t in self._watchers.values():
                t.cancel()
            self._watchers.clear()


def _first_meaningful_line(text: str) -> str:
    """Pull a short human summary from the freshly-stable screen delta."""
    for ln in (text or "").splitlines():
        s = ln.strip()
        if len(s) > 2:
            return s[:200]
    return ""


def classify_screen(text: str) -> tuple[str, str]:
    """Decide whether a freshly-stable screen is Claude WAITING FOR INPUT (a menu, a
    y/n, a question) or a FINISHED result, and return (kind, summary)."""
    body = text or ""
    # A trailing question mark on the last meaningful line is a question to the user.
    last = _first_meaningful_line("\n".join(reversed(body.splitlines())))
    if looks_actionable(body) or last.endswith("?"):
        q = _first_meaningful_line(body) or last
        return "needs_input", q
    return "finished", _first_meaningful_line(body)
