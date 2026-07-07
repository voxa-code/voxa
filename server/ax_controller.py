"""Universal terminal controller: Accessibility keystroke injection.

Covers any terminal app we have no scripting bridge for (Ghostty, Warp,
VS Code, ...). Input goes in as keyboard events posted straight to the app's
pid (no focus steal). Reading: apps that expose text through the Accessibility
tree get real screens; GPU terminals that do not are monitored through the
session transcript instead (TranscriptMonitor).

All system access is behind injectable seams so tests (and the Linux cloud
box, which installs the same package) never import pyobjc.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import subprocess
from typing import Callable, Optional

from .tmux_controller import FinalCallback, monitor_loop, clean_pane, clean_pane_with_color
from .transcript_monitor import TranscriptMonitor
from .transcripts import PROJECTS_DIR

logger = logging.getLogger(__name__)

AX_PERMISSION_ERROR = "accessibility_permission_needed"
NO_LIVE_VIEW_NOTE = "Live view isn't available for this terminal."
_AX_SETTINGS_URL = ("x-apple.systempreferences:"
                    "com.apple.preference.security?Privacy_Accessibility")
_RETURN_KEYCODE = 36


def _quartz_post_keys(pid: int, text: str) -> None:
    """Default poster: type `text` + Return into the app via CGEventPostToPid."""
    import Quartz  # lazy: macOS-only

    for ch in text:
        for down in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
            Quartz.CGEventKeyboardSetUnicodeString(ev, len(ch), ch)
            Quartz.CGEventPostToPid(pid, ev)
    for down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(None, _RETURN_KEYCODE, down)
        Quartz.CGEventPostToPid(pid, ev)


def _ax_capture(pid: int) -> str:
    """Default capturer: best-effort text from the app's AX tree ('' if none)."""
    try:
        from ApplicationServices import (  # lazy: macOS-only
            AXUIElementCreateApplication, AXUIElementCopyAttributeValue,
        )
        app = AXUIElementCreateApplication(int(pid))
        err, win = AXUIElementCopyAttributeValue(app, "AXFocusedWindow", None)
        if err != 0 or win is None:
            return ""
        err, val = AXUIElementCopyAttributeValue(win, "AXValue", None)
        return str(val) if err == 0 and val else ""
    except Exception:
        return ""


def _ax_trusted() -> bool:
    try:
        from ApplicationServices import AXIsProcessTrusted  # lazy: macOS-only
        return bool(AXIsProcessTrusted())
    except Exception:
        return False


def _open_url(url: str) -> None:
    try:
        subprocess.Popen(["open", url])
    except Exception:
        logger.exception("could not open settings URL")


class AXController:
    """Drives ANY terminal app running Claude via Accessibility keystrokes.
    Same interface as the other controllers. stop() never touches the app."""

    def __init__(
        self,
        app_pid: int | str,
        cwd: str,
        *,
        poster: Callable[[int, str], None] = _quartz_post_keys,
        capturer: Callable[[int], str] = _ax_capture,
        trusted: Callable[[], bool] = _ax_trusted,
        opener: Callable[[str], None] = _open_url,
        poll_interval: float = 1.2,
        idle_polls: int = 3,
        quiet_secs: float = 5.0,
        projects_dir: str = PROJECTS_DIR,
    ):
        self._pid = int(app_pid)
        self._poster = poster
        self._capturer = capturer
        self._trusted = trusted
        self._opener = opener
        self._poll = poll_interval
        self._idle_polls = idle_polls
        self._quiet = quiet_secs
        self._projects = projects_dir
        self.status = "idle"
        self.working_dir: Optional[str] = cwd
        self._final_cb: Optional[FinalCallback] = None
        self._on_output = None
        self._on_output_color = None
        self.mirrors_screen = True
        self._started = False
        self._monitor_task: Optional[asyncio.Task] = None
        self._transcript_mon: Optional[TranscriptMonitor] = None

    def on_final(self, cb: FinalCallback) -> None:
        self._final_cb = cb
        if self._transcript_mon is not None:
            self._transcript_mon.on_final(cb)

    def on_output(self, cb) -> None:
        self._on_output = cb

    def on_output_color(self, cb) -> None:
        self._on_output_color = cb

    async def _emit_output(self, raw: str) -> None:
        if self._on_output is None:
            return
        text = clean_pane(raw)
        if not text.strip():
            return
        result = self._on_output(text)
        if inspect.isawaitable(result):
            await result

    async def _emit_output_color(self, raw: str) -> None:
        if self._on_output_color is None:
            return
        text = clean_pane_with_color(raw)
        if not text.strip():
            return
        result = self._on_output_color(text)
        if inspect.isawaitable(result):
            await result

    def capture_scrollback(self, lines: int = 1200) -> str:
        if not self.mirrors_screen:
            return NO_LIVE_VIEW_NOTE
        raw = self._capture()
        return clean_pane_with_color(raw, max_lines=lines, max_bytes=128000)

    def set_terminal_app(self, app: str) -> None:
        pass

    def _capture(self) -> str:
        return self._capturer(self._pid)

    async def _emit(self, text: str) -> None:
        if text.strip() and self._final_cb is not None:
            result = self._final_cb(text)
            if inspect.isawaitable(result):
                await result

    async def start(self, working_dir: Optional[str] = None) -> None:
        if not self._trusted():
            self._opener(_AX_SETTINGS_URL)
            raise PermissionError(
                f"{AX_PERMISSION_ERROR}: grant Accessibility permission to the "
                "Voxa server in System Settings (the pane was just opened), "
                "then attach again.")
        if working_dir:
            self.working_dir = working_dir
        self._started = True
        self.status = "idle"
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        first = self._capture()
        self.mirrors_screen = bool(first.strip())
        if self.mirrors_screen:
            self._monitor_task = asyncio.ensure_future(monitor_loop(self))
        else:
            # GPU terminal with no AX text: transcript is the source of truth.
            self._transcript_mon = TranscriptMonitor(
                self.working_dir or "", self._final_cb,
                poll_interval=self._poll, quiet_secs=self._quiet,
                projects_dir=self._projects)
            self._monitor_task = asyncio.ensure_future(self._transcript_mon.run())

    async def send(self, text: str) -> None:
        if not self._started:
            raise ValueError("attach to a terminal before sending")
        self.status = "working"
        await asyncio.to_thread(self._poster, self._pid, text)

    async def stop(self, *, detach_only: bool = False) -> None:
        self._started = False
        if self._transcript_mon is not None:
            await self._transcript_mon.stop()
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        self.status = "idle"
