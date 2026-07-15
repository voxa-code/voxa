"""Multi-session (fleet) awareness wiring: when several Claude sessions run at
once, a finish/question relayed to the phone must be attributed to the right
project, a foreign session finishing mid-call must still reach the user, and
Gemini must be told what else is open. Covers fixes 1, 2, and 5 from
docs/superpowers/plans (SessionHub labeling, notifier.on_update_speak,
fleet-status context lines), wired through serve_ws exactly like a real call.
Fakes follow the patterns in tests/test_ws_prewarm.py.
"""
from __future__ import annotations

import asyncio

from server.call_manager import CallManager
from server.config import Config
from server.notifier import Notifier
from server.prewarm import Prewarmer
from server.session import Session, SessionRegistry
from server.session_hub import SessionHub
from server.ws_session import serve_ws


class _NoPushPusher:
    async def send_voip(self, *a, **k):
        return False

    async def send_voip_cancel(self, *a, **k):
        return None


class _FakeRegistry:
    def tokens(self, account=None):
        return []

    def remove(self, *a, **k):
        pass


class _FakeDrivenCtrl:
    def __init__(self, cwd, status="idle"):
        self.working_dir = cwd
        self.status = status
        self._started = True   # skip serve_ws's reattach path

    def on_final(self, cb):
        pass

    def capture_text(self):
        return ""


class _FakeWSChannel:
    def __init__(self, incoming, query_params=None):
        self.query_params = query_params or {}
        self.sent_bytes: list[bytes] = []
        self.sent_json: list[dict] = []
        self._in = incoming

    async def send_json(self, m):
        self.sent_json.append(m)

    async def send_bytes(self, b):
        self.sent_bytes.append(b)

    async def receive(self):
        return await self._in.get()


class _ColdOperator:
    """A cold (non-prewarmed) operator fake that records everything spoken and
    every session_context passed to open_with_context, so the wiring under
    test (label prefixing, foreign-update speak, fleet context) is directly
    observable."""

    def __init__(self):
        self.spoke: list[tuple] = []
        self.contexts: list[tuple] = []

    def set_audio_out(self, cb):
        pass

    def set_text_out(self, cb):
        pass

    async def send_audio(self, pcm):
        pass

    def suppress_greeting(self):
        pass

    async def speak(self, text, immediate=False, dedupe_key=""):
        self.spoke.append((text, immediate, dedupe_key))

    async def open_with_context(self, opening, context=""):
        self.contexts.append((opening, context))

    async def run(self):
        await asyncio.Event().wait()   # blocks until cancelled, like the real loop


def _cfg():
    return Config(gemini_api_key="k", gemini_live_model="m",
                  auth_token="t", host="127.0.0.1", port=8787)


async def _drive_two_sessions(monkeypatch, *, one_session=False):
    """Build a registry with one or two fleet members, drive serve_ws (cold path,
    no prewarmer) through `begin` on the first (active) one, and return everything
    a test needs once the opening has been spoken."""
    monkeypatch.setattr("server.terminals.discover_claude_sessions", lambda: [])

    cm = CallManager(_NoPushPusher(), _FakeRegistry())
    notifier = Notifier(cm, push_enabled=True)
    sessions = SessionRegistry()

    ctrl1 = _FakeDrivenCtrl("/p/loop", status="idle")
    hub1 = SessionHub(ctrl1, cm)
    sessions.add(Session("s1", ctrl1, hub1, cm))
    sessions.set_active("s1")

    if not one_session:
        ctrl2 = _FakeDrivenCtrl("/p/veil", status="working")
        hub2 = SessionHub(ctrl2, CallManager(_NoPushPusher(), _FakeRegistry()))
        sessions.add(Session("s2", ctrl2, hub2, cm))

    ops: list[_ColdOperator] = []

    def cold_factory(config, handle_tool_call, voice="", lang="", account=""):
        op = _ColdOperator()
        ops.append(op)
        return op

    incoming: asyncio.Queue = asyncio.Queue()
    ws = _FakeWSChannel(incoming)
    task = asyncio.ensure_future(serve_ws(
        ws, config=_cfg(), mode="attach", sessions=sessions, notifier=notifier,
        operator_factory=cold_factory, prewarmer=None))
    await incoming.put({"type": "websocket.receive", "text": '{"type":"begin"}'})
    for _ in range(400):
        if ops and ops[0].contexts:
            break
        await asyncio.sleep(0.005)
    return ws, ops[0], notifier, sessions, hub1, task, incoming


async def _teardown(task, incoming):
    await incoming.put({"type": "websocket.disconnect"})
    try:
        await asyncio.wait_for(task, 1.0)
    except Exception:
        task.cancel()


# --- Fix 1: label the driven session's narration when several are live -----------

async def test_hub_on_final_labels_when_two_sessions_are_live(monkeypatch):
    ws, op, notifier, sessions, hub1, task, incoming = await _drive_two_sessions(monkeypatch)
    try:
        assert hub1.multi_fn() is True
        assert hub1.label_fn() == "loop"
        await hub1.on_final("finished: tests pass")
        assert op.spoke[-1][0] == "[loop] finished: tests pass"
    finally:
        await _teardown(task, incoming)


async def test_hub_on_final_stays_bare_with_one_session(monkeypatch):
    ws, op, notifier, sessions, hub1, task, incoming = await _drive_two_sessions(
        monkeypatch, one_session=True)
    try:
        assert hub1.multi_fn() is False
        await hub1.on_final("finished: tests pass")
        assert op.spoke[-1][0] == "finished: tests pass"
    finally:
        await _teardown(task, incoming)


# --- Fix 2: a foreign session's finish is spoken live, named by its project ------

async def test_foreign_update_is_spoken_with_dedupe_key(monkeypatch):
    ws, op, notifier, sessions, hub1, task, incoming = await _drive_two_sessions(monkeypatch)
    try:
        assert notifier.on_update_speak is not None
        await notifier.report("veil finished: tests pass", cwd="/p/veil")
        assert ("veil finished: tests pass", True, "/p/veil") in op.spoke
        assert any(
            m.get("type") == "missed_updates"
            and any(i.get("text") == "veil finished: tests pass" for i in m.get("items", []))
            for m in ws.sent_json
        )
    finally:
        await _teardown(task, incoming)


async def test_driven_cwds_own_update_is_not_re_narrated(monkeypatch):
    # The driven pane's own monitor narrates its own finishes on this line already
    # (via hub.on_final); on_update_speak must skip re-speaking the SAME cwd.
    ws, op, notifier, sessions, hub1, task, incoming = await _drive_two_sessions(monkeypatch)
    try:
        before = len(op.spoke)
        await notifier.report("loop finished: again", cwd="/p/loop")
        assert len(op.spoke) == before   # nothing new spoken via the foreign path
    finally:
        await _teardown(task, incoming)


async def test_on_update_speak_cleared_after_disconnect(monkeypatch):
    ws, op, notifier, sessions, hub1, task, incoming = await _drive_two_sessions(monkeypatch)
    await _teardown(task, incoming)
    assert notifier.on_update_speak is None


# --- Fix 5: Gemini is told about the fleet in the opening's session context ------

async def test_opening_context_includes_fleet_status_line(monkeypatch):
    ws, op, notifier, sessions, hub1, task, incoming = await _drive_two_sessions(monkeypatch)
    try:
        _, context = op.contexts[0]
        assert "Open sessions right now:" in context
        assert "loop (attached, idle)" in context
        assert "veil (working)" in context
    finally:
        await _teardown(task, incoming)


async def test_opening_context_has_no_fleet_line_with_one_session(monkeypatch):
    ws, op, notifier, sessions, hub1, task, incoming = await _drive_two_sessions(
        monkeypatch, one_session=True)
    try:
        _, context = op.contexts[0]
        assert "Open sessions right now:" not in context
    finally:
        await _teardown(task, incoming)


# --- Fix 5 (prewarm path): the warm-greeting recap also carries the fleet line ---

class _FakeWarmOperator:
    def __init__(self, config, handle_tool_call, voice="", lang="", account=""):
        self.contexts: list[tuple] = []
        self.spoke: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def set_audio_out(self, cb):
        pass

    def set_text_out(self, cb):
        pass

    def suppress_greeting(self):
        pass

    async def open_with_context(self, opening, context=""):
        self.contexts.append((opening, context))

    async def speak(self, text, immediate=False, dedupe_key=""):
        self.spoke.append(text)

    async def run(self):
        await asyncio.Event().wait()


class _FakePrewarmNotifier:
    def __init__(self):
        self.last_voice = ""
        self.last_lang = ""
        self.last_account = ""


async def test_prewarm_recap_includes_fleet_status_line(monkeypatch):
    ops: list[_FakeWarmOperator] = []

    def factory(config, handle_tool_call, voice="", lang="", account=""):
        op = _FakeWarmOperator(config, handle_tool_call, voice=voice, lang=lang, account=account)
        ops.append(op)
        return op

    sessions = SessionRegistry()
    cm = CallManager(_NoPushPusher(), _FakeRegistry())
    ctrl1 = _FakeDrivenCtrl("/p/loop", status="idle")
    sessions.add(Session("s1", ctrl1, SessionHub(ctrl1, cm), cm))
    ctrl2 = _FakeDrivenCtrl("/p/veil", status="working")
    sessions.add(Session("s2", ctrl2, SessionHub(ctrl2, cm), cm))
    sessions.set_active("s1")

    pw = Prewarmer(None, factory, _FakePrewarmNotifier(), sessions)
    monkeypatch.setattr("server.transcripts.recap", lambda cwd: "You: fix it\nClaude: done")
    try:
        await pw.start("loop finished: done", "/p/loop", None)
        opening, context = ops[0].contexts[0]
        assert "Open sessions right now:" in context
        assert "loop (attached, idle)" in context
        assert "veil (working)" in context
        assert "fix it" in context
    finally:
        await pw.discard()
