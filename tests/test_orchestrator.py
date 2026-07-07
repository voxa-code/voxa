import asyncio
import pytest
from server.orchestrator import Orchestrator


class FakeController:
    def __init__(self):
        self.status = "idle"
        self.working_dir = None
        self._final = None
        self.sent = []
    def on_final(self, cb): self._final = cb
    async def start(self, wd):
        if wd == "/bad": raise ValueError("nope")
        self.working_dir = wd; self.status = "idle"
    async def send(self, text):
        self.sent.append(text); self.status = "working"
    async def stop(self): self.status = "idle"
    async def fire_final(self, text):   # test helper
        await self._final(text)


def make(controller=None):
    controller = controller or FakeController()
    spoken, ui = [], []
    async def speak(t): spoken.append(t)
    async def notify(m): ui.append(m)
    orch = Orchestrator(controller, speak, notify)
    return orch, controller, spoken, ui


async def test_start_session_returns_status():
    orch, ctrl, _, _ = make()
    res = await orch.handle_tool_call("start_claude_session", {"working_dir": "/tmp"})
    assert res == {"status": "idle", "working_dir": "/tmp"}
    assert ctrl.working_dir == "/tmp"

async def test_start_session_error():
    orch, _, _, _ = make()
    res = await orch.handle_tool_call("start_claude_session", {"working_dir": "/bad"})
    assert "error" in res

async def test_send_is_nonblocking_and_accepts():
    orch, ctrl, _, _ = make()
    await orch.handle_tool_call("start_claude_session", {"working_dir": "/tmp"})
    res = await orch.handle_tool_call("send_to_claude", {"text": "hi"})
    assert res == {"accepted": True, "status": "working"}
    await asyncio.sleep(0)  # let the background task run
    assert ctrl.sent == ["hi"]

async def test_final_text_is_spoken_and_notified():
    orch, ctrl, spoken, ui = make()
    await ctrl.fire_final("all done")
    assert spoken == ["all done"]
    assert {"type": "status", "status": "finished"} in ui


async def test_start_surfaces_window_hint_when_set():
    ctrl = FakeController()
    ctrl.window_hint = "run: tmux -L voxa attach -t voxa"
    orch, _, _, ui = make(ctrl)
    await orch.handle_tool_call("start_claude_session", {"working_dir": "/tmp"})
    assert {"type": "status", "status": "run: tmux -L voxa attach -t voxa"} in ui


async def test_send_direct_types_into_claude():
    # Raw terminal chat from the phone goes straight into the session, non-blocking.
    orch, ctrl, _, ui = make()
    await orch.send_direct("hello claude")
    await asyncio.sleep(0)   # let the background send task run
    assert ctrl.sent == ["hello claude"]


# --- command_sent feedback: report whether the send actually submitted ----------

async def test_send_and_report_emits_command_sent_confirmed():
    class ConfirmCtrl(FakeController):
        async def send(self, text):
            self.sent.append(text); self.status = "working"; return True
    orch, ctrl, _, ui = make(ConfirmCtrl())
    await orch._send_and_report("do it")
    assert ctrl.sent == ["do it"]
    msgs = [m for m in ui if m.get("type") == "command_sent"]
    assert msgs == [{"type": "command_sent", "text": "do it", "ok": True}]


async def test_send_and_report_emits_ok_false_when_unconfirmed():
    class UnconfirmedCtrl(FakeController):
        async def send(self, text):
            self.sent.append(text); return False
    orch, ctrl, _, ui = make(UnconfirmedCtrl())
    await orch._send_and_report("do it")
    msgs = [m for m in ui if m.get("type") == "command_sent"]
    assert msgs and msgs[0]["ok"] is False and msgs[0]["text"] == "do it"


async def test_send_and_report_treats_none_as_sent():
    # A non-tmux controller whose send() returns None still reports ok=True
    # (optimistic sent): it has no verification to surface.
    orch, ctrl, _, ui = make()   # FakeController.send returns None
    await orch._send_and_report("hi")
    msgs = [m for m in ui if m.get("type") == "command_sent"]
    assert msgs and msgs[0]["ok"] is True


async def test_send_and_report_failing_notify_does_not_break_task():
    calls = []

    class ConfirmCtrl(FakeController):
        async def send(self, text):
            calls.append(text); return True

    ctrl = ConfirmCtrl()
    async def speak(t): pass
    async def notify(m): raise RuntimeError("socket closed")
    orch = Orchestrator(ctrl, speak, notify)
    await orch._send_and_report("hi")   # must not raise
    assert calls == ["hi"]


async def test_send_direct_emits_working_status_and_command_sent():
    orch, ctrl, _, ui = make()
    await orch.send_direct("hey")
    await asyncio.sleep(0)   # let the background send-and-report task run
    assert {"type": "status", "status": "Claude working (mic paused)"} in ui
    assert any(m.get("type") == "command_sent" and m.get("text") == "hey"
               for m in ui)


async def test_send_to_claude_emits_command_sent():
    orch, ctrl, _, ui = make()
    await orch.handle_tool_call("start_claude_session", {"working_dir": "/tmp"})
    await orch.handle_tool_call("send_to_claude", {"text": "go"})
    await asyncio.sleep(0)   # let the background send-and-report task run
    assert ctrl.sent == ["go"]
    assert any(m.get("type") == "command_sent" and m.get("text") == "go"
               for m in ui)


async def test_start_no_window_hint_by_default():
    orch, _, _, ui = make()   # FakeController has no window_hint -> nothing surfaced
    await orch.handle_tool_call("start_claude_session", {"working_dir": "/tmp"})
    assert not any(
        isinstance(m, dict) and str(m.get("status", "")).startswith("run: tmux")
        for m in ui
    )

async def test_get_status():
    orch, ctrl, _, _ = make()
    ctrl.status = "working"; ctrl.working_dir = "/x"
    res = await orch.handle_tool_call("get_claude_status", {})
    assert res == {"status": "working", "working_dir": "/x"}

async def test_unknown_tool():
    orch, _, _, _ = make()
    res = await orch.handle_tool_call("frobnicate", {})
    assert "error" in res

async def test_stop_cancels_in_flight_send():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class SlowController(FakeController):
        async def send(self, text):
            self.status = "working"
            started.set()
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                cancelled.set()
                raise

    orch, ctrl, _, _ = make(SlowController())
    await orch.handle_tool_call("start_claude_session", {"working_dir": "/tmp"})
    await orch.handle_tool_call("send_to_claude", {"text": "hi"})
    await asyncio.wait_for(started.wait(), 1)
    res = await orch.handle_tool_call("stop_claude", {})
    assert res == {"status": "idle"}
    await asyncio.wait_for(cancelled.wait(), 1)


async def test_set_working_dir_error_suggests_siblings(tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()

    class FailController(FakeController):
        async def start(self, wd):
            raise ValueError("nope")

    orch, _, _, _ = make(FailController())
    res = await orch.handle_tool_call("set_working_dir", {"path": str(tmp_path / "ghost")})
    assert "error" in res
    assert res["searched_in"] == str(tmp_path)
    assert "alpha" in res["suggestions"] and "beta" in res["suggestions"]


async def test_list_dirs(tmp_path):
    (tmp_path / "x").mkdir()
    orch, _, _, _ = make()
    res = await orch.handle_tool_call("list_dirs", {"parent": str(tmp_path)})
    assert res["path"] == str(tmp_path)
    assert "x" in res["dirs"]


async def test_make_dir_creates_and_starts(tmp_path):
    target = tmp_path / "newproj"
    orch, ctrl, _, _ = make()
    res = await orch.handle_tool_call("make_dir", {"path": str(target)})
    assert target.is_dir()
    assert ctrl.working_dir == str(target)
    assert res["working_dir"] == str(target)


async def test_list_terminals_pushes_and_returns(monkeypatch):
    import server.terminals as term
    fake = [{"id": "iterm:1", "label": "veil", "app": "iTerm2", "cwd": "/x",
             "backend": "iterm", "raw_id": "1", "controllable": True}]
    monkeypatch.setattr(term, "discover_claude_sessions", lambda: fake)
    orch, ctrl, spoken, ui = make()
    res = await orch.handle_tool_call("list_terminals", {})
    assert res["terminals"][0]["id"] == "iterm:1"
    assert any(m.get("type") == "terminals" for m in ui)


async def test_attach_terminal_swaps_controller(monkeypatch):
    orch, ctrl, _, _ = make()
    orch._last_terminals = [{"id": "x:1", "label": "veil", "app": "tmux", "cwd": "/tmp",
                             "backend": "tmux", "raw_id": "work", "socket": None,
                             "controllable": True}]
    captured = {}

    class FakeNew:
        def __init__(self): self.status = "idle"; self.working_dir = None
        def on_final(self, cb): captured["final"] = cb
        async def start(self, wd=None): captured["started"] = wd; self.working_dir = wd
        async def stop(self): pass

    monkeypatch.setattr(orch, "_build_controller", lambda sess: FakeNew())
    res = await orch.handle_tool_call("attach_terminal", {"id": "x:1"})
    assert res["attached"] == "veil"
    assert orch._c.__class__.__name__ == "FakeNew"
    assert captured["started"] == "/tmp"
    assert captured["final"] is orch._final_cb


async def test_attach_terminal_unknown_id():
    orch, ctrl, _, _ = make()
    orch._last_terminals = []
    res = await orch.handle_tool_call("attach_terminal", {"id": "nope"})
    assert "error" in res


async def test_send_without_session_returns_error_not_crash():
    import asyncio
    from server.orchestrator import Orchestrator
    class FakeCtrl:
        _started = False
        def on_final(self, cb): pass
        async def send(self, t): raise ValueError("call start() before send()")
    async def speak(t): pass
    async def notify(m): pass
    orch = Orchestrator(FakeCtrl(), speak, notify)
    res = await orch.handle_tool_call("send_to_claude", {"text": "hi"})
    assert res.get("error") == "no_session"   # graceful, no background crash


async def test_resume_session_passes_stem_to_resume_capable_controller(tmp_path):
    # A controller whose start() accepts `resume` gets the stem (last "/"-segment
    # of the history id), and _start emits the working_dir status like set_dir does.
    calls = {}

    class ResumeCtrl:
        def __init__(self): self.status = "idle"; self.working_dir = None
        def on_final(self, cb): pass
        async def start(self, wd, resume=None):
            calls["wd"] = wd; calls["resume"] = resume; self.working_dir = wd
        async def stop(self): pass

    orch, _, _, ui = make(ResumeCtrl())
    res = await orch.resume_session(str(tmp_path), "enc_dir/abc123")
    assert calls == {"wd": str(tmp_path), "resume": "abc123"}
    assert res["working_dir"] == str(tmp_path)
    assert {"type": "status", "working_dir": str(tmp_path)} in ui


async def test_resume_session_degrades_for_non_resume_controller(tmp_path):
    # FakeController.start(wd) has no `resume` param: resume degrades to a normal
    # launch (no crash, no error).
    orch, ctrl, _, _ = make()
    res = await orch.resume_session(str(tmp_path), "enc/xyz")
    assert ctrl.working_dir == str(tmp_path)
    assert "error" not in res


async def test_resume_session_bare_stem_and_bad_dir(tmp_path):
    # A bare stem (no "/") is used as-is; a bad dir returns an error with suggestions.
    calls = {}

    class ResumeCtrl:
        def __init__(self): self.status = "idle"; self.working_dir = None
        def on_final(self, cb): pass
        async def start(self, wd, resume=None):
            if wd == "/bad": raise ValueError("nope")
            calls["resume"] = resume; self.working_dir = wd
        async def stop(self): pass

    orch, _, _, _ = make(ResumeCtrl())
    await orch.resume_session(str(tmp_path), "plainstem")
    assert calls["resume"] == "plainstem"
    res = await orch.resume_session("/bad", "s")
    assert "error" in res


async def test_attach_source_matches_cwd(monkeypatch):
    orch, _, _, _ = make()
    import server.terminals as terms
    monkeypatch.setattr(terms, "discover_claude_sessions",
        lambda *a, **k: [{"id": "tmux::x", "raw_id": "x", "backend": "tmux",
                          "label": "proj", "cwd": "/p/app", "app": "tmux",
                          "controllable": True}])
    async def fake_attach(sess):
        return {"attached": sess["label"], "working_dir": sess["cwd"]}
    monkeypatch.setattr(orch, "_attach", fake_attach)
    res = await orch.attach_source("/p/app/")           # trailing slash tolerated
    assert res["attached"] == "proj"


async def test_attach_source_no_open_terminal(monkeypatch):
    orch, _, _, _ = make()
    import server.terminals as terms
    monkeypatch.setattr(terms, "discover_claude_sessions", lambda *a, **k: [])
    res = await orch.attach_source("/p/app")
    assert "error" in res


async def test_attach_source_not_controllable(monkeypatch):
    orch, _, _, _ = make()
    import server.terminals as terms
    monkeypatch.setattr(terms, "discover_claude_sessions",
        lambda *a, **k: [{"id": "x", "cwd": "/p/app", "app": "Terminal",
                          "controllable": False, "label": "t"}])
    res = await orch.attach_source("/p/app")
    assert "error" in res


async def test_attach_terminal_app_backend(monkeypatch):
    orch, ctrl, spoken, notes = make()
    sess = {"id": "term:77:1", "raw_id": "77:1", "cwd": "/tmp", "label": "tmp",
            "app": "Terminal", "backend": "terminal_app", "controllable": True}
    orch.remember_terminals([sess])
    import server.terminals as terminals

    class FakeTerm:
        def __init__(self, raw_id, **kw):
            self.raw_id, self.status, self.working_dir = raw_id, "idle", None
        def on_final(self, cb): pass
        async def start(self, wd=None): self.working_dir = wd
        async def stop(self, *, detach_only=False): pass

    monkeypatch.setattr(terminals, "TerminalAppController", FakeTerm)
    res = await orch.handle_tool_call("attach_terminal", {"id": "term:77:1"})
    assert res.get("attached") == "tmp"


async def test_read_session_tool_uses_working_dir(monkeypatch):
    orch, ctrl, spoken, notes = make()
    ctrl.working_dir = "/Users/dev/proj"
    import server.transcripts as tr
    monkeypatch.setattr(tr, "read_session",
                        lambda cwd, last=None, search=None: {"messages": [],
                                                             "cwd": cwd})
    res = await orch.handle_tool_call("read_session", {"last": 5})
    assert res["cwd"] == "/Users/dev/proj"


async def test_attach_ax_permission_error_becomes_tool_error(monkeypatch):
    orch, ctrl, spoken, notes = make()
    sess = {"id": "ax:ttys010", "raw_id": "ttys010", "cwd": "/tmp", "label": "tmp",
            "app": "Ghostty", "backend": "ax", "app_pid": "700",
            "controllable": True}
    orch.remember_terminals([sess])
    import server.ax_controller as axmod

    class Denied:
        def __init__(self, *a, **kw): self.status, self.working_dir = "idle", None
        def on_final(self, cb): pass
        async def start(self, wd=None):
            raise PermissionError("accessibility_permission_needed: grant it")
        async def stop(self, *, detach_only=False): pass

    monkeypatch.setattr(axmod, "AXController", Denied)
    res = await orch.handle_tool_call("attach_terminal", {"id": "ax:ttys010"})
    assert "accessibility_permission_needed" in res.get("error", "")


async def test_attach_screenless_sends_no_live_view_note(monkeypatch):
    orch, ctrl, spoken, ui = make()
    sess = {"id": "ax:ttys010", "raw_id": "ttys010", "cwd": "/tmp", "label": "tmp",
            "app": "Ghostty", "backend": "ax", "app_pid": "700",
            "controllable": True}
    orch.remember_terminals([sess])
    import server.ax_controller as axmod

    class ScreenlessAX:
        def __init__(self, *a, **kw):
            self.status, self.working_dir, self.mirrors_screen = "idle", "/tmp", False
        def on_final(self, cb): pass
        def on_output(self, cb): pass
        def on_output_color(self, cb): pass
        async def start(self, wd=None): pass
        async def stop(self, *, detach_only=False): pass

    monkeypatch.setattr(axmod, "AXController", ScreenlessAX)
    await orch.handle_tool_call("attach_terminal", {"id": "ax:ttys010"})
    notes = [m for m in ui if m.get("type") == "claude_output"
             and "Live view" in m.get("text", "")]
    assert len(notes) == 1


async def test_press_key_delegates_to_controller():
    class PressableController(FakeController):
        def __init__(self):
            super().__init__()
            self._started = True
            self.pressed = []
        async def press(self, key): self.pressed.append(key)

    orch, ctrl, _, _ = make(PressableController())
    res = await orch.press_key("1")
    assert ctrl.pressed == ["1"]
    assert res == {"pressed": "1"}


async def test_press_key_unsupported_controller_errors():
    class NoPress(FakeController):
        def __init__(self):
            super().__init__()
            self._started = True

    orch, _, _, _ = make(NoPress())
    res = await orch.press_key("1")
    assert res == {"error": "press not supported"}


async def test_press_key_guards_unstarted_session():
    class PressableController(FakeController):
        def __init__(self):
            super().__init__()
            self._started = False
            self.pressed = []
        async def press(self, key): self.pressed.append(key)

    orch, ctrl, _, _ = make(PressableController())
    res = await orch.press_key("1")
    assert "error" in res
    assert ctrl.pressed == []


async def test_attach_screenfull_sends_no_note(monkeypatch):
    orch, ctrl, spoken, ui = make()
    sess = {"id": "term:77:1", "raw_id": "77:1", "cwd": "/tmp", "label": "tmp",
            "app": "Terminal", "backend": "terminal_app", "controllable": True}
    orch.remember_terminals([sess])
    import server.terminals as terminals

    class FakeTerm:
        def __init__(self, raw_id, **kw):
            self.raw_id, self.status, self.working_dir = raw_id, "idle", None
            self.mirrors_screen = True
        def on_final(self, cb): pass
        def on_output(self, cb): pass
        def on_output_color(self, cb): pass
        async def start(self, wd=None): self.working_dir = wd
        async def stop(self, *, detach_only=False): pass

    monkeypatch.setattr(terminals, "TerminalAppController", FakeTerm)
    await orch.handle_tool_call("attach_terminal", {"id": "term:77:1"})
    notes = [m for m in ui if m.get("type") == "claude_output"
             and "Live view" in m.get("text", "")]
    assert notes == []


async def test_resolve_approval_presses_mapped_key():
    orch, ctrl, _, _ = make()
    ctrl.working_dir = "/p/loop"   # driven pane must match the approval's cwd
    from server.approvals import ApprovalStore, build_approval
    st = ApprovalStore()
    a = build_approval("/p/loop", "s", "> 1. Yes\n  2. No")
    st.put(a)
    orch.approvals = st
    pressed = []
    async def fake_press(k):
        pressed.append(k)
        return {"pressed": k}
    orch.press_key = fake_press
    res = await orch.handle_tool_call("resolve_approval", {"decision": "yes"})
    assert pressed == ["1"] and res["resolved"] == "1"
    assert st.latest() is None


async def test_resolve_approval_without_pending_reports_error():
    orch, ctrl, _, _ = make()
    ctrl.working_dir = "/p/loop"
    from server.approvals import ApprovalStore
    orch.approvals = ApprovalStore()
    res = await orch.handle_tool_call("resolve_approval", {"decision": "yes"})
    assert "error" in res


async def test_resolve_approval_after_swap_refuses():
    # A prompt raised in /p/A must not be actuated once the driven pane has
    # swapped to /p/B (mid-call attach_terminal): pressing now would type into
    # a DIFFERENT live terminal than the one that asked. Mirrors ws_session.py's
    # approval_decision guard, applied to the voice path.
    orch, ctrl, _, _ = make()
    from server.approvals import ApprovalStore, build_approval
    st = ApprovalStore()
    a = build_approval("/p/A", "s", "> 1. Yes\n  2. No")
    st.put(a)
    orch.approvals = st
    ctrl.working_dir = "/p/B"
    pressed = []
    async def fake_press(k):
        pressed.append(k)
        return {"pressed": k}
    orch.press_key = fake_press
    res = await orch.handle_tool_call("resolve_approval", {"decision": "yes"})
    assert pressed == []
    assert "error" in res
    assert st.get(a["approval_id"]) is not None   # left untouched, not resolved


async def test_resolve_approval_notifies_phone_when_wired():
    # The tap path (ws_session's approval_decision) pushes approval_resolved to
    # the phone directly; the voice path has no websocket, so it must reach the
    # same push through notifier.on_approval_resolved instead.
    orch, ctrl, _, _ = make()
    ctrl.working_dir = "/p/loop"
    from server.approvals import ApprovalStore, build_approval
    st = ApprovalStore()
    a = build_approval("/p/loop", "s", "> 1. Yes\n  2. No")
    st.put(a)
    orch.approvals = st

    resolved_ids = []

    class FakeNotifier:
        async def on_approval_resolved(self, approval_id):
            resolved_ids.append(approval_id)

    orch.notifier = FakeNotifier()

    async def fake_press(k):
        return {"pressed": k}
    orch.press_key = fake_press

    res = await orch.handle_tool_call("resolve_approval", {"decision": "yes"})
    assert res["resolved"] == "1"
    assert resolved_ids == [a["approval_id"]]


# --- fleet tools: list_sessions / switch_session / new_session -------------------

from server.session import Session, SessionRegistry


class FleetController(FakeController):
    """Records HOW stop() was called so tests can prove a fleet switch detaches
    only the local monitor and never kills the session's Claude process. Models
    the real controller's `_started` lifecycle: a detach_only stop clears it (the
    monitor is dead, so send/press would return no_session) and reattach re-arms
    the still-running session WITHOUT a kill+relaunch, matching TmuxController."""
    def __init__(self, cwd=None, session_name=""):
        super().__init__()
        self.working_dir = cwd
        self._session = session_name
        self._started = True
        self.stop_calls = []
        self.reattach_calls = 0
        self.presses = []

    async def stop(self, *, detach_only=False):
        self.stop_calls.append(detach_only)
        self._started = False
        if not detach_only:
            self.status = "stopped"

    async def reattach(self):
        self.reattach_calls += 1
        self._started = True
        return True

    async def send(self, text):
        if not self._started:
            raise ValueError("call start() before send()")
        self.sent.append(text)
        self.status = "working"

    async def press(self, key):
        self.presses.append(key)


class QuietHub:
    def set_offline_ring(self, v): pass


def make_fleet():
    """An orchestrator driving member 'aid' of a two-member registry."""
    a = FleetController("/p/alpha", "voxa-a")
    b = FleetController("/p/beta-proj", "voxa-b")
    reg = SessionRegistry()
    reg.add(Session("aid", a, QuietHub(), call_manager=None))
    reg.add(Session("bid", b, QuietHub(), call_manager=None))
    reg.set_active("aid")
    orch, _, spoken, ui = make(a)
    orch.sessions = reg
    return orch, reg, a, b, ui


async def test_list_sessions_shape_and_push():
    orch, reg, a, b, ui = make_fleet()
    a.status = "working"
    res = await orch.handle_tool_call("list_sessions", {})
    assert res["sessions"] == [
        {"id": "aid", "label": "alpha", "cwd": "/p/alpha",
         "status": "working", "active": True},
        {"id": "bid", "label": "beta-proj", "cwd": "/p/beta-proj",
         "status": "idle", "active": False},
    ]
    # The same payload reaches the phone as an unsolicited push too.
    pushes = [m for m in ui if m.get("type") == "sessions"]
    assert pushes and pushes[-1]["sessions"] == res["sessions"]


async def test_switch_session_by_label_substring_never_stops_old_process(monkeypatch):
    orch, reg, a, b, ui = make_fleet()
    a.status = "working"
    import server.transcripts as tr
    monkeypatch.setattr(tr, "recap", lambda cwd, **k: f"recap:{cwd}")
    res = await orch.handle_tool_call("switch_session", {"target": "BETA"})
    assert res["switched"] == "beta-proj"
    assert res["recap"] == "recap:/p/beta-proj"
    assert orch.controller is b
    assert reg.active_id == "bid"
    # The old controller only had its local monitor detached; the Claude
    # process underneath keeps running (never fully stopped).
    assert a.stop_calls == [True]
    assert a.status == "working"
    assert any(m.get("type") == "sessions" for m in ui)
    assert any(m.get("type") == "status" and m.get("working_dir") == "/p/beta-proj"
               for m in ui)


async def test_switch_session_resolves_by_exact_id_then_cwd():
    orch, reg, a, b, ui = make_fleet()
    res = await orch.handle_tool_call("switch_session", {"target": "bid"})
    assert res["switched"] == "beta-proj"
    # Back by cwd, trailing slash tolerated (rstrip-normalized compare).
    res = await orch.handle_tool_call("switch_session", {"target": "/p/alpha/"})
    assert res["switched"] == "alpha"
    assert reg.active_id == "aid"
    assert orch.controller is a


async def test_switch_session_unknown_target_lists_available_labels():
    orch, reg, a, b, ui = make_fleet()
    res = await orch.handle_tool_call("switch_session", {"target": "zzz"})
    assert "error" in res
    assert "alpha" in res["error"] and "beta-proj" in res["error"]
    assert orch.controller is a          # nothing swapped
    assert reg.active_id == "aid"        # selection untouched


async def test_switched_to_session_is_drivable_after_the_swap(monkeypatch):
    # The headline Phase 3 capability: switch the voice line to another session
    # AND drive it. The swap detaches the target's monitor on the way out (an
    # earlier switch-away), so switching back must re-arm it or send/press return
    # no_session. This is the regression the fully-green suite used to miss.
    orch, reg, a, b, ui = make_fleet()
    import server.transcripts as tr
    monkeypatch.setattr(tr, "recap", lambda cwd, **k: "")
    await orch.handle_tool_call("switch_session", {"target": "beta"})   # a detached
    assert a._started is False and orch.controller is b
    await orch.handle_tool_call("switch_session", {"target": "alpha"})  # back to a
    assert orch.controller is a
    assert a.reattach_calls == 1 and a._started is True
    # Now actually drive it: send and an approval keypress must both land.
    res = await orch.handle_tool_call("send_to_claude", {"text": "run the tests"})
    assert "error" not in res      # not the no_session the broken swap returned
    await asyncio.gather(*list(orch._bg), return_exceptions=True)  # drain the send task
    assert a.sent == ["run the tests"]
    pr = await orch.press_key("2")
    assert "error" not in pr and a.presses == ["2"]


async def test_switch_to_already_active_session_keeps_it_drivable(monkeypatch):
    # Switching to the session already being driven (a spoken "switch to alpha"
    # that fuzzy-matches the current project) must NOT detach it.
    orch, reg, a, b, ui = make_fleet()
    import server.transcripts as tr
    monkeypatch.setattr(tr, "recap", lambda cwd, **k: "")
    res = await orch.handle_tool_call("switch_session", {"target": "alpha"})
    assert res["switched"] == "alpha"
    assert a.stop_calls == []          # never detached itself
    assert a._started is True
    assert orch.controller is a and reg.active_id == "aid"
    pr = await orch.press_key("1")
    assert "error" not in pr and a.presses == ["1"]
    # The card still refreshes so the phone shows the (unchanged) active session.
    assert any(m.get("type") == "sessions" for m in ui)


async def test_new_session_creates_second_member_first_keeps_status(tmp_path, monkeypatch):
    a = FleetController(str(tmp_path / "alpha"), "voxa-a")
    a.status = "working"
    reg = SessionRegistry()
    reg.add(Session("aid", a, QuietHub(), call_manager=None))
    reg.set_active("aid")
    orch, _, spoken, ui = make(a)
    orch.sessions = reg

    created = {}

    class FakeTmux:
        def __init__(self, session_name="voxa", launch_terminal=True,
                     terminal_app="auto", **kw):
            created["name"] = session_name
            created["launch_terminal"] = launch_terminal
            self._session = session_name
            self.status = "idle"
            self.working_dir = None
        def on_final(self, cb): pass
        async def start(self, wd=None):
            self.working_dir = wd
            created["started"] = wd
        async def stop(self, *, detach_only=False): pass

    import server.tmux_controller as tc
    monkeypatch.setattr(tc, "TmuxController", FakeTmux)
    monkeypatch.setattr(tc, "pick_session_name",
                        lambda sid, cwd=None, **k: f"voxa-{sid}")

    class FakeNotifier:
        hooks_live = False
        call_manager = object()
    orch.notifier = FakeNotifier()

    proj = tmp_path / "gamma"
    proj.mkdir()
    res = await orch.handle_tool_call("new_session", {"path": str(proj)})
    assert res == {"created": "gamma"}
    assert len(reg.all()) == 2
    new = reg.active()
    assert new.id != "aid"
    # A distinct session-scoped tmux name, launched in its own terminal.
    assert created["name"] == f"voxa-{new.id}"
    assert created["name"] != "voxa-a"
    assert created["launch_terminal"] is True
    assert created["started"] == str(proj)
    assert orch.controller is new.controller
    # The previous session keeps running untouched (monitor detached only).
    assert a.status == "working"
    assert a.stop_calls == [True]
    assert any(m.get("type") == "sessions" for m in ui)


async def test_new_session_failed_start_rearms_previous_session(tmp_path, monkeypatch):
    # Latent bug: a new_session whose start() fails detached the previous session
    # (via _swap_controller's detach_only stop) and left it undrivable. The rollback
    # must re-arm the restored previous controller so it accepts input again.
    a = FleetController(str(tmp_path / "alpha"), "voxa-a")
    a.status = "working"
    reg = SessionRegistry()
    reg.add(Session("aid", a, QuietHub(), call_manager=None))
    reg.set_active("aid")
    orch, _, spoken, ui = make(a)
    orch.sessions = reg

    class BoomTmux:
        def __init__(self, session_name="voxa", launch_terminal=True,
                     terminal_app="auto", **kw):
            self._session = session_name
            self.status = "idle"
            self.working_dir = None
        def on_final(self, cb): pass
        async def start(self, wd=None):
            raise RuntimeError("terminal failed to open")
        async def stop(self, *, detach_only=False): pass

    import server.tmux_controller as tc
    monkeypatch.setattr(tc, "TmuxController", BoomTmux)
    monkeypatch.setattr(tc, "pick_session_name",
                        lambda sid, cwd=None, **k: f"voxa-{sid}")

    class FakeNotifier:
        hooks_live = False
        call_manager = object()
    orch.notifier = FakeNotifier()

    proj = tmp_path / "gamma"
    proj.mkdir()
    res = await orch.handle_tool_call("new_session", {"path": str(proj)})
    # It failed: an error dict, not a "created" one.
    assert "error" in res and "created" not in res
    # The fleet is back to just the previous member, active and DRIVABLE again.
    assert len(reg.all()) == 1
    assert reg.active_id == "aid"
    assert orch.controller is a
    assert a.reattach_calls == 1 and a._started is True
    # And it actually accepts input (the regression: no_session before the re-arm).
    r = await orch.handle_tool_call("send_to_claude", {"text": "still works"})
    assert "error" not in r
    await asyncio.gather(*list(orch._bg), return_exceptions=True)
    assert a.sent == ["still works"]


async def test_new_session_bad_path_suggests_siblings(tmp_path):
    orch, reg, a, b, ui = make_fleet()
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    res = await orch.handle_tool_call("new_session", {"path": str(tmp_path / "ghost")})
    assert "error" in res
    assert res["searched_in"] == str(tmp_path)
    assert "alpha" in res["suggestions"] and "beta" in res["suggestions"]
    assert len(reg.all()) == 2           # nothing was created
    assert orch.controller is a          # nothing swapped


async def test_fleet_tools_without_registry_report_error():
    # An orchestrator that was never wired to a registry (e.g. a bare unit
    # test) must degrade to a tool error, not crash the voice loop.
    orch, ctrl, _, _ = make()
    for tool, args in [("list_sessions", {}),
                       ("switch_session", {"target": "x"}),
                       ("new_session", {"path": "/tmp"})]:
        res = await orch.handle_tool_call(tool, args)
        assert "error" in res


# --- Task 2: queue runner (queue_task, burst, digest, pause/resume) --------------

from server.task_queue import TaskQueue


class SpyNotifier:
    """Records digest reports and exposes the queue-active set + needs-input hook
    the runner drives. line_open toggles the digest routing (speak vs report)."""
    def __init__(self, line_open=False):
        self.queue_active_cwds = set()
        self.on_queue_needs_input = None
        self.reports = []
        self.call_manager = type("CM", (), {"line_open": line_open})()

    async def report(self, summary, *, kind="finish", cwd="", approval=None):
        self.reports.append((summary, kind, cwd))


def make_queued(tmp_path, cwd="/p/loop", line_open=False):
    """An orchestrator wired with a real disk-backed TaskQueue and a spy notifier,
    driving a started, idle FakeController in ``cwd``."""
    ctrl = FakeController()
    ctrl._started = True
    ctrl.working_dir = cwd
    orch, ctrl, spoken, ui = make(ctrl)
    orch.queue = TaskQueue(str(tmp_path / "q.json"))
    orch.notifier = SpyNotifier(line_open=line_open)
    orch.notifier.on_queue_needs_input = orch._queue_note_needs_input
    touched = {"n": 0}
    orch._on_between_items = lambda: touched.__setitem__("n", touched["n"] + 1)
    return orch, ctrl, spoken, ui, touched


async def test_queue_task_immediate_when_idle_and_empty(tmp_path):
    orch, ctrl, _, ui, _ = make_queued(tmp_path)
    res = await orch.handle_tool_call("queue_task", {"text": "do A"})
    assert res == {"accepted": True, "queued": False}
    await asyncio.sleep(0)
    assert ctrl.sent == ["do A"]
    assert any(m.get("type") == "task_queue" for m in ui)


async def test_queue_task_enqueues_while_working(tmp_path):
    orch, ctrl, _, ui, _ = make_queued(tmp_path)
    await orch.handle_tool_call("queue_task", {"text": "do A"})
    await asyncio.sleep(0)
    res = await orch.handle_tool_call("queue_task", {"text": "do B"})
    assert res == {"queued": True, "position": 1}
    assert ctrl.sent == ["do A"]                       # B not dispatched yet
    assert "/p/loop" in orch.notifier.queue_active_cwds
    states = [i["state"] for i in orch.queue.items("/p/loop")]
    assert states == ["running", "queued"]


async def test_queue_task_no_session_guard_mirrors_send(tmp_path):
    orch, ctrl, _, _, _ = make_queued(tmp_path)
    ctrl._started = False
    res = await orch.handle_tool_call("queue_task", {"text": "do A"})
    assert res.get("error") == "no_session"


async def test_runner_dispatches_next_on_final_and_touches(tmp_path):
    orch, ctrl, spoken, ui, touched = make_queued(tmp_path)
    await orch.handle_tool_call("queue_task", {"text": "do A"})
    await asyncio.sleep(0)
    await orch.handle_tool_call("queue_task", {"text": "do B"})
    await ctrl.fire_final("A result")
    await asyncio.sleep(0)
    assert ctrl.sent == ["do A", "do B"]               # next item dispatched
    assert touched["n"] == 1                            # watchdog kept alive
    assert spoken == []                                 # per-item final not spoken
    assert orch.notifier.reports == []                  # no digest yet (not drained)


async def test_digest_composed_once_on_drain_all_done(tmp_path):
    orch, ctrl, spoken, ui, _ = make_queued(tmp_path)
    await orch.handle_tool_call("queue_task", {"text": "do A"})
    await asyncio.sleep(0)
    await orch.handle_tool_call("queue_task", {"text": "do B"})
    await ctrl.fire_final("A done")     # A -> done, dispatch B
    await asyncio.sleep(0)
    await ctrl.fire_final("B done")     # B -> done, queue drains
    await asyncio.sleep(0)
    assert spoken == []                                 # per-item finals suppressed
    assert len(orch.notifier.reports) == 1              # exactly ONE digest
    summary, kind, cwd = orch.notifier.reports[0]
    assert kind == "finish" and cwd == "/p/loop"
    assert summary == "2 tasks done in loop."
    assert "/p/loop" not in orch.notifier.queue_active_cwds   # burst cleared


async def test_digest_spoken_on_open_line(tmp_path):
    orch, ctrl, spoken, ui, _ = make_queued(tmp_path, line_open=True)
    await orch.handle_tool_call("queue_task", {"text": "do A"})
    await asyncio.sleep(0)
    await orch.handle_tool_call("queue_task", {"text": "do B"})
    await ctrl.fire_final("A done")
    await asyncio.sleep(0)
    await ctrl.fire_final("B done")
    await asyncio.sleep(0)
    assert spoken == ["2 tasks done in loop."]          # narrated on the live line
    assert orch.notifier.reports == []                  # not routed through report


async def test_single_unqueued_task_is_byte_identical(tmp_path):
    # One immediate task, nothing queued behind it: the final rings/speaks exactly
    # as today (inner callback), no digest, no suppression, no leftover history.
    orch, ctrl, spoken, ui, _ = make_queued(tmp_path)
    await orch.handle_tool_call("queue_task", {"text": "do A"})
    await asyncio.sleep(0)
    await ctrl.fire_final("A done")
    await asyncio.sleep(0)
    assert spoken == ["A done"]                          # inner _on_final spoke it
    assert {"type": "status", "status": "finished"} in ui
    assert orch.notifier.reports == []                   # no digest
    assert orch.queue.items("/p/loop") == []             # nothing left running
    assert orch.queue.drain_outcomes("/p/loop") == []    # history not polluted


async def test_plain_send_to_claude_final_unaffected_by_runner(tmp_path):
    # send_to_claude (not queue_task) with a queue wired must behave like today.
    orch, ctrl, spoken, ui, _ = make_queued(tmp_path)
    await orch.handle_tool_call("send_to_claude", {"text": "hi"})
    await asyncio.sleep(0)
    await ctrl.fire_final("all done")
    assert spoken == ["all done"]
    assert orch.notifier.reports == []


async def test_pause_on_needs_input_then_resume_dispatches_next(tmp_path):
    orch, ctrl, spoken, ui, touched = make_queued(tmp_path)
    await orch.handle_tool_call("queue_task", {"text": "do A"})
    await asyncio.sleep(0)
    await orch.handle_tool_call("queue_task", {"text": "do B"})
    # A hits a permission prompt: the notifier tells the runner to pause.
    await orch.notifier.on_queue_needs_input("/p/loop")
    assert ctrl.sent == ["do A"]                         # B NOT dispatched (paused)
    assert orch._queue_paused is True
    # Approval resolves -> the runner resumes and dispatches the next item.
    await orch._queue_resume("/p/loop")
    await asyncio.sleep(0)
    assert ctrl.sent == ["do A", "do B"]
    assert orch._queue_paused is False


async def test_digest_after_needs_input_terminates_with_needs_you(tmp_path):
    orch, ctrl, spoken, ui, _ = make_queued(tmp_path)
    await orch.handle_tool_call("queue_task", {"text": "do A"})
    await asyncio.sleep(0)
    await orch.handle_tool_call("queue_task", {"text": "bump the deps"})
    await ctrl.fire_final("A done")          # A done, dispatch B (bump the deps)
    await asyncio.sleep(0)
    await orch.notifier.on_queue_needs_input("/p/loop")   # B needs input, pause
    await orch._queue_resume("/p/loop")      # resolve -> nothing left -> digest
    await asyncio.sleep(0)
    assert len(orch.notifier.reports) == 1
    summary = orch.notifier.reports[0][0]
    assert "1 done" in summary and "needs you: bump the deps" in summary


async def test_resolve_approval_resumes_paused_queue(tmp_path):
    # The voice resolve_approval tool must resume a queue paused on needs_input.
    from server.approvals import ApprovalStore, build_approval
    orch, ctrl, _, _, _ = make_queued(tmp_path)
    await orch.handle_tool_call("queue_task", {"text": "do A"})
    await asyncio.sleep(0)
    await orch.handle_tool_call("queue_task", {"text": "do B"})
    await orch.notifier.on_queue_needs_input("/p/loop")
    assert ctrl.sent == ["do A"]
    st = ApprovalStore()
    a = build_approval("/p/loop", "s", "> 1. Yes\n  2. No")
    st.put(a)
    orch.approvals = st
    orch.notifier = orch.notifier   # keep spy (no on_approval_resolved needed)
    async def fake_press(k): return {"pressed": k}
    orch.press_key = fake_press
    await orch.handle_tool_call("resolve_approval", {"decision": "yes"})
    await asyncio.sleep(0)
    assert ctrl.sent == ["do A", "do B"]                 # resumed after resolve


async def test_stop_flushes_queue_with_count(tmp_path):
    orch, ctrl, _, _, _ = make_queued(tmp_path)
    await orch.handle_tool_call("queue_task", {"text": "do A"})
    await asyncio.sleep(0)
    await orch.handle_tool_call("queue_task", {"text": "do B"})
    res = await orch.handle_tool_call("stop_claude", {})
    assert res["status"] == "idle"
    assert res["dropped"] == 2                            # running + queued dropped
    assert orch.queue.items("/p/loop") == []
    assert "/p/loop" not in orch.notifier.queue_active_cwds


async def test_queue_task_without_queue_falls_back_to_send(tmp_path):
    # A bare orchestrator (queue never wired) treats queue_task like send_to_claude.
    orch, ctrl, _, _ = make()
    ctrl._started = True
    ctrl.working_dir = "/p/loop"
    res = await orch.handle_tool_call("queue_task", {"text": "do A"})
    assert res == {"accepted": True, "status": "working"}
    await asyncio.sleep(0)
    assert ctrl.sent == ["do A"]


async def test_queue_remove_and_move_push_updates(tmp_path):
    orch, ctrl, _, ui, _ = make_queued(tmp_path)
    await orch.handle_tool_call("queue_task", {"text": "do A"})
    await asyncio.sleep(0)
    r1 = await orch.handle_tool_call("queue_task", {"text": "do B"})
    r2 = await orch.handle_tool_call("queue_task", {"text": "do C"})
    items = orch.queue.items("/p/loop")
    b_id = next(i["id"] for i in items if i["text"] == "do B")
    await orch.queue_remove(b_id)
    texts = [i["text"] for i in orch.queue.items("/p/loop")]
    assert "do B" not in texts and texts == ["do A", "do C"]
    # A task_queue push followed each mutation.
    assert sum(1 for m in ui if m.get("type") == "task_queue") >= 3


async def test_queue_engaged_reflects_queue_and_status(tmp_path):
    # The mic-gate signal (Task 3): the line stays open while idle no matter what,
    # and while WORKING only when the driven cwd has a non-empty queue. Working with
    # an empty queue keeps today's cost-saving pause.
    orch, ctrl, _, _, _ = make_queued(tmp_path)
    ctrl.status = "idle"
    assert orch.queue_engaged is True             # idle: mic always open
    ctrl.status = "working"
    assert orch.queue_engaged is False            # working + empty queue -> paused
    orch.queue.add("/p/loop", "later item")
    assert orch.queue_engaged is True             # working + queued -> mic stays open


def test_queue_engaged_without_queue_pauses_while_working():
    # A bare orchestrator (no queue wired) keeps the mic paused while working, so a
    # non-queue user's cost profile is byte-identical to today.
    orch, ctrl, _, _ = make()
    ctrl.working_dir = "/p/loop"
    ctrl.status = "working"
    assert orch.queue_engaged is False
    ctrl.status = "idle"
    assert orch.queue_engaged is True


async def test_swapping_controller_ends_an_active_burst(tmp_path):
    # Final-review IMPORTANT: switching away mid-burst (fleet switch, new_session,
    # attach) must END the burst. Otherwise the backgrounded cwd stays in the
    # notifier suppression set forever, so every later finish for it (arriving via
    # the Stop hook once it is headless) is silently dropped, and stale burst state
    # could resurrect a queued item after switching back.
    orch, ctrl, _, _, _ = make_queued(tmp_path)
    orch._burst_cwd = "/p/loop"
    orch._running_item_id = "someid"
    orch._queue_paused = True
    orch.notifier.queue_active_cwds.add("/p/loop")

    other = FakeController()
    other._started = True
    other.working_dir = "/p/other"
    await orch._swap_controller(other)

    assert orch.controller is other
    assert orch._burst_cwd is None
    assert orch._running_item_id is None
    assert orch._queue_paused is False
    assert "/p/loop" not in orch.notifier.queue_active_cwds


# --- Phase 5.2: git by voice ------------------------------------------------

async def test_git_status_tool_reads_driven_cwd(monkeypatch):
    import server.git_ops as git_ops
    calls = []

    def fake(cwd):
        calls.append(cwd)
        return {"summary": "On branch main, the working tree is clean.",
                "branch": "main"}

    monkeypatch.setattr(git_ops, "git_status_summary", fake)
    orch, ctrl, _, _ = make()
    ctrl.working_dir = "/p/loop"
    res = await orch.handle_tool_call("git_status", {})
    assert calls == ["/p/loop"]
    assert res["branch"] == "main"


async def test_git_tools_require_a_session_folder():
    orch, ctrl, _, _ = make()
    ctrl.working_dir = None
    for tool in ("git_status", "git_diff", "git_commit", "git_push"):
        res = await orch.handle_tool_call(tool, {"message": "m"})
        assert "error" in res, tool


async def test_git_diff_tool_returns_diff(monkeypatch):
    import server.git_ops as git_ops
    monkeypatch.setattr(git_ops, "git_diff_summary",
                        lambda cwd: {"summary": "a.txt | 1 +", "diff": "+two",
                                     "branch": "main"})
    orch, ctrl, _, _ = make()
    ctrl.working_dir = "/p/loop"
    res = await orch.handle_tool_call("git_diff", {})
    assert res["diff"] == "+two"


async def test_git_commit_builds_approval_and_does_not_execute(monkeypatch):
    import server.git_ops as git_ops
    executed = []
    monkeypatch.setattr(git_ops, "commit_preflight",
                        lambda cwd: {"branch": "main", "changes": 2})
    monkeypatch.setattr(git_ops, "git_commit",
                        lambda cwd, m: executed.append((cwd, m)))
    from server.approvals import ApprovalStore
    orch, ctrl, _, _ = make()
    ctrl.working_dir = "/p/loop"
    orch.approvals = ApprovalStore()
    res = await orch.handle_tool_call("git_commit", {"message": "fix bug"})
    assert executed == []                          # guarded: nothing ran yet
    a = orch.approvals.active_for("/p/loop")
    assert res["pending_approval"] == a["approval_id"]
    assert a["tool"] == "git_commit"
    assert a["action"] == {"kind": "git_commit", "cwd": "/p/loop",
                           "message": "fix bug"}
    assert [o["key"] for o in a["options"]] == ["y", "n"]
    assert "fix bug" in a["summary"] and "main" in a["summary"]


async def test_git_commit_requires_message():
    from server.approvals import ApprovalStore
    orch, ctrl, _, _ = make()
    ctrl.working_dir = "/p/loop"
    orch.approvals = ApprovalStore()
    res = await orch.handle_tool_call("git_commit", {"message": "   "})
    assert "error" in res
    assert orch.approvals.active_for("/p/loop") is None


async def test_git_commit_preflight_error_passes_through(monkeypatch):
    import server.git_ops as git_ops
    monkeypatch.setattr(
        git_ops, "commit_preflight",
        lambda cwd: {"error": "Nothing to commit; the working tree is clean."})
    from server.approvals import ApprovalStore
    orch, ctrl, _, _ = make()
    ctrl.working_dir = "/p/loop"
    orch.approvals = ApprovalStore()
    res = await orch.handle_tool_call("git_commit", {"message": "m"})
    assert "Nothing to commit" in res["error"]
    assert orch.approvals.active_for("/p/loop") is None


async def test_git_commit_with_push_names_branch_and_upstream(monkeypatch):
    import server.git_ops as git_ops
    monkeypatch.setattr(git_ops, "commit_preflight",
                        lambda cwd: {"branch": "main", "changes": 1})
    monkeypatch.setattr(git_ops, "push_preflight",
                        lambda cwd: {"branch": "main", "upstream": "origin/main"})
    from server.approvals import ApprovalStore
    orch, ctrl, _, _ = make()
    ctrl.working_dir = "/p/loop"
    orch.approvals = ApprovalStore()
    await orch.handle_tool_call("git_commit", {"message": "m", "push": True})
    a = orch.approvals.active_for("/p/loop")
    assert a["action"]["push"] is True
    assert "main" in a["summary"] and "origin/main" in a["summary"]


async def test_git_commit_with_push_refuses_without_upstream(monkeypatch):
    import server.git_ops as git_ops
    monkeypatch.setattr(git_ops, "commit_preflight",
                        lambda cwd: {"branch": "main", "changes": 1})
    monkeypatch.setattr(
        git_ops, "push_preflight",
        lambda cwd: {"error": "Branch main has no upstream configured, so I won't push it."})
    from server.approvals import ApprovalStore
    orch, ctrl, _, _ = make()
    ctrl.working_dir = "/p/loop"
    orch.approvals = ApprovalStore()
    res = await orch.handle_tool_call("git_commit", {"message": "m", "push": True})
    assert "upstream" in res["error"]
    assert orch.approvals.active_for("/p/loop") is None


async def test_git_push_approval_summary_names_the_branch(monkeypatch):
    import server.git_ops as git_ops
    monkeypatch.setattr(git_ops, "push_preflight",
                        lambda cwd: {"branch": "feat/x", "upstream": "origin/feat/x"})
    from server.approvals import ApprovalStore
    orch, ctrl, _, _ = make()
    ctrl.working_dir = "/p/loop"
    orch.approvals = ApprovalStore()
    res = await orch.handle_tool_call("git_push", {})
    a = orch.approvals.active_for("/p/loop")
    assert res["pending_approval"] == a["approval_id"]
    assert a["tool"] == "git_push"
    assert "feat/x" in a["summary"]
    assert a["action"] == {"kind": "git_push", "cwd": "/p/loop"}


async def test_git_push_without_upstream_is_spoken_error(monkeypatch):
    import server.git_ops as git_ops
    monkeypatch.setattr(
        git_ops, "push_preflight",
        lambda cwd: {"error": "Branch main has no upstream configured, so I won't push it."})
    from server.approvals import ApprovalStore
    orch, ctrl, _, _ = make()
    ctrl.working_dir = "/p/loop"
    orch.approvals = ApprovalStore()
    res = await orch.handle_tool_call("git_push", {})
    assert "upstream" in res["error"]
    assert orch.approvals.active_for("/p/loop") is None


async def test_git_commit_pushes_card_to_phone_when_line_attached(monkeypatch):
    import server.git_ops as git_ops
    monkeypatch.setattr(git_ops, "commit_preflight",
                        lambda cwd: {"branch": "main", "changes": 1})
    from server.approvals import ApprovalStore
    orch, ctrl, _, _ = make()
    ctrl.working_dir = "/p/loop"
    orch.approvals = ApprovalStore()
    cards = []

    class FakeNotifier:
        async def on_approval(self, a):
            cards.append(a)

    orch.notifier = FakeNotifier()
    res = await orch.handle_tool_call("git_commit", {"message": "m"})
    assert len(cards) == 1
    assert cards[0]["approval_id"] == res["pending_approval"]


async def test_resolve_approval_git_yes_runs_action_not_keypress(monkeypatch):
    import server.git_ops as git_ops
    committed = []
    monkeypatch.setattr(
        git_ops, "git_commit",
        lambda cwd, m: committed.append((cwd, m)) or
        {"summary": "Committed on main: m.", "branch": "main"})
    from server.approvals import ApprovalStore, build_action_approval
    orch, ctrl, _, _ = make()
    ctrl.working_dir = "/p/loop"
    st = ApprovalStore()
    a = build_action_approval("/p/loop", "Commit 1 change(s) on main: m",
                              tool="git_commit",
                              action={"kind": "git_commit", "cwd": "/p/loop",
                                      "message": "m"})
    st.put(a)
    orch.approvals = st
    pressed = []

    async def fake_press(k):
        pressed.append(k)
        return {"pressed": k}

    orch.press_key = fake_press
    res = await orch.handle_tool_call("resolve_approval", {"decision": "yes"})
    assert committed == [("/p/loop", "m")]
    assert pressed == []                    # a git approval never touches a pane
    assert res["summary"].startswith("Committed")
    assert st.get(a["approval_id"]) is None


async def test_resolve_approval_git_no_declines_without_running(monkeypatch):
    import server.git_ops as git_ops
    committed = []
    monkeypatch.setattr(git_ops, "git_commit",
                        lambda cwd, m: committed.append((cwd, m)))
    from server.approvals import ApprovalStore, build_action_approval
    orch, ctrl, _, _ = make()
    ctrl.working_dir = "/p/loop"
    st = ApprovalStore()
    a = build_action_approval("/p/loop", "s", tool="git_commit",
                              action={"kind": "git_commit", "cwd": "/p/loop",
                                      "message": "m"})
    st.put(a)
    orch.approvals = st
    res = await orch.handle_tool_call("resolve_approval", {"decision": "no"})
    assert committed == []
    assert res.get("declined") is True
    assert st.get(a["approval_id"]) is None


async def test_resolve_approval_git_clears_phone_card():
    from server.approvals import ApprovalStore, build_action_approval
    orch, ctrl, _, _ = make()
    ctrl.working_dir = "/p/loop"
    st = ApprovalStore()
    a = build_action_approval("/p/loop", "s", tool="git_push",
                              action={"kind": "git_push", "cwd": "/p/loop"})
    st.put(a)
    orch.approvals = st
    resolved = []

    class FakeNotifier:
        async def on_approval_resolved(self, approval_id):
            resolved.append(approval_id)

    orch.notifier = FakeNotifier()
    await orch.handle_tool_call("resolve_approval", {"decision": "no"})
    assert resolved == [a["approval_id"]]


async def test_resolve_approval_git_respects_cwd_guard():
    # Same Phase 1 semantics as pane approvals: a decision only acts on the
    # approval active for the DRIVEN cwd, never one left over from a swap.
    from server.approvals import ApprovalStore, build_action_approval
    orch, ctrl, _, _ = make()
    ctrl.working_dir = "/p/B"
    st = ApprovalStore()
    a = build_action_approval("/p/A", "s", tool="git_push",
                              action={"kind": "git_push", "cwd": "/p/A"})
    st.put(a)
    orch.approvals = st
    res = await orch.handle_tool_call("resolve_approval", {"decision": "yes"})
    assert "error" in res
    assert st.get(a["approval_id"]) is not None


async def test_execute_approved_action_commit_then_push(monkeypatch):
    import server.git_ops as git_ops
    monkeypatch.setattr(git_ops, "git_commit",
                        lambda cwd, m: {"summary": "Committed on main: m.",
                                        "branch": "main"})
    monkeypatch.setattr(git_ops, "git_push",
                        lambda cwd: {"summary": "Pushed main to origin/main.",
                                     "branch": "main"})
    orch, _, _, _ = make()
    res = await orch.execute_approved_action(
        {"action": {"kind": "git_commit", "cwd": "/p/loop", "message": "m",
                    "push": True}})
    assert "Committed" in res["summary"] and "Pushed" in res["summary"]
    assert "error" not in res


async def test_execute_approved_action_push_failure_after_commit(monkeypatch):
    import server.git_ops as git_ops
    monkeypatch.setattr(git_ops, "git_commit",
                        lambda cwd, m: {"summary": "Committed on main: m.",
                                        "branch": "main"})
    monkeypatch.setattr(git_ops, "git_push",
                        lambda cwd: {"error": "Push failed: rejected."})
    orch, _, _, _ = make()
    res = await orch.execute_approved_action(
        {"action": {"kind": "git_commit", "cwd": "/p/loop", "message": "m",
                    "push": True}})
    assert "Committed" in res["error"] and "Push failed" in res["error"]


async def test_execute_approved_action_unknown_kind():
    orch, _, _, _ = make()
    res = await orch.execute_approved_action({"action": {"kind": "rm_rf"}})
    assert "error" in res
