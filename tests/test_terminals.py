import asyncio

from server.terminals import discover_iterm, discover_tmux, ItermController
from server.terminals import (
    list_claude_processes, discover_terminal_app, gui_owner_of_tty,
    discover_claude_sessions,
)
from server.terminals import TerminalAppController


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
