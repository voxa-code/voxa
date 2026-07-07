"""Discover already-open Claude sessions and attach to them.

There is no universal way to control an arbitrary GUI terminal app, so control is
tiered (see the spec): tmux works everywhere; iTerm2/Terminal via scripting; others
are detected-only. v1 implements tmux + iTerm2 discovery and an iTerm2 controller.
The tmux controller (tmux_controller.TmuxController) is reused for tmux attach.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import subprocess
from typing import Callable, Optional, Sequence

from .tmux_controller import (
    FinalCallback, clean_pane, clean_pane_with_color, monitor_loop,
)

logger = logging.getLogger(__name__)

# Seams so tests can inject fakes instead of touching the real shell / AppleScript.
Runner = Callable[[Sequence[str]], str]
OsaRunner = Callable[[str], str]


def _shell(args: Sequence[str]) -> str:
    try:
        p = subprocess.run(list(args), capture_output=True, text=True, timeout=8)
        return p.stdout
    except Exception:
        return ""


def _osa(script: str) -> str:
    try:
        p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
        return p.stdout
    except Exception:
        return ""


def _cwd_of_pid(pid: str, run: Runner) -> str:
    for line in run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"]).splitlines():
        if line.startswith("n"):
            return line[1:]
    return ""


def _claude_pid_on_tty(tty: str, run: Runner) -> Optional[str]:
    """Return the pid of a `claude` process attached to this tty, if any."""
    ttyname = tty.replace("/dev/", "")
    for raw in run(["ps", "-t", ttyname, "-o", "pid=,command="]).splitlines():
        raw = raw.strip()
        if not raw:
            continue
        pid_str, _, cmd = raw.partition(" ")
        if "claude" in cmd.lower():
            return pid_str.strip()
    return None


_ITERM_LIST = (
    'tell application "iTerm2"\n'
    '  set out to ""\n'
    '  repeat with w in windows\n'
    '    repeat with t in tabs of w\n'
    '      repeat with s in sessions of t\n'
    '        set out to out & (id of s) & "\t" & (tty of s) & "\t" & (name of s) & "\n"\n'
    '      end repeat\n'
    '    end repeat\n'
    '  end repeat\n'
    '  return out\n'
    'end tell'
)


def discover_iterm(run: Runner = _shell, osa: OsaRunner = _osa) -> list[dict]:
    """List iTerm2 sessions that are running Claude."""
    sessions: list[dict] = []
    for line in osa(_ITERM_LIST).splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        sid, tty = parts[0].strip(), parts[1].strip()
        name = parts[2].strip() if len(parts) > 2 else ""
        if not sid or not tty:
            continue
        pid = _claude_pid_on_tty(tty, run)
        if not pid:
            continue
        cwd = _cwd_of_pid(pid, run)
        sessions.append({
            "id": f"iterm:{sid}",
            "raw_id": sid,
            "cwd": cwd,
            "label": os.path.basename(cwd) or name or "terminal",
            "app": "iTerm2",
            "backend": "iterm",
            "controllable": True,
        })
    return sessions


def discover_tmux(run: Runner = _shell) -> list[dict]:
    """List tmux sessions (default socket) whose active pane is running Claude."""
    fmt = "#{session_name}\t#{pane_current_path}\t#{pane_current_command}"
    out = run(["tmux", "list-panes", "-a", "-F", fmt])
    seen: dict[str, dict] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        sess, path, cmd = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if "claude" not in cmd.lower():
            continue
        seen.setdefault(sess, {
            "id": f"tmux::{sess}",          # empty socket field == default socket
            "raw_id": sess,
            "socket": None,
            "cwd": path,
            "label": os.path.basename(path) or sess,
            "app": "tmux",
            "backend": "tmux",
            "controllable": True,
        })
    return list(seen.values())


def list_claude_processes(run: Runner = _shell) -> list[dict]:
    """Every `claude` process attached to a tty: process-first discovery ground truth."""
    procs: list[dict] = []
    for raw in run(["ps", "-axo", "pid=,tty=,command="]).splitlines():
        parts = raw.split(None, 2)
        if len(parts) < 3:
            continue
        pid, tty, cmd = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if tty in ("??", "-", "") or "claude" not in cmd.lower():
            continue
        procs.append({"pid": pid, "tty": tty, "cwd": _cwd_of_pid(pid, run)})
    return procs


_TERMINAL_APP_LIST = (
    'tell application "Terminal"\n'
    '  set out to ""\n'
    '  repeat with w in windows\n'
    '    set ti to 1\n'
    '    repeat with t in tabs of w\n'
    '      set out to out & (id of w) & "\t" & ti & "\t" & (tty of t) & "\n"\n'
    '      set ti to ti + 1\n'
    '    end repeat\n'
    '  end repeat\n'
    '  return out\n'
    'end tell'
)


def discover_terminal_app(run: Runner = _shell, osa: OsaRunner = _osa) -> list[dict]:
    """List Terminal.app tabs running Claude. Guarded by pgrep so the AppleScript
    never launches Terminal when it is not already running."""
    if not run(["pgrep", "-x", "Terminal"]).strip():
        return []
    sessions: list[dict] = []
    for line in osa(_TERMINAL_APP_LIST).splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        wid, tab, tty = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if not wid or not tty:
            continue
        ttyname = tty.replace("/dev/", "")
        pid = _claude_pid_on_tty(tty, run)
        cwd = _cwd_of_pid(pid, run) if pid else ""
        if not pid:
            continue
        sessions.append({
            "id": f"term:{wid}:{tab}",
            "raw_id": f"{wid}:{tab}",
            "tty": ttyname,
            "pid": pid,
            "cwd": cwd,
            "label": os.path.basename(cwd) or "Terminal",
            "app": "Terminal",
            "backend": "terminal_app",
            "controllable": True,
        })
    return sessions


def gui_owner_of_tty(tty: str, run: Runner = _shell) -> tuple[str, str]:
    """Walk process ancestry from the tty's shell up to the owning GUI app bundle.
    Returns (app_name, app_pid), or ("", "") when no .app ancestor is found."""
    ttyname = tty.replace("/dev/", "")
    first = run(["ps", "-t", ttyname, "-o", "pid=,command="]).splitlines()
    if not first:
        return "", ""
    pid = first[0].strip().split(None, 1)[0]
    for _ in range(12):
        out = run(["ps", "-o", "ppid=,command=", "-p", pid]).strip().splitlines()
        if not out:
            return "", ""
        ppid, _, cmd = out[0].strip().partition(" ")
        ppid, cmd = ppid.strip(), cmd.strip()
        if ".app/Contents/MacOS/" in cmd:
            app = cmd.split(".app/Contents/MacOS/")[0].rsplit("/", 1)[-1]
            return app, pid
        if ppid in ("0", "1", "", pid):
            return "", ""
        pid = ppid
    return "", ""


def _tty_of_tmux_panes(run: Runner) -> set[str]:
    try:
        out = run(["tmux", "list-panes", "-a", "-F", "#{pane_tty}"])
    except Exception:
        return set()
    return {ln.strip().replace("/dev/", "") for ln in out.splitlines() if ln.strip()}


def discover_claude_sessions(run: Runner = _shell, osa: OsaRunner = _osa) -> list[dict]:
    """Process-first union of every open Claude session, deduped by tty with
    priority tmux > iTerm2 > Terminal.app > ax (universal fallback)."""
    found: list[dict] = []
    claimed: set[str] = set()

    try:
        found.extend(discover_tmux(run))
        claimed |= _tty_of_tmux_panes(run)
    except Exception:
        logger.exception("tmux discovery failed")
    try:
        for s in discover_iterm(run, osa):
            found.append(s)
        # iTerm discovery already knows each session's tty; claim them
        for line in osa(_ITERM_LIST).splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[1].strip():
                claimed.add(parts[1].strip().replace("/dev/", ""))
    except Exception:
        logger.exception("iterm discovery failed")
    try:
        for s in discover_terminal_app(run, osa):
            if s["tty"] not in claimed:
                found.append(s)
                claimed.add(s["tty"])
    except Exception:
        logger.exception("Terminal.app discovery failed")

    # Universal fallback: any claude on an unclaimed tty gets an ax session.
    try:
        for p in list_claude_processes(run):
            if p["tty"] in claimed:
                continue
            app, app_pid = gui_owner_of_tty(p["tty"], run)
            found.append({
                "id": f"ax:{p['tty']}",
                "raw_id": p["tty"],
                "tty": p["tty"],
                "pid": p["pid"],
                "app_pid": app_pid,
                "cwd": p["cwd"],
                "label": os.path.basename(p["cwd"]) or (app or "terminal"),
                "app": app or "terminal",
                "backend": "ax",
                # Only controllable if we resolved the owning GUI app: keystrokes
                # post to that pid. No owner (e.g. a detached tmux on a private
                # socket, ssh, or a headless process) means detected-only.
                "controllable": bool(app_pid),
            })
            claimed.add(p["tty"])
    except Exception:
        logger.exception("ax discovery failed")
    return found


def _iterm_capture_script(sid: str) -> str:
    """AppleScript that returns the contents of the iTerm2 session with id ``sid``.

    iTerm2 has no ``session id "X"`` element specifier (that raises error -1728),
    so we loop windows/tabs/sessions and match on the ``id`` property instead.
    """
    return (
        'tell application "iTerm2"\n'
        '  repeat with w in windows\n'
        '    repeat with t in tabs of w\n'
        '      repeat with s in sessions of t\n'
        f'        if (id of s) is "{sid}" then return contents of s\n'
        '      end repeat\n'
        '    end repeat\n'
        '  end repeat\n'
        '  return ""\n'
        'end tell'
    )


def _iterm_write_script(sid: str, text: str) -> str:
    """AppleScript that types ``text`` into the iTerm2 session with id ``sid``.
    Same loop-and-match approach as :func:`_iterm_capture_script`."""
    esc = text.replace("\\", "\\\\").replace('"', '\\"')
    return (
        'tell application "iTerm2"\n'
        '  repeat with w in windows\n'
        '    repeat with t in tabs of w\n'
        '      repeat with s in sessions of t\n'
        f'        if (id of s) is "{sid}" then tell s to write text "{esc}"\n'
        '      end repeat\n'
        '    end repeat\n'
        '  end repeat\n'
        'end tell'
    )


class ItermController:
    """Drives an already-open iTerm2 session running Claude. Same interface as
    TmuxController. stop() NEVER closes the user's terminal; it only detaches."""

    def __init__(
        self,
        session_id: str,
        osa: OsaRunner = _osa,
        poll_interval: float = 1.2,
        idle_polls: int = 3,
    ):
        self._sid = session_id          # raw iTerm session id (no "iterm:" prefix)
        self._osa = osa
        self._poll = poll_interval
        self._idle_polls = idle_polls
        self.status = "idle"
        self.working_dir: Optional[str] = None
        self._final_cb: Optional[FinalCallback] = None
        self._on_output = None
        self._on_output_color = None
        self._started = False
        self._monitor_task: Optional[asyncio.Task] = None

    def on_final(self, cb: FinalCallback) -> None:
        self._final_cb = cb

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
        # iTerm AppleScript cannot reach full scrollback; return the visible
        # screen (same loop-and-match capture the monitor uses), cleaned.
        raw = self._osa(_iterm_capture_script(self._sid))
        return clean_pane_with_color(raw, max_lines=lines, max_bytes=128000)

    def set_terminal_app(self, app: str) -> None:
        pass  # parity with TmuxController; not meaningful when attached

    def _capture(self) -> str:
        return self._osa(_iterm_capture_script(self._sid))

    async def _emit(self, text: str) -> None:
        if text.strip() and self._final_cb is not None:
            result = self._final_cb(text)
            if inspect.isawaitable(result):
                await result

    async def start(self, working_dir: Optional[str] = None) -> None:
        self._started = True
        if working_dir:
            self.working_dir = working_dir
        self.status = "idle"
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        self._monitor_task = asyncio.ensure_future(monitor_loop(self))

    async def send(self, text: str) -> None:
        if not self._started:
            raise ValueError("attach to a terminal before sending")
        self.status = "working"
        self._osa(_iterm_write_script(self._sid, text))

    async def stop(self, *, detach_only: bool = False) -> None:
        # Detach only. Never close the user's own terminal. (detach_only is accepted for
        # a uniform controller interface; this controller never sends an interrupt.)
        self._started = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        self.status = "idle"


class TerminalAppController:
    """Drives an already-open Terminal.app tab running Claude. Same interface as
    ItermController. stop() NEVER closes the user's tab; it only detaches."""

    def __init__(
        self,
        raw_id: str,                     # "<window_id>:<tab_index>"
        osa: OsaRunner = _osa,
        poll_interval: float = 1.2,
        idle_polls: int = 3,
    ):
        wid, _, tab = raw_id.partition(":")
        self._wid, self._tab = wid, (tab or "1")
        self._osa = osa
        self._poll = poll_interval
        self._idle_polls = idle_polls
        self.status = "idle"
        self.working_dir: Optional[str] = None
        self._final_cb: Optional[FinalCallback] = None
        self._on_output = None
        self._on_output_color = None
        self._started = False
        self._monitor_task: Optional[asyncio.Task] = None

    def on_final(self, cb: FinalCallback) -> None:
        self._final_cb = cb

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
        raw = self._osa(
            f'tell application "Terminal" to return history of {self._target()}'
        )
        return clean_pane_with_color(raw, max_lines=lines, max_bytes=128000)

    def set_terminal_app(self, app: str) -> None:
        pass  # parity with the other controllers; not meaningful when attached

    def _target(self) -> str:
        return f"tab {self._tab} of window id {self._wid}"

    def _capture(self) -> str:
        return self._osa(
            f'tell application "Terminal" to return history of {self._target()}'
        )

    async def _emit(self, text: str) -> None:
        if text.strip() and self._final_cb is not None:
            result = self._final_cb(text)
            if inspect.isawaitable(result):
                await result

    async def start(self, working_dir: Optional[str] = None) -> None:
        self._started = True
        if working_dir:
            self.working_dir = working_dir
        self.status = "idle"
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        self._monitor_task = asyncio.ensure_future(monitor_loop(self))

    async def send(self, text: str) -> None:
        if not self._started:
            raise ValueError("attach to a terminal before sending")
        self.status = "working"
        esc = text.replace("\\", "\\\\").replace('"', '\\"')
        self._osa(
            f'tell application "Terminal" to do script "{esc}" in {self._target()}'
        )

    async def stop(self, *, detach_only: bool = False) -> None:
        # Detach only. Never close the user's own tab.
        self._started = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        self.status = "idle"
