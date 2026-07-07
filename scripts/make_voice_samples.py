"""Render a short preview clip for each curated Gemini voice.

Writes ios/Voxa/Resources/<VoiceId>.wav (24 kHz mono 16-bit) using the Gemini
key in .env. Run once; the clips are bundled in the app for the voice picker.
"""
from __future__ import annotations

import asyncio
import os
import sys
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from server.config import load_config            # noqa: E402
from server.gemini_operator import GeminiOperator  # noqa: E402

VOICES = ["Puck", "Charon", "Kore", "Aoede", "Fenrir", "Leda"]
LINE = "Hi, I'm Voxa, your coding copilot. Tell me what to build."
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "ios", "Voxa", "Resources")


async def render(cfg, voice: str) -> bytes:
    buf = bytearray()

    async def audio_out(b):
        buf.extend(b)

    async def text_out(_m):
        pass

    async def handle(_n, _a):
        return {}

    async with GeminiOperator(cfg, handle, voice=voice) as op:
        op.set_audio_out(audio_out)
        op.set_text_out(text_out)
        await op.speak(LINE)
        try:
            await asyncio.wait_for(op.run(), timeout=10)
        except asyncio.TimeoutError:
            pass
    return bytes(buf)


async def main() -> int:
    cfg = load_config()
    os.makedirs(OUT, exist_ok=True)
    for v in VOICES:
        pcm = await render(cfg, v)
        path = os.path.join(OUT, f"{v}.wav")
        with wave.open(path, "w") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(pcm)
        print(f"{v}: {len(pcm)} bytes -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
