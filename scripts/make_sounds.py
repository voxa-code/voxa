"""Synthesize Voxa's bundled UI sounds (no external deps).

Writes ios/Voxa/Resources/chime.wav (soft pair-success two-note) and tick.wav
(short selection click), 44.1 kHz mono 16-bit.
"""
from __future__ import annotations

import math
import os
import struct
import wave

SR = 44100
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "ios", "Voxa", "Resources")


def _write(path: str, samples: list[float]) -> None:
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        frames = b"".join(struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767)) for s in samples)
        w.writeframes(frames)


def _note(freq: float, dur: float, attack: float = 0.01, release: float = 0.12) -> list[float]:
    n = int(SR * dur)
    out = []
    for i in range(n):
        t = i / SR
        env = 1.0
        if t < attack:
            env = t / attack
        elif t > dur - release:
            env = max(0.0, (dur - t) / release)
        out.append(0.5 * env * math.sin(2 * math.pi * freq * t))
    return out


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    # Chime: a rising perfect-fourth (E5 -> A5), soft.
    chime = _note(659.25, 0.16) + _note(880.0, 0.34)
    _write(os.path.join(OUT, "chime.wav"), chime)
    # Tick: a tiny high blip with fast decay.
    tick = []
    for i in range(int(SR * 0.04)):
        t = i / SR
        env = math.exp(-t * 90)
        tick.append(0.35 * env * math.sin(2 * math.pi * 1200 * t))
    _write(os.path.join(OUT, "tick.wav"), tick)
    print("wrote", os.path.join(OUT, "chime.wav"), "and tick.wav")


if __name__ == "__main__":
    main()
