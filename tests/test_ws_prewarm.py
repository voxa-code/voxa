"""serve_ws's warm-claim answer path: adopting an already-open, already-greeted
operator from a Prewarmer instead of building one cold. Covers Workstream B
tasks B2 (claim/flush/no-double-greeting) and B3 (parallel cold-path answer
sequence) from docs/superpowers/plans/2026-07-12-voice-latency-overhaul.md.
"""
from __future__ import annotations

import asyncio

from server.call_manager import CallManager
from server.config import Config
from server.notifier import Notifier
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
    status = "idle"

    def __init__(self, cwd):
        self.working_dir = cwd
        self._started = True   # skip serve_ws's reattach path

    def on_final(self, cb):
        pass

    def capture_text(self):
        return ""


class _FakeWSChannel:
    """Minimal WebSocket for driving serve_ws directly: a queue feeds
    receive(), sends are recorded (bytes and JSON separately, so binary
    ordering is easy to assert on)."""

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


class _FakeWarmOperator:
    """Stands in for a GeminiOperator claimed mid-ring: already entered,
    already greeted. Records whether serve_ws tries to speak ANOTHER opening
    on top of what the (fake) prewarm already spoke."""

    def __init__(self):
        self.audio_out = None
        self.text_out = None
        self.spoke: list[str] = []
        self.contexts: list[tuple] = []
        self.exited = False

    def set_audio_out(self, cb):
        self.audio_out = cb

    def set_text_out(self, cb):
        self.text_out = cb

    async def send_audio(self, pcm):
        pass

    async def speak(self, text, immediate=False):
        self.spoke.append(text)

    async def open_with_context(self, opening, context=""):
        self.contexts.append((opening, context))

    async def run(self):
        await asyncio.Event().wait()   # blocks until cancelled, like GeminiOperator's loop

    async def __aexit__(self, *exc):
        self.exited = True
        return False


class _FakeWarmCall:
    """Fake WarmCall: pre-buffered audio/controls from the ring, as if a real
    Prewarmer.start() had already spoken the greeting."""

    def __init__(self, operator, audio, controls):
        self.operator = operator
        self.opening = "Hi. Warm greeting already spoken during the ring."
        self.audio = list(audio)
        self.controls = list(controls)
        self.run_task = asyncio.ensure_future(operator.run())
        self.bound_handler = None

    def bind_tools(self, handler):
        self.bound_handler = handler

    def stop_buffering(self, audio_out, text_out):
        audio, controls = self.audio, self.controls
        self.audio, self.controls = [], []
        self.operator.set_audio_out(audio_out)
        self.operator.set_text_out(text_out)
        return audio, controls


class _FakePrewarmer:
    def __init__(self, warm):
        self._warm = warm
        self.claim_calls: list[tuple] = []

    def claim(self, voice, lang, account):
        self.claim_calls.append((voice, lang, account))
        return self._warm


def _cfg():
    return Config(gemini_api_key="k", gemini_live_model="m",
                  auth_token="t", host="127.0.0.1", port=8787)


async def _drive_to_warm_greeting(monkeypatch, *, cold_factory=None):
    """Set up a registry with one attached session, a fake prewarmer holding a
    ready-to-claim warm slot, drive serve_ws through `begin`, and return
    (ws, op, warm, notifier, task) once the greeting audio has been flushed."""
    monkeypatch.setattr("server.terminals.discover_claude_sessions", lambda: [])

    op = _FakeWarmOperator()
    warm = _FakeWarmCall(op, [b"greet-1", b"greet-2"],
                         [{"type": "transcript", "role": "agent", "text": "hi there"}])
    prewarmer = _FakePrewarmer(warm)

    cm = CallManager(_NoPushPusher(), _FakeRegistry())
    notifier = Notifier(cm, push_enabled=True)
    sessions = SessionRegistry()
    ctrl = _FakeDrivenCtrl("/p/warm")
    hub = SessionHub(ctrl, cm)
    sessions.add(Session("s1", ctrl, hub, cm))
    sessions.set_active("s1")

    incoming: asyncio.Queue = asyncio.Queue()
    ws = _FakeWSChannel(incoming)

    async def _cold_factory_default(config, handle_tool_call, voice="", lang="", account=""):
        raise AssertionError("cold operator_factory must not be called on a warm-claim hit")

    task = asyncio.ensure_future(serve_ws(
        ws, config=_cfg(), mode="attach", sessions=sessions, notifier=notifier,
        operator_factory=cold_factory or _cold_factory_default, prewarmer=prewarmer))
    await incoming.put({"type": "websocket.receive", "text": '{"type":"begin"}'})
    for _ in range(400):
        if ws.sent_bytes:
            break
        await asyncio.sleep(0.005)
    return ws, op, warm, notifier, prewarmer, task, incoming


async def _teardown(task, incoming):
    await incoming.put({"type": "websocket.disconnect"})
    try:
        await asyncio.wait_for(task, 1.0)
    except Exception:
        task.cancel()


async def test_warm_claim_flushes_buffered_greeting_audio_before_loops(monkeypatch):
    ws, op, warm, notifier, prewarmer, task, incoming = await _drive_to_warm_greeting(monkeypatch)
    try:
        assert ws.sent_bytes[:2] == [b"greet-1", b"greet-2"]
        assert any(m.get("type") == "transcript" for m in ws.sent_json)
    finally:
        await _teardown(task, incoming)


async def test_warm_claim_skips_the_duplicate_opening(monkeypatch):
    ws, op, warm, notifier, prewarmer, task, incoming = await _drive_to_warm_greeting(monkeypatch)
    try:
        # serve_ws's own opening block (compose_opening + speak/open_with_context)
        # must NOT run on the warm path: the greeting was already spoken during
        # the ring and is buffered in `warm`.
        assert op.spoke == []
        assert op.contexts == []
    finally:
        await _teardown(task, incoming)


async def test_warm_claim_binds_real_tool_handler(monkeypatch):
    ws, op, warm, notifier, prewarmer, task, incoming = await _drive_to_warm_greeting(monkeypatch)
    try:
        assert warm.bound_handler is not None
        result = await warm.bound_handler("get_claude_status", {})
        assert isinstance(result, dict)
    finally:
        await _teardown(task, incoming)


async def test_warm_claim_cancels_prewarm_run_task_and_starts_a_fresh_one(monkeypatch):
    ws, op, warm, notifier, prewarmer, task, incoming = await _drive_to_warm_greeting(monkeypatch)
    try:
        # The prewarm run_task fed the buffers above; serve_ws must cancel it and
        # start its OWN run() task before the recv/idle loops, so there is never
        # more than one concurrent receive() loop on the adopted operator.
        assert warm.run_task.cancelled() or warm.run_task.done()
    finally:
        await _teardown(task, incoming)


async def test_no_warm_slot_falls_back_to_cold_factory(monkeypatch):
    monkeypatch.setattr("server.terminals.discover_claude_sessions", lambda: [])

    class _EmptyPrewarmer:
        def claim(self, voice, lang, account):
            return None

    called = {"n": 0}

    class _ColdOperator:
        def set_audio_out(self, cb):
            pass

        def set_text_out(self, cb):
            pass

        async def send_audio(self, pcm):
            pass

        async def speak(self, text, immediate=False):
            self.spoke = text

        def suppress_greeting(self):
            pass

        async def run(self):
            await asyncio.Event().wait()

    def cold_factory(config, handle_tool_call, voice="", lang="", account=""):
        called["n"] += 1
        return _ColdOperator()

    cm = CallManager(_NoPushPusher(), _FakeRegistry())
    notifier = Notifier(cm, push_enabled=True)
    sessions = SessionRegistry()
    ctrl = _FakeDrivenCtrl("/p/cold")
    hub = SessionHub(ctrl, cm)
    sessions.add(Session("s1", ctrl, hub, cm))
    sessions.set_active("s1")

    incoming: asyncio.Queue = asyncio.Queue()
    ws = _FakeWSChannel(incoming)
    task = asyncio.ensure_future(serve_ws(
        ws, config=_cfg(), mode="attach", sessions=sessions, notifier=notifier,
        operator_factory=cold_factory, prewarmer=_EmptyPrewarmer()))
    await incoming.put({"type": "websocket.receive", "text": '{"type":"begin"}'})
    for _ in range(400):
        if called["n"]:
            break
        await asyncio.sleep(0.005)
    try:
        assert called["n"] == 1   # cold path used when claim() misses
    finally:
        await _teardown(task, incoming)


async def test_serve_ws_works_with_no_prewarmer_arg_at_all(monkeypatch):
    """Default prewarmer=None (e.g. an older caller/test) must behave exactly
    like today's cold path, never raise."""
    monkeypatch.setattr("server.terminals.discover_claude_sessions", lambda: [])

    class _ColdOperator:
        def set_audio_out(self, cb):
            pass

        def set_text_out(self, cb):
            pass

        async def send_audio(self, pcm):
            pass

        async def speak(self, text, immediate=False):
            self.spoke = text

        def suppress_greeting(self):
            pass

        async def run(self):
            await asyncio.Event().wait()

    def cold_factory(config, handle_tool_call, voice=""):
        return _ColdOperator()

    cm = CallManager(_NoPushPusher(), _FakeRegistry())
    notifier = Notifier(cm, push_enabled=True)
    sessions = SessionRegistry()
    ctrl = _FakeDrivenCtrl("/p/nopw")
    hub = SessionHub(ctrl, cm)
    sessions.add(Session("s1", ctrl, hub, cm))
    sessions.set_active("s1")

    incoming: asyncio.Queue = asyncio.Queue()
    ws = _FakeWSChannel(incoming)
    task = asyncio.ensure_future(serve_ws(
        ws, config=_cfg(), mode="attach", sessions=sessions, notifier=notifier,
        operator_factory=cold_factory))
    await incoming.put({"type": "websocket.receive", "text": '{"type":"begin"}'})
    await incoming.put({"type": "websocket.disconnect"})
    await asyncio.wait_for(task, 1.0)


# --- user-pinned begin: a deliberate terminal choice outranks the last ring ---

class _FakeColdOperator:
    """Plain cold-path operator (no context manager): records the opening."""

    def __init__(self):
        self.spoke: list[str] = []
        self.contexts: list[tuple] = []

    def set_audio_out(self, cb):
        pass

    def set_text_out(self, cb):
        pass

    async def send_audio(self, pcm):
        pass

    async def speak(self, text, immediate=False, dedupe_key=""):
        self.spoke.append(text)

    async def open_with_context(self, opening, context=""):
        self.contexts.append((opening, context))

    async def run(self):
        await asyncio.Event().wait()


class _DiscardingPrewarmer(_FakePrewarmer):
    def __init__(self, warm):
        super().__init__(warm)
        self.discards = 0

    async def discard(self):
        self.discards += 1


async def _drive_pinned_begin(monkeypatch, *, queued=None, pending_cwd=None):
    """Attach a terminal DURING the free gate (the pin), then begin. Returns
    everything needed to assert that the ring-answer machinery stood down."""
    monkeypatch.setattr("server.terminals.discover_claude_sessions", lambda: [])

    warm_op = _FakeWarmOperator()
    warm = _FakeWarmCall(warm_op, [b"stale-ring-greeting"], [])
    prewarmer = _DiscardingPrewarmer(warm)

    cm = CallManager(_NoPushPusher(), _FakeRegistry())
    notifier = Notifier(cm, push_enabled=True)
    for q in queued or []:
        cm.queue(q)
    sessions = SessionRegistry()
    if pending_cwd:
        sessions.push_pending(pending_cwd)
    ctrl = _FakeDrivenCtrl("/p/ti0")
    hub = SessionHub(ctrl, cm)
    sessions.add(Session("s1", ctrl, hub, cm))
    sessions.set_active("s1")

    cold = _FakeColdOperator()

    def cold_factory(config, handle_tool_call, voice="", lang="", account=""):
        return cold

    incoming: asyncio.Queue = asyncio.Queue()
    ws = _FakeWSChannel(incoming)
    task = asyncio.ensure_future(serve_ws(
        ws, config=_cfg(), mode="attach", sessions=sessions, notifier=notifier,
        operator_factory=cold_factory, prewarmer=prewarmer))
    # The pin: the user explicitly attached a terminal during the free gate.
    await incoming.put({"type": "websocket.receive",
                        "text": '{"type":"attach_terminal","id":"nope"}'})
    await incoming.put({"type": "websocket.receive", "text": '{"type":"begin"}'})
    for _ in range(400):
        if cold.contexts or cold.spoke:
            break
        await asyncio.sleep(0.005)
    return ws, cold, prewarmer, sessions, task, incoming


async def test_pinned_begin_skips_warm_claim_and_discards_the_slot(monkeypatch):
    ws, cold, prewarmer, sessions, task, incoming = await _drive_pinned_begin(monkeypatch)
    try:
        assert prewarmer.claim_calls == []      # ring greeting never adopted
        assert prewarmer.discards == 1          # and torn down, not left burning
        assert b"stale-ring-greeting" not in ws.sent_bytes
        assert cold.contexts or cold.spoke      # cold path spoke the opening
    finally:
        task.cancel()
        with __import__("contextlib").suppress(BaseException):
            await task


async def test_pinned_begin_leaves_the_pending_ring_source_alone(monkeypatch):
    ws, cold, prewarmer, sessions, task, incoming = await _drive_pinned_begin(
        monkeypatch, pending_cwd="/p/loop")
    try:
        # The rung session was NOT auto-attached over the user's choice; its
        # pending marker survives for a future real answer.
        assert sessions.pending_source == {"cwd": "/p/loop"}
        opening = (cold.contexts[0][0] if cold.contexts else
                   (cold.spoke[0] if cold.spoke else ""))
        assert "ti0" in opening.lower()         # opened where the user pinned
    finally:
        task.cancel()
        with __import__("contextlib").suppress(BaseException):
            await task


async def test_pinned_begin_reports_foreign_finish_as_meanwhile(monkeypatch):
    ws, cold, prewarmer, sessions, task, incoming = await _drive_pinned_begin(
        monkeypatch, queued=["loop finished: built the thing"])
    try:
        opening = (cold.contexts[0][0] if cold.contexts else
                   (cold.spoke[0] if cold.spoke else ""))
        # The other project's finish is reported by name, never claimed as the
        # pinned project's own ("Your last task in ti0 finished: ...").
        assert "Meanwhile: loop finished: built the thing" in opening
        assert "last task in ti0" not in opening.lower()
    finally:
        task.cancel()
        with __import__("contextlib").suppress(BaseException):
            await task
