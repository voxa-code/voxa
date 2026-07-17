"""Tests for server/prewarm.py: opening a Gemini Live session and speaking the
greeting WHILE the phone is still ringing, so answering pays no connect
latency. Every behavior here is fail-open by contract: a broken prewarm must
never break the call, only degrade to the cold path (operator_factory called
fresh by serve_ws, same as today)."""
from __future__ import annotations

import asyncio

from server.prewarm import Prewarmer


class _FakeWarmOperator:
    """Stands in for GeminiOperator: an async context manager on itself (like
    the real operator), recording everything the prewarm path does to it."""

    def __init__(self, config, handle_tool_call, voice="", lang="", account=""):
        self.config = config
        self.handle = handle_tool_call
        self.voice = voice
        self.lang = lang
        self.account = account
        self.audio_out = None
        self.text_out = None
        self.spoke: list[str] = []
        self.contexts: list[tuple[str, str]] = []
        self.suppressed = False
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc):
        self.exited = True
        return False

    def set_audio_out(self, cb):
        self.audio_out = cb

    def set_text_out(self, cb):
        self.text_out = cb

    def suppress_greeting(self):
        self.suppressed = True

    async def open_with_context(self, opening, context=""):
        self.contexts.append((opening, context))

    async def speak(self, text, immediate=False):
        self.spoke.append(text)

    async def run(self):
        await asyncio.Event().wait()   # blocks until the run_task is cancelled


class _FakeNotifier:
    def __init__(self, voice="", lang="", account=""):
        self.last_voice = voice
        self.last_lang = lang
        self.last_account = account


def _factory(ops: list):
    def factory(config, handle_tool_call, voice="", lang="", account=""):
        op = _FakeWarmOperator(config, handle_tool_call, voice=voice, lang=lang, account=account)
        ops.append(op)
        return op
    return factory


async def _cleanup(pw: Prewarmer) -> None:
    await pw.discard()


async def _settle(rounds: int = 10) -> None:
    """Let fire-and-forget background tasks (asyncio.ensure_future) run to
    completion; a single sleep(0) only advances one loop turn, which isn't
    enough for a cancel() + await + aexit() chain."""
    for _ in range(rounds):
        await asyncio.sleep(0)


async def test_claim_hit_returns_slot_with_buffered_audio_and_binds_tools():
    notifier = _FakeNotifier(voice="Kore", lang="en", account="acct1")
    ops: list[_FakeWarmOperator] = []
    pw = Prewarmer(None, _factory(ops), notifier, sessions=None)

    await pw.start("loop finished: done", "/p/x", None)
    op = ops[0]
    assert op.entered
    assert op.suppressed
    # Simulate the greeting audio Gemini streamed back during the ring.
    await op.audio_out(b"chunk-1")
    await op.audio_out(b"chunk-2")

    warm = pw.claim("Kore", "en", "acct1")
    try:
        assert warm is not None
        assert warm.operator is op
        assert warm.audio == [b"chunk-1", b"chunk-2"]
        assert pw.claim("Kore", "en", "acct1") is None   # detached: claimed once only
    finally:
        warm.run_task.cancel()


async def test_claim_voice_mismatch_discards_and_returns_none():
    notifier = _FakeNotifier(voice="Kore", lang="", account="")
    ops: list[_FakeWarmOperator] = []
    pw = Prewarmer(None, _factory(ops), notifier, sessions=None)

    await pw.start("s", "/p/x", None)
    warm = pw.claim("Aoede", "", "")   # different voice than what was warmed
    assert warm is None
    await _settle()   # let the fire-and-forget discard task run to completion
    assert ops[0].exited


async def test_second_start_discards_the_first_slot():
    notifier = _FakeNotifier(voice="Kore", lang="", account="")
    ops: list[_FakeWarmOperator] = []
    pw = Prewarmer(None, _factory(ops), notifier, sessions=None)

    await pw.start("s1", "/p/x", None)
    await pw.start("s2", "/p/y", None)   # a second finish rang before the first was answered
    assert ops[0].exited
    assert not ops[1].exited
    await _cleanup(pw)


async def test_enabled_in_proxy_mode_with_short_ttl(monkeypatch):
    # Proxy mode prewarms the RemoteOperator's metered cloud connection too;
    # the cost cap is the much shorter default TTL, not disabling the feature.
    monkeypatch.setenv("VOXA_LIVE_PROXY", "https://relay.example")
    monkeypatch.delenv("VOXA_PREWARM", raising=False)
    monkeypatch.delenv("VOXA_PREWARM_TTL", raising=False)
    from server.prewarm import _ttl_seconds
    pw = Prewarmer(None, _factory([]), _FakeNotifier(), sessions=None)
    assert pw.enabled() is True
    assert _ttl_seconds() == 40.0


async def test_ttl_defaults_and_env_override(monkeypatch):
    from server.prewarm import _ttl_seconds
    monkeypatch.delenv("VOXA_LIVE_PROXY", raising=False)
    monkeypatch.delenv("VOXA_PREWARM_TTL", raising=False)
    assert _ttl_seconds() == 90.0
    monkeypatch.setenv("VOXA_PREWARM_TTL", "12.5")
    assert _ttl_seconds() == 12.5


async def test_enabled_false_when_prewarm_disabled_by_env(monkeypatch):
    monkeypatch.delenv("VOXA_LIVE_PROXY", raising=False)
    monkeypatch.setenv("VOXA_PREWARM", "0")
    pw = Prewarmer(None, _factory([]), _FakeNotifier(), sessions=None)
    assert pw.enabled() is False


async def test_disabled_prewarmer_start_is_a_noop(monkeypatch):
    monkeypatch.setenv("VOXA_PREWARM", "0")
    ops: list[_FakeWarmOperator] = []
    pw = Prewarmer(None, _factory(ops), _FakeNotifier(), sessions=None)
    await pw.start("s", "/p/x", None)
    assert ops == []   # never built an operator


async def test_late_handler_errors_before_bind_then_routes_to_real_handler_after():
    notifier = _FakeNotifier()
    ops: list[_FakeWarmOperator] = []
    pw = Prewarmer(None, _factory(ops), notifier, sessions=None)

    await pw.start("s", "/p/x", None)
    late_handler = ops[0].handle
    connecting = await late_handler("send_to_claude", {"text": "hi"})
    assert "error" in connecting

    warm = pw.claim("", "", "")
    assert warm is not None
    try:
        calls = []

        async def real_handler(name, args):
            calls.append((name, args))
            return {"ok": True}

        warm.bind_tools(real_handler)
        result = await late_handler("send_to_claude", {"text": "hi"})
        assert result == {"ok": True}
        assert calls == [("send_to_claude", {"text": "hi"})]
    finally:
        warm.run_task.cancel()


async def test_start_swallows_factory_errors_fail_open():
    def boom_factory(config, handle_tool_call, voice="", lang="", account=""):
        raise RuntimeError("gemini connect failed")

    pw = Prewarmer(None, boom_factory, _FakeNotifier(), sessions=None)
    await pw.start("s", "/p/x", None)   # must not raise
    assert pw.claim("", "", "") is None


async def test_claim_with_no_slot_returns_none():
    pw = Prewarmer(None, _factory([]), _FakeNotifier(), sessions=None)
    assert pw.claim("", "", "") is None


async def test_stale_slot_is_discarded_on_claim(monkeypatch):
    notifier = _FakeNotifier()
    ops: list[_FakeWarmOperator] = []
    pw = Prewarmer(None, _factory(ops), notifier, sessions=None)
    await pw.start("s", "/p/x", None)

    import server.prewarm as prewarm_mod
    real_monotonic = prewarm_mod.time.monotonic
    monkeypatch.setattr(prewarm_mod.time, "monotonic", lambda: real_monotonic() + 1000)
    assert pw.claim("", "", "") is None
    monkeypatch.undo()
    await _settle()
    assert ops[0].exited


async def test_start_skipped_in_proxy_mode_without_account(monkeypatch):
    # Metered mode with no paired account known (fresh `voxa` run, phone never
    # connected this process): a warm session would open under a fallback
    # identity the answering phone can never match, burning metered minutes
    # for nothing. It must not even build an operator.
    monkeypatch.setenv("VOXA_LIVE_PROXY", "wss://cloud.example/live")
    notifier = _FakeNotifier(voice="Kore", lang="", account="")
    ops: list[_FakeWarmOperator] = []
    pw = Prewarmer(None, _factory(ops), notifier, sessions=None)
    await pw.start("loop finished: done", "/p/x", None)
    assert ops == []
    assert pw.claim("Kore", "", "") is None


async def test_start_proceeds_in_proxy_mode_with_account(monkeypatch):
    monkeypatch.setenv("VOXA_LIVE_PROXY", "wss://cloud.example/live")
    notifier = _FakeNotifier(voice="Kore", lang="", account="acct1")
    ops: list[_FakeWarmOperator] = []
    pw = Prewarmer(None, _factory(ops), notifier, sessions=None)
    await pw.start("loop finished: done", "/p/x", None)
    try:
        assert len(ops) == 1
        assert ops[0].account == "acct1"
    finally:
        await _cleanup(pw)


async def test_start_without_account_still_warms_in_direct_mode(monkeypatch):
    # Direct (own-key) mode has no metering and no per-account identity to
    # mismatch on the operator itself; the first-ever answer should still get
    # a warm greeting.
    monkeypatch.delenv("VOXA_LIVE_PROXY", raising=False)
    notifier = _FakeNotifier(voice="Kore", lang="", account="")
    ops: list[_FakeWarmOperator] = []
    pw = Prewarmer(None, _factory(ops), notifier, sessions=None)
    await pw.start("loop finished: done", "/p/x", None)
    try:
        assert len(ops) == 1
    finally:
        await _cleanup(pw)
