"""Attach-mode controller for Loop.

Runs an interactive ``claude`` inside a tmux session that the user can BOTH watch
and type into on the laptop (a Terminal window attached to the session) AND drive
by voice from the phone (we inject transcribed text with ``tmux send-keys``).

It implements the same interface as :class:`ClaudeController`
(``status``, ``working_dir``, ``on_final``, ``start``, ``send``, ``stop``) so the
orchestrator and server use the two interchangeably.

Speaking results back is best-effort: we capture the tmux pane, strip the TUI
chrome, and return the new text since the prompt was sent. The laptop terminal is
always the source of truth.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Awaitable, Callable, Optional, Sequence


# A resume stem (a Claude transcript/session id) must be a safe shell token before
# it is spliced into the launch command. Claude session ids are alnum + dash; we
# also allow underscore. Anything else (spaces, `;`, `$()`, `/`, backticks, empty)
# is rejected so it can NEVER be shell-injected: the command below is handed to the
# login shell by tmux as one argv element, and there is no shell=True anywhere.
_RESUME_STEM_RE = re.compile(r"\A[A-Za-z0-9_-]+\Z")


def _claude_launch_cmd(resume: str | None = None) -> str:
    """`claude` invocation for the Voxa-driven session. By default it uses the
    user's NORMAL environment (their plugins, MCP, hooks) so it behaves like their
    own Claude. Set VOXA_ISOLATE_CLAUDE=1 to run in an isolated config dir instead
    (no global hooks/plugins) if some hook interferes; auth stays in the Keychain.

    A ``resume`` stem launches ``claude --resume <stem> --dangerously-skip-permissions``
    so the phone can reconnect to a past conversation. The stem is validated against
    a strict safe-token pattern and silently DROPPED if it fails (falling back to a
    normal launch), so a malformed/hostile id can never inject into the command."""
    flags = "--dangerously-skip-permissions"
    if resume and _RESUME_STEM_RE.match(resume):
        base = f"claude --resume {resume} {flags}"
    else:
        base = f"claude {flags}"
    if os.environ.get("VOXA_ISOLATE_CLAUDE", "").strip().lower() not in ("1", "true", "yes"):
        return base
    cfg = os.path.expanduser("~/.voxa/claude-config")
    try:
        os.makedirs(cfg, exist_ok=True)
        sp = os.path.join(cfg, "settings.json")
        if not os.path.exists(sp):
            with open(sp, "w") as f:
                json.dump({
                    "permissions": {"defaultMode": "bypassPermissions"},
                    "skipDangerousModePermissionPrompt": True,
                    "hasCompletedOnboarding": True,
                    "theme": "dark",
                }, f)
    except OSError:
        return base
    return f"CLAUDE_CONFIG_DIR={shlex.quote(cfg)} {base}"

logger = logging.getLogger(__name__)

FinalCallback = Callable[[str], Awaitable[None]] | Callable[[str], None]
# A tmux runner runs ``tmux <args>`` and returns stdout; raises on failure.
TmuxRunner = Callable[[Sequence[str]], str]

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_BORDER_CHARS = set("─━│┃╭╮╰╯┌┐└┘├┤┬┴┼ =·•")
# Spinner / bullet glyphs Claude Code prints at the start of status lines.
_SPINNER_PREFIX = "✶✻✽✢✣✤✥◐◓◑◒*•◦›❯⏵"
# Substrings that mark Claude Code's own interface noise (not its actual answer).
_NOISE = (
    "mcp", "esc to interrupt", "for shortcuts", "ctrl+", "shift+tab",
    "bypass permissions", "auto-update", "/doctor", "release-notes",
    "what's new", "tips for getting started", "welcome back", "welcome to",
    "claude code v", "to expand", "1m context", "/1m", "tokens", "-- insert",
    "accept edits", "plan mode", "for agents", "/effort", "/model",
)
# Claude Code's status bar / footer carries these glyphs (model, effort level, usage,
# cost, mode). It's pure UI chrome, never relay it to the operator or show it in the
# live view (the agent kept reading "⚡ xhigh /effort" and asking the user about it).
_STATUS_GLYPHS = "⚡🤖💰📅⊙⏵⏷◀▸"
# Whimsical "working" verbs Claude shows in its spinner (Crunched/Sautéing/...).
_WORK_TIMER_RE = re.compile(r"\bfor\s+\d+s\b", re.IGNORECASE)
_COST_RE = re.compile(r"\$\d")


def _make_default_runner(socket: Optional[str]) -> TmuxRunner:
    """Build a tmux runner. A named ``socket`` uses a private server with NO user
    config (so a broken ~/.tmux.conf can't break Loop), used for sessions Loop
    launches. ``socket=None`` targets the user's DEFAULT tmux server, used when
    attaching to a session the user already started there."""
    base = ["tmux"] + (["-L", socket, "-f", "/dev/null"] if socket else [])

    def run(args: Sequence[str]) -> str:
        proc = subprocess.run(base + list(args), capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"tmux {list(args)} failed: {proc.stderr.strip()}")
        return proc.stdout
    return run


def pick_session_name(session_id: str, socket: str = "voxa",
                      runner: Optional[TmuxRunner] = None,
                      cwd: Optional[str] = None) -> str:
    """Session-scoped tmux name (voxa-<id>) for a new driven session, ADOPTING a
    leftover voxa/voxa-* session from a previous server run instead of orphaning
    it (pre-registry servers hardcoded the name "voxa", and a restarted server
    must keep finding the user's running Claude).

    ``cwd=None`` (every pre-fleet caller) keeps today's behavior: adopt the
    FIRST leftover voxa/voxa-* session found, no matter where its pane sits.
    Passing ``cwd`` makes adoption cwd-aware (for the fleet view, where several
    leftover sessions can exist): only a leftover whose pane cwd rstrip-matches
    ``cwd`` is adopted; otherwise a fresh session name is minted so a DIFFERENT
    folder's session is never accidentally taken over."""
    run = runner or _make_default_runner(socket)
    try:
        out = run(["list-sessions", "-F", "#{session_name}"])
    except Exception:
        return f"voxa-{session_id}"
    candidates = [name for name in (n.strip() for n in out.splitlines())
                  if name == "voxa" or name.startswith("voxa-")]
    if cwd is None:
        return candidates[0] if candidates else f"voxa-{session_id}"
    target = cwd.rstrip("/")
    for name in candidates:
        try:
            path = run(["display-message", "-p", "-t", name, "#{pane_current_path}"]).strip()
        except Exception:
            continue
        if path.rstrip("/") == target:
            return name
    return f"voxa-{session_id}"


def _resolve_terminal_app(choice: str) -> str:
    """Pick the macOS terminal app: explicit choice, else auto-detect (prefer iTerm2)."""
    choice = (choice or "auto").strip()
    if choice.lower() in ("iterm", "iterm2"):
        return "iTerm"
    if choice.lower() == "terminal":
        return "Terminal"
    # auto
    return "iTerm" if os.path.isdir("/Applications/iTerm.app") else "Terminal"


def clean_pane(text: str) -> str:
    """Strip ANSI escapes and Claude-Code TUI chrome (borders, prompt, spinners,
    MCP/status/cost noise) so only Claude's actual output reaches the user."""
    text = _ANSI_RE.sub("", text)
    out: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if set(stripped) <= _BORDER_CHARS:           # box-drawing borders
            continue
        if stripped[0] in "│┃>" or stripped[0] in _SPINNER_PREFIX:  # input box / spinner
            continue
        low = stripped.lower()
        if any(n in low for n in _NOISE):            # MCP/tips/status-bar noise
            continue
        if _WORK_TIMER_RE.search(low):               # "Crunched for 5s" spinner lines
            continue
        if _COST_RE.search(stripped) and "%" in stripped:  # the cost/token status bar
            continue
        if any(g in stripped for g in _STATUS_GLYPHS):      # model/effort/usage footer
            continue
        out.append(stripped)
    return "\n".join(out)


def clean_pane_with_color(text: str, max_lines: int = 200, max_bytes: int = 16000) -> str:
    """Like clean_pane, but KEEP each surviving line's ANSI colour escapes (the phone
    parses them into coloured text). Every filter DECISION still runs against an
    ANSI-stripped copy of the line, so colour bytes can't smuggle chrome/noise past the
    substring checks. Leading indentation is preserved; bounded to the last
    ``max_lines`` / ``max_bytes`` to protect the socket and the iOS Text view."""
    out: list[str] = []
    for raw in text.splitlines():
        visible = _ANSI_RE.sub("", raw)
        stripped = visible.strip()
        if not stripped:
            continue
        if set(stripped) <= _BORDER_CHARS:
            continue
        if stripped[0] in "│┃>" or stripped[0] in _SPINNER_PREFIX:
            continue
        low = stripped.lower()
        if any(n in low for n in _NOISE):
            continue
        if _WORK_TIMER_RE.search(low):
            continue
        if _COST_RE.search(stripped) and "%" in stripped:
            continue
        if any(g in stripped for g in _STATUS_GLYPHS):      # model/effort/usage footer
            continue
        out.append(raw.rstrip())   # keep colour escapes + leading indent; trim trailing pad
    out = out[-max_lines:]
    s = "\n".join(out)
    data = s.encode("utf-8")                       # bound by BYTES, not code points
    if len(data) > max_bytes:
        s = data[-max_bytes:].decode("utf-8", errors="ignore")
    return s


def new_text(before: str, after: str) -> str:
    """Best-effort: lines present in ``after`` but not in ``before`` (else all of after)."""
    before_lines = set(before.splitlines())
    fresh = [ln for ln in after.splitlines() if ln not in before_lines]
    return "\n".join(fresh) if fresh else after


def stable_key(text: str) -> str:
    """Normalize a screen for change-detection: drop the remaining volatile chrome and
    ignore ticking numbers, so 'idle' is detected even while timers/costs change."""
    out = []
    for ln in clean_pane(text).splitlines():
        low = ln.lower()
        if any(k in low for k in (
            "esc to interrupt", "tokens", "context left", "auto-update",
            "/doctor", "crunch", "✶", "✻", "✽",
        )):
            continue
        out.append(re.sub(r"\d+", "#", ln))  # ignore changing numbers
    return "\n".join(out)


# Markers that mean SOMETHING is happening on screen (generation or a prompt
# waiting on the user): they arm the finished-announce (_saw_work).
_ACTIVE_MARKERS = ("esc to interrupt", "esc to cancel")
# Marker Claude shows ONLY while actively generating. This, and only this, may
# hold status at "working": "esc to cancel" also appears on interactive prompts
# (trust dialogs, permission menus, "Enter to confirm · Esc to cancel"), where
# Claude is WAITING on the user, not working — treating those as busy suppressed
# the prompt announcement and made the orchestrator refuse dispatches while a
# question sat on screen.
_GENERATING_MARKERS = ("esc to interrupt",)

# Newer Claude Code builds dropped the persistent "esc to interrupt" hint; while
# working they show a spinner-glyph status line instead, e.g.
#   "✳ Protoys, Build and ship prototypes, protoys.app…"  (early)
#   "✻ Doing X… (4s · thinking with high effort)"          (thinking)
#   "✳ Churned for 2m 25s · 1 shell still running"         (tool running)
# The settled post-turn line ("✻ Crunched for 13s") also starts with a glyph but
# has none of the live suffixes, so it must NOT count as generating.
_SPIN_GLYPHS = "·✳✶✻✽✢✣✤✥*"
_GEN_LINE_RE = re.compile(r"(…|\(\d+[smh]|still running)")


def pane_is_generating(text: str) -> bool:
    """True while the pane shows Claude actively generating, across TUI versions:
    the old persistent "esc to interrupt" hint, or a live spinner status line
    (glyph-prefixed with an ellipsis / elapsed-timer / running-tools suffix)."""
    plain = _ANSI_RE.sub("", text or "")
    if any(m in plain.lower() for m in _GENERATING_MARKERS):
        return True
    for ln in plain.splitlines():
        s = ln.strip()
        if s and s[0] in _SPIN_GLYPHS and _GEN_LINE_RE.search(s):
            return True
    return False

def interrupt_needs_retry(before: str, now: str) -> bool:
    """After an Escape aimed at a running generation, decide whether ANOTHER
    press is needed. One Escape is not always enough: with vim keybindings the
    composer sits in INSERT mode after a send and the first press is consumed
    leaving insert mode while the generation keeps streaming (verified live).

    Version-proof across Claude Code TUIs, which no longer show a reliable
    "working" marker (current builds stream with NO spinner line and NO footer):
      - the interrupt is CONFIRMED when "interrupted" appears near the bottom
        (checked on the tail only: Terminal.app/AX captures include scrollback,
        where an old interrupt would false-positive);
      - a settled screen showing the composer prompt (❯) means nothing was
        running (or the interrupt already landed without its banner) -> stop;
      - anything else (screen still mutating = streaming, or a promptless
        static screen = thinking) -> press again."""
    plain_now = _ANSI_RE.sub("", now or "")
    tail = "\n".join(plain_now.splitlines()[-40:]).lower()
    if "interrupted" in tail:
        return False
    if pane_is_generating(now):
        return True
    if "❯" in plain_now and now == before:
        return False
    return True


_MENU_RE = re.compile(r"^\s*[>❯]?\s*\d+[.)]\s+\S", re.MULTILINE)
_YESNO_RE = re.compile(r"\(y/n\)|\[y/n\]|\(yes/no\)", re.IGNORECASE)
_PROMPT_WORDS = ("do you trust", "allow ", "permission", "proceed?", "overwrite",
                 "continue?")


def looks_actionable(text: str) -> bool:
    """True when the screen is a real prompt waiting on the user: a numbered menu (>=2
    options), a (y/n), or a trust/permission question. Lets a fresh session announce
    ONLY for an actual prompt, never for a plain idle prompt (which would call the user
    the moment they start a session)."""
    body = text or ""
    if _YESNO_RE.search(body):
        return True
    low = body.lower()
    if any(w in low for w in _PROMPT_WORDS):
        return True
    return len(_MENU_RE.findall(body)) >= 2


def _send_settle_seconds() -> float:
    """Seconds to wait after typing the command before the first Enter, read PER
    CALL so it can be tuned live (or zeroed in tests) via VOXA_SEND_SETTLE_SECONDS.
    A settle window lets a busy Claude Code TUI finish rendering the input so the
    Enter lands as a submit, not as a newline inside the box."""
    try:
        return max(0.0, float(os.environ.get("VOXA_SEND_SETTLE_SECONDS", "0.3")))
    except (TypeError, ValueError):
        return 0.3


def _default_stuck_seconds() -> float:
    """How long the pane may sit unchanged WHILE GENERATING before the monitor
    fires the one-time "stuck" signal, read PER CALL (VOXA_STUCK_SECONDS) so it
    is tunable and testable. 0 (or negative) disables stuck detection."""
    try:
        return float(os.environ.get("VOXA_STUCK_SECONDS", "300"))
    except (TypeError, ValueError):
        return 300.0


def _send_enter_retries() -> int:
    """How many extra Enters to try if the first didn't submit (VOXA_SEND_ENTER_RETRIES).
    An Enter on an already-submitted/empty input is a harmless no-op in Claude Code, so
    over-retrying is SAFE; under-retrying is the send-reliability bug (command typed but
    never sent). Read per call so it is tunable and testable."""
    try:
        return max(0, int(os.environ.get("VOXA_SEND_ENTER_RETRIES", "3")))
    except (TypeError, ValueError):
        return 3


def _busy_grace_seconds() -> float:
    """Seconds after a send during which status='working' is trusted WITHOUT
    consulting the pane (VOXA_BUSY_GRACE_SECONDS): generation takes a moment to
    start rendering, and verify-on-read must never double-dispatch into that
    gap. Read per call so it is tunable and testable."""
    try:
        return max(0.0, float(os.environ.get("VOXA_BUSY_GRACE_SECONDS", "10")))
    except (TypeError, ValueError):
        return 10.0


def _input_still_pending(pane_text: str, typed: str) -> bool:
    """True when the just-typed command is STILL sitting unsubmitted in the pane's
    bottom input region, i.e. the Enter was absorbed as a newline instead of a submit.

    We match a distinctive tail of the typed text (its last ~24 non-space chars, or
    all of it if shorter) against the last few non-empty lines with whitespace
    removed: whitespace is collapsed because the input box soft-wraps/re-flows what
    was typed, and we look only at the BOTTOM lines so a match in the scrollback
    history (an earlier, already-submitted copy) can't be mistaken for pending input.
    Pure and tmux-free so the retry logic can be unit-tested without a real pane."""
    compact = "".join((typed or "").split())
    if not compact:
        return False
    tail = compact[-24:]
    stripped = _ANSI_RE.sub("", pane_text or "")
    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    if not lines:
        return False
    bottom = "".join("".join(ln.split()) for ln in lines[-6:])
    return tail in bottom


async def monitor_loop(ctrl) -> None:
    """Shared live monitor: announce new screen content when the controller's
    ``_capture()`` stabilises (Claude finished, or is waiting on a prompt).

    ``ctrl`` must expose: ``_capture()``, ``_started``, ``_poll``, ``_idle_polls``,
    ``status`` and an async ``_emit(text)``. Used by both the tmux and iTerm controllers.
    """
    try:
        baseline = ctrl._capture()
    except Exception:
        return
    last_key = stable_key(baseline)
    announced = clean_pane(baseline)
    stable = 0
    active = False
    while ctrl._started:
        await asyncio.sleep(ctrl._poll)
        try:
            cur = ctrl._capture()
        except Exception:
            break  # session gone
        # The generating marker is the authoritative "still working" signal.
        # Screen stability alone must not flip to idle (a long thinking stretch
        # renders no new output, and the stability key strips the marker
        # itself), so hold "working" and reset the count: idle needs a full
        # quiet window AFTER the marker is gone. ONLY the generating marker
        # counts: prompt screens ("Esc to cancel") are waiting on the USER.
        busy = pane_is_generating(cur)
        if busy:
            ctrl.status = "working"
            active = True
            stable = 0
        key = stable_key(cur)
        if key != last_key:
            last_key = key
            stable = 0
            active = True
            emit_output = getattr(ctrl, "_emit_output", None)
            if emit_output is not None:
                await emit_output(cur)
            emit_output_color = getattr(ctrl, "_emit_output_color", None)
            if emit_output_color is not None:
                await emit_output_color(cur)
        else:
            stable += 1
            if not busy and active and stable >= ctrl._idle_polls:
                cur_clean = clean_pane(cur)
                delta = new_text(announced, cur_clean)
                announced = cur_clean
                active = False
                ctrl.status = "idle"
                await ctrl._emit(delta)


# Named special keys `TmuxController.press()` sends by tmux key name (no `-l`
# literal flag), so tmux resolves each as that key rather than typing it out.
# Shared with ws_session's `claude_key` control as the server-side allowlist.
PRESS_KEY_NAMES: dict[str, str] = {
    "ctrl-c": "C-c",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "tab": "Tab",
    "esc": "Escape",
    "enter": "Enter",
    "backspace": "BSpace",
}


class TmuxController:
    def __init__(
        self,
        session_name: str = "voxa",
        runner: Optional[TmuxRunner] = None,
        launch_terminal: bool = True,
        terminal_app: str = "auto",
        socket: str = "voxa",
        poll_interval: float = 1.2,
        idle_polls: int = 3,
        timeout: float = 180.0,
        stuck_seconds: float | None = None,
    ):
        self._socket = socket
        self._run = runner or _make_default_runner(socket)
        self._session = session_name
        self._launch_terminal = launch_terminal
        self._terminal_app = terminal_app
        self._poll = poll_interval
        self._idle_polls = idle_polls
        self._timeout = timeout
        # Seconds the pane may sit unchanged while Claude is GENERATING before
        # _monitor fires the one-time "stuck" signal (VOXA_STUCK_SECONDS, read
        # here so a fresh controller always picks up the current env; 0 disables).
        self._stuck_seconds = _default_stuck_seconds() if stuck_seconds is None else stuck_seconds
        self.status = "idle"
        # True once this session has actually worked (or been sent a task) since the
        # last announce. Gates the "finished" announce so a fresh session that merely
        # booted to its idle prompt does not ring the phone on startup.
        self._saw_work = False
        # Serialize send(): a send now types + settles + verifies + retries Enter (up
        # to ~1.2s), so two rapid sends (double-tap, or a queue item overlapping a
        # typed command) must not interleave their send-keys into the same pane.
        self._send_lock = asyncio.Lock()
        # When the last send() happened, for verify_working's grace window.
        self._last_send_at = float("-inf")
        self.working_dir: Optional[str] = None
        # Set by start() when the visible terminal window could NOT be opened, so the
        # caller can tell the user how to attach manually (e.g. Automation denied).
        self.window_hint = ""
        self._final_cb: Optional[FinalCallback] = None
        # Optional callback for a stuck-agent alert (see on_stuck / _monitor); None
        # means stuck detection is fully inert (fail-open, no behavior change).
        self._on_stuck = None
        # Live-output callbacks: stream Claude's current screen to the UI while it
        # works. _on_output gets plain text (back-compat); _on_output_color gets the
        # same lines with ANSI colour escapes kept, for the terminal-themed view.
        self._on_output = None
        self._on_output_color = None
        self._started = False
        self._monitor_task: Optional[asyncio.Task] = None
        # One auto-Enter per appearance of the folder-trust prompt (see _monitor).
        self._trust_answered = False

    def set_terminal_app(self, app: str) -> None:
        """Override which terminal app to open (e.g. from a phone setting)."""
        if app:
            self._terminal_app = app

    def on_final(self, cb: FinalCallback) -> None:
        self._final_cb = cb

    def on_stuck(self, cb) -> None:
        """Register a callback invoked ONCE per quiet-while-generating stretch
        that outlasts VOXA_STUCK_SECONDS: cb(elapsed_seconds). Mirrors on_final;
        default is None, so nothing fires unless a caller registers one (the
        controller itself never rings the phone directly for a stuck agent)."""
        self._on_stuck = cb

    def on_output(self, cb) -> None:
        """Register a callback that receives Claude's live screen text (cleaned)."""
        self._on_output = cb

    def on_output_color(self, cb) -> None:
        """Register a callback that receives Claude's live screen WITH ANSI colour."""
        self._on_output_color = cb

    def _capture(self) -> str:
        # -e preserves SGR colour escapes (the live colour feed parses them on the
        # phone). clean_pane and _stable_key still strip ANSI, so idle detection and
        # the spoken finals are unaffected by this.
        return self._run(["capture-pane", "-p", "-e", "-t", self._session])

    def capture_text(self) -> str:
        """Public alias for the raw pane capture, used by the /hook approval
        scraper to turn an on-screen prompt into structured buttons."""
        return self._capture()

    def capture_scrollback(self, lines: int = 1200) -> str:
        """Capture the pane PLUS scrollback history (coloured, chrome-stripped) for the
        phone's full-screen terminal view. On-demand only (heavier than the live pane
        feed), `-S -N` reaches back into history."""
        try:
            raw = self._run(["capture-pane", "-p", "-e", "-S", f"-{lines}", "-t", self._session])
        except Exception:
            return ""
        return clean_pane_with_color(raw, max_lines=lines, max_bytes=128000)

    def _has_session(self) -> bool:
        try:
            self._run(["has-session", "-t", self._session])
            return True
        except Exception:
            return False

    def _has_client(self) -> bool:
        """True if a terminal window is currently attached to the session, so we
        don't open a duplicate, but DO open one if the window was closed."""
        try:
            return bool(self._run(["list-clients", "-t", self._session]).strip())
        except Exception:
            return False

    def _session_path(self) -> str:
        """The folder the existing tmux session is actually in (its pane's cwd), so
        a stale session from a previous run isn't reused for a different folder."""
        try:
            return self._run(
                ["display-message", "-p", "-t", self._session, "#{pane_current_path}"]
            ).strip()
        except Exception:
            return ""

    async def start(self, working_dir: str, resume: str | None = None) -> None:
        """Launch (kill+relaunch) the driven claude session in ``working_dir``.
        An optional ``resume`` stem launches ``claude --resume <stem>`` so a past
        conversation is reopened; an unsafe stem is dropped in _claude_launch_cmd."""
        path = os.path.abspath(os.path.expanduser(working_dir))
        if not os.path.isdir(path):
            raise ValueError(f"not a directory: {working_dir}")
        self.working_dir = path
        self.status = "idle"

        # An explicit "open/start a session" ALWAYS starts fresh: kill any existing
        # session (a leftover from a previous run, or a different project) and
        # relaunch claude clean in the requested folder. (Plain phone reconnects do
        # NOT call start(), so a running session still persists across reconnects.)
        if self._has_session():
            try:
                self._run(["kill-session", "-t", self._session])
            except Exception:
                pass
        existed = False
        if not existed:
            # Run interactive claude inside a detached tmux session in the project dir.
            # Launch via a login shell so the user's PATH (e.g. ~/.local/bin) is loaded,
            # and drop back to an interactive shell when claude exits so the window stays.
            shell = os.environ.get("SHELL", "/bin/bash")
            self._run([
                "new-session", "-d", "-s", self._session, "-c", path,
                "-x", "220", "-y", "50",
                shell, "-lc", f"{_claude_launch_cmd(resume)}; exec {shell} -il",
            ])
        self._started = True

        # Open a terminal window so the user can SEE the session. Open it when we
        # just made the session, OR when one exists but no window is attached (a
        # lingering detached session, or the user closed the window). Skip only when
        # a window is already attached, to avoid duplicates on reconnect.
        self.window_hint = ""
        if self._launch_terminal and sys.platform == "darwin" and (not existed or not self._has_client()):
            if not self._open_terminal():
                self.window_hint = (
                    "Couldn't open the terminal window automatically (check macOS "
                    "Automation permission for your terminal app). To see it, run: "
                    f"tmux -L {self._socket} attach -t {self._session}")

        # Start (or restart) the live monitor that watches the pane and surfaces
        # whatever Claude says or asks to the user by voice.
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        self._monitor_task = asyncio.ensure_future(self._monitor())

    def _open_terminal(self) -> bool:
        # Write an attach script so the window never just vanishes: it re-attaches while
        # the session lives, and falls back to an interactive shell if the session ends
        # (iTerm/Terminal close a window the moment its command returns, which caused the
        # "window flashes then disappears" bug).
        sock, sess = self._socket, self._session
        # Absolute tmux path: the terminal window runs a non-login shell whose PATH may
        # not include Homebrew, so a bare "tmux" is not found.
        tmux_bin = shutil.which("tmux") or "tmux"
        script_path = os.path.join(tempfile.gettempdir(), f"voxa-attach-{sock}.sh")
        body = (
            "#!/bin/bash\n"
            f'echo "Voxa, attaching to your Claude session ({sess})..."\n'
            "while true; do\n"
            f"  {tmux_bin} -L {sock} -f /dev/null attach -t {sess}\n"
            f"  {tmux_bin} -L {sock} -f /dev/null has-session -t {sess} 2>/dev/null || break\n"
            "  sleep 0.5\n"
            "done\n"
            'echo "Voxa session ended. (window kept open)"\n'
            'exec "$SHELL" -il\n'
        )
        try:
            with open(script_path, "w") as f:
                f.write(body)
            os.chmod(script_path, 0o755)
        except OSError:
            logger.exception("could not write attach script")
            return False

        cmd = f"bash {script_path}"
        # Try the preferred terminal app, then fall back to the other if its
        # AppleScript fails (e.g. iTerm isn't authorised for Automation, or isn't
        # installed). Returns True only if a window was actually opened.
        preferred = _resolve_terminal_app(self._terminal_app)
        order = ["iTerm", "Terminal"] if preferred == "iTerm" else ["Terminal", "iTerm"]
        for app in order:
            if self._run_open_script(app, cmd):
                return True
        logger.warning(
            "could not open a terminal window (Automation permission?); "
            "attach manually: tmux -L %s attach -t %s", self._socket, self._session)
        return False

    @staticmethod
    def _open_script_for(app: str, cmd: str) -> str:
        if app == "iTerm":
            return ('tell application "iTerm"\n'
                    "  activate\n"
                    f'  create window with default profile command "{cmd}"\n'
                    "end tell")
        return (f'tell application "Terminal" to do script "{cmd}"\n'
                'tell application "Terminal" to activate')

    def _run_open_script(self, app: str, cmd: str) -> bool:
        """Run the open-window AppleScript for `app`; True if it actually succeeded
        (osascript exits non-zero when Automation is denied or the app is missing)."""
        try:
            r = subprocess.run(["osascript", "-e", self._open_script_for(app, cmd)],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                return True
            logger.warning("osascript open via %s failed: %s", app, (r.stderr or "").strip())
        except Exception:
            logger.exception("osascript open via %s raised", app)
        return False

    async def send(self, text: str) -> bool:
        """Inject the user's words into the live claude session AND make sure they
        actually submit. On a busy Claude Code TUI the single Enter can race the input
        rendering and be absorbed as a newline, leaving the command typed but UNSENT
        (the send-reliability bug). So we type, settle, Enter, then VERIFY the input box
        cleared by re-capturing the pane and re-send Enter up to VOXA_SEND_ENTER_RETRIES
        times (an Enter on an already-submitted input is a harmless no-op).

        Returns True once submission is confirmed (the typed tail is gone from the
        bottom input region), or optimistically when the pane can't be read (empty
        capture: one best-effort extra Enter, no blind spinning). Returns False if the
        input never cleared within the retries. NEVER raises: any tmux error logs, sets
        status 'error', and returns False, so a failed send can be reported, not crash."""
        if not self._started:
            raise ValueError("call start() before send()")
        self.status = "working"
        self._last_send_at = time.monotonic()   # verify_working's grace window
        self._saw_work = True   # a dispatched task should announce when it finishes
        typed = text or ""
        settle = _send_settle_seconds()
        retries = _send_enter_retries()
        # Hold the lock across the whole type+verify+retry sequence so a second send
        # can't inject its keystrokes into this one's pane mid-submit.
        async with self._send_lock:
            try:
                self._run(["send-keys", "-t", self._session, "-l", typed])
                await asyncio.sleep(settle)
                self._run(["send-keys", "-t", self._session, "Enter"])
                for _ in range(retries):
                    if settle:
                        await asyncio.sleep(settle)
                    try:
                        pane = self._capture()
                    except Exception:
                        pane = ""
                    if not pane:
                        # No pane text to verify against: one best-effort extra Enter and
                        # report sent optimistically, rather than spin blind on a session
                        # whose capture returns nothing.
                        self._run(["send-keys", "-t", self._session, "Enter"])
                        return True
                    if not _input_still_pending(pane, typed):
                        return True   # confirmed: the input box cleared, it submitted
                    # Still sitting unsubmitted: the Enter was swallowed, press it again.
                    self._run(["send-keys", "-t", self._session, "Enter"])
                return False   # never cleared within the retries
            except Exception:
                logger.exception("tmux send failed")
                self.status = "error"
                return False

    async def verify_working(self) -> bool:
        """Is Claude REALLY still working? send() sets status='working'
        optimistically and only the monitor's activity-then-quiet cycle resets
        it, so a send that never produced pane activity (e.g. an instruction
        that failed to submit) wedges the flag forever and every later dispatch
        is refused as busy. The orchestrator's busy guard calls this at
        decision time: a fresh send is trusted for a short grace window, after
        that the LIVE pane is consulted; no generating marker means the flag is
        stale, so it heals to idle. Fail-safe: a capture error keeps the old
        answer (True) rather than risking a double dispatch."""
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
        """Inject a single keypress WITHOUT Enter, used to answer a structured
        approval (a menu digit, "y"/"n"), dismiss a prompt ("esc"), or drive a
        named special key ("up", "tab", ...) without also submitting whatever
        text happens to sit in the input box.

        `key` in `PRESS_KEY_NAMES` is sent by its tmux key name (no `-l`), so
        tmux resolves it as that key rather than typing it out. A single
        printable character (an approval's option key: "1", "y", ...) is sent
        `-l` literal, so it's a keystroke, not e.g. tmux's window-1 binding.
        A run of digits ("10", "11", ...) is ALSO sent `-l` literal: an
        approval menu can have 10+ options, and the digit is still just a
        keystroke, never a tmux binding. Anything else (an unrecognised
        multi-character name that isn't all digits) is rejected with
        `ValueError` BEFORE touching tmux, so callers (`Orchestrator.press_key`)
        can surface it as an error instead of silently doing nothing."""
        if not self._started:
            raise ValueError("call start() before press()")
        tmux_name = PRESS_KEY_NAMES.get(key)
        if tmux_name is None and len(key) > 1 and key not in PRESS_KEY_NAMES and not key.isdigit():
            raise ValueError(f"press: unsupported key name {key!r}")
        try:
            if tmux_name is not None:
                self._run(["send-keys", "-t", self._session, tmux_name])
            else:
                self._run(["send-keys", "-t", self._session, "-l", key])
        except Exception:
            logger.exception("tmux press failed")

    def _stable_key(self, text: str) -> str:
        """Normalize a pane for change-detection: drop volatile chrome (spinners, the
        status bar, ticking timers/costs) so 'idle' is detected even though those keep
        changing."""
        out = []
        for ln in clean_pane(text).splitlines():
            low = ln.lower()
            if any(k in low for k in (
                "esc to interrupt", "tokens", "context left", "auto-update",
                "/doctor", "crunch", "✶", "✻", "✽",
            )):
                continue
            out.append(re.sub(r"\d+", "#", ln))  # ignore changing numbers
        return "\n".join(out)

    async def _emit(self, text: str) -> None:
        if text.strip() and self._final_cb is not None:
            result = self._final_cb(text)
            if inspect.isawaitable(result):
                await result

    async def _emit_output(self, raw: str) -> None:
        """Push the current (cleaned) screen to the live-output UI, if anyone's
        listening. Throttled naturally by the monitor's poll interval."""
        if self._on_output is None:
            return
        text = clean_pane(raw)
        if not text.strip():
            return
        result = self._on_output(text)
        if inspect.isawaitable(result):
            await result

    async def _emit_output_color(self, raw: str) -> None:
        """Push the current screen WITH colour to the terminal-themed UI, if anyone's
        listening. Mirrors _emit_output; throttled by the monitor's poll interval."""
        if self._on_output_color is None:
            return
        text = clean_pane_with_color(raw)
        if not text.strip():
            return
        result = self._on_output_color(text)
        if inspect.isawaitable(result):
            await result

    async def _monitor(self) -> None:
        """Watch the pane; when Claude stops changing (finished, or waiting on a
        question/menu/permission prompt), announce the new screen content so the
        operator can read it to the user and ask what to do."""
        try:
            baseline = self._capture()
        except Exception:
            return
        last_key = self._stable_key(baseline)
        announced = clean_pane(baseline)
        stable = 0
        active = False
        # Stuck-detection state: stuck_since is the monotonic time the CURRENT
        # stable_key first appeared while Claude was generating; stuck_key is
        # that key (so a pane change is detected even if last_key/key bookkeeping
        # above hasn't updated yet); stuck_fired latches so at most one on_stuck
        # callback fires per quiet-while-generating stretch.
        stuck_since: float | None = None
        stuck_key: str | None = None
        stuck_fired = False
        while self._started:
            await asyncio.sleep(self._poll)
            try:
                cur = self._capture()
            except Exception:
                break  # session gone
            plain = _ANSI_RE.sub("", cur).lower()
            if any(m in plain for m in _ACTIVE_MARKERS):
                self._saw_work = True   # generation OR a prompt: announce on settle
            # Auto-accept the folder-trust prompt on sessions VOXA launched: the
            # user explicitly asked to open this folder, so the trust question is
            # their own already-stated intent, and an unanswered prompt strands
            # the session ("it can't press enter"). "Yes, I trust this folder" is
            # preselected, so a single Enter confirms. One press per appearance.
            if "trust this folder" in plain:
                if not self._trust_answered:
                    self._trust_answered = True
                    try:
                        self._run(["send-keys", "-t", self._session, "Enter"])
                    except Exception:
                        logger.exception("auto-trust press failed")
                continue
            self._trust_answered = False
            # The generating marker is the authoritative "still working" signal.
            # Screen stability alone must not flip to idle (a long thinking
            # stretch renders no new output, and _stable_key strips the marker
            # itself), so hold "working" and reset the count: idle needs a full
            # quiet window AFTER the marker is gone. ONLY the generating marker
            # counts here: prompt screens ("Esc to cancel") are waiting on the
            # USER and must settle to idle so they get announced and answered.
            busy = pane_is_generating(cur)
            if busy:
                self.status = "working"
                active = True
                stable = 0
            key = self._stable_key(cur)
            # Stuck detection: only while ACTUALLY generating (never on a mere
            # idle prompt sitting unanswered). Reset the timer and the one-time
            # latch whenever the pane changes or Claude stops generating, so
            # each new quiet-while-working stretch can alert at most once, and
            # a normal finish (busy goes False) never triggers it.
            if busy and self._stuck_seconds > 0:
                if stuck_since is None or key != stuck_key:
                    stuck_since = time.monotonic()
                    stuck_key = key
                    stuck_fired = False
                elif not stuck_fired and (time.monotonic() - stuck_since) >= self._stuck_seconds:
                    stuck_fired = True
                    cb = self._on_stuck
                    if cb is not None:
                        result = cb(time.monotonic() - stuck_since)
                        if inspect.isawaitable(result):
                            await result
            else:
                stuck_since = None
                stuck_key = None
                stuck_fired = False
            if key != last_key:
                last_key = key
                stable = 0
                active = True
                await self._emit_output(cur)         # plain live stream (back-compat)
                await self._emit_output_color(cur)   # colour live stream (themed view)
            else:
                stable += 1
                if not busy and active and stable >= self._idle_polls:
                    cur_clean = clean_pane(cur)
                    delta = new_text(announced, cur_clean)
                    announced = cur_clean
                    active = False
                    self.status = "idle"
                    # Announce only a real finish (work happened) or a genuine prompt;
                    # a fresh boot settling to its idle prompt must not fire (it would
                    # ring the phone the moment a session starts).
                    if self._saw_work or looks_actionable(cur_clean):
                        self._saw_work = False
                        await self._emit(delta)

    async def interrupt(self) -> None:
        """Stop the CURRENT generation only: send Escape and leave everything else
        running (tmux session, monitor, _started). Claude Code shows 'Interrupted'
        and returns to its prompt with context intact, so the user can follow up
        immediately; the monitor announces the settled screen as usual. Status is
        optimistically set idle so a follow-up dispatch isn't refused as busy; if
        Claude is somehow still generating, the monitor re-detects the active
        marker on its next poll and flips it back. Contrast stop(), which also
        detaches the monitor (call teardown / controller swap).

        One Escape is NOT always enough (vim INSERT mode eats the first press);
        see interrupt_needs_retry for the retry decision. The 0.7s gap keeps two
        consecutive presses from reading as a double-esc (history jump) if the
        first one already landed the interrupt."""
        if not self._started:
            return
        try:
            before = self._capture()
            self._run(["send-keys", "-t", self._session, "Escape"])
            for _ in range(2):
                await asyncio.sleep(0.7)
                now = self._capture()
                if not interrupt_needs_retry(before, now):
                    break
                before = now
                self._run(["send-keys", "-t", self._session, "Escape"])
        except Exception:
            logger.exception("tmux interrupt failed")
            return
        self.status = "idle"

    async def stop(self, *, detach_only: bool = False) -> None:
        # Do NOT kill the tmux session: the laptop terminal must stay usable after the
        # phone hangs up. Stop the monitor and (unless detach_only) send an interrupt
        # (Escape) to halt any in-progress generation. detach_only is used when swapping
        # to another terminal, so the session we leave keeps running its task.
        self._started = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        if not detach_only:
            try:
                self._run(["send-keys", "-t", self._session, "Escape"])
            except Exception:
                pass
        self.status = "idle"

    async def reattach(self) -> bool:
        """Re-arm this controller to drive an ALREADY-RUNNING tmux session after a
        prior detach_only stop, WITHOUT the kill+relaunch that start() does.

        Fleet switching detaches the session it leaves (stop(detach_only=True),
        which clears _started and cancels the monitor); switching back must be
        able to drive that still-running Claude again. start() cannot be reused
        here because it kills and relaunches the session, throwing away the very
        work we kept alive. This restarts the live monitor from the current pane
        state and marks the controller drivable again.

        No-op returning False when the tmux session is gone (e.g. the very first
        connect before any start(), or a session the user closed): there is
        nothing to re-arm, so _started stays False and send()/press() keep
        degrading to a clean error rather than talking to a dead pane."""
        if not self._has_session():
            return False
        self._started = True
        # A session we merely re-armed (never launched via start()) has no
        # working_dir yet, so it would report "" and show as "/" on the phone,
        # and cwd-keyed routing (find_by_cwd, the fleet label) would miss it.
        # Probe the pane's real cwd to fill it in. Fail-open: an empty/failed
        # probe leaves working_dir as-is rather than blanking a known folder.
        if not self.working_dir:
            path = self._session_path()
            if path:
                self.working_dir = path
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        self._monitor_task = asyncio.ensure_future(self._monitor())
        return True
