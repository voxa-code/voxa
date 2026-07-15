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
import signal
import subprocess
import time
from typing import Callable, Optional, Sequence

from .tmux_controller import (
    _ANSI_RE, FinalCallback, clean_pane, clean_pane_with_color,
    interrupt_needs_retry, monitor_loop, pane_is_generating,
    PRESS_KEY_NAMES, _busy_grace_seconds,
)

logger = logging.getLogger(__name__)

# Seams so tests can inject fakes instead of touching the real shell / AppleScript.
Runner = Callable[[Sequence[str]], str]
OsaRunner = Callable[[str], str]
# Seams for the closing/ending-a-process path (close_terminal and friends,
# below): a signal poster and a liveness probe, so tests never touch a real pid.
Killer = Callable[[int, int], None]
Alive = Callable[[int], bool]


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


def _os_kill(pid: int, sig: int) -> None:
    """Default poster for the close/end-process path below: send ``sig`` to
    ``pid``. A pid that is already gone raises ProcessLookupError, which
    callers treat as "nothing left to end", not a failure."""
    os.kill(pid, sig)


def _pid_alive(pid: int) -> bool:
    """True while ``pid`` still exists, probed via signal 0 (posts nothing to
    the process, just asks the kernel whether it's there)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except Exception:
        return True
    return True


def pane_activity(raw: str, max_len: int = 80) -> tuple[str, str]:
    """(status, hint) for a discovered session's screen, so identically-named
    sessions (several terminals in the SAME folder) are tellable apart by what
    each one is doing. status is "working" while Claude's active marker shows,
    else "idle"; hint is the last meaningful line of the cleaned screen,
    whitespace-collapsed and capped. ("", "") for an empty/unreadable capture."""
    if not (raw or "").strip():
        return "", ""
    status = "working" if pane_is_generating(raw) else "idle"
    lines = [ln for ln in clean_pane(raw).splitlines() if ln.strip()]
    hint = " ".join(lines[-1].split()) if lines else ""
    if len(hint) > max_len:
        hint = hint[:max_len - 1] + "…"
    return status, hint


def humanize_etime(etime: str) -> str:
    """ps's [[dd-]hh:]mm:ss elapsed time -> a short spoken/displayed age:
    "3d" / "2h" / "35m" / "now" (<60s). "" for anything unparseable."""
    s = (etime or "").strip()
    if not s:
        return ""
    days = 0
    if "-" in s:
        d, _, s = s.partition("-")
        try:
            days = int(d)
        except ValueError:
            return ""
    parts = s.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return ""
    if len(nums) == 3:
        hours, minutes = nums[0], nums[1]
    elif len(nums) == 2:
        hours, minutes = 0, nums[0]
    else:
        return ""
    if days:
        return f"{days}d"
    if hours:
        return f"{hours}h"
    if minutes:
        return f"{minutes}m"
    return "now"


def _age_of_pid(pid: str, run: Runner) -> str:
    """How long this claude process has been running, humanized ('' on failure)."""
    if not pid:
        return ""
    try:
        return humanize_etime(run(["ps", "-o", "etime=", "-p", str(pid)]))
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
        try:
            status, hint = pane_activity(osa(_iterm_capture_script(sid)))
        except Exception:
            status, hint = "", ""
        sessions.append({
            "id": f"iterm:{sid}",
            "raw_id": sid,
            "cwd": cwd,
            "label": os.path.basename(cwd) or name or "terminal",
            "app": "iTerm2",
            "backend": "iterm",
            "controllable": True,
            "status": status,
            "hint": hint,
            "age": _age_of_pid(pid, run),
        })
    return sessions


def discover_tmux(run: Runner = _shell) -> list[dict]:
    """List tmux sessions (default socket) whose active pane is running Claude."""
    fmt = "#{session_name}\t#{pane_current_path}\t#{pane_current_command}\t#{pane_pid}"
    out = run(["tmux", "list-panes", "-a", "-F", fmt])
    seen: dict[str, dict] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        sess, path, cmd = parts[0].strip(), parts[1].strip(), parts[2].strip()
        pane_pid = parts[3].strip() if len(parts) > 3 else ""
        if "claude" not in cmd.lower():
            continue
        if sess in seen:
            continue
        try:
            status, hint = pane_activity(run(["tmux", "capture-pane", "-p", "-t", sess]))
        except Exception:
            status, hint = "", ""
        seen[sess] = {
            "id": f"tmux::{sess}",          # empty socket field == default socket
            "raw_id": sess,
            "socket": None,
            "cwd": path,
            "label": os.path.basename(path) or sess,
            "app": "tmux",
            "backend": "tmux",
            "controllable": True,
            "status": status,
            "hint": hint,
            "age": _age_of_pid(pane_pid, run),
        }
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
        try:
            status, hint = pane_activity(osa(
                f'tell application "Terminal" to return history of tab {tab} of window id {wid}'))
        except Exception:
            status, hint = "", ""
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
            "status": status,
            "hint": hint,
            "age": _age_of_pid(pid, run),
        })
    return sessions


def find_live_prompt_pane(cwd: Optional[str] = None,
                          discover=None, run: Runner = _shell,
                          osa: OsaRunner = _osa) -> Optional[tuple[str, str]]:
    """One-shot, read-only sweep of every open Claude terminal for a LIVE
    interactive prompt (permission menu / AskUserQuestion). Returns
    ``(cwd, raw_pane)`` for the first match, or None.

    ``cwd`` narrows the sweep to terminals in that folder (the Notification
    hook's case); None scans them all (answer time: a prompt may be waiting in
    a session other than the one that rang). Only panes showing a prompt
    footer count, so ordinary output can never be mistaken for a menu."""
    from server.approvals import pane_shows_live_prompt
    target = (cwd or "").rstrip("/")
    if discover is None:
        discover = discover_claude_sessions
    try:
        sessions = discover()
    except Exception:
        return None
    for sess in sessions:
        sess_cwd = (sess.get("cwd") or "").rstrip("/")
        if target and sess_cwd != target:
            continue
        backend, raw = sess.get("backend"), sess.get("raw_id") or ""
        try:
            if backend == "iterm":
                pane = osa(_iterm_capture_script(raw))
            elif backend == "terminal_app":
                wid, _, tab = raw.partition(":")
                pane = osa(f'tell application "Terminal" to return history of '
                           f'tab {tab or "1"} of window id {wid}')
            elif backend == "tmux":
                pane = run(["tmux", "capture-pane", "-p", "-e", "-t", raw])
            else:
                continue
        except Exception:
            continue
        if pane and pane_shows_live_prompt(pane):
            return sess_cwd, pane
    return None


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


# Short-TTL cache over the full discovery sweep. One sweep spawns an
# osascript per terminal app plus several shell-outs (easily a second or
# more with many windows), and it runs from several places at once now: the
# connect-time push, the phone's 5s home poll, and every attach miss. Only
# the DEFAULT seams are cached, so tests injecting fake run/osa always see
# their fakes. invalidate_discovery_cache() forces the next sweep fresh
# (used right after a close so the closed terminal never lingers).
_DISCOVERY_CACHE: dict = {"at": float("-inf"), "result": None}


def _discovery_ttl() -> float:
    try:
        return max(0.0, float(os.environ.get("VOXA_DISCOVERY_TTL", "2.5")))
    except (TypeError, ValueError):
        return 2.5


def invalidate_discovery_cache() -> None:
    _DISCOVERY_CACHE["at"] = float("-inf")
    _DISCOVERY_CACHE["result"] = None


def discover_claude_sessions(run: Runner = _shell, osa: OsaRunner = _osa) -> list[dict]:
    """Process-first union of every open Claude session, deduped by tty with
    priority tmux > iTerm2 > Terminal.app > ax (universal fallback).
    Default-seam calls are served from a short-TTL cache (see above)."""
    cacheable = run is _shell and osa is _osa
    if cacheable and _DISCOVERY_CACHE["result"] is not None \
            and time.monotonic() - _DISCOVERY_CACHE["at"] < _discovery_ttl():
        return [dict(s) for s in _DISCOVERY_CACHE["result"]]
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
                # No screen access on this fallback path: no activity preview.
                "status": "",
                "hint": "",
                "age": _age_of_pid(p["pid"], run),
            })
            claimed.add(p["tty"])
    except Exception:
        logger.exception("ax discovery failed")
    if cacheable:
        _DISCOVERY_CACHE["at"] = time.monotonic()
        _DISCOVERY_CACHE["result"] = [dict(s) for s in found]
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


def _iterm_close_script(sid: str) -> str:
    """AppleScript that closes the TAB containing the iTerm2 session with id
    ``sid`` (the whole tab, every pane in it, not just this one session), via
    the same loop-and-match approach as :func:`_iterm_capture_script` (no
    direct ``session id "X"`` specifier, which iTerm2's AppleScript dictionary
    doesn't support)."""
    return (
        'tell application "iTerm2"\n'
        '  repeat with w in windows\n'
        '    repeat with t in tabs of w\n'
        '      repeat with s in sessions of t\n'
        f'        if (id of s) is "{sid}" then close t\n'
        '      end repeat\n'
        '    end repeat\n'
        '  end repeat\n'
        'end tell'
    )


def _iterm_write_expr_script(sid: str, expr: str) -> str:
    """AppleScript that writes the AppleScript TEXT EXPRESSION ``expr`` (e.g.
    ``(character id 27)`` or a quoted string literal) into the iTerm2 session
    with id ``sid``, WITHOUT a trailing newline (`newline NO`), so it posts a
    bare keypress instead of also submitting whatever sits in the input box.
    Shared by :func:`_iterm_escape_script` (interrupt) and
    :meth:`ItermController.press` (approval answers / named keys)."""
    return (
        'tell application "iTerm2"\n'
        '  repeat with w in windows\n'
        '    repeat with t in tabs of w\n'
        '      repeat with s in sessions of t\n'
        f'        if (id of s) is "{sid}" then tell s to write text {expr} newline NO\n'
        '      end repeat\n'
        '    end repeat\n'
        '  end repeat\n'
        'end tell'
    )


def _iterm_escape_script(sid: str) -> str:
    """AppleScript that sends a bare ESC keypress (no newline) into the iTerm2
    session with id ``sid``: `character id 27` is the ESC byte, `newline NO`
    keeps write from also submitting. This is how a running Claude generation
    is interrupted in an attached iTerm terminal."""
    return _iterm_write_expr_script(sid, "(character id 27)")


# Named keys ItermController.press() sends as an AppleScript text EXPRESSION
# (evaluated by `write text <expr> newline NO`, see _iterm_write_expr_script),
# rather than typing the name out as a literal string. Arrow keys are the CSI
# escape sequence iTerm's terminal emulation expects (ESC "[" letter).
_ITERM_PRESS_EXPRS: dict[str, str] = {
    "enter": "(character id 13)",
    "return": "(character id 13)",
    "esc": "(character id 27)",
    "tab": "(character id 9)",
    "backspace": "(character id 127)",
    "space": '" "',
    "up": '(character id 27) & "[A"',
    "down": '(character id 27) & "[B"',
    "right": '(character id 27) & "[C"',
    "left": '(character id 27) & "[D"',
}


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


# --- closing a terminal from the phone -------------------------------------
#
# Unlike every controller's stop() above (detach-only, NEVER closes the user's
# terminal/session/window), the functions below are the one place that ends a
# terminal for real, on the phone's explicit request. Each mirrors the backend
# it closes: tmux has a session to end outright; iTerm2 can be told to close a
# tab; Terminal.app pops an "are you sure" prompt when a tab still has a live
# foreground process, so its process is ended FIRST; ax (Ghostty and anything
# else with no scripting bridge) has no window Voxa can address at all, so only
# the Claude process behind it can be ended, and the window is left as-is.

_TERMINAL_APP_SETTLE_SECONDS = 0.3
_AX_POLL_TOTAL_SECONDS = 1.5
_AX_POLL_STEP_SECONDS = 0.3
_AX_ENDED_NOTE = "ended the Claude process; the window itself stays open"


def close_tmux(raw_id: str, run: Runner = _shell) -> None:
    """End the tmux session (default socket, no -L flag: matches discover_tmux,
    which only ever lists sessions on that socket) that ``raw_id`` names, panes
    and all."""
    run(["tmux", "kill-session", "-t", raw_id])


def close_iterm(raw_id: str, osa: OsaRunner = _osa) -> None:
    """Close the iTerm2 tab containing the session with id ``raw_id``."""
    osa(_iterm_close_script(raw_id))


def close_terminal_app(pid: str, raw_id: str, *, kill: Killer = _os_kill,
                       sleep: Callable[[float], None] = time.sleep,
                       osa: OsaRunner = _osa) -> Optional[str]:
    """Close a Terminal.app tab (``raw_id`` is "<window_id>:<tab_index>",
    mirroring TerminalAppController). The tty's Claude process is ended FIRST
    (a plain SIGHUP, catchable) so the "are you sure you want to close this
    window?" prompt never has a live foreground process to ask about; only
    after a brief settle does the AppleScript close run. Returns a best-effort
    note (else None) only when the process end couldn't be confirmed, since a
    close prompt may then still appear."""
    wid, _, tab = raw_id.partition(":")
    tab = tab or "1"
    note = None
    try:
        pid_i = int(pid) if pid else None
    except (TypeError, ValueError):
        pid_i = None
    if pid_i is not None:
        try:
            kill(pid_i, signal.SIGHUP)
        except ProcessLookupError:
            pass
        except Exception:
            logger.exception("ending Terminal.app pid %s failed", pid_i)
            note = "could not confirm the shell process ended; a close prompt may appear"
    sleep(_TERMINAL_APP_SETTLE_SECONDS)
    osa(f'tell application "Terminal" to close tab {tab} of window id {wid}')
    return note


def close_ax(pid: str, *, kill: Killer = _os_kill, alive: Alive = _pid_alive,
             sleep: Callable[[float], None] = time.sleep) -> str:
    """End the Claude process behind an ax-backed terminal (Ghostty and
    anything else with no scripting bridge): there is no window/tab Voxa can
    address directly, only the process. A plain SIGHUP first (catchable, lets
    Claude exit cleanly); poll for up to ~1.5s (signal 0, no actual signal
    posted) so a process that exits quickly isn't escalated needlessly; still
    alive after that gets ONE SIGTERM. The window is never touched either way,
    so the note is always the same honest, partial one."""
    try:
        pid_i = int(pid)
    except (TypeError, ValueError):
        return _AX_ENDED_NOTE
    try:
        kill(pid_i, signal.SIGHUP)
    except Exception:
        logger.exception("ending ax pid %s failed", pid_i)
    elapsed = 0.0
    still_alive = True
    while elapsed < _AX_POLL_TOTAL_SECONDS:
        sleep(_AX_POLL_STEP_SECONDS)
        elapsed += _AX_POLL_STEP_SECONDS
        try:
            still_alive = alive(pid_i)
        except Exception:
            still_alive = False
        if not still_alive:
            break
    if still_alive:
        try:
            kill(pid_i, signal.SIGTERM)
        except Exception:
            logger.exception("escalating to ax pid %s failed", pid_i)
    return _AX_ENDED_NOTE


def close_terminal(id_: str, discover=None, run: Runner = _shell, osa: OsaRunner = _osa,
                    kill: Killer = _os_kill, alive: Alive = _pid_alive,
                    sleep: Callable[[float], None] = time.sleep) -> dict:
    """Resolve ``id_`` to a currently-open session via a fresh discovery pass
    (never string-parse the id itself, e.g. tmux's "tmux::<name>" double-colon
    form) and close/end it on its own backend. Returns ``{"cwd", "backend",
    "note"?}`` on success, or ``{"error": ...}`` when the id no longer
    resolves to an open session (already closed, or never existed)."""
    sessions = discover() if discover is not None else discover_claude_sessions(run, osa)
    sess = next((s for s in sessions if s.get("id") == id_), None)
    if sess is None:
        return {"error": "that terminal is no longer open"}
    backend = sess.get("backend")
    cwd = sess.get("cwd", "")
    note = None
    if backend == "tmux":
        close_tmux(sess["raw_id"], run=run)
    elif backend == "iterm":
        close_iterm(sess["raw_id"], osa=osa)
    elif backend == "terminal_app":
        note = close_terminal_app(sess.get("pid", ""), sess["raw_id"],
                                  kill=kill, sleep=sleep, osa=osa)
    elif backend == "ax":
        pid = sess.get("pid", "")
        if not pid:
            return {"error": "no process to end for this terminal"}
        note = close_ax(pid, kill=kill, alive=alive, sleep=sleep)
    else:
        return {"error": f"cannot close a {backend or sess.get('app', 'terminal')}"}
    # The world just changed under the discovery cache: the follow-up list
    # refresh must not serve a snapshot that still shows the closed terminal.
    invalidate_discovery_cache()
    result = {"cwd": cwd, "backend": backend}
    if note:
        result["note"] = note
    return result


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
        # Seconds press() waits after posting a key before re-capturing the pane
        # to verify delivery (mirrors AXController.press's 0.7s gap). An instance
        # attribute (not a constructor arg) so tests can zero it out.
        self._press_verify_secs = 0.7
        # When the last send() happened, for verify_working's grace window.
        self._last_send_at = float("-inf")
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
        self._last_send_at = time.monotonic()   # verify_working's grace window
        self._osa(_iterm_write_script(self._sid, text))

    async def verify_working(self) -> bool:
        """Is Claude REALLY still working? Mirrors TmuxController.verify_working
        (see its docstring for the wedged-flag failure this heals): a fresh send
        is trusted for the grace window, after that the live pane is consulted;
        no generating marker means the flag is stale, so it heals to idle.
        Fail-safe: a capture error keeps the old answer (True)."""
        if time.monotonic() - self._last_send_at < _busy_grace_seconds():
            return True
        try:
            pane = await asyncio.to_thread(self._capture)
        except Exception:
            return True
        if pane_is_generating(pane):
            return True
        logger.info("busy flag was stale; verified idle from the live pane")
        self.status = "idle"
        return False

    async def press(self, key: str) -> None:
        """Inject a single keypress WITHOUT Enter (an approval's option digit,
        "y"/"n", a named key like "esc"/"tab"), mirroring TmuxController.press /
        AXController.press: a name in ``_ITERM_PRESS_EXPRS`` is posted as that
        AppleScript text expression; a single printable character or a run of
        digits is posted as a literal string. Both go through
        ``write text <expr> newline NO`` so nothing already sitting in the input
        box is submitted. Anything else (an unrecognised multi-character name
        that isn't all digits) raises ``ValueError`` BEFORE any I/O; a name
        tmux/AX know about but that has no mapping here (e.g. "ctrl-c", which
        needs a modifier iTerm's AppleScript `write text` can't post) gets a
        clearer message.

        Delivery verification mirrors AXController.press: capture before, post,
        wait ``self._press_verify_secs``, re-capture; a COMPLETELY unchanged pane
        gets one repost (never more than once); a changed pane is never
        reposted. Fail-open on a capture error."""
        if not self._started:
            raise ValueError("call start() before press()")
        expr = _ITERM_PRESS_EXPRS.get(key)
        if expr is None and len(key) > 1 and not key.isdigit():
            if key in PRESS_KEY_NAMES:
                raise ValueError(f"press: {key!r} has no iTerm2 key mapping")
            raise ValueError(f"press: unsupported key name {key!r}")
        if expr is None:
            esc = key.replace("\\", "\\\\").replace('"', '\\"')
            expr = f'"{esc}"'

        def _post() -> None:
            self._osa(_iterm_write_expr_script(self._sid, expr))

        try:
            before = self._capture()
        except Exception:
            _post()
            return
        _post()
        await asyncio.sleep(self._press_verify_secs)
        try:
            now = self._capture()
        except Exception:
            return
        if now == before:
            _post()

    async def interrupt(self) -> None:
        """Stop the CURRENT generation only: inject a bare ESC keypress into the
        session and leave everything running (terminal, monitor, _started), so
        the user can immediately follow up. Mirrors TmuxController.interrupt,
        including the retry (vim INSERT mode eats the first Escape); see
        interrupt_needs_retry for the decision."""
        if not self._started:
            return
        before = self._capture()
        self._osa(_iterm_escape_script(self._sid))
        for _ in range(2):
            await asyncio.sleep(0.7)
            now = self._capture()
            if not interrupt_needs_retry(before, now):
                break
            before = now
            self._osa(_iterm_escape_script(self._sid))
        self.status = "idle"

    async def stop(self, *, detach_only: bool = False) -> None:
        # Detach only. Never close the user's own terminal. (detach_only is accepted for
        # a uniform controller interface; this controller never sends an interrupt.)
        self._started = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        self.status = "idle"


# Named keys TerminalAppController.press() sends as a System Events `key code
# <n>` (bare macOS virtual keycode, no text, no Return). Mirrors
# ax_controller._AX_KEYCODES: only names that map onto a PLAIN keycode with no
# modifier live here, so a name that needs one (e.g. "ctrl-c") is deliberately
# absent and press() raises ValueError for it instead of posting the wrong thing.
_TERMINAL_APP_PRESS_KEYCODES: dict[str, int] = {
    "enter": 36,
    "return": 36,
    "esc": 53,
    "tab": 48,
    "space": 49,
    "up": 126,
    "down": 125,
    "left": 123,
    "right": 124,
    "backspace": 51,
}


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
        # Seconds press() waits after posting a key before re-capturing the pane
        # to verify delivery (mirrors AXController.press's 0.7s gap). An instance
        # attribute (not a constructor arg) so tests can zero it out.
        self._press_verify_secs = 0.7
        # When the last send() happened, for verify_working's grace window.
        self._last_send_at = float("-inf")
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
        self._last_send_at = time.monotonic()   # verify_working's grace window
        esc = text.replace("\\", "\\\\").replace('"', '\\"')
        self._osa(
            f'tell application "Terminal" to do script "{esc}" in {self._target()}'
        )

    def _post_key_event(self, event: str) -> None:
        """Focus this tab (activate + select it + bring its window forward),
        then post ``event`` (a System Events `key code <n>` or
        `keystroke "..."`) to it. Shared by _press_escape (interrupt) and
        press(): Terminal.app's AppleScript can run shell commands in a tab but
        cannot inject a raw keypress, so posting goes through System Events,
        which needs the tab focused first (briefly steals focus; best-effort)."""
        self._osa(
            'tell application "Terminal"\n'
            '  activate\n'
            f'  set selected of {self._target()} to true\n'
            f'  set index of window id {self._wid} to 1\n'
            'end tell\n'
            f'tell application "System Events" to {event}'
        )

    def _press_escape(self) -> None:
        self._post_key_event("key code 53")

    async def verify_working(self) -> bool:
        """Is Claude REALLY still working? Mirrors TmuxController.verify_working
        (see its docstring for the wedged-flag failure this heals): a fresh send
        is trusted for the grace window, after that the live pane is consulted;
        no generating marker means the flag is stale, so it heals to idle.
        Fail-safe: a capture error keeps the old answer (True)."""
        if time.monotonic() - self._last_send_at < _busy_grace_seconds():
            return True
        try:
            pane = await asyncio.to_thread(self._capture)
        except Exception:
            return True
        if pane_is_generating(pane):
            return True
        logger.info("busy flag was stale; verified idle from the live pane")
        self.status = "idle"
        return False

    async def press(self, key: str) -> None:
        """Inject a single keypress WITHOUT Enter (an approval's option digit,
        "y"/"n", a named key like "esc"/"tab"), mirroring TmuxController.press /
        AXController.press: a name in ``_TERMINAL_APP_PRESS_KEYCODES`` is posted
        as a bare `key code <n>`; a single printable character or a run of
        digits is posted as a `keystroke "..."`. Both go through
        ``_post_key_event`` (focus the tab, then System Events), the same
        delivery ``_press_escape`` already uses. Anything else (an unrecognised
        multi-character name that isn't all digits) raises ``ValueError`` BEFORE
        any I/O; a name tmux/AX know about but with no plain-keycode mapping
        here (e.g. "ctrl-c", which needs a modifier) gets a clearer message.

        Delivery verification mirrors AXController.press: capture before, post,
        wait ``self._press_verify_secs``, re-capture; a COMPLETELY unchanged
        pane gets one repost (never more than once); a changed pane is never
        reposted. Fail-open on a capture error."""
        if not self._started:
            raise ValueError("call start() before press()")
        keycode = _TERMINAL_APP_PRESS_KEYCODES.get(key)
        if keycode is None and len(key) > 1 and not key.isdigit():
            if key in PRESS_KEY_NAMES:
                raise ValueError(f"press: {key!r} has no Terminal.app key mapping")
            raise ValueError(f"press: unsupported key name {key!r}")
        if keycode is not None:
            event = f"key code {keycode}"
        else:
            esc = key.replace("\\", "\\\\").replace('"', '\\"')
            event = f'keystroke "{esc}"'

        try:
            before = self._capture()
        except Exception:
            self._post_key_event(event)
            return
        self._post_key_event(event)
        await asyncio.sleep(self._press_verify_secs)
        try:
            now = self._capture()
        except Exception:
            return
        if now == before:
            self._post_key_event(event)

    async def interrupt(self) -> None:
        """Stop the CURRENT generation only. Terminal.app's AppleScript can run
        shell commands in a tab but cannot inject a raw keypress, so this
        selects the tab, brings its window forward, and posts an Escape via
        System Events (briefly steals focus; best-effort). Everything stays
        running so the user can follow up immediately. Retries like the other
        controllers (vim INSERT mode eats the first Escape); see
        interrupt_needs_retry for the decision."""
        if not self._started:
            return
        before = self._capture()
        self._press_escape()
        for _ in range(2):
            await asyncio.sleep(0.7)
            now = self._capture()
            if not interrupt_needs_retry(before, now):
                break
            before = now
            self._press_escape()
        self.status = "idle"

    async def stop(self, *, detach_only: bool = False) -> None:
        # Detach only. Never close the user's own tab.
        self._started = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        self.status = "idle"
