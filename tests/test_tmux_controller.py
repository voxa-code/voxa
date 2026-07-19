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


async def test_start_raises_tmux_not_installed_when_missing(tmp_path, monkeypatch):
    # A fresh Mac (install.sh only guarantees uv + voxa-code) has no tmux: start()
    # must fail fast with an actionable RuntimeError instead of a raw
    # FileNotFoundError("'tmux'") bubbling out of subprocess.run.
    import server.tmux_controller as tc
    monkeypatch.setattr(tc.shutil, "which", lambda name: None)
    fake = FakeTmux(["> "])
    c = TmuxController(runner=fake, launch_terminal=False)
    try:
        await c.start(str(tmp_path))
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert str(e).startswith("tmux_not_installed")
        assert "brew install tmux" in str(e)
    # Nothing was recorded as a live session, and no tmux command was even attempted.
    assert c.working_dir is None
    assert c._started is False
    assert fake.calls == []


async def test_start_leaves_working_dir_unset_when_new_session_fails(tmp_path):
    # working_dir must reflect an ACTUAL running session: a failed new-session
    # (any tmux error) must not leave get_claude_status reporting a live-looking
    # folder for a session that never started.
    class BoomTmux:
        def __call__(self, args):
            args = list(args)
            if args[0] == "has-session":
                raise RuntimeError("no session")
            if args[0] == "new-session":
                raise RuntimeError("tmux new-session failed: boom")
            return ""

    c = TmuxController(runner=BoomTmux(), launch_terminal=False)
    try:
        await c.start(str(tmp_path))
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass
    assert c.working_dir is None
    assert c._started is False


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


async def test_monitor_holds_working_while_active_marker_shows(tmp_path):
    # A long thinking stretch renders NO new output: the screen goes stable while
    # the "esc to interrupt" footer is still up. Status must HOLD "working" and no
    # mid-task final may fire (the old stability-only check flipped to idle here,
    # reopening the mic and relaying junk while Claude still worked).
    fake = FakeTmux(["", "Thinking hard\nesc to interrupt"])  # marker persists
    spoken = []
    c = TmuxController(runner=fake, launch_terminal=False, poll_interval=0.005, idle_polls=2)
    c.on_final(lambda t: spoken.append(t))
    await c.start(str(tmp_path))
    await asyncio.sleep(0.15)
    assert c.status == "working"
    assert spoken == []
    # The marker clears (Claude finished): only now may it settle to idle + announce.
    fake._caps = ["Thinking hard\nAll done: created index.html"]
    await asyncio.sleep(0.15)
    await c.stop()
    assert c.status == "idle"
    assert spoken and "All done" in spoken[0]


async def test_interrupt_sends_escape_and_keeps_session_driveable(tmp_path):
    # interrupt() stops the CURRENT generation only: Escape goes to the pane, but
    # the session, monitor and _started all survive (unlike stop(), which detaches).
    fake = FakeTmux(["> "])
    c = TmuxController(runner=fake, launch_terminal=False, poll_interval=0.005, idle_polls=2)
    await c.start(str(tmp_path))
    c.status = "working"
    fake.calls.clear()
    await c.interrupt()
    assert ["send-keys", "-t", "voxa", "Escape"] in fake.calls
    assert c._started is True
    assert c._monitor_task is not None and not c._monitor_task.done()
    assert c.status == "idle"          # optimistic: ready for a follow-up dispatch
    assert all(a[0] != "kill-session" for a in fake.calls)
    await c.stop()


async def test_interrupt_before_start_is_a_noop():
    calls = []
    c = TmuxController(runner=lambda a: calls.append(list(a)) or "", launch_terminal=False)
    await c.interrupt()               # not started: nothing sent, no crash
    assert calls == []


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


async def test_monitor_loop_holds_working_while_active_marker_shows():
    # Same busy-hold contract as TmuxController._monitor, for the shared loop the
    # iTerm/Terminal controllers use: a stable screen that still shows the active
    # marker must NOT settle to idle or emit a mid-task final.
    screens = ["boot", "Working away\nesc to interrupt"]
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
    assert c.status == "working"
    assert c.finals == []
    # Marker clears -> the loop may now settle to idle and announce.
    screens.append("All done here")
    state["i"] = len(screens) - 1
    await _asyncio.sleep(0.1)
    c._started = False
    await _asyncio.sleep(0.02)
    task.cancel()
    assert c.status == "idle"
    assert any("All done here" in f for f in c.finals)


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


async def test_monitor_announces_prompt_with_esc_to_cancel(tmp_path):
    # An interactive prompt ("Enter to confirm · Esc to cancel") means Claude is
    # WAITING on the user, not generating: status must settle to idle and the
    # prompt must be announced. Treating "esc to cancel" as busy suppressed the
    # announcement and made send_to_claude get busy-refused while a question
    # sat on screen (caught by the live stop smoke test).
    fake = FakeTmux(["", "Working\nesc to interrupt",
                     "Allow this edit?\n> 1. Yes\n  2. No\nEnter to confirm · Esc to cancel"])
    spoken = []
    c = TmuxController(runner=fake, launch_terminal=False, poll_interval=0.005, idle_polls=2)
    c.on_final(lambda t: spoken.append(t))
    await c.start(str(tmp_path))
    await asyncio.sleep(0.2)
    assert c.status == "idle"                  # waiting on the user, NOT working
    assert spoken and "allow this edit" in spoken[0].lower()
    await c.stop()


# --- pane_is_generating: busy detection across Claude Code TUI versions ----------

from server.tmux_controller import pane_is_generating


def test_pane_is_generating_old_and_new_markers():
    # Old builds: persistent "esc to interrupt" hint.
    assert pane_is_generating("stuff\n✶ Crunching… (esc to interrupt)\n") is True
    assert pane_is_generating("stuff\n  esc to interrupt\n") is True
    # New builds: glyph spinner line with ellipsis / elapsed timer / running tools.
    assert pane_is_generating("✳ Protoys, Build and ship prototypes, protoys.app…\n") is True
    assert pane_is_generating("✻ Reticulating (4s · thinking with high effort)\n") is True
    assert pane_is_generating("✳ Churned for 2m 25s · 1 shell still running\n") is True


def test_pane_is_generating_false_for_idle_and_prompts():
    # Settled post-turn timing line: glyph but no live suffix.
    assert pane_is_generating("✻ Crunched for 13s\n> \n") is False
    # Interactive prompt waiting on the user ("Esc to cancel") is NOT generating.
    assert pane_is_generating(
        "Do you trust this folder?\n> 1. Yes\nEnter to confirm · Esc to cancel\n") is False
    # Plain idle screen.
    assert pane_is_generating("Here is your answer.\n> \n? for shortcuts\n") is False
    assert pane_is_generating("") is False


# --- interrupt_needs_retry: does the Escape need pressing again? ----------------

from server.tmux_controller import interrupt_needs_retry


def test_interrupt_retry_confirmed_interrupt_stops():
    # "Interrupted" near the bottom = the Escape landed; no retry.
    now = "42. some poem line\n⎿  Interrupted · What should Claude do instead?\n❯ \n"
    assert interrupt_needs_retry("whatever", now) is False


def test_interrupt_retry_old_interrupt_in_scrollback_does_not_count():
    # Terminal.app/AX captures include history; an interrupt from a PREVIOUS
    # turn far above the tail must not read as confirmation of this one.
    old = "⎿  Interrupted · What should Claude do instead?\n" + "line\n" * 60
    assert interrupt_needs_retry(old, old + "still streaming") is True


def test_interrupt_retry_settled_idle_prompt_stops():
    idle = "Here is your answer.\n❯ \n⏵⏵ bypass permissions on\n"
    assert interrupt_needs_retry(idle, idle) is False


def test_interrupt_retry_streaming_screen_retries():
    # Current builds stream with no spinner line, no footer, no prompt: the
    # screen keeps changing between captures -> press again (vim INSERT mode
    # ate the first Escape).
    before = "10. line ten\n11. line eleven\n"
    now = "11. line eleven\n12. line twelve\n"
    assert interrupt_needs_retry(before, now) is True


def test_interrupt_retry_promptless_static_screen_retries():
    # Thinking phase: static screen, no prompt, no confirmation -> retry.
    assert interrupt_needs_retry("screen", "screen") is True


def test_interrupt_retry_old_build_generating_marker_retries():
    gen = "stuff\n✶ Crunching… (esc to interrupt)\n❯ \n"
    assert interrupt_needs_retry(gen, gen) is True


async def test_tmux_interrupt_presses_again_while_streaming(tmp_path):
    # End-to-end on the controller: pane keeps changing (vim ate Esc #1), so
    # interrupt() sends Escape again until the pane confirms.
    panes = iter([
        "10. ten\n",                         # before-press capture
        "12. twelve\n",                      # after Esc 1: still streaming
        "⎿  Interrupted · ok\n❯ \n",          # after Esc 2: confirmed
    ])
    sent = []

    def fake(args):
        if args[0] == "send-keys" and args[-1] == "Escape":
            sent.append("Esc")
            return ""
        if args[0] == "capture-pane":
            return next(panes, "❯ \n")
        return ""

    c = TmuxController(runner=fake, launch_terminal=False)
    c._started = True
    await c.interrupt()
    assert sent == ["Esc", "Esc"]
    assert c.status == "idle"


async def test_monitor_holds_working_on_new_style_spinner(tmp_path):
    # A new-TUI working screen (no "esc to interrupt" anywhere) must still hold
    # status at working and suppress the mid-task final.
    fake = FakeTmux(["", "✻ Pondering deeply (12s · thinking with high effort)"])
    spoken = []
    c = TmuxController(runner=fake, launch_terminal=False, poll_interval=0.005, idle_polls=2)
    c.on_final(lambda t: spoken.append(t))
    await c.start(str(tmp_path))
    await asyncio.sleep(0.15)
    assert c.status == "working"
    assert spoken == []
    await c.stop()


async def test_default_stuck_seconds_defaults_to_300(monkeypatch):
    monkeypatch.delenv("VOXA_STUCK_SECONDS", raising=False)
    from server.tmux_controller import _default_stuck_seconds
    assert _default_stuck_seconds() == 300.0


async def test_default_stuck_seconds_reads_env(monkeypatch):
    monkeypatch.setenv("VOXA_STUCK_SECONDS", "45")
    from server.tmux_controller import _default_stuck_seconds
    assert _default_stuck_seconds() == 45.0


async def test_monitor_fires_on_stuck_once_when_pane_never_changes_while_generating(tmp_path):
    # A pane stuck on the SAME generating screen past VOXA_STUCK_SECONDS fires
    # on_stuck exactly once for the stretch, not once per poll thereafter.
    fake = FakeTmux(["", "Thinking hard\nesc to interrupt"])   # repeats forever
    stuck_calls = []
    c = TmuxController(runner=fake, launch_terminal=False, poll_interval=0.01,
                       idle_polls=2, stuck_seconds=0.03)
    c.on_stuck(lambda elapsed: stuck_calls.append(elapsed))
    await c.start(str(tmp_path))
    await asyncio.sleep(0.3)
    await c.stop()
    assert len(stuck_calls) == 1
    assert stuck_calls[0] >= 0.03


async def test_monitor_never_fires_on_stuck_when_pane_keeps_changing(tmp_path):
    # The pane changes every poll (still generating throughout): each change
    # resets the stuck timer, so it must never accumulate enough quiet time
    # to fire, no matter how long the whole stretch runs.
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    state = {"i": 0}

    def runner(args):
        args = list(args)
        cmd = args[0]
        if cmd == "has-session":
            raise RuntimeError("no session")
        if cmd == "capture-pane":
            letter = alphabet[state["i"] % len(alphabet)]
            state["i"] += 1
            return f"Doing task {letter}\nesc to interrupt"
        return ""

    stuck_calls = []
    c = TmuxController(runner=runner, launch_terminal=False, poll_interval=0.01,
                       idle_polls=2, stuck_seconds=0.03)
    c.on_stuck(lambda elapsed: stuck_calls.append(elapsed))
    await c.start(str(tmp_path))
    await asyncio.sleep(0.3)
    await c.stop()
    assert stuck_calls == []


async def test_monitor_never_fires_on_stuck_when_idle_before_threshold(tmp_path):
    # Claude finishes (goes idle) well before the stuck threshold elapses: a
    # normal finish must never trigger the stuck alert.
    fake = FakeTmux(["", "Thinking hard\nesc to interrupt", "All done: task complete"])
    stuck_calls = []
    c = TmuxController(runner=fake, launch_terminal=False, poll_interval=0.01,
                       idle_polls=2, stuck_seconds=5.0)
    c.on_stuck(lambda elapsed: stuck_calls.append(elapsed))
    await c.start(str(tmp_path))
    await asyncio.sleep(0.2)
    await c.stop()
    assert stuck_calls == []


async def test_monitor_stuck_disabled_by_zero_seconds(tmp_path):
    # stuck_seconds=0 (mirrors VOXA_STUCK_SECONDS=0) disables detection outright,
    # even though the pane is stuck generating well past what would otherwise fire.
    fake = FakeTmux(["", "Thinking hard\nesc to interrupt"])
    stuck_calls = []
    c = TmuxController(runner=fake, launch_terminal=False, poll_interval=0.01,
                       idle_polls=2, stuck_seconds=0)
    c.on_stuck(lambda elapsed: stuck_calls.append(elapsed))
    await c.start(str(tmp_path))
    await asyncio.sleep(0.2)
    await c.stop()
    assert stuck_calls == []


async def test_monitor_stuck_disabled_by_zero_env(tmp_path, monkeypatch):
    # The env var form (VOXA_STUCK_SECONDS=0) reaches the same disabled state via
    # the constructor default, without an explicit stuck_seconds= kwarg.
    monkeypatch.setenv("VOXA_STUCK_SECONDS", "0")
    fake = FakeTmux(["", "Thinking hard\nesc to interrupt"])
    stuck_calls = []
    c = TmuxController(runner=fake, launch_terminal=False, poll_interval=0.01, idle_polls=2)
    c.on_stuck(lambda elapsed: stuck_calls.append(elapsed))
    await c.start(str(tmp_path))
    await asyncio.sleep(0.2)
    await c.stop()
    assert stuck_calls == []


async def test_monitor_auto_accepts_trust_prompt_once(tmp_path):
    # A Voxa-launched session that boots to the folder-trust prompt must be
    # unstuck automatically (the user already asked to open this folder): ONE
    # Enter per appearance, not a spam of them while the prompt renders.
    trust = ("Quick safety check: Is this a project you created or one you trust?\n"
             "> 1. Yes, I trust this folder\n  2. No, exit\n"
             "Enter to confirm · Esc to cancel")
    fake = FakeTmux(["", trust, trust, trust, "> ready\n? for shortcuts"])
    c = TmuxController(runner=fake, launch_terminal=False, poll_interval=0.005, idle_polls=2)
    await c.start(str(tmp_path))
    await asyncio.sleep(0.15)
    await c.stop()
    enters = [a for a in fake.calls if a == ["send-keys", "-t", "voxa", "Enter"]]
    assert len(enters) == 1


# --- verify_working: the busy guard's verify-on-read (stale flag healing) ----

def _vw_controller():
    import time as _t
    from server.tmux_controller import TmuxController
    c = TmuxController(runner=lambda a: "", launch_terminal=False)
    c._started = True
    c.status = "working"
    c._last_send_at = _t.monotonic()
    return c


async def test_verify_working_trusts_the_grace_window(monkeypatch):
    monkeypatch.setenv("VOXA_BUSY_GRACE_SECONDS", "60")
    c = _vw_controller()
    captures = []
    c._capture = lambda: captures.append(1) or "irrelevant"
    assert await c.verify_working() is True
    assert captures == []          # trusted a fresh send without reading the pane
    assert c.status == "working"


async def test_verify_working_true_while_pane_shows_generation(monkeypatch):
    monkeypatch.setenv("VOXA_BUSY_GRACE_SECONDS", "0")
    c = _vw_controller()
    c._capture = lambda: "output...\n* Crunching (12s - esc to interrupt)\n"
    assert await c.verify_working() is True
    assert c.status == "working"


async def test_verify_working_heals_a_stale_flag_to_idle(monkeypatch):
    monkeypatch.setenv("VOXA_BUSY_GRACE_SECONDS", "0")
    c = _vw_controller()
    c._capture = lambda: "done.\n> \n"   # idle prompt, no generating marker
    assert await c.verify_working() is False
    assert c.status == "idle"


async def test_verify_working_fail_safe_when_capture_raises(monkeypatch):
    monkeypatch.setenv("VOXA_BUSY_GRACE_SECONDS", "0")
    c = _vw_controller()

    def boom():
        raise RuntimeError("pane gone")
    c._capture = boom
    assert await c.verify_working() is True
    assert c.status == "working"
