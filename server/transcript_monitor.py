"""Screen-independent Claude status detection from its own transcript JSONL.

For terminals whose screen we cannot capture (GPU terminals behind the ax
backend), the transcript under ~/.claude/projects/<encoded-cwd>/ is the source
of truth: growing file means working; quiet file ending in an assistant text
message means done; quiet file ending mid tool-call means Claude is likely
waiting on something (permission prompts never reach the transcript, so this
is a heuristic).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
from typing import Optional

from .transcripts import PROJECTS_DIR, latest_transcript, _text_of
from .tmux_controller import FinalCallback


def _last_entry(path: str) -> Optional[dict]:
    last = None
    try:
        with open(path) as f:
            for line in f:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("type") in ("user", "assistant"):
                    last = o
    except OSError:
        return None
    return last


def transcript_state(path: str, quiet_secs: float = 5.0,
                     now: float | None = None) -> tuple[str, str]:
    """Classify a transcript: ("working"|"done"|"needs_input"|"none", text)."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return "none", ""
    if (now or time.time()) - mtime < quiet_secs:
        return "working", ""
    o = _last_entry(path)
    if o is None:
        return "none", ""
    m = o.get("message") or {}
    if o.get("type") == "assistant":
        content = m.get("content")
        blocks = content if isinstance(content, list) else []
        has_text = isinstance(content, str) or any(
            isinstance(b, dict) and b.get("type") == "text" and b.get("text")
            for b in blocks)
        if has_text:
            return "done", _text_of(content).strip()
        return "needs_input", "Claude stopped mid tool call"
    return "needs_input", _text_of(m.get("content")).strip()[:200]


class TranscriptMonitor:
    """Same emit contract as the screen monitors: after the session has WORKED
    (mtime advanced since attach) and then gone quiet, fire on_final once."""

    def __init__(self, cwd: str, on_final: Optional[FinalCallback] = None, *,
                 poll_interval: float = 2.0, quiet_secs: float = 5.0,
                 projects_dir: str = PROJECTS_DIR):
        self._cwd = cwd
        self._final_cb = on_final
        self._poll = poll_interval
        self._quiet = quiet_secs
        self._projects = projects_dir
        self.status = "idle"
        self.working_dir: Optional[str] = cwd
        self._started = False
        self._task: Optional[asyncio.Task] = None

    def on_final(self, cb: FinalCallback) -> None:
        self._final_cb = cb

    async def _emit(self, text: str) -> None:
        if text.strip() and self._final_cb is not None:
            result = self._final_cb(text)
            if inspect.isawaitable(result):
                await result

    async def run(self) -> None:
        self._started = True
        path = latest_transcript(self._cwd, self._projects)
        baseline = 0.0
        if path:
            try:
                baseline = os.path.getmtime(path)
            except OSError:
                baseline = 0.0
        saw_work = False
        while self._started:
            await asyncio.sleep(self._poll)
            path = latest_transcript(self._cwd, self._projects)
            if not path:
                continue
            kind, text = transcript_state(path, self._quiet)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime > baseline:
                baseline = mtime
                saw_work = True
                self.status = "working"
                continue
            if saw_work and kind in ("done", "needs_input"):
                saw_work = False
                self.status = "idle"
                prefix = "needs input: " if kind == "needs_input" else ""
                await self._emit(prefix + text if text else prefix.strip())

    async def start(self, working_dir: Optional[str] = None) -> None:
        if working_dir:
            self.working_dir = working_dir
            self._cwd = working_dir
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = asyncio.ensure_future(self.run())

    async def stop(self, *, detach_only: bool = False) -> None:
        self._started = False
        if self._task and not self._task.done():
            self._task.cancel()
        self.status = "idle"
