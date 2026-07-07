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

    def set_offline_ring(self, on: bool) -> None:
        self._offline_ring = on

    def attach(self, speak_fn) -> list[str]:
        self._speak = speak_fn
        return self._cm.attach()

    def detach(self) -> None:
        self._speak = None
        self._cm.detach()

    async def on_final(self, text: str) -> None:
        if self._speak is not None:
            result = self._speak(text)
            if inspect.isawaitable(result):
                await result
        elif self._offline_ring:
            await self._cm.on_update(text)
