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
import os
import subprocess
import time
from typing import Callable, Optional

from .tmux_controller import (FinalCallback, monitor_loop, clean_pane,
                              interrupt_needs_retry, clean_pane_with_color,
                              _send_settle_seconds, _send_enter_retries,
                              _input_still_pending, PRESS_KEY_NAMES,
                              pane_is_generating, _busy_grace_seconds)
from .transcript_monitor import TranscriptMonitor
from .transcripts import PROJECTS_DIR

logger = logging.getLogger(__name__)

AX_PERMISSION_ERROR = "accessibility_permission_needed"
NO_LIVE_VIEW_NOTE = "Live view isn't available for this terminal."
_AX_SETTINGS_URL = ("x-apple.systempreferences:"
                    "com.apple.preference.security?Privacy_Accessibility")
_RETURN_KEYCODE = 36
_ESCAPE_KEYCODE = 53

# Named special keys press() can post as a bare macOS virtual keycode (down+up,
# no text, no Return). Only the subset of tmux_controller.PRESS_KEY_NAMES that
# maps onto a PLAIN keycode with no modifier flags lives here; an entry that
# needs a modifier (e.g. "ctrl-c") is deliberately absent so press() raises
# ValueError for it instead of silently posting the wrong thing.
_AX_KEYCODES: dict[str, int] = {
    "enter": _RETURN_KEYCODE,
    "return": _RETURN_KEYCODE,
    "esc": _ESCAPE_KEYCODE,
    "tab": 48,
    "space": 49,
    "up": 126,
    "down": 125,
    "left": 123,
    "right": 124,
    "backspace": 51,
}


def _quartz_post_keys(pid: int, text: str) -> None:
    """Default poster: type ONLY `text` into the app via CGEventPostToPid. Return is
    posted SEPARATELY (see AXController.send/key_poster): baking it into the same
    keystroke burst is the send-reliability bug (a busy Claude Code TUI can absorb a
    Return arriving mid-burst as a newline in the composer instead of a submit)."""
    import Quartz  # lazy: macOS-only

    for ch in text:
        for down in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
            Quartz.CGEventKeyboardSetUnicodeString(ev, len(ch), ch)
            Quartz.CGEventPostToPid(pid, ev)


def _quartz_post_keycode(pid: int, keycode: int) -> None:
    """Post a single bare keypress (down+up, no text, no Return) to the app."""
    import Quartz  # lazy: macOS-only

    for down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(None, keycode, down)
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
        key_poster: Callable[[int, int], None] = _quartz_post_keycode,
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
        self._key_poster = key_poster
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
        # Seconds press() waits after posting a key before re-capturing the pane
        # to verify delivery (mirrors interrupt()'s 0.7s gap). An attribute (not
        # a constructor arg) so tests can zero it out without touching every
        # other call site.
        self._press_verify_secs = 0.7
        # When the last send() happened, for verify_working's grace/decay windows.
        self._last_send_at = float("-inf")
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
        # Both AX calls below run in a thread: Accessibility queries against a
        # busy terminal app can take seconds with no timeout, and on the event
        # loop (this runs inside serve_ws's answer path) that froze every
        # websocket in the process.
        if not await asyncio.to_thread(self._trusted):
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
        first = await asyncio.to_thread(self._capture)
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

    async def send(self, text: str) -> bool:
        """Type `text`, let the TUI settle, THEN post Return separately (see
        _quartz_post_keys/key_poster): a Return arriving inside the keystroke burst
        can be absorbed as a newline in the composer instead of a submit, leaving an
        approved instruction typed but never sent (the send-reliability bug this
        mirrors from TmuxController.send, see its docstring).

        When ``mirrors_screen`` is True (this app exposes AX text), re-capture the
        screen and use ``_input_still_pending`` to verify the typed text actually
        left the bottom input region; if it's still sitting there the Return was
        swallowed, so press it again, waiting the settle interval between tries, up
        to ``_send_enter_retries()`` times. When ``mirrors_screen`` is False (a GPU
        terminal with no AX text to verify against) there is nothing to check, so we
        mirror the tmux no-capture fallback: one best-effort extra Return after a
        short settle (a Return on an already-submitted empty input is a harmless
        no-op, so over-retrying here is safe).

        Returns True once submission is confirmed (or best-effort/unverifiable),
        False if the text was still pending after every retry (logged as a warning
        so a silently-stuck command can be noticed)."""
        if not self._started:
            raise ValueError("attach to a terminal before sending")
        self.status = "working"
        self._last_send_at = time.monotonic()   # verify_working's grace window
        typed = text or ""
        settle = _send_settle_seconds()
        await asyncio.to_thread(self._poster, self._pid, typed)
        await asyncio.sleep(settle)
        await asyncio.to_thread(self._key_poster, self._pid, _RETURN_KEYCODE)
        if not self.mirrors_screen:
            if settle:
                await asyncio.sleep(settle)
            await asyncio.to_thread(self._key_poster, self._pid, _RETURN_KEYCODE)
            return True
        retries = _send_enter_retries()
        for _ in range(retries):
            if settle:
                await asyncio.sleep(settle)
            try:
                pane = await asyncio.to_thread(self._capture)
            except Exception:
                pane = ""
            if not _input_still_pending(pane, typed):
                return True
            await asyncio.to_thread(self._key_poster, self._pid, _RETURN_KEYCODE)
        logger.warning(
            "AX send: input still pending after %d retries, command may not have "
            "submitted", retries)
        return False

    async def verify_working(self) -> bool:
        """Is Claude REALLY still working? Mirrors TmuxController.verify_working
        (see its docstring for the wedged-flag failure this heals): trust a
        fresh send for the grace window, then consult the live AX screen for a
        generating marker. When this backend has NO screen to consult
        (mirrors_screen False, GPU terminal), there is nothing to verify
        against, so the flag DECAYS instead: trusted until
        VOXA_BUSY_DECAY_SECONDS after the last send, then healed to idle so a
        wedged flag can't refuse dispatches forever."""
        if time.monotonic() - self._last_send_at < _busy_grace_seconds():
            return True
        if not self.mirrors_screen:
            try:
                decay = max(0.0, float(os.environ.get("VOXA_BUSY_DECAY_SECONDS", "300")))
            except (TypeError, ValueError):
                decay = 300.0
            if time.monotonic() - self._last_send_at > decay:
                logger.info("busy flag decayed to idle (no screen to verify against)")
                self.status = "idle"
                return False
            return True
        try:
            pane = await asyncio.to_thread(self._capture)
        except Exception:
            return True
        if pane_is_generating(pane):
            return True
        logger.info("busy flag was stale; verified idle from the live AX screen")
        self.status = "idle"
        return False

    async def interrupt(self) -> None:
        """Stop the CURRENT generation only: post a bare Escape keypress to the
        app's pid (no focus steal, no Return), keeping the monitor and _started
        so the session stays driveable for an immediate follow-up. Retries like
        the other controllers (vim INSERT mode eats the first Escape); see
        interrupt_needs_retry for the decision. Skipped when this backend has
        no live screen to consult (mirrors_screen False)."""
        if not self._started:
            return
        await asyncio.to_thread(self._key_poster, self._pid, _ESCAPE_KEYCODE)
        if not self.mirrors_screen:
            self.status = "idle"
            return
        try:
            before = self._capture()
            for _ in range(2):
                await asyncio.sleep(0.7)
                now = self._capture()
                if not interrupt_needs_retry(before, now):
                    break
                before = now
                await asyncio.to_thread(self._key_poster, self._pid, _ESCAPE_KEYCODE)
        except Exception:
            logger.exception("AX interrupt retry failed")
        self.status = "idle"

    async def press(self, key: str) -> None:
        """Inject a single keypress WITHOUT Enter, the AX-driven mirror of
        TmuxController.press: answer a structured approval (a menu digit,
        "y"/"n"), dismiss a prompt ("esc"), or drive a named special key
        ("up", "tab", ...) without submitting whatever text sits in the
        input box.

        A name in ``_AX_KEYCODES`` is posted as a bare macOS keycode via
        ``key_poster``. A single printable character or a run of digits (an
        approval's option key: "1", "y", "10", ...) is posted as TEXT via
        ``poster`` (which types without Return, see send()). Anything else
        (an unrecognised multi-character name that isn't all digits) raises
        ValueError BEFORE anything is posted; a name tmux_controller knows
        about but that has no plain-keycode mapping here (e.g. "ctrl-c",
        which needs a modifier flag AX keystroke injection can't post this
        way) gets a clearer message than the generic "unsupported" one so
        callers (Orchestrator.press_key) can surface it as an error instead
        of silently doing nothing.

        Delivery verification mirrors interrupt()'s pattern: when
        ``mirrors_screen`` is True, capture before, post, wait
        ``_press_verify_secs``, re-capture; if the pane is COMPLETELY
        unchanged, post once more (a changed pane means it landed, so it is
        NEVER retried after a change: a second "1" could act on whatever
        screen came next). When ``mirrors_screen`` is False there is nothing
        to verify against, so post once. Fail-open on a capture error: post
        once and return."""
        if not self._started:
            raise ValueError("call start() before press()")
        keycode = _AX_KEYCODES.get(key)
        if keycode is None and len(key) > 1 and not key.isdigit():
            if key in PRESS_KEY_NAMES:
                raise ValueError(
                    f"press: {key!r} needs a modifier key, which AX keystroke "
                    "injection can't post for this terminal")
            raise ValueError(f"press: unsupported key name {key!r}")

        async def _post() -> None:
            if keycode is not None:
                await asyncio.to_thread(self._key_poster, self._pid, keycode)
            else:
                await asyncio.to_thread(self._poster, self._pid, key)

        if not self.mirrors_screen:
            await _post()
            return
        try:
            before = self._capture()
        except Exception:
            # No screen to verify against: post once and fail open.
            await _post()
            return
        await _post()
        await asyncio.sleep(self._press_verify_secs)
        try:
            now = self._capture()
        except Exception:
            return
        if now == before:
            await _post()

    async def stop(self, *, detach_only: bool = False) -> None:
        self._started = False
        if self._transcript_mon is not None:
            await self._transcript_mon.stop()
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        self.status = "idle"
