import asyncio
import signal
import time

from server.terminals import discover_iterm, discover_tmux, ItermController
from server.terminals import (
    list_claude_processes, discover_terminal_app, gui_owner_of_tty,
    discover_claude_sessions,
)
from server.terminals import TerminalAppController
from server.terminals import (
    close_tmux, close_iterm, close_terminal_app, close_ax, close_terminal,
)


def test_discover_iterm_finds_only_claude_sessions():
    osa_out = "ABC123\t/dev/ttys005\tveil\nDEF456\t/dev/ttys006\tshell\n"

    def osa(script):
        return osa_out

    def run(args):
        if args[0] == "ps":
            return "501 node /Users/dev/.local/bin/claude\n" if "ttys005" in args else "777 -zsh\n"
        if args[0] == "lsof":
            return "p501\nfcwd\nn/Users/dev/proj\n"
        return ""

    sessions = discover_iterm(run=run, osa=osa)
    assert len(sessions) == 1
    s = sessions[0]
    assert s["backend"] == "iterm" and s["raw_id"] == "ABC123"
    assert s["cwd"] == "/Users/dev/proj" and s["label"] == "proj"
    assert s["controllable"] is True


def test_discover_tmux_finds_claude_panes():
    out = "work\t/Users/dev/app\tclaude\nmisc\t/tmp\tzsh\n"
    sessions = discover_tmux(run=lambda args: out)
    assert len(sessions) == 1
    assert sessions[0]["raw_id"] == "work" and sessions[0]["cwd"] == "/Users/dev/app"
    assert sessions[0]["backend"] == "tmux"


async def test_iterm_send_writes_text():
    calls = []

    def osa(script):
        calls.append(script)
        return ""

    c = ItermController("SID", osa=osa, poll_interval=0.005, idle_polls=2)
    await c.start("/x")
    await c.send("hello")
    await c.stop()
    joined = "\n".join(calls)
    assert 'write text "hello"' in joined
    # Must address the session by matching its id property in a loop, NOT via the
    # bogus `session id "X"` specifier (iTerm error -1728), which silently no-ops.
    assert 'session id "SID"' not in joined
    assert '(id of s) is "SID"' in joined


async def test_iterm_monitor_announces_idle():
    seq = ["", "Claude: done here"]
    state = {"i": 0}

    def osa(script):
        if "contents of s" in script:
            i = min(state["i"], len(seq) - 1)
            state["i"] += 1
            return seq[i]
        return ""

    spoken = []
    c = ItermController("SID", osa=osa, poll_interval=0.005, idle_polls=2)
    c.on_final(lambda t: spoken.append(t))
    await c.start()
    await asyncio.sleep(0.2)
    await c.stop()
    assert spoken and "done here" in spoken[0]


async def test_iterm_stop_never_closes_terminal():
    calls = []
    c = ItermController("SID", osa=lambda s: calls.append(s) or "", poll_interval=0.005)
    await c.start()
    calls.clear()
    await c.stop()
    # stop must not issue any 'close' AppleScript
    assert not any("close" in s.lower() for s in calls)
    assert c.status == "idle"


def test_list_claude_processes_filters_tty_and_claude():
    def run(args):
        if args[0] == "ps":
            return ("  312 ttys004  node /Users/dev/.local/bin/claude\n"
                    "  400 ??       claude-helper\n"          # no tty: skipped
                    "  500 ttys005  -zsh\n")                  # not claude: skipped
        if args[0] == "lsof":
            return "p312\nfcwd\nn/Users/dev/proj\n"
        return ""
    procs = list_claude_processes(run)
    assert procs == [{"pid": "312", "tty": "ttys004", "cwd": "/Users/dev/proj"}]


def test_discover_terminal_app_lists_tabs_running_claude():
    def run(args):
        if args[0] == "pgrep":
            return "9000\n"
        if args[0] == "ps":
            return "312 node /Users/dev/.local/bin/claude\n"
        if args[0] == "lsof":
            return "p312\nfcwd\nn/Users/dev/proj\n"
        return ""

    def osa(script):
        assert "Terminal" in script
        return "77\t1\t/dev/ttys004\n77\t2\t/dev/ttys009\n"

    def run_no_claude(args):
        if args[0] == "pgrep":
            return "9000\n"
        if args[0] == "ps":
            return "500 -zsh\n"
        return ""

    sessions = discover_terminal_app(run=run, osa=osa)
    # only the tab whose tty runs claude
    assert len(sessions) >= 1
    s = sessions[0]
    assert s["backend"] == "terminal_app" and s["raw_id"] == "77:1"
    assert s["cwd"] == "/Users/dev/proj" and s["controllable"] is True


def test_discover_terminal_app_skips_when_app_not_running():
    def run(args):
        return "" if args[0] == "pgrep" else ""
    called = []
    sessions = discover_terminal_app(run=run, osa=lambda s: called.append(s) or "")
    assert sessions == [] and called == []   # no AppleScript when Terminal is closed


def test_gui_owner_of_tty_walks_ancestry_to_app_bundle():
    def run(args):
        if args[0] == "ps" and args[1] == "-t":
            return "600 -zsh\n"                       # shell on the tty
        if args[0] == "ps" and args[1] == "-o":
            # each row reports the queried pid's OWN ppid and OWN command:
            # 600 (-zsh, parent 700) -> 700 (Ghostty binary, parent 1)
            pid = args[-1]
            table = {
                "600": "700 -zsh\n",
                "700": "1 /Applications/Ghostty.app/Contents/MacOS/ghostty\n",
            }
            return table.get(pid, "")
        return ""
    name, pid = gui_owner_of_tty("ttys010", run)
    assert name == "Ghostty" and pid == "700"


def test_discover_claude_sessions_dedupes_and_falls_back_to_ax():
    # one claude on ttys004 owned by Terminal.app, one on ttys010 owned by Ghostty
    def run(args):
        if args[0] == "ps" and args[1] == "-axo":
            return ("312 ttys004  claude\n"
                    "313 ttys010  claude\n")
        if args[0] == "lsof":
            return "p1\nfcwd\nn/Users/dev/proj\n"
        if args[0] == "pgrep":
            return "9000\n"
        if args[0] == "tmux":
            return ""
        if args[0] == "ps" and args[1] == "-t":
            # ps -t <tty> -o pid=,command=: response depends on which tty is
            # being queried. Used by both _claude_pid_on_tty (terminal_app,
            # ttys004) and gui_owner_of_tty (ax fallback, ttys010).
            ttyname = args[2]
            if ttyname == "ttys004":
                return "312 node /Users/dev/.local/bin/claude\n"
            if ttyname == "ttys010":
                return "600 -zsh\n"
            return ""
        if args[0] == "ps" and args[1] == "-o":
            # each row reports the queried pid's OWN ppid and OWN command
            pid = args[-1]
            table = {
                "600": "700 -zsh\n",
                "700": "1 /Applications/Ghostty.app/Contents/MacOS/ghostty\n",
            }
            return table.get(pid, "")
        return ""

    def osa(script):
        if "iTerm2" in script:
            return ""
        if "Terminal" in script:
            return "77\t1\t/dev/ttys004\n"
        return ""

    sessions = discover_claude_sessions(run, osa)
    backends = sorted(s["backend"] for s in sessions)
    assert backends == ["ax", "terminal_app"]
    ax = next(s for s in sessions if s["backend"] == "ax")
    assert ax["app"] == "Ghostty" and ax["app_pid"] == "700"
    assert ax["raw_id"] == "ttys010" and ax["controllable"] is True


def test_ax_session_not_controllable_without_gui_owner():
    # A claude whose ancestry has no .app bundle (e.g. a detached tmux on a
    # private socket, ssh, or headless) cannot receive keystrokes: it must be
    # reported detected-only, not controllable.
    def run(args):
        if args[0] == "ps" and args[1] == "-axo":
            return "313 ttys010  claude\n"
        if args[0] == "lsof":
            return "p1\nfcwd\nn/Users/dev/Desktop\n"
        if args[0] == "pgrep":
            return ""
        if args[0] == "tmux":
            return ""
        if args[0] == "ps" and args[1] == "-t":
            return "600 -fish\n"
        if args[0] == "ps" and args[1] == "-o":
            # ancestry terminates at tmux (ppid 1), no .app bundle anywhere
            table = {
                "600": "18926 -fish\n",
                "18926": "1 tmux -L voxa -f /dev/null new-session\n",
            }
            return table.get(args[-1], "")
        return ""

    sessions = discover_claude_sessions(run, lambda s: "")
    ax = next(s for s in sessions if s["backend"] == "ax")
    assert ax["app_pid"] == "" and ax["controllable"] is False


async def test_terminal_app_send_types_into_tab():
    calls = []
    c = TerminalAppController("77:2", osa=lambda s: calls.append(s) or "",
                              poll_interval=0.005, idle_polls=2)
    await c.start("/x")
    await c.send('say "hi"')
    await c.stop()
    joined = "\n".join(calls)
    assert 'do script "say \\"hi\\""' in joined
    assert "tab 2 of window id 77" in joined


async def test_terminal_app_monitor_announces_idle():
    seq = ["", "Claude: created the file"]
    state = {"i": 0}

    def osa(script):
        if "history of" in script:
            i = min(state["i"], len(seq) - 1)
            state["i"] += 1
            return seq[i]
        return ""

    spoken = []
    c = TerminalAppController("77:1", osa=osa, poll_interval=0.005, idle_polls=2)
    c.on_final(lambda t: spoken.append(t))
    await c.start()
    await asyncio.sleep(0.2)
    await c.stop()
    assert spoken and "created the file" in spoken[0]


async def test_terminal_app_stop_never_closes_tab():
    calls = []
    c = TerminalAppController("77:1", osa=lambda s: calls.append(s) or "",
                              poll_interval=0.005)
    await c.start()
    calls.clear()
    await c.stop()
    assert not any("close" in s.lower() for s in calls)
    assert c.status == "idle"


async def test_terminal_app_send_before_start_raises():
    c = TerminalAppController("77:1", osa=lambda s: "")
    try:
        await c.send("hello")
        assert False, "expected ValueError"
    except ValueError:
        pass


async def test_iterm_streams_output_to_phone():
    seq = ["", "Claude: building the page"]
    state = {"i": 0}

    def osa(script):
        if "contents of s" in script:
            i = min(state["i"], len(seq) - 1)
            state["i"] += 1
            return seq[i]
        return ""

    outputs = []
    c = ItermController("SID", osa=osa, poll_interval=0.005, idle_polls=2)
    c.on_output(lambda t: outputs.append(t))
    await c.start()
    await asyncio.sleep(0.2)
    await c.stop()
    assert any("building the page" in o for o in outputs)


def test_iterm_capture_scrollback_cleans_and_matches_by_id():
    calls = []

    def osa(script):
        calls.append(script)
        return "ok line\n\x1b[1mcolored\x1b[0m\n? for shortcuts"

    c = ItermController("SID", osa=osa)
    out = c.capture_scrollback()
    # addresses by matching the id, never the bogus specifier
    assert '(id of s) is "SID"' in "\n".join(calls)
    assert 'session id "SID"' not in "\n".join(calls)
    # chrome ("? for shortcuts") stripped by the shared cleaner
    assert "ok line" in out and "for shortcuts" not in out


async def test_terminal_app_streams_output_to_phone():
    seq = ["", "Claude: ran the tests"]
    state = {"i": 0}

    def osa(script):
        if "history of" in script:
            i = min(state["i"], len(seq) - 1)
            state["i"] += 1
            return seq[i]
        return ""

    outputs = []
    c = TerminalAppController("77:1", osa=osa, poll_interval=0.005, idle_polls=2)
    c.on_output(lambda t: outputs.append(t))
    await c.start()
    await asyncio.sleep(0.2)
    await c.stop()
    assert any("ran the tests" in o for o in outputs)


def test_terminal_app_capture_scrollback_reads_history():
    calls = []
    c = TerminalAppController("77:3", osa=lambda s: calls.append(s) or "hello\n")
    out = c.capture_scrollback()
    joined = "\n".join(calls)
    assert "history of tab 3 of window id 77" in joined
    assert "hello" in out


# --- activity preview: tell apart same-named sessions by what each is doing ------

from server.terminals import pane_activity


def test_pane_activity_working_vs_idle():
    working = "Building the pricing page\n✶ Crunching…\n  esc to interrupt\n"
    status, hint = pane_activity(working)
    assert status == "working"
    assert hint == "Building the pricing page"   # chrome stripped, real line kept
    idle = "Added the pricing section to index.html\n> \n? for shortcuts\n"
    status, hint = pane_activity(idle)
    assert status == "idle"
    assert hint == "Added the pricing section to index.html"


def test_pane_activity_empty_and_truncation():
    assert pane_activity("") == ("", "")
    long = "x" * 200
    status, hint = pane_activity(long)
    assert status == "idle"
    assert len(hint) <= 80 and hint.endswith("…")


def test_discover_tmux_includes_activity_preview():
    listing = "work\t/Users/dev/app\tclaude\n"

    def run(args):
        if args[0] == "tmux" and args[1] == "list-panes":
            return listing
        if args[0] == "tmux" and args[1] == "capture-pane":
            assert args[-1] == "work"            # captures THAT session's pane
            return "Refactoring the send path\nesc to interrupt\n"
        return ""

    sessions = discover_tmux(run=run)
    assert sessions[0]["status"] == "working"
    assert sessions[0]["hint"] == "Refactoring the send path"


def test_discover_iterm_includes_activity_preview_per_session():
    # Two Claude sessions in the SAME folder must come back with DIFFERENT hints,
    # captured from each one's own screen.
    osa_list = "AAA\t/dev/ttys005\tloop\nBBB\t/dev/ttys006\tloop\n"
    screens = {"AAA": "Running the tests\nesc to interrupt\n",
               "BBB": "Created index.html\n> \n"}

    def osa(script):
        if "contents of s" in script:            # the per-session capture script
            for sid, screen in screens.items():
                if f'(id of s) is "{sid}"' in script:
                    return screen
            return ""
        return osa_list

    def run(args):
        if args[0] == "ps":
            return "501 claude\n"
        if args[0] == "lsof":
            return "p501\nfcwd\nn/Users/dev/loop\n"
        return ""

    sessions = discover_iterm(run=run, osa=osa)
    assert [s["label"] for s in sessions] == ["loop", "loop"]
    assert sessions[0]["status"] == "working" and "tests" in sessions[0]["hint"]
    assert sessions[1]["status"] == "idle" and "index.html" in sessions[1]["hint"]


# --- session age ("2h") in the picker subtitle -----------------------------------

from server.terminals import humanize_etime


def test_humanize_etime_formats():
    assert humanize_etime("03:12:45") == "3h"      # hh:mm:ss
    assert humanize_etime("2-01:00:00") == "2d"    # dd-hh:mm:ss
    assert humanize_etime("35:10") == "35m"        # mm:ss
    assert humanize_etime("00:42") == "now"        # under a minute
    assert humanize_etime("") == ""
    assert humanize_etime("garbage") == ""


def test_discover_tmux_includes_age():
    listing = "work\t/Users/dev/app\tclaude\t4242\n"

    def run(args):
        if args[0] == "tmux" and args[1] == "list-panes":
            return listing
        if args[0] == "tmux" and args[1] == "capture-pane":
            return "hello\n"
        if args[0] == "ps" and "-o" in args and "etime=" in args:
            assert args[-1] == "4242"              # the pane's own pid
            return "  02:15:00\n"
        return ""

    sessions = discover_tmux(run=run)
    assert sessions[0]["age"] == "2h"


def test_discover_iterm_includes_age():
    def osa(script):
        if "contents of s" in script:
            return "screen\n"
        return "AAA\t/dev/ttys005\tloop\n"

    def run(args):
        if args[0] == "ps" and "etime=" in " ".join(args):
            return "45:00\n"
        if args[0] == "ps":
            return "501 claude\n"
        if args[0] == "lsof":
            return "p501\nfcwd\nn/Users/dev/loop\n"
        return ""

    sessions = discover_iterm(run=run, osa=osa)
    assert sessions[0]["age"] == "45m"


# --- interrupt: a voice "stop" must actually stop attached sessions --------------


async def test_iterm_interrupt_sends_escape_and_keeps_session():
    calls = []
    c = ItermController("SID", osa=lambda s: calls.append(s) or "",
                        poll_interval=0.005, idle_polls=2)
    await c.start("/x")
    c.status = "working"
    await c.interrupt()
    joined = "\n".join(calls)
    assert "character id 27" in joined and "newline NO" in joined
    assert '(id of s) is "SID"' in joined
    assert c._started is True                 # session stays attached/driveable
    assert c.status == "idle"
    await c.stop()


async def test_iterm_interrupt_noop_before_attach():
    calls = []
    c = ItermController("SID", osa=lambda s: calls.append(s) or "")
    await c.interrupt()
    assert calls == []


async def test_terminal_app_interrupt_presses_escape():
    calls = []
    c = TerminalAppController("77:2", osa=lambda s: calls.append(s) or "",
                              poll_interval=0.005, idle_polls=2)
    await c.start("/x")
    c.status = "working"
    await c.interrupt()
    joined = "\n".join(calls)
    assert "key code 53" in joined            # Escape via System Events
    assert "tab 2 of window id 77" in joined  # the RIGHT tab is selected first
    assert c._started is True
    assert c.status == "idle"
    await c.stop()


# --- press(): answer approvals in ItermController / TerminalAppController -------
# (real-world bug: only TmuxController and AXController had press(), so tapping
# an approval option for an attached iTerm2/Terminal.app session errored with
# "press not supported")


async def test_iterm_press_digit_writes_literal():
    calls = []
    c = ItermController("SID", osa=lambda s: calls.append(s) or "")
    c._started = True
    c._press_verify_secs = 0
    await c.press("1")
    joined = "\n".join(calls)
    assert 'write text "1" newline NO' in joined
    assert '(id of s) is "SID"' in joined


async def test_iterm_press_enter_sends_named_key():
    calls = []
    c = ItermController("SID", osa=lambda s: calls.append(s) or "")
    c._started = True
    c._press_verify_secs = 0
    await c.press("enter")
    joined = "\n".join(calls)
    assert "character id 13" in joined and "newline NO" in joined


async def test_iterm_press_unsupported_key_raises():
    c = ItermController("SID", osa=lambda s: "")
    c._started = True
    try:
        await c.press("ctrl-c")
        assert False, "expected ValueError"
    except ValueError:
        pass


async def test_iterm_press_before_start_raises():
    c = ItermController("SID", osa=lambda s: "")
    try:
        await c.press("1")
        assert False, "expected ValueError"
    except ValueError:
        pass


async def test_iterm_press_reposts_only_when_pane_unchanged():
    calls = []

    def osa(script):
        calls.append(script)
        return "same screen"

    c = ItermController("SID", osa=osa)
    c._started = True
    c._press_verify_secs = 0
    await c.press("1")
    write_calls = [s for s in calls if "write text" in s]
    assert len(write_calls) == 2   # posted once, unchanged -> reposted once


async def test_iterm_press_no_repost_when_pane_changed():
    calls = []
    seq = ["before", "after"]
    state = {"i": 0}

    def osa(script):
        calls.append(script)
        if "write text" in script:
            return ""
        i = min(state["i"], len(seq) - 1)
        state["i"] += 1
        return seq[i]

    c = ItermController("SID", osa=osa)
    c._started = True
    c._press_verify_secs = 0
    await c.press("1")
    write_calls = [s for s in calls if "write text" in s]
    assert len(write_calls) == 1   # pane changed -> never reposted


async def test_terminal_app_press_digit_types_keystroke():
    calls = []
    c = TerminalAppController("77:2", osa=lambda s: calls.append(s) or "")
    c._started = True
    c._press_verify_secs = 0
    await c.press("1")
    joined = "\n".join(calls)
    assert 'keystroke "1"' in joined
    assert "tab 2 of window id 77" in joined


async def test_terminal_app_press_enter_sends_key_code():
    calls = []
    c = TerminalAppController("77:1", osa=lambda s: calls.append(s) or "")
    c._started = True
    c._press_verify_secs = 0
    await c.press("enter")
    joined = "\n".join(calls)
    assert "key code 36" in joined


async def test_terminal_app_press_unsupported_key_raises():
    c = TerminalAppController("77:1", osa=lambda s: "")
    c._started = True
    try:
        await c.press("ctrl-c")
        assert False, "expected ValueError"
    except ValueError:
        pass


async def test_terminal_app_press_before_start_raises():
    c = TerminalAppController("77:1", osa=lambda s: "")
    try:
        await c.press("1")
        assert False, "expected ValueError"
    except ValueError:
        pass


async def test_terminal_app_press_reposts_only_when_pane_unchanged():
    calls = []

    def osa(script):
        calls.append(script)
        return "same screen"

    c = TerminalAppController("77:1", osa=osa)
    c._started = True
    c._press_verify_secs = 0
    await c.press("1")
    key_calls = [s for s in calls if "System Events" in s]
    assert len(key_calls) == 2   # posted once, unchanged -> reposted once


async def test_terminal_app_press_no_repost_when_pane_changed():
    calls = []
    seq = ["before", "after"]
    state = {"i": 0}

    def osa(script):
        calls.append(script)
        if "System Events" in script:
            return ""
        i = min(state["i"], len(seq) - 1)
        state["i"] += 1
        return seq[i]

    c = TerminalAppController("77:1", osa=osa)
    c._started = True
    c._press_verify_secs = 0
    await c.press("1")
    key_calls = [s for s in calls if "System Events" in s]
    assert len(key_calls) == 1   # pane changed -> never reposted


# --- verify_working(): heal a stale "working" flag on iTerm/Terminal.app --------


async def test_iterm_verify_working_heals_stale_to_idle(monkeypatch):
    monkeypatch.setenv("VOXA_BUSY_GRACE_SECONDS", "0")
    c = ItermController("SID", osa=lambda s: "Added file\n> \n? for shortcuts\n")
    c._started = True
    c.status = "working"
    c._last_send_at = 0.0
    assert await c.verify_working() is False
    assert c.status == "idle"


async def test_iterm_verify_working_stays_true_while_generating(monkeypatch):
    monkeypatch.setenv("VOXA_BUSY_GRACE_SECONDS", "0")
    c = ItermController("SID", osa=lambda s: "Building\n  esc to interrupt\n")
    c._started = True
    c.status = "working"
    c._last_send_at = 0.0
    assert await c.verify_working() is True
    assert c.status == "working"


async def test_iterm_verify_working_true_within_grace_window():
    calls = []
    c = ItermController("SID", osa=lambda s: calls.append(s) or "anything")
    c._started = True
    c.status = "working"
    c._last_send_at = time.monotonic()
    assert await c.verify_working() is True
    assert calls == []   # trusted without consulting the pane


async def test_terminal_app_verify_working_heals_stale_to_idle(monkeypatch):
    monkeypatch.setenv("VOXA_BUSY_GRACE_SECONDS", "0")
    c = TerminalAppController("77:1", osa=lambda s: "Added file\n> \n? for shortcuts\n")
    c._started = True
    c.status = "working"
    c._last_send_at = 0.0
    assert await c.verify_working() is False
    assert c.status == "idle"


async def test_terminal_app_verify_working_stays_true_while_generating(monkeypatch):
    monkeypatch.setenv("VOXA_BUSY_GRACE_SECONDS", "0")
    c = TerminalAppController("77:1", osa=lambda s: "Building\n  esc to interrupt\n")
    c._started = True
    c.status = "working"
    c._last_send_at = 0.0
    assert await c.verify_working() is True
    assert c.status == "working"


async def test_terminal_app_verify_working_true_within_grace_window():
    calls = []
    c = TerminalAppController("77:1", osa=lambda s: calls.append(s) or "anything")
    c._started = True
    c.status = "working"
    c._last_send_at = time.monotonic()
    assert await c.verify_working() is True
    assert calls == []   # trusted without consulting the pane


# --- closing a terminal from the phone -------------------------------------


def test_close_tmux_exact_argv():
    calls = []
    close_tmux("work", run=lambda a: calls.append(a) or "")
    assert calls == [["tmux", "kill-session", "-t", "work"]]


def test_close_iterm_loop_and_match_closes_the_tab():
    calls = []
    close_iterm("SID", osa=lambda s: calls.append(s) or "")
    script = calls[0]
    assert '(id of s) is "SID"' in script
    assert "close t" in script
    # Never the bogus direct specifier (iTerm2 raises error -1728 for it).
    assert 'session id "SID"' not in script


def test_close_terminal_app_ends_process_before_closing_tab():
    log = []

    def kill(pid, sig):
        log.append(("kill", pid, sig))

    def osa(script):
        log.append(("osa", script))
        return ""

    note = close_terminal_app("312", "77:2", kill=kill, sleep=lambda s: None, osa=osa)
    assert note is None
    kinds = [entry[0] for entry in log]
    assert kinds.index("kill") < kinds.index("osa")   # process ended BEFORE the tab closes
    assert log[0] == ("kill", 312, signal.SIGHUP)
    assert "tab 2 of window id 77" in log[1][1]


def test_close_terminal_app_process_already_gone_is_not_an_error():
    calls = []

    def kill(pid, sig):
        raise ProcessLookupError()

    note = close_terminal_app("312", "77:1", kill=kill, sleep=lambda s: None,
                              osa=lambda s: calls.append(s) or "")
    assert note is None
    assert calls   # the tab close still runs


def test_close_terminal_app_kill_failure_returns_best_effort_note():
    def kill(pid, sig):
        raise PermissionError("nope")

    note = close_terminal_app("312", "77:1", kill=kill, sleep=lambda s: None,
                              osa=lambda s: "")
    assert note and "prompt" in note


def test_close_terminal_app_no_pid_still_closes_tab():
    calls = []
    note = close_terminal_app("", "77:1", kill=lambda p, s: calls.append((p, s)),
                              sleep=lambda s: None,
                              osa=lambda s: calls.append(("osa", s)) or "")
    assert note is None
    assert ("osa", 'tell application "Terminal" to close tab 1 of window id 77') in calls
    assert not any(c[0] != "osa" for c in calls)   # kill() never called without a pid


def test_close_ax_escalates_to_sigterm_when_process_never_dies():
    calls = []
    sleeps = []
    note = close_ax("500", kill=lambda pid, sig: calls.append((pid, sig)),
                    alive=lambda pid: True,             # never reports dead
                    sleep=lambda s: sleeps.append(s))
    assert calls[0] == (500, signal.SIGHUP)
    assert calls.count((500, signal.SIGTERM)) == 1      # escalated exactly once
    assert calls[-1] == (500, signal.SIGTERM)
    assert sum(sleeps) >= 1.5
    assert note == "ended the Claude process; the window itself stays open"


def test_close_ax_no_escalation_once_process_ends():
    calls = []
    note = close_ax("500", kill=lambda pid, sig: calls.append((pid, sig)),
                    alive=lambda pid: False,            # dead on the first poll
                    sleep=lambda s: None)
    assert calls == [(500, signal.SIGHUP)]              # never escalated
    assert note == "ended the Claude process; the window itself stays open"


def test_close_ax_survives_escalation_error():
    def kill(pid, sig):
        if sig == signal.SIGTERM:
            raise ProcessLookupError()

    note = close_ax("500", kill=kill, alive=lambda pid: True, sleep=lambda s: None)
    assert note == "ended the Claude process; the window itself stays open"


def test_close_ax_survives_alive_check_error():
    def alive(pid):
        raise OSError("no such process")

    note = close_ax("500", kill=lambda pid, sig: None, alive=alive, sleep=lambda s: None)
    assert note == "ended the Claude process; the window itself stays open"


def test_close_ax_bad_pid_returns_note_without_crashing():
    assert close_ax("not-a-pid") == "ended the Claude process; the window itself stays open"


def test_close_terminal_dispatches_tmux():
    calls = []
    fake = [{"id": "tmux::work", "raw_id": "work", "backend": "tmux", "cwd": "/p/work"}]
    result = close_terminal("tmux::work", discover=lambda: fake,
                            run=lambda a: calls.append(a) or "")
    assert result == {"cwd": "/p/work", "backend": "tmux"}
    assert calls == [["tmux", "kill-session", "-t", "work"]]


def test_close_terminal_dispatches_iterm():
    calls = []
    fake = [{"id": "iterm:SID", "raw_id": "SID", "backend": "iterm", "cwd": "/p/x"}]
    result = close_terminal("iterm:SID", discover=lambda: fake,
                            osa=lambda s: calls.append(s) or "")
    assert result == {"cwd": "/p/x", "backend": "iterm"}
    assert "close t" in calls[0]


def test_close_terminal_dispatches_terminal_app():
    calls = []
    fake = [{"id": "term:77:2", "raw_id": "77:2", "backend": "terminal_app",
             "cwd": "/p/y", "pid": "312"}]
    result = close_terminal("term:77:2", discover=lambda: fake,
                            kill=lambda p, s: calls.append((p, s)),
                            sleep=lambda s: None,
                            osa=lambda s: calls.append(("osa", s)) or "")
    assert result == {"cwd": "/p/y", "backend": "terminal_app"}
    assert calls[0] == (312, signal.SIGHUP)


def test_close_terminal_dispatches_ax_and_returns_honest_note():
    fake = [{"id": "ax:ttys010", "raw_id": "ttys010", "backend": "ax",
             "cwd": "/p/z", "pid": "700"}]
    result = close_terminal("ax:ttys010", discover=lambda: fake,
                            kill=lambda p, s: None, alive=lambda p: False,
                            sleep=lambda s: None)
    assert result["cwd"] == "/p/z" and result["backend"] == "ax"
    assert result["note"] == "ended the Claude process; the window itself stays open"


def test_close_terminal_ax_without_pid_errors():
    fake = [{"id": "ax:ttys010", "raw_id": "ttys010", "backend": "ax",
             "cwd": "/p/z", "pid": ""}]
    result = close_terminal("ax:ttys010", discover=lambda: fake)
    assert "error" in result


def test_close_terminal_unknown_id_errors():
    result = close_terminal("nope", discover=lambda: [])
    assert result == {"error": "that terminal is no longer open"}


def test_close_terminal_never_string_parses_the_tmux_double_colon_id():
    # The tmux id is "tmux::<session>" (empty socket field); close_terminal must
    # dispatch off the RESOLVED dict's raw_id/backend, never split the id itself.
    calls = []
    fake = [{"id": "tmux::my::session", "raw_id": "my::session", "backend": "tmux",
             "cwd": "/p/w"}]
    result = close_terminal("tmux::my::session", discover=lambda: fake,
                            run=lambda a: calls.append(a) or "")
    assert result == {"cwd": "/p/w", "backend": "tmux"}
    assert calls == [["tmux", "kill-session", "-t", "my::session"]]


# --- discovery cache: one sweep serves the connect push, the 5s poll, and ----
# --- attach misses within the TTL; a close invalidates it ---------------------

def test_discovery_cache_only_applies_to_default_seams(monkeypatch):
    # Injected fakes (every other test in this file) must NEVER be served a
    # cached result from someone else's sweep.
    from server.terminals import discover_claude_sessions, invalidate_discovery_cache
    invalidate_discovery_cache()
    calls = []

    def fake_run(args):
        calls.append(args)
        raise RuntimeError("no tmux")

    def fake_osa(script):
        raise RuntimeError("no osa")

    a = discover_claude_sessions(fake_run, fake_osa)
    b = discover_claude_sessions(fake_run, fake_osa)
    assert a == [] and b == []
    assert len(calls) >= 2   # ran twice, nothing cached for injected seams


def test_invalidate_discovery_cache_clears_state():
    from server import terminals
    terminals._DISCOVERY_CACHE["at"] = 10.0
    terminals._DISCOVERY_CACHE["result"] = [{"id": "x"}]
    terminals.invalidate_discovery_cache()
    assert terminals._DISCOVERY_CACHE["result"] is None


def test_close_terminal_invalidates_the_discovery_cache():
    from server import terminals
    terminals._DISCOVERY_CACHE["at"] = 10.0 ** 9
    terminals._DISCOVERY_CACHE["result"] = [{"id": "tmux::work"}]
    ran = []
    res = terminals.close_terminal(
        "tmux::work",
        discover=lambda: [{"id": "tmux::work", "raw_id": "work",
                           "backend": "tmux", "cwd": "/p/w"}],
        run=lambda a: ran.append(a) or "")
    assert "error" not in res
    assert terminals._DISCOVERY_CACHE["result"] is None
