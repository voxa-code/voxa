"""Warm the Gemini Live session and pre-speak the greeting WHILE the phone is
still ringing, so answering the call doesn't pay Live-connect + greeting
latency on top of the ring itself.

Notifier.report() kicks a Prewarmer.start() in the background the instant a
ring fires (fire-and-forget: the ring must never wait on this). If the user
answers before it expires, serve_ws's claim() adopts the already-open,
already-greeted operator and flushes whatever audio/controls were buffered
during the ring straight to the websocket, so the greeting plays instantly
instead of the phone hearing dead air while Live connects.

This is PURELY an optimization. If prewarm is disabled, races the answer, or
hits any error, serve_ws's cold path (build a fresh operator, speak the
opening as usual) runs exactly as it does today. To keep that contract solid:
``claim()`` must never raise, and every other public method swallows its own
errors and simply leaves nothing warm to claim.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import time

from server.greetings import compose_opening, suppress_greeting_if_supported

logger = logging.getLogger(__name__)

def _ttl_seconds() -> float:
    """How long a warmed session waits to be claimed before it's torn down. A
    ring that goes unanswered this long was declined/missed; keeping the Live
    session open past that just burns a connection. In proxy mode the warm
    RemoteOperator is a METERED cloud session, so the default is much shorter
    there (roughly a ring's length) to cap the minutes an unanswered call can
    burn. VOXA_PREWARM_TTL overrides either default."""
    raw = os.environ.get("VOXA_PREWARM_TTL", "").strip()
    if raw:
        try:
            return max(1.0, float(raw))
        except (TypeError, ValueError):
            pass
    return 40.0 if os.environ.get("VOXA_LIVE_PROXY", "").strip() else 90.0
# Greeting audio is only ever a few seconds of 24kHz PCM; cap the buffer so a
# slot that somehow never gets claimed can't grow without bound. Drop the
# oldest chunks first, keep the most recent (start of the greeting matters
# less than not falling silent partway through if this ever runs long).
_AUDIO_CAP_BYTES = 5 * 1024 * 1024
_CONTROLS_CAP = 200


def _fleet_status_line(sessions) -> str:
    """'Open sessions right now: loop (attached, idle); veil (working).' built from
    every fleet member's project (its controller's working_dir basename) and status,
    with an '(attached)' marker for whichever one is currently active. Returns ''
    when zero or one session is open, so the common single-session warm opening is
    unaffected (fix 5: tell Gemini about the fleet). Mirrors ws_session._fleet_status_line.
    Fail-open on a None/legacy registry (some tests build a Prewarmer with
    sessions=None): treated the same as "nothing to report"."""
    if sessions is None:
        return ""
    members = sessions.all()
    if len(members) <= 1:
        return ""
    active = sessions.active()
    parts = []
    for member in members:
        cwd = getattr(member.controller, "working_dir", "") or ""
        label = os.path.basename(cwd.rstrip("/")) or cwd or "session"
        status = getattr(member.controller, "status", "?")
        tag = "attached, " if member is active else ""
        parts.append(f"{label} ({tag}{status})")
    return "Open sessions right now: " + "; ".join(parts) + "."


def prewarm_enabled() -> bool:
    """On unless explicitly disabled (VOXA_PREWARM=0). Proxy mode warms the
    RemoteOperator's cloud connection the same way: the cloud session is
    metered while warm, which is why _ttl_seconds() is much shorter there."""
    return os.environ.get("VOXA_PREWARM", "1").strip() not in ("0", "false", "")


class WarmCall:
    """One warmed-and-greeted operator, plus everything it has spoken/pushed
    so far, waiting to be claimed by the connection that answers the call."""

    def __init__(self, operator, opening: str, run_task, key: tuple[str, str, str]):
        self.operator = operator
        self.opening = opening
        self.audio: list[bytes] = []
        self.controls: list[dict] = []
        self.run_task = run_task
        self.key = key
        self.created_at = time.monotonic()
        # Bound by serve_ws once a real orchestrator exists for the answering
        # connection; None until then (see the module docstring).
        self.tool_handler = None

    def bind_tools(self, handler) -> None:
        """Swap in the REAL orchestrator.handle_tool_call for this connection.
        The operator was built during the ring with a late-bound stub (no
        orchestrator existed yet, since no phone had answered)."""
        self.tool_handler = handler

    def stop_buffering(self, audio_out, text_out):
        """Atomically hand the operator's callbacks over to the real websocket
        sinks and return whatever was buffered up to this instant, so the
        caller can flush it before its own loops start. There is no `await`
        between grabbing the buffered lists and re-pointing the callbacks, so
        nothing can land in the old buffers unseen in between."""
        audio, controls = self.audio, self.controls
        self.audio, self.controls = [], []
        self.operator.set_audio_out(audio_out)
        self.operator.set_text_out(text_out)
        return audio, controls


class Prewarmer:
    """Owns at most one warmed slot at a time. A fresh ring always wins: a
    slot still unclaimed when another finish rings is discarded (that ring's
    ``start()`` supersedes it), the same way a real answer would only ever
    pick up the newest ring."""

    def __init__(self, config, operator_factory, notifier, sessions):
        self._config = config
        self._operator_factory = operator_factory
        self._notifier = notifier
        self._sessions = sessions
        self._slot: WarmCall | None = None

    def enabled(self) -> bool:
        return prewarm_enabled()

    async def start(self, summary: str, cwd: str, approval: dict | None) -> None:
        """Open a Live session and speak the greeting now, while the phone is
        still ringing. Fail-open by contract: any error here just means
        nothing is warm to claim, and serve_ws's cold path takes over as it
        does today."""
        if not self.enabled():
            return
        if self._slot is not None:
            await self.discard()
        try:
            await self._start(summary, cwd, approval)
        except Exception:
            logger.exception("prewarm start failed; cold path will still work")
            await self.discard()

    async def _start(self, summary: str, cwd: str, approval: dict | None) -> None:
        voice = getattr(self._notifier, "last_voice", "") or ""
        lang = getattr(self._notifier, "last_lang", "") or ""
        account = getattr(self._notifier, "last_account", "") or ""
        if not account and os.environ.get("VOXA_LIVE_PROXY", "").strip():
            # Metered mode with no paired account known: the warm session would
            # open under a fallback identity the answering phone can never match
            # (claim() would discard it), burning metered minutes for nothing.
            # Skip; the cold path covers the first-ever answer.
            logger.info("prewarm skipped: no paired account known yet (metered mode)")
            return
        key = (voice, lang, account)

        # `slot` is assigned below, AFTER the operator (and therefore this
        # closure) is built. Python closures capture the variable, not its
        # value at definition time, so by the time serve_ws actually calls
        # this (well after `slot` is assigned) it resolves fine.
        slot: WarmCall | None = None

        async def _late_handler(name: str, args: dict) -> dict:
            handler = slot.tool_handler if slot is not None else None
            if handler is None:
                return {"error": "The call is still connecting; try again in a moment."}
            return await handler(name, args)

        kwargs = {"voice": voice, "lang": lang}
        params = inspect.signature(self._operator_factory).parameters
        if "account" in params:
            kwargs["account"] = account

        operator = self._operator_factory(self._config, _late_handler, **kwargs)
        if hasattr(operator, "__aenter__"):
            operator = await operator.__aenter__()

        suppress_greeting_if_supported(operator)

        opening = compose_opening(
            os.path.basename(cwd.rstrip("/")) if cwd else "",
            [summary] if summary else [],
            approval=approval,
        )

        recap = ""
        if cwd:
            try:
                from server.transcripts import recap as build_recap
                recap = await asyncio.to_thread(build_recap, cwd)
            except Exception:
                logger.exception("prewarm recap build failed")
        # Fleet awareness (fix 5): tell Gemini what ELSE is open right now, so it
        # never attributes a foreign session's update or question to the one it's
        # driving. '' (no-op) when zero or one session is open, so the single-session
        # warm opening stays byte-identical to today.
        fleet_line = _fleet_status_line(self._sessions)
        if fleet_line:
            recap = f"{fleet_line}\n\n{recap}" if recap else fleet_line

        slot = WarmCall(operator, opening, None, key)

        async def audio_out(pcm: bytes) -> None:
            slot.audio.append(pcm)
            total = sum(len(c) for c in slot.audio)
            while total > _AUDIO_CAP_BYTES and slot.audio:
                total -= len(slot.audio.pop(0))

        async def text_out(msg: dict) -> None:
            slot.controls.append(msg)
            del slot.controls[:-_CONTROLS_CAP]

        operator.set_audio_out(audio_out)
        operator.set_text_out(text_out)

        open_fn = getattr(operator, "open_with_context", None)
        if open_fn is not None:
            await open_fn(opening, recap)
        else:
            await operator.speak(opening, immediate=True)

        # Start receiving now, so the greeting audio triggered above is
        # actually produced and buffered instead of sitting unread.
        slot.run_task = asyncio.ensure_future(operator.run())
        self._slot = slot
        asyncio.ensure_future(self._expire(slot))

    async def _expire(self, slot: WarmCall) -> None:
        await asyncio.sleep(_ttl_seconds())
        if self._slot is slot:
            # Still unclaimed after the TTL: the call was declined/missed, or
            # nobody ever answered. Tear it down rather than leak a Live
            # connection (and, in metered mode, keep burning minutes).
            self._slot = None
            await self._discard_slot(slot)

    def claim(self, voice: str, lang: str, account: str) -> WarmCall | None:
        """Detach and return the warm slot if it matches, else None. Must
        NEVER raise: any failure here degrades to serve_ws building a fresh
        operator, exactly like today."""
        try:
            slot = self._slot
            if slot is None:
                return None
            self._slot = None   # detach either way; a rejected slot is torn down below
            stale = time.monotonic() - slot.created_at > _ttl_seconds()
            mismatched = slot.key != (voice, lang, account)
            dead = slot.run_task is not None and slot.run_task.done()
            if stale or mismatched or dead:
                asyncio.ensure_future(self._discard_slot(slot))
                return None
            return slot
        except Exception:
            logger.exception("prewarm claim failed; cold path takes over")
            return None

    async def discard(self) -> None:
        slot, self._slot = self._slot, None
        if slot is not None:
            await self._discard_slot(slot)

    async def _discard_slot(self, slot: WarmCall) -> None:
        if slot.run_task is not None:
            slot.run_task.cancel()
            with contextlib.suppress(BaseException):
                await slot.run_task
        aexit = getattr(slot.operator, "__aexit__", None)
        if aexit is not None:
            with contextlib.suppress(Exception):
                await aexit(None, None, None)
