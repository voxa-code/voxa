from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

# Bound the in-memory queues so a long-running process that never gets a phone
# connection (or a chatty decline stream) can't grow without limit.
_MAX_PENDING = 10
_MAX_DECLINED = 50


class CallManager:
    def __init__(self, pusher, registry, ring_fn=None):
        self._pusher = pusher
        self._registry = registry
        self._pending: list[str] = []
        self._pending_approvals: list[dict] = []
        self._line_open = False
        self._seq = 0
        self._declined: list[str] = []
        self._last_call_id: dict[str, str] = {}   # account ("" = default) -> last call_id
        self._last_detach = float("-inf")          # monotonic time the line last closed

    @property
    def line_open(self) -> bool:
        return self._line_open

    def recently_open(self, within: float = 90.0) -> bool:
        """True if a phone line is open or closed less than `within` seconds ago.
        Used to suppress ring-cancels for a call the user already answered."""
        return self._line_open or (time.monotonic() - self._last_detach) < within

    def attach(self) -> list[str]:
        self._line_open = True
        # The phone answered: any pending ring is consumed, so a later cancel()
        # (e.g. the terminal "resuming" because Voxa itself is driving it) must not
        # push the answering phone a spurious extra call.
        self._last_call_id.clear()
        drained, self._pending = self._pending, []
        return drained

    def attach_approvals(self) -> list[dict]:
        """Drain any approvals queued alongside summaries. Separate from attach()
        so existing callers of attach() keep getting a plain list[str]."""
        drained, self._pending_approvals = self._pending_approvals, []
        return drained

    def detach(self) -> None:
        self._line_open = False
        self._last_detach = time.monotonic()

    def queue(self, summary: str, approval: dict | None = None) -> None:
        """Queue an update to speak on the next attach WITHOUT ringing (the app is
        open but no metered line is up yet)."""
        self._pending.append(summary)
        del self._pending[:-_MAX_PENDING]
        if approval is not None:
            self._pending_approvals.append(approval)
            del self._pending_approvals[:-_MAX_PENDING]

    async def decline(self, call_id: str) -> None:
        """The phone declined (or missed) this specific call. Record it (idempotent)
        and cancel the matching ring on the account's other devices, so a call the
        user rejected on one phone stops ringing everywhere."""
        if not call_id or call_id in self._declined:
            return
        self._declined.append(call_id)
        del self._declined[:-_MAX_DECLINED]
        account = next((a for a, cid in self._last_call_id.items() if cid == call_id), None)
        if account is not None:
            await self.cancel(account)

    async def on_update(self, summary: str, approval: dict | None = None) -> None:
        if self._line_open:
            return
        self._pending.append(summary)
        del self._pending[:-_MAX_PENDING]
        if approval is not None:
            self._pending_approvals.append(approval)
            del self._pending_approvals[:-_MAX_PENDING]
        await self.ring(None, summary, approval=approval)

    async def ring(self, account: str | None, summary: str, approval: dict | None = None) -> None:
        """Ring a specific account's registered phones (or all if account is None).
        Used by the cloud /notify path when a background terminal finishes and the
        user isn't on the line."""
        self._seq += 1
        call_id = f"call-{self._seq}"
        self._last_call_id[account or ""] = call_id

        # Push every registered token at once instead of one after another: a
        # fleet with several phones must not wait N x (TLS+APNs round trip) to
        # ring the last one. Per-token failure handling is unchanged, just
        # fanned out under gather so one slow/raising token never delays (or
        # blocks) the others.
        async def _push(token: str) -> None:
            try:
                res = await self._pusher.send_voip(token, call_id, summary, approval=approval)
            except Exception:
                logger.exception("voip push raised for token %s", token[:8])
                return
            if res is not True:
                logger.warning("voip push rejected for token %s (call %s, status %s)",
                               token[:8], call_id, res)
                # 410 Gone = the token is permanently dead (app deleted/reinstalled).
                # Prune it so we stop ringing a phone that will never answer.
                if res == 410 and hasattr(self._registry, "remove"):
                    self._registry.remove(token)

        await asyncio.gather(*(_push(t) for t in self._registry.tokens(account)))

    def last_call_id(self, account: str | None = None) -> str | None:
        return self._last_call_id.get(account or "")

    async def cancel(self, account: str | None = None) -> None:
        """Stop a ringing/active call on the account's phones (handled elsewhere, or no
        longer relevant). No-op if nothing was rung for this account."""
        call_id = self._last_call_id.pop(account or "", None)
        if not call_id:
            return
        for token in self._registry.tokens(account):
            await self._pusher.send_voip_cancel(token, call_id)
