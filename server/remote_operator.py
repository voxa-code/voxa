"""RemoteOperator: drop-in replacement for GeminiOperator that routes V2V through
the cloud `/live` proxy instead of talking to Gemini directly.

Why: the metered/paid model runs Gemini with YOUR key on the cloud (so the key
isn't on customer laptops) and counts minutes there. The laptop streams the
phone's mic to `/live`, plays back the audio it returns, and EXECUTES the tool
calls the cloud's Gemini decides (start_claude_session, send_to_claude, ...) via
the same `handle_tool_call` the local operator would use.

Enabled on the laptop by setting VOXA_LIVE_PROXY (+ VOXA_PROXY_TOKEN, VOXA_ACCOUNT).
Same interface as GeminiOperator: async context manager + set_audio_out /
set_text_out / send_audio / speak / run.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Awaitable, Callable, Optional

import websockets

logger = logging.getLogger(__name__)


class RemoteOperator:
    def __init__(self, config, handle_tool_call: Callable[[str, dict], Awaitable[dict]],
                 *, proxy_url: str, account: str, token: str = "", voice: str = "",
                 lang: str = ""):
        self._handle = handle_tool_call
        url = f"{proxy_url}?account={account}"
        if token:
            url += f"&token={token}"
        if voice:
            url += f"&voice={voice}"
        if lang:
            url += f"&lang={lang}"
        self._url = url
        self._audio_out: Optional[Callable[[bytes], Awaitable[None]]] = None
        self._text_out: Optional[Callable[[dict], Awaitable[None]]] = None
        self._ws = None
        self._suppress_pending = False
        self._stack = contextlib.AsyncExitStack()

    async def __aenter__(self) -> "RemoteOperator":
        # The cloud /live can briefly refuse mid-deploy/restart; retry a few times with
        # a longer handshake timeout so a momentary blip doesn't drop the session.
        last_err: Exception | None = None
        for attempt in range(4):
            try:
                self._ws = await self._stack.enter_async_context(
                    websockets.connect(self._url, max_size=None,
                                       ping_interval=20, open_timeout=20))
                return self
            except Exception as e:
                last_err = e
                logger.warning("/live connect attempt %d failed: %s", attempt + 1, e)
                if attempt < 3:
                    await asyncio.sleep(1.5)
        raise last_err

    async def __aexit__(self, *exc) -> bool:
        await self._stack.aclose()
        self._ws = None
        return False

    def set_audio_out(self, cb): self._audio_out = cb
    def set_text_out(self, cb): self._text_out = cb

    async def send_audio(self, pcm: bytes) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.send(pcm)
        except Exception:
            return  # cloud link closed (e.g. out of minutes); run()'s loop handles the end

    async def speak(self, text: str, immediate: bool = False,
                    dedupe_key: str = "") -> None:
        # dedupe_key is accepted for signature parity with GeminiOperator (the
        # multi-session paths pass it for per-session dedupe). The cloud brain
        # owns dedupe on this metered path, so it is deliberately unused here;
        # without the parameter every labeled relay raised TypeError and the
        # foreign-session update was never spoken at all.
        if self._ws is None:
            return
        try:
            # Flush a pending greeting suppression FIRST, in-order on this single writer
            # (a separate task could interleave a concurrent send on the same socket).
            if self._suppress_pending:
                self._suppress_pending = False
                await self._ws.send(json.dumps({"type": "suppress_greeting"}))
            await self._ws.send(json.dumps(
                {"type": "speak", "text": text, "immediate": immediate}))
        except Exception:
            return  # cloud link closed; don't crash the answer flow

    def suppress_greeting(self) -> None:
        # Ask the cloud brain not to speak its generic opening (the laptop supplies a
        # contextual one on answer-attach). Sent in-order before the next speak().
        self._suppress_pending = True

    async def open_with_context(self, opening: str, context: str = "") -> None:
        """The on-answer opening plus the attached session's recent transcript as
        SILENT background, forwarded to the cloud brain in one frame. Without
        this the metered path dropped the recap entirely (ws_session falls back
        to a bare speak when the operator lacks open_with_context), so a cloud
        call opened knowing only the ring's one-line summary and "what did it
        actually do?" drew a blank. Tail-capped like GeminiOperator: the newest
        turns (the last full answer) must survive the cut."""
        if self._ws is None:
            return
        try:
            if self._suppress_pending:
                self._suppress_pending = False
                await self._ws.send(json.dumps({"type": "suppress_greeting"}))
            await self._ws.send(json.dumps({
                "type": "open_with_context",
                "opening": opening,
                "context": (context or "").strip()[-6000:],
            }))
        except Exception:
            return  # cloud link closed; don't crash the answer flow

    async def send_text(self, text: str) -> None:
        # A typed user turn from the phone. Forward it to the cloud brain the same way
        # audio is; without this, a `say` during a metered call raised AttributeError
        # and tore the whole call down.
        if self._ws is None:
            return
        try:
            await self._ws.send(json.dumps({"type": "user_text", "text": text}))
        except Exception:
            return  # cloud link closed; run()'s loop handles the end

    async def run(self) -> None:
        if self._ws is None:
            raise RuntimeError("RemoteOperator is not open; use 'async with'.")
        async for msg in self._ws:
            if isinstance(msg, (bytes, bytearray)):
                if self._audio_out is not None:
                    await self._audio_out(bytes(msg))
                continue
            try:
                data = json.loads(msg)
            except ValueError:
                continue
            if data.get("type") == "tool":            # cloud Gemini -> run a tool here
                try:
                    result = await self._handle(data.get("name", ""), data.get("args") or {})
                except Exception as e:
                    result = {"error": str(e)}
                with contextlib.suppress(Exception):
                    await self._ws.send(json.dumps(
                        {"type": "tool_result", "id": data.get("id"), "result": result}))
            elif self._text_out is not None:           # transcripts / status -> phone
                await self._text_out(data)
