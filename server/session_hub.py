from __future__ import annotations

import inspect


class SessionHub:
    def __init__(self, controller, call_manager):
        self._c = controller
        self._cm = call_manager
        self._speak = None
        # Ring on finish when no line is attached. Disabled once Claude Code hooks are
        # live, so the hook becomes the single offline-ring source (no double-report).
        self._offline_ring = True
        controller.on_final(self.on_final)
        # Multi-session labeling (fleet awareness): assigned by ws_session once the
        # Session exists. label_fn returns the current project label (e.g. the
        # controller's working_dir basename); multi_fn returns True when more than
        # one session/terminal is live. Both None by default, so single-session
        # narration stays byte-identical to today.
        self.label_fn = None
        self.multi_fn = None

    def set_offline_ring(self, on: bool) -> None:
        self._offline_ring = on

    def attach(self, speak_fn) -> list[str]:
        self._speak = speak_fn
        return self._cm.attach()

    def detach(self) -> None:
        self._speak = None
        self._cm.detach()

    async def on_final(self, text: str) -> None:
        text = self._labeled(text)
        if self._speak is not None:
            result = self._speak(text)
            if inspect.isawaitable(result):
                await result
        elif self._offline_ring:
            await self._cm.on_update(text)

    def _labeled(self, text: str) -> str:
        """Prefix `text` with the driven session's project label when several
        sessions/terminals are live, so a foreign monitor (or the phone) never
        attributes one session's finish to another. No-op (byte-identical to
        today) unless both label_fn and multi_fn are set, multi_fn() is True,
        and label_fn() returns a non-empty label."""
        if self.label_fn is None or self.multi_fn is None:
            return text
        if not self.multi_fn():
            return text
        label = self.label_fn()
        if not label:
            return text
        return f"[{label}] {text}"
