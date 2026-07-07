import asyncio

from server.tmux_controller import (
    TmuxController, clean_pane, clean_pane_with_color, new_text, _resolve_terminal_app,
)


def test_clean_pane_drops_claude_status_footer():
    # The model/effort/usage footer must never reach the operator (it kept reading
    # "⚡ xhigh /effort" and asking the user about it) or the live view.
    raw = (
        "Here is your answer.\n"
        "🤖 Opus 4.8 (1M context) ⚡ xhigh ⊙ 5h 66%\n"
        "💰 $0.47 │ 42.1k/1M\n"
        "⏵⏵ bypass permissions on · ← for agents\n"
        "/effort to change\n"
    )
    out = clean_pane(raw)
    assert "Here is your answer." in out
    assert "xhigh" not in out and "Opus 4.8" not in out and "effort" not in out.lower()
    color = clean_pane_with_color(raw)
    assert "xhigh" not in color and "Opus 4.8" not in color


def test_clean_pane_with_color_keeps_ansi_but_drops_chrome():
    raw = (
        "\x1b[31mError\x1b[39m in \x1b[34m/Users/dev/app.py\x1b[39m\n"
        "────────────\n"
        "  esc to interrupt\n"
        "\x1b[2m✶ Crunched for 5s\x1b[22m\n"
        "    indented code line\n"
    )
    colored = clean_pane_with_color(raw)
    plain = clean_pane(raw)
    # colour escapes preserved on surviving lines
    assert "\x1b[31m" in colored and "\x1b[34m" in colored
    # same chrome dropped as clean_pane (decisions run on the ANSI-stripped copy)
    assert "esc to interrupt" not in colored
    assert "Crunched" not in colored
    assert "────" not in colored
    # leading indentation preserved
    assert "    indented code line" in colored
    # clean_pane (unchanged) still strips all ANSI
    assert "\x1b[" not in plain


def test_resolve_terminal_app_explicit():
    assert _resolve_terminal_app("iTerm2") == "iTerm"
    assert _resolve_terminal_app("iterm") == "iTerm"
    assert _resolve_terminal_app("Terminal") == "Terminal"


class FakeTmux:
    """Records tmux invocations and returns scripted capture-pane output."""
    def __init__(self, captures):
        self.calls = []
        self._caps = list(captures)
        self.exists = False
        self.session_path = ""     # what display-message reports as the pane cwd
        self._last = ""

    def __call__(self, args):
        args = list(args)
        self.calls.append(args)
        cmd = args[0]
        if cmd == "has-session":
            if not self.exists:
                raise RuntimeError("no session")
            return ""
        if cmd == "new-session":
            self.exists = True
            return ""
        if cmd == "kill-session":
            self.exists = False
            return ""
        if cmd == "display-message":
            return self.session_path
        if cmd == "capture-pane":
            if self._caps:
                self._last = self._caps.pop(0)
            return self._last
        return ""


def test_clean_pane_strips_chrome():
    raw = (
        "\x1b[31mHello\x1b[0m\n"
        "────────────\n"
        "│ type here │\n"
        "> prompt\n"
        "  esc to interrupt\n"
        "Real answer"
    )
    cleaned = clean_pane(raw)
    assert "Hello" in cleaned
    assert "Real answer" in cleaned
    assert "esc to interrupt" not in cleaned
    assert "─" not in cleaned
    assert "type here" not in cleaned


def test_clean_pane_strips_mcp_and_status_noise():
    raw = (
        "Files in /Users/dev/Desktop:\n"
        "- 9XAI/\n"
        "⚠ 1 MCP\n"
        "MCP server github failed to connect\n"
        "✶ Crunched for 5s\n"
        "Opus 4.8 (1M context)  $0.57  32%\n"
        "-- INSERT --  bypass permissions on (shift+tab to cycle)\n"
    )
    cleaned = clean_pane(raw)
    assert "Files in /Users/dev/Desktop:" in cleaned
    assert "9XAI" in cleaned
    assert "MCP" not in cleaned and "mcp" not in cleaned.lower()
    assert "Crunched" not in cleaned
    assert "bypass permissions" not in cleaned.lower()
    assert "$0.57" not in cleaned


def test_new_text_returns_delta_else_all():
    assert new_text("a\nb", "a\nb\nc") == "c"
    assert new_text("x", "x") == "x"  # nothing new -> fall back to whole capture


def test_open_script_for_targets_the_right_app():
    c = TmuxController(runner=lambda a: "", launch_terminal=False)
    assert 'application "iTerm"' in c._open_script_for("iTerm", "bash x")
    assert 'application "Terminal"' in c._open_script_for("Terminal", "bash x")


def test_open_terminal_falls_back_when_preferred_fails(monkeypatch):
    c = TmuxController(runner=lambda a: "", launch_terminal=True, terminal_app="iterm",
                       socket="t", session_name="s")
    tried = []

    def fake(app, cmd):
        tried.append(app)
        return app == "Terminal"   # iTerm denied/unavailable, Terminal works

    monkeypatch.setattr(c, "_run_open_script", fake)
    assert c._open_terminal() is True
    assert tried == ["iTerm", "Terminal"]   # preferred first, then fallback


def test_open_terminal_returns_false_when_all_fail(monkeypatch):
    c = TmuxController(runner=lambda a: "", launch_terminal=True, terminal_app="iterm",
                       socket="t", session_name="s")
    monkeypatch.setattr(c, "_run_open_script", lambda app, cmd: False)
    assert c._open_terminal() is False


async def test_start_creates_session_and_send_injects_keys(tmp_path):
    fake = FakeTmux(["> "])
    c = TmuxController(runner=fake, launch_terminal=False, poll_interval=0.005, idle_polls=2)
    await c.start(str(tmp_path))
    assert any(a[0] == "new-session" for a in fake.calls)
    assert c.working_dir == str(tmp_path)

    await c.send("make a page")
    assert ["send-keys", "-t", "voxa", "-l", "make a page"] in fake.calls
    assert ["send-keys", "-t", "voxa", "Enter"] in fake.calls
    await c.stop()


async def test_monitor_announces_when_claude_goes_idle(tmp_path):
    # Claude WORKS (shows the running marker) then settles on a prompt: announce it.
    fake = FakeTmux(["", "Working on it\nesc to interrupt", "Claude asks: 1. Yes  2. No"])
    spoken = []
    c = TmuxController(runner=fake, launch_terminal=False, poll_interval=0.005, idle_polls=2)
    c.on_final(lambda t: spoken.append(t))
    await c.start(str(tmp_path))
    await asyncio.sleep(0.15)
    await c.stop()
    assert spoken and "Yes" in spoken[0]


async def test_monitor_does_not_announce_on_fresh_boot(tmp_path):
    # A fresh session that just boots to its idle prompt (no work, no prompt) must NOT
    # announce, so starting a session never rings the phone.
    fake = FakeTmux(["", "Welcome to Claude Code", "> ready to help"])
    spoken = []
    c = TmuxController(runner=fake, launch_terminal=False, poll_interval=0.005, idle_polls=2)
    c.on_final(lambda t: spoken.append(t))
    await c.start(str(tmp_path))
    await asyncio.sleep(0.15)
    await c.stop()
    assert spoken == []


async def test_start_always_starts_fresh(tmp_path):
    # An explicit "open a session" kills any existing session and recreates it,
    # even in the same folder (the user wants a fresh Claude, not the old chat).
    fake = FakeTmux(["> "])
    fake.exists = True
    fake.session_path = str(tmp_path)
    c = TmuxController(runner=fake, launch_terminal=False, poll_interval=0.005)
    await c.start(str(tmp_path))
    assert any(a[0] == "kill-session" for a in fake.calls)
    assert any(a[0] == "new-session" for a in fake.calls)
    await c.stop()


async def test_stale_session_in_other_folder_is_recreated(tmp_path):
    # A leftover session from a previous run, in a DIFFERENT folder, must be killed
    # and recreated in the requested folder (not silently reused).
    old = tmp_path / "old"; new = tmp_path / "new"; old.mkdir(); new.mkdir()
    fake = FakeTmux(["> "])
    fake.exists = True
    fake.session_path = str(old)
    c = TmuxController(runner=fake, launch_terminal=False, poll_interval=0.005)
    await c.start(str(new))
    assert any(a[0] == "kill-session" for a in fake.calls)
    assert any(a[0] == "new-session" for a in fake.calls)
    await c.stop()


async def test_reattach_populates_working_dir_from_pane():
    # A resumed/adopted session must report its REAL folder, not "" (shown as "/"):
    # reattach probes the pane's current path when working_dir is unset.
    fake = FakeTmux(["> "])
    fake.exists = True
    fake.session_path = "/Users/me/loop"
    c = TmuxController(runner=fake, launch_terminal=False, poll_interval=0.005)
    assert c.working_dir is None
    ok = await c.reattach()
    assert ok is True
    assert c.working_dir == "/Users/me/loop"
    await c.stop()


async def test_reattach_empty_pane_path_leaves_working_dir_unchanged():
    # Fail-open: an empty/failed pane-path probe must not crash and must leave
    # working_dir as it was (still None), never blanking a real folder.
    fake = FakeTmux(["> "])
    fake.exists = True
    fake.session_path = ""            # probe returns nothing
    c = TmuxController(runner=fake, launch_terminal=False, poll_interval=0.005)
    ok = await c.reattach()
    assert ok is True
    assert c.working_dir is None
    await c.stop()


async def test_reattach_does_not_overwrite_existing_working_dir():
    # When working_dir is already known, reattach must not re-probe/overwrite it.
    fake = FakeTmux(["> "])
    fake.exists = True
    fake.session_path = "/Users/me/other"
    c = TmuxController(runner=fake, launch_terminal=False, poll_interval=0.005)
    c.working_dir = "/Users/me/loop"
    await c.reattach()
    assert c.working_dir == "/Users/me/loop"
    await c.stop()


async def test_stop_does_not_kill_session(tmp_path):
    fake = FakeTmux(["> "])
    c = TmuxController(runner=fake, launch_terminal=False)
    await c.start(str(tmp_path))
    fake.calls.clear()
    await c.stop()
    assert all(a[0] != "kill-session" for a in fake.calls)
    assert c.status == "idle"


def test_claude_launch_normal_env_by_default(monkeypatch):
    # Default: the user's normal environment (no isolated config dir).
    monkeypatch.delenv("VOXA_ISOLATE_CLAUDE", raising=False)
    from server.tmux_controller import _claude_launch_cmd
    assert _claude_launch_cmd() == "claude --dangerously-skip-permissions"

def test_claude_launch_can_isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VOXA_ISOLATE_CLAUDE", "1")
    from server.tmux_controller import _claude_launch_cmd
    assert "CLAUDE_CONFIG_DIR=" in _claude_launch_cmd()


def test_claude_launch_with_resume_stem(monkeypatch):
    # A valid resume stem builds `claude --resume <stem> --dangerously-skip-permissions`
    # so the phone can reconnect to a past conversation.
    monkeypatch.delenv("VOXA_ISOLATE_CLAUDE", raising=False)
    from server.tmux_controller import _claude_launch_cmd
    assert _claude_launch_cmd("abc-123_DEF") == (
        "claude --resume abc-123_DEF --dangerously-skip-permissions")


def test_claude_launch_rejects_unsafe_resume_stem(monkeypatch):
    # A stem with anything but alnum/dash/underscore is dropped (never shell-injected);
    # the launch falls back to a normal (non-resume) invocation.
    monkeypatch.delenv("VOXA_ISOLATE_CLAUDE", raising=False)
    from server.tmux_controller import _claude_launch_cmd
    for bad in ("foo; rm -rf /", "a b", "$(x)", "`id`", "a/b", ""):
        assert _claude_launch_cmd(bad) == "claude --dangerously-skip-permissions"


async def test_start_passes_resume_into_launch(tmp_path):
    # start(working_dir, resume=...) threads the stem into the launched command.
    fake = FakeTmux(["> "])
    c = TmuxController(runner=fake, launch_terminal=False, poll_interval=0.005, idle_polls=2)
    await c.start(str(tmp_path), resume="sess42")
    new = next(a for a in fake.calls if a[0] == "new-session")
    joined = " ".join(new)
    assert "--resume sess42" in joined
    await c.stop()


async def test_start_without_resume_has_no_resume_flag(tmp_path):
    fake = FakeTmux(["> "])
    c = TmuxController(runner=fake, launch_terminal=False, poll_interval=0.005, idle_polls=2)
    await c.start(str(tmp_path))
    new = next(a for a in fake.calls if a[0] == "new-session")
    assert "--resume" not in " ".join(new)
    await c.stop()


import asyncio as _asyncio
from server.tmux_controller import monitor_loop as _monitor_loop


async def test_monitor_loop_streams_live_output_when_hooks_present():
    # A minimal controller exposing the monitor_loop contract plus the new hooks.
    screens = ["first screen", "first screen", "second screen", "second screen"]
    state = {"i": 0}

    class Ctrl:
        def __init__(self):
            self.status = "idle"
            self._started = True
            self._poll = 0.005
            self._idle_polls = 2
            self.outputs = []
            self.color_outputs = []
            self.finals = []

        def _capture(self):
            i = min(state["i"], len(screens) - 1)
            state["i"] += 1
            return screens[i]

        async def _emit(self, text):
            self.finals.append(text)

        async def _emit_output(self, raw):
            self.outputs.append(raw)

        async def _emit_output_color(self, raw):
            self.color_outputs.append(raw)

    c = Ctrl()
    task = _asyncio.ensure_future(_monitor_loop(c))
    await _asyncio.sleep(0.1)
    c._started = False
    await _asyncio.sleep(0.02)
    task.cancel()
    # The screen changed once (first -> second), so the live-output hook fired
    # with the raw screen at least once.
    assert any("second screen" in o for o in c.outputs)
    assert any("second screen" in o for o in c.color_outputs)


async def test_monitor_loop_without_hooks_still_works():
    # A controller lacking _emit_output must not raise (getattr guard).
    screens = ["a", "a", "b", "b", "b"]
    state = {"i": 0}

    class Ctrl:
        def __init__(self):
            self.status = "idle"; self._started = True
            self._poll = 0.005; self._idle_polls = 2; self.finals = []
        def _capture(self):
            i = min(state["i"], len(screens) - 1); state["i"] += 1; return screens[i]
        async def _emit(self, text): self.finals.append(text)

    c = Ctrl()
    task = _asyncio.ensure_future(_monitor_loop(c))
    await _asyncio.sleep(0.1)
    c._started = False
    await _asyncio.sleep(0.02)
    task.cancel()
    assert c.finals  # emitted a final on stabilise, no crash from the missing hook


# --- pick_session_name (per-session names, legacy adoption) --------------------
from server.tmux_controller import pick_session_name


def test_pick_adopts_legacy_voxa_session():
    def run(args):
        assert list(args) == ["list-sessions", "-F", "#{session_name}"]
        return "scratch\nvoxa\nother\n"
    assert pick_session_name("abc123", runner=run) == "voxa"


def test_pick_adopts_prefixed_session_from_previous_run():
    def run(args):
        return "voxa-9f21aa77\n"
    assert pick_session_name("new1", runner=run) == "voxa-9f21aa77"


def test_pick_new_name_when_tmux_unavailable():
    def run(args):
        raise RuntimeError("no tmux server")
    assert pick_session_name("abc123", runner=run) == "voxa-abc123"


def test_pick_new_name_when_no_voxa_sessions():
    def run(args):
        return "work\nmisc\n"
    assert pick_session_name("abc123", runner=run) == "voxa-abc123"


def test_pick_cwd_aware_adopts_only_matching_cwd_leftover():
    # Two leftover voxa sessions from previous runs, in different folders. Passing
    # a cwd must adopt ONLY the one whose pane is actually sitting there.
    paths = {"voxa-old1": "/p/other", "voxa-old2": "/p/match"}

    def run(args):
        if args[0] == "list-sessions":
            return "voxa-old1\nvoxa-old2\n"
        if args[0] == "display-message":
            name = args[args.index("-t") + 1]
            return paths[name]
        return ""
    assert pick_session_name("new1", runner=run, cwd="/p/match") == "voxa-old2"
    assert pick_session_name("new1", runner=run, cwd="/p/match/") == "voxa-old2"


def test_pick_cwd_aware_fresh_name_when_no_leftover_matches():
    def run(args):
        if args[0] == "list-sessions":
            return "voxa-old1\n"
        if args[0] == "display-message":
            return "/p/other"
        return ""
    assert pick_session_name("new1", runner=run, cwd="/p/match") == "voxa-new1"


def test_pick_cwd_none_keeps_back_compat_behavior():
    # cwd=None (existing callers) must never probe display-message and must adopt
    # the FIRST candidate exactly like today, regardless of its actual pane cwd.
    probed = []

    def run(args):
        if args[0] == "list-sessions":
            return "voxa-old1\nvoxa-old2\n"
        probed.append(args)
        return ""
    assert pick_session_name("new1", runner=run) == "voxa-old1"
    assert probed == []   # no display-message probe when cwd is not given


# --- press (approval decision -> keypress, no Enter) ----------------------------


async def test_press_sends_bare_key_without_enter():
    calls = []
    c = TmuxController(runner=lambda a: calls.append(list(a)) or "", launch_terminal=False)
    c._started = True
    await c.press("1")
    assert ["send-keys", "-t", c._session, "-l", "1"] in calls
    assert not any(a[-1] == "Enter" for a in calls)


async def test_press_esc_sends_named_escape():
    calls = []
    c = TmuxController(runner=lambda a: calls.append(list(a)) or "", launch_terminal=False)
    c._started = True
    await c.press("esc")
    assert ["send-keys", "-t", c._session, "Escape"] in calls


async def test_press_named_keys_send_without_literal_flag():
    # Every entry in the name map is sent by tmux key name, never `-l` literal,
    # so tmux resolves e.g. "Up" as the arrow key rather than typing "U", "p".
    from server.tmux_controller import PRESS_KEY_NAMES
    for name, tmux_name in PRESS_KEY_NAMES.items():
        calls = []
        c = TmuxController(runner=lambda a: calls.append(list(a)) or "", launch_terminal=False)
        c._started = True
        await c.press(name)
        assert calls == [["send-keys", "-t", c._session, tmux_name]], name


async def test_press_single_printable_char_still_sent_literal():
    # Approval keys ("1", "y", ...) must keep going through -l: they are NOT in
    # the name map and must not be reinterpreted as tmux bindings.
    calls = []
    c = TmuxController(runner=lambda a: calls.append(list(a)) or "", launch_terminal=False)
    c._started = True
    await c.press("y")
    assert calls == [["send-keys", "-t", c._session, "-l", "y"]]


async def test_press_unknown_multi_char_key_raises_value_error():
    calls = []
    c = TmuxController(runner=lambda a: calls.append(list(a)) or "", launch_terminal=False)
    c._started = True
    try:
        await c.press("pagedown")
        assert False, "expected ValueError"
    except ValueError:
        pass
    assert calls == []   # nothing was sent to tmux for an unrecognised name


async def test_press_multi_digit_number_sent_literal_no_enter():
    # An approval menu can have 10+ options ("10", "11", ...): a run of digits
    # must reach tmux as a literal keystroke, not be rejected as an unknown
    # multi-character name, and never followed by Enter.
    calls = []
    c = TmuxController(runner=lambda a: calls.append(list(a)) or "", launch_terminal=False)
    c._started = True
    await c.press("10")
    assert calls == [["send-keys", "-t", c._session, "-l", "10"]]
    assert not any(a[-1] == "Enter" for a in calls)


async def test_press_non_digit_multi_char_key_still_raises_value_error():
    calls = []
    c = TmuxController(runner=lambda a: calls.append(list(a)) or "", launch_terminal=False)
    c._started = True
    try:
        await c.press("hello")
        assert False, "expected ValueError"
    except ValueError:
        pass
    assert calls == []


def test_capture_text_aliases_capture():
    c = TmuxController(runner=lambda a: "pane text", launch_terminal=False)
    assert c.capture_text() == "pane text"


# --- reliable submit: verify + retry the Enter (the send-reliability bug) --------

from server.tmux_controller import _input_still_pending


def test_input_still_pending_detects_unsubmitted_tail():
    # The typed command still sitting in the bottom input box means the Enter was
    # swallowed (not submitted); once the box clears it is no longer pending.
    typed = "please refactor the send path and add tests for it"
    pane_pending = ("scrollback line\nmore output\n"
                    "> please refactor the send path and add tests for it\n")
    assert _input_still_pending(pane_pending, typed) is True
    pane_clear = "scrollback line\nClaude is working on it...\n> \n"
    assert _input_still_pending(pane_clear, typed) is False
    # Empty typed text is never pending (nothing to submit).
    assert _input_still_pending("> ", "") is False


def test_input_still_pending_ignores_soft_wrap_whitespace():
    # The input box re-flows/soft-wraps what was typed; matching on a whitespace-
    # collapsed tail still finds it across the wrap.
    typed = "run the entire integration suite against staging now"
    wrapped = "> run the entire integration\n  suite against staging now\n"
    assert _input_still_pending(wrapped, typed) is True


class FakeSendTmux:
    """Records tmux invocations and returns scripted capture-pane output, keeping
    the LAST scripted capture once the script is exhausted (so 'still pending'
    tests can keep returning the unsubmitted screen without listing it N times)."""
    def __init__(self, captures):
        self.calls = []
        self._caps = list(captures)

    def __call__(self, args):
        args = list(args)
        self.calls.append(args)
        if args[0] == "capture-pane":
            if len(self._caps) > 1:
                return self._caps.pop(0)
            return self._caps[0] if self._caps else ""
        return ""


def _enters(calls):
    return [a for a in calls if a and a[-1] == "Enter"]


async def test_send_retries_enter_until_input_clears(monkeypatch):
    monkeypatch.setenv("VOXA_SEND_SETTLE_SECONDS", "0")
    monkeypatch.setenv("VOXA_SEND_ENTER_RETRIES", "3")
    typed = "please run the full test suite now"
    pending = "output\n> " + typed          # tail still sitting in the input box
    caps = [pending, pending, "> \n(idle, ready)"]   # clears on the 3rd capture
    fake = FakeSendTmux(caps)
    c = TmuxController(runner=fake, launch_terminal=False)
    c._started = True
    ok = await c.send(typed)
    assert ok is True
    # Initial Enter plus at least one retry Enter fired while it was still pending.
    assert len(_enters(fake.calls)) >= 2


async def test_send_returns_false_when_never_submits(monkeypatch):
    monkeypatch.setenv("VOXA_SEND_SETTLE_SECONDS", "0")
    monkeypatch.setenv("VOXA_SEND_ENTER_RETRIES", "2")
    typed = "deploy to production"
    stuck = "> " + typed                     # the box never clears
    fake = FakeSendTmux([stuck])
    c = TmuxController(runner=fake, launch_terminal=False)
    c._started = True
    assert await c.send(typed) is False


async def test_send_returns_true_on_confirmed_submit(monkeypatch):
    monkeypatch.setenv("VOXA_SEND_SETTLE_SECONDS", "0")
    fake = FakeSendTmux(["> \nprompt cleared, working"])   # box already clear
    c = TmuxController(runner=fake, launch_terminal=False)
    c._started = True
    assert await c.send("run tests") is True


async def test_send_optimistic_when_capture_empty(monkeypatch):
    monkeypatch.setenv("VOXA_SEND_SETTLE_SECONDS", "0")
    monkeypatch.setenv("VOXA_SEND_ENTER_RETRIES", "3")
    fake = FakeSendTmux([""])                # capture yields nothing to verify
    c = TmuxController(runner=fake, launch_terminal=False)
    c._started = True
    ok = await c.send("do the thing")
    assert ok is True                        # optimistic sent
    # Did not spin: initial Enter plus at most one best-effort extra Enter.
    assert len(_enters(fake.calls)) <= 2


async def test_send_guards_unstarted():
    c = TmuxController(runner=lambda a: "", launch_terminal=False)
    try:
        await c.send("hi")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_input_still_pending_echo_in_bottom_window_stays_pending():
    # Known cosmetic limitation, locked in on purpose: right after a real submit the
    # echoed command can still sit in the bottom line window (thin transcript, instant
    # reply), so the heuristic reads it as still-pending. The bias is deliberate:
    # "pending" costs one harmless extra Enter, never a dropped or duplicated command.
    typed = "please refactor the send path and add tests for it"
    pane_after_submit = ("earlier output 1\nearlier output 2\n"
                         "> please refactor the send path and add tests for it\n"
                         "> \n ? for shortcuts\n")
    assert _input_still_pending(pane_after_submit, typed) is True


async def test_send_serializes_concurrent_sends(monkeypatch):
    # Two rapid sends (double-tap, or a queue item overlapping a typed command) must
    # NOT interleave their send-keys into the same pane: the lock makes each send's
    # type+Enter run to completion before the next begins.
    monkeypatch.setenv("VOXA_SEND_SETTLE_SECONDS", "0")
    monkeypatch.setenv("VOXA_SEND_ENTER_RETRIES", "1")
    fake = FakeSendTmux(["> \n(cleared)"])   # every capture shows a clear box
    c = TmuxController(runner=fake, launch_terminal=False)
    c._started = True
    await asyncio.gather(c.send("first command"), c.send("second command"))
    # The two literal-text injections must be non-adjacent: the first command's Enter
    # must land before the second command's text is typed.
    literals = [i for i, a in enumerate(fake.calls) if "-l" in a]
    assert len(literals) == 2
    between = fake.calls[literals[0] + 1:literals[1]]
    assert any(a and a[-1] == "Enter" for a in between), "sends interleaved (no lock)"
