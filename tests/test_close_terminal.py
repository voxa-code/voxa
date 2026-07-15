"""ws_session's close_terminal control branch: resolve -> detach the driven
controller/fleet member if they point at the closed cwd -> perform the actual
close -> drop that cwd's pending approvals as stale -> reply -> refresh the
terminals list. Fail-open throughout (server/ws_session.py:_handle_close_terminal).
"""
import json

import server.terminals as term
from server.approvals import ApprovalStore, build_approval
from server.ws_session import _handle_close_terminal, handle_client_control


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, m):
        self.sent.append(m)


class FakeController:
    def __init__(self, working_dir=""):
        self.working_dir = working_dir
        self.stopped = []

    async def stop(self, detach_only=False):
        self.stopped.append(detach_only)


class FakeMember:
    def __init__(self, session_id, controller):
        self.id = session_id
        self.controller = controller


class FakeRegistry:
    def __init__(self, members=None):
        self._members = members or []
        self.removed = []
        self.find_calls = []

    def find_by_cwd(self, cwd):
        self.find_calls.append(cwd)
        for m in self._members:
            if (m.controller.working_dir or "").rstrip("/") == cwd.rstrip("/"):
                return m
        return None

    def remove(self, session_id):
        self.removed.append(session_id)


class FakeOrch:
    def __init__(self, controller=None, sessions=None):
        self.controller = controller or FakeController()
        self.sessions = sessions
        self.tool_calls = []
        self.pushed_sessions = 0

    async def handle_tool_call(self, name, args):
        self.tool_calls.append((name, args))
        return {}

    async def push_sessions(self):
        self.pushed_sessions += 1


class FakeNotifier:
    def __init__(self):
        self.approvals = ApprovalStore()


def _fake_session(id_, cwd, backend="tmux", **extra):
    d = {"id": id_, "raw_id": id_.split(":")[-1], "cwd": cwd, "backend": backend,
         "label": cwd.rsplit("/", 1)[-1], "app": backend, "controllable": True}
    d.update(extra)
    return d


async def test_close_terminal_happy_path(monkeypatch):
    sess = _fake_session("tmux::work", "/p/work")
    monkeypatch.setattr(term, "discover_claude_sessions", lambda: [sess])
    monkeypatch.setattr(term, "close_terminal",
                        lambda id_, **kw: {"cwd": "/p/work", "backend": "tmux"})
    orch = FakeOrch()
    ws = FakeWS()
    await _handle_close_terminal("tmux::work", orch, ws, notifier=None)
    assert {"type": "terminal_closed", "id": "tmux::work"} in ws.sent
    assert ("list_terminals", {}) in orch.tool_calls


async def test_close_terminal_includes_note_when_backend_reports_one(monkeypatch):
    sess = _fake_session("ax:ttys010", "/p/y", backend="ax")
    monkeypatch.setattr(term, "discover_claude_sessions", lambda: [sess])
    monkeypatch.setattr(term, "close_terminal", lambda id_, **kw: {
        "cwd": "/p/y", "backend": "ax",
        "note": "ended the Claude process; the window itself stays open"})
    orch = FakeOrch()
    ws = FakeWS()
    await _handle_close_terminal("ax:ttys010", orch, ws, notifier=None)
    closed = next(m for m in ws.sent if m["type"] == "terminal_closed")
    assert closed["note"] == "ended the Claude process; the window itself stays open"


async def test_close_terminal_detaches_driven_controller_same_cwd(monkeypatch):
    sess = _fake_session("iterm:SID", "/p/driven")
    monkeypatch.setattr(term, "discover_claude_sessions", lambda: [sess])
    monkeypatch.setattr(term, "close_terminal",
                        lambda id_, **kw: {"cwd": "/p/driven", "backend": "iterm"})
    driven = FakeController("/p/driven")
    orch = FakeOrch(controller=driven)
    ws = FakeWS()
    await _handle_close_terminal("iterm:SID", orch, ws, notifier=None)
    assert driven.stopped == [True]


async def test_close_terminal_leaves_other_driven_controller_alone(monkeypatch):
    sess = _fake_session("iterm:SID", "/p/other")
    monkeypatch.setattr(term, "discover_claude_sessions", lambda: [sess])
    monkeypatch.setattr(term, "close_terminal",
                        lambda id_, **kw: {"cwd": "/p/other", "backend": "iterm"})
    driven = FakeController("/p/driven")   # a DIFFERENT cwd than the one being closed
    orch = FakeOrch(controller=driven)
    ws = FakeWS()
    await _handle_close_terminal("iterm:SID", orch, ws, notifier=None)
    assert driven.stopped == []


async def test_close_terminal_removes_matching_fleet_member(monkeypatch):
    sess = _fake_session("tmux::work", "/p/work")
    monkeypatch.setattr(term, "discover_claude_sessions", lambda: [sess])
    monkeypatch.setattr(term, "close_terminal",
                        lambda id_, **kw: {"cwd": "/p/work", "backend": "tmux"})
    member_ctrl = FakeController("/p/work")
    member = FakeMember("sess-1", member_ctrl)
    registry = FakeRegistry([member])
    orch = FakeOrch(controller=FakeController("/p/elsewhere"), sessions=registry)
    ws = FakeWS()
    await _handle_close_terminal("tmux::work", orch, ws, notifier=None)
    assert member_ctrl.stopped == [True]
    assert registry.removed == ["sess-1"]
    assert orch.pushed_sessions == 1


async def test_close_terminal_no_matching_fleet_member_is_a_noop(monkeypatch):
    sess = _fake_session("tmux::work", "/p/work")
    monkeypatch.setattr(term, "discover_claude_sessions", lambda: [sess])
    monkeypatch.setattr(term, "close_terminal",
                        lambda id_, **kw: {"cwd": "/p/work", "backend": "tmux"})
    registry = FakeRegistry([])   # nothing registered for this cwd
    orch = FakeOrch(sessions=registry)
    ws = FakeWS()
    await _handle_close_terminal("tmux::work", orch, ws, notifier=None)
    assert registry.removed == []
    assert orch.pushed_sessions == 0


async def test_close_terminal_drops_approvals_and_pushes_stale(monkeypatch):
    sess = _fake_session("tmux::work", "/p/work")
    monkeypatch.setattr(term, "discover_claude_sessions", lambda: [sess])
    monkeypatch.setattr(term, "close_terminal",
                        lambda id_, **kw: {"cwd": "/p/work", "backend": "tmux"})
    notifier = FakeNotifier()
    a = build_approval("/p/work", "s", "> 1. Yes\n  2. No")
    notifier.approvals.put(a)
    orch = FakeOrch()
    ws = FakeWS()
    await _handle_close_terminal("tmux::work", orch, ws, notifier=notifier)
    resolved = [m for m in ws.sent if m["type"] == "approval_resolved"]
    assert resolved == [{"type": "approval_resolved",
                         "approval_id": a["approval_id"], "outcome": "stale"}]
    assert notifier.approvals.active_for("/p/work") is None


async def test_close_terminal_no_approvals_for_other_cwd_untouched(monkeypatch):
    sess = _fake_session("tmux::work", "/p/work")
    monkeypatch.setattr(term, "discover_claude_sessions", lambda: [sess])
    monkeypatch.setattr(term, "close_terminal",
                        lambda id_, **kw: {"cwd": "/p/work", "backend": "tmux"})
    notifier = FakeNotifier()
    other = build_approval("/p/unrelated", "s", "> 1. Yes\n  2. No")
    notifier.approvals.put(other)
    orch = FakeOrch()
    ws = FakeWS()
    await _handle_close_terminal("tmux::work", orch, ws, notifier=notifier)
    assert not any(m["type"] == "approval_resolved" for m in ws.sent)
    assert notifier.approvals.get(other["approval_id"]) is not None


async def test_close_terminal_failure_leaves_registry_untouched(monkeypatch):
    monkeypatch.setattr(term, "discover_claude_sessions", lambda: [])   # nothing open
    monkeypatch.setattr(term, "close_terminal",
                        lambda id_, **kw: {"error": "that terminal is no longer open"})
    registry = FakeRegistry([FakeMember("sess-1", FakeController("/p/anything"))])
    orch = FakeOrch(controller=FakeController("/p/anything"), sessions=registry)
    ws = FakeWS()
    await _handle_close_terminal("nope", orch, ws, notifier=None)
    assert orch.controller.stopped == []
    assert registry.removed == []
    assert registry.find_calls == []          # cwd never resolved -> cleanup skipped entirely
    assert ws.sent[-1] == {"type": "status", "status": "close error: that terminal is no longer open"}
    # Still refreshes the list even on failure, so a stale phone view can heal.
    assert ("list_terminals", {}) in orch.tool_calls


async def test_close_terminal_failure_does_not_push_terminal_closed(monkeypatch):
    monkeypatch.setattr(term, "discover_claude_sessions", lambda: [])
    monkeypatch.setattr(term, "close_terminal", lambda id_, **kw: {"error": "boom"})
    orch = FakeOrch()
    ws = FakeWS()
    await _handle_close_terminal("nope", orch, ws, notifier=None)
    assert not any(m.get("type") == "terminal_closed" for m in ws.sent)


async def test_close_terminal_discovery_exception_fails_open(monkeypatch):
    def boom():
        raise RuntimeError("discovery blew up")
    monkeypatch.setattr(term, "discover_claude_sessions", boom)
    monkeypatch.setattr(term, "close_terminal", lambda id_, **kw: {"error": "that terminal is no longer open"})
    orch = FakeOrch()
    ws = FakeWS()
    await _handle_close_terminal("nope", orch, ws, notifier=None)   # must not raise
    assert ws.sent[-1]["type"] == "status"


async def test_close_terminal_close_call_exception_fails_open(monkeypatch):
    sess = _fake_session("tmux::work", "/p/work")
    monkeypatch.setattr(term, "discover_claude_sessions", lambda: [sess])

    def boom(id_, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(term, "close_terminal", boom)
    orch = FakeOrch()
    ws = FakeWS()
    await _handle_close_terminal("tmux::work", orch, ws, notifier=None)   # must not raise
    assert ws.sent[-1]["type"] == "status"
    assert "close error" in ws.sent[-1]["status"]


# --- via handle_client_control (the phone's wire message) -----------------

async def test_close_terminal_control_message_dispatches(monkeypatch):
    sess = _fake_session("tmux::work", "/p/work")
    monkeypatch.setattr(term, "discover_claude_sessions", lambda: [sess])
    monkeypatch.setattr(term, "close_terminal",
                        lambda id_, **kw: {"cwd": "/p/work", "backend": "tmux"})
    orch = FakeOrch()
    ws = FakeWS()
    await handle_client_control(
        json.dumps({"type": "close_terminal", "id": "tmux::work"}), orch, ws)
    assert {"type": "terminal_closed", "id": "tmux::work"} in ws.sent


async def test_close_terminal_control_message_without_id_is_ignored():
    orch = FakeOrch()
    ws = FakeWS()
    await handle_client_control(json.dumps({"type": "close_terminal"}), orch, ws)
    assert ws.sent == []
