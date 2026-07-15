from __future__ import annotations

import contextlib
import inspect
import logging
import os
import subprocess
import sys
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


def text_from_assistant(message: object) -> Optional[str]:
    content = getattr(message, "content", None)
    if content is None:
        return None
    parts = [getattr(b, "text", None) for b in content]
    parts = [p for p in parts if p]
    return "".join(parts) if parts else None


def render_watch(message: object) -> str:
    """Render a streamed Claude message as human-readable lines for the watch log.

    Emits assistant text verbatim and a marker line for each tool use, so the
    Terminal `tail -f` shows live progress as Claude works.
    """
    content = getattr(message, "content", None)
    if content is None:
        return ""
    out: list[str] = []
    for block in content:
        text = getattr(block, "text", None)
        tool_name = getattr(block, "name", None)  # ToolUseBlock.name
        if text:
            out.append(text)
        elif tool_name:
            out.append(f"\n  ⚙ {tool_name}")
    return ("".join(out) + "\n") if out else ""


def _default_session_factory(working_dir: str):
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
    opts = ClaudeAgentOptions(cwd=working_dir, permission_mode="bypassPermissions")
    return ClaudeSDKClient(options=opts)


FinalCallback = Callable[[str], Awaitable[None]] | Callable[[str], None]


class ClaudeController:
    def __init__(
        self,
        session_factory: Optional[Callable[[str], object]] = None,
        watch_log_path: Optional[str] = None,
        launch_terminal: bool = False,
    ):
        self._factory = session_factory or _default_session_factory
        self.status = "idle"
        self.working_dir: Optional[str] = None
        self._final_cb: Optional[FinalCallback] = None
        # One persistent Claude session per call, so multi-turn follow-ups keep
        # context ("open it", "now add a test", ...).
        self._client: Optional[object] = None
        self._stack: Optional[contextlib.AsyncExitStack] = None
        # Read-only "watch window": stream Claude's live output to a log file and
        # (optionally, on macOS) open a Terminal tailing it.
        self._watch_log_path = watch_log_path
        self._launch_terminal = launch_terminal
        self._watch_launched = False

    def on_final(self, cb: FinalCallback) -> None:
        self._final_cb = cb

    async def start(self, working_dir: str) -> None:
        path = os.path.abspath(os.path.expanduser(working_dir))
        if not os.path.isdir(path):
            raise ValueError(f"not a directory: {working_dir}")
        # Switching directories closes the previous session (cwd is fixed per client).
        await self._close_client()
        self.working_dir = path
        self.status = "idle"
        self._open_watch(path)
        self._stack = contextlib.AsyncExitStack()
        self._client = await self._stack.enter_async_context(self._factory(path))

    async def _close_client(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:
                logger.exception("error closing Claude session")
        self._stack = None
        self._client = None

    def _open_watch(self, working_dir: str) -> None:
        if not self._watch_log_path or self._watch_launched:
            return
        try:
            with open(self._watch_log_path, "w") as f:
                f.write(f"Loop, watching Claude in {working_dir}\n")
                f.write("(read-only live view; close this window any time)\n")
        except OSError:
            logger.exception("could not create watch log %s", self._watch_log_path)
            return
        if self._launch_terminal and sys.platform == "darwin":
            script = f'tell application "Terminal" to do script "tail -f {self._watch_log_path}"'
            try:
                subprocess.Popen(
                    ["osascript", "-e", script, "-e",
                     'tell application "Terminal" to activate']
                )
            except Exception:
                logger.exception("could not open watch Terminal window")
        self._watch_launched = True

    def _watch_write(self, text: str) -> None:
        if not self._watch_log_path or not text:
            return
        try:
            with open(self._watch_log_path, "a") as f:
                f.write(text)
                f.flush()
        except OSError:
            pass

    async def send(self, text: str) -> None:
        if self._client is None:
            raise ValueError("call start() before send()")
        self.status = "working"
        final_text: Optional[str] = None
        try:
            self._watch_write(f"\n▶ {text}\n")
            await self._client.query(text)
            async for msg in self._client.receive_response():
                t = text_from_assistant(msg)
                if t:
                    final_text = t
                self._watch_write(render_watch(msg))
            self.status = "finished"
            self._watch_write("\n✓ done\n")
        except Exception:
            self.status = "error"
            self._watch_write("\n✗ error\n")
            return
        if final_text is not None and self._final_cb is not None:
            result = self._final_cb(final_text)
            if inspect.isawaitable(result):
                await result

    async def interrupt(self) -> None:
        """Stop the current generation but KEEP the session (and its context) so
        follow-ups still work; stop() closes the whole client. The SDK's
        interrupt() ends the in-flight receive_response stream, so a running
        send() unwinds on its own."""
        if self._client is None:
            return
        try:
            await self._client.interrupt()
        except Exception:
            logger.exception("SDK interrupt failed")
        self.status = "idle"

    async def stop(self, *, detach_only: bool = False) -> None:
        # detach_only is accepted for a uniform controller interface; the driven SDK
        # session is closed either way (there is no separate terminal to leave running).
        await self._close_client()
        self.status = "idle"
