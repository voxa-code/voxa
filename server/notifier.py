"""Routes background/hook updates to the phone.

- Line attached (a metered session is live): stay silent; that session narrates
  its own result on the line, so we don't talk over it.
- App open but no line yet (connected for a terminals-first home, or for setup
  pre-Start): queue the update so it's spoken (and rendered) when the user taps
  Start/attaches, AND still ring (CallKit via the local APNs key, or via the
  cloud relay in zero-config mode) so a finish/needs_input always reaches the
  phone even while the app is simply open.
- App closed: ring the same way.
- Per-cwd "silent" rule: always queue, never ring, regardless of the above.

Also owns the notification state that used to live loose on app.state:
phone-client count, last paired account, hooks-live flag, cross-source ring
debounce, and the hook turn bookkeeping dicts.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from server.approvals import ApprovalStore
from server.notify_rules import NotifyRules


class Notifier:
    def __init__(self, call_manager, push_enabled: bool,
                 ring_debounce: float | None = None,
                 rules: NotifyRules | None = None):
        self._cm = call_manager
        self._push_enabled = push_enabled
        if ring_debounce is None:
            # The finish-hook and the screen-scraper can both report the SAME
            # finish; collapse rings inside this window into one call.
            ring_debounce = float(os.environ.get("VOXA_RING_DEBOUNCE_SECONDS", "6"))
        self._ring_debounce = ring_debounce
        self._last_ring_at = 0.0
        self.phone_clients = 0
        self.last_account = ""
        from server.machine_id import machine_id, machine_name
        self.machine_id = machine_id()
        self.machine_name = machine_name()
        # The caller's voice/lang from the most recent ws connection's query
        # params (set by serve_ws). report() reads these to warm an operator
        # with the SAME config the answering phone will ask for; empty until
        # a phone has connected at least once.
        self.last_voice = ""
        self.last_lang = ""
        # Set by app.py to a Prewarmer sharing this notifier/sessions/config, so
        # report() can kick it the instant a ring fires. None (the default) keeps
        # report() a no-op on this front, e.g. in tests that build a bare Notifier.
        self.prewarmer = None
        self.hooks_live = False   # flips true once a real Claude Code hook arrives
        self.turn_start: dict = {}  # hook session_id -> turn start (UserPromptSubmit)
        self.hook_last: dict = {}   # hook session_id -> last announced time
        self.pre_tool: dict = {}    # hook session_id -> last PreToolUse context
        # Finish messages HELD because other sessions were still mid-turn when
        # they arrived (session_id -> (msg, cwd)); drained into the one ring
        # that fires when the last open turn stops. See hooks.others_mid_turn.
        self.held_finishes: dict = {}
        self.rules = rules if rules is not None else NotifyRules()
        self.approvals = ApprovalStore()
        # Set by serve_ws while a phone line is attached (mirrors hub.attach), so a
        # FRESH approval created mid-session still reaches the phone live even
        # though report() below returns early (silent) while the line is open.
        # None whenever no line is attached; cleared in serve_ws's finally.
        self.on_approval = None
        # Set alongside on_approval while a phone line is attached, so the voice
        # path (Orchestrator.resolve_approval, which never touches this
        # websocket) can still tell the phone to clear a resolved approval card,
        # the same way the tap path's approval_decision handler does directly.
        # None whenever no line is attached; cleared in serve_ws's finally.
        self.on_approval_resolved = None
        # Set alongside on_approval while a phone line is attached. A FRESH
        # approval whose cwd differs from the driven session is read aloud through
        # this callback: the driven pane's monitor re-narrates its OWN prompts, but
        # a foreign session has no narration on this line, so its card would
        # otherwise arrive silent. serve_ws's callback guards against speaking the
        # driven cwd twice. None whenever no line is attached; cleared in the finally.
        self.on_approval_speak = None
        # Set alongside on_approval_speak while a phone line is attached. A finish
        # (or other non-approval) report for a cwd OTHER than the driven session
        # must still reach the user live, with its project name, instead of the
        # silent no-op the line-open branch below would otherwise be. serve_ws's
        # callback guards against re-narrating the driven cwd's own finish (the
        # pane monitor on that line already speaks it). None whenever no line is
        # attached; cleared in serve_ws's finally.
        self.on_update_speak = None
        # Task 2 (queue runner): cwds with an ACTIVE queue burst. A per-item finish
        # for such a cwd is folded into the ONE drain digest instead of ringing;
        # needs_input still surfaces immediately. The runner adds/discards cwds as a
        # burst starts/drains. cwds are stored rstrip-normalized.
        self.queue_active_cwds: set[str] = set()
        # Set by the queue runner (via serve_ws): notified when a needs_input report
        # arrives for a burst cwd so the runner can pause the queue. It does NOT
        # suppress the ring: needs_input still surfaces per Phase 1.
        self.on_queue_needs_input = None

    @property
    def call_manager(self):
        return self._cm

    def note_client_connected(self) -> None:
        self.phone_clients += 1

    def note_client_disconnected(self) -> None:
        self.phone_clients = max(0, self.phone_clients - 1)

    async def report(self, summary: str, *, kind: str = "finish", cwd: str = "",
                      approval: dict | None = None) -> None:
        if approval is not None and self.on_approval is not None:
            # Push the structured prompt to the attached phone even though the
            # line-open branch below stays silent on the SUMMARY (the session
            # narrates that itself); the approval buttons are not narration.
            try:
                await self.on_approval(approval)
            except Exception:
                logging.exception("on_approval callback failed")
        if approval is not None and self.on_approval_speak is not None:
            # Read a foreign session's fresh prompt aloud (the callback itself skips
            # the driven cwd to avoid doubling the pane monitor's narration).
            # Fail-open: a narration error must never break the live call.
            try:
                await self.on_approval_speak(approval)
            except Exception:
                logging.exception("on_approval_speak callback failed")
        norm_cwd = (cwd or "").rstrip("/")
        if kind == "finish" and norm_cwd in self.queue_active_cwds:
            # A queue burst is engaged for this cwd: the per-item finish is folded
            # into the ONE drain digest, so it must not ring on its own.
            return
        if kind == "needs_input" and norm_cwd in self.queue_active_cwds \
                and self.on_queue_needs_input is not None:
            # Tell the runner to pause the queue, then fall through: needs_input
            # still rings immediately (Phase 1). Fail-open on the callback.
            try:
                await self.on_queue_needs_input(norm_cwd)
            except Exception:
                logging.exception("on_queue_needs_input callback failed")
        if self._cm.line_open:
            if self.on_update_speak is not None and summary:
                # A foreign session's finish must reach the user live too, named by
                # its project, instead of arriving as a silent no-op just because
                # the DRIVEN session's line happens to be open. Fail-open: a
                # narration error must never break the live call.
                try:
                    await self.on_update_speak(summary, cwd)
                except Exception:
                    logging.exception("on_update_speak callback failed")
            return
        if self.rules.mode(cwd, kind) == "silent":
            # The user asked this project to stay quiet: queue it (spoken later,
            # e.g. on the next attach) but never ring. Checked BEFORE the
            # app-open queue-and-continue below so silent still short-circuits.
            self._cm.queue(summary, approval=approval)
            return
        if self.phone_clients > 0:
            # A terminals-first app is "connected" (open) far more often than
            # not, so no metered line yet must no longer swallow the ring:
            # queue the update (spoken on begin/attach, and so the UI can
            # render it right away) AND fall through to the normal ring path
            # below (rules, debounce, prewarm kick all unchanged).
            self._cm.queue(summary, approval=approval)
        now = time.monotonic()
        if now - self._last_ring_at < self._ring_debounce:
            logging.info("suppressing duplicate ring within debounce window")
            return
        self._last_ring_at = now
        await self._cm.on_update(summary, approval=approval)
        pw = self.prewarmer
        if pw is not None and pw.enabled():
            # Warm the Gemini session while the phone is still ringing so the
            # greeting is already synthesized when the user answers. Fire and
            # forget: the ring itself must never wait on this.
            asyncio.ensure_future(pw.start(summary, cwd, approval))
        if not self._push_enabled:
            # Avoid a double call: with a local APNs key, on_update already rang.
            await self._ring_via_cloud(summary, approval=approval)

    async def cancel_ring(self) -> None:
        """Stop a still-ringing (or just-answered, harmlessly) phone: used by
        instant-ring mode when follow-on hook activity proves a finish rang
        too soon. Same local-push-vs-cloud-relay split as report()/on_update:
        a local APNs key cancels the specific call directly, zero-config mode
        asks the cloud relay to do it. Fail-open: never raise into the caller
        (a cancel racing a real state change is a shrug, not an error)."""
        try:
            if self._push_enabled:
                await self._cm.cancel(self.last_account or None)
            else:
                await self.cancel_via_cloud()
        except Exception:
            logging.exception("cancel_ring failed")
        # A cancelled ring will never be answered: tear down any session warmed
        # for it instead of letting it idle out its TTL (metered minutes in
        # proxy mode). Fail-open like everything else in this method.
        pw = self.prewarmer
        if pw is not None:
            try:
                await pw.discard()
            except Exception:
                logging.exception("prewarm discard on cancel failed")

    async def _ring_via_cloud(self, summary: str, approval: dict | None = None) -> None:
        # The laptop holds no APNs key (zero-config); ask the cloud to ring the
        # last-paired account's phone. The account id is the authorization.
        relay = os.environ.get("VOXA_RELAY_URL", "").strip().rstrip("/")
        if not relay or not self.last_account:
            return
        payload = {"account": self.last_account, "summary": summary, "machine_id": self.machine_id}
        if approval is not None:
            payload["approval"] = approval
        try:
            import httpx
            # 20s, not 10: this round trip includes the cloud's own APNs push to
            # Apple, which can be slow on a cold HTTP/2 connection right after a
            # relay restart. A client-side timeout here is cosmetic (the relay
            # keeps running the request and still rings), but a needless one
            # logs a scary traceback for what was actually a successful ring.
            async with httpx.AsyncClient(timeout=20) as c:
                await c.post(f"{relay}/notify", json=payload)
        except Exception:
            logging.exception("ring via cloud failed")

    async def register_machine_cloud(self) -> None:
        """Register/refresh this Mac in the cloud roster for the paired account.
        No-op until a phone has paired (account known) and a relay is set."""
        relay = os.environ.get("VOXA_RELAY_URL", "").strip().rstrip("/")
        if not relay or not self.last_account:
            return
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as c:
                await c.post(f"{relay}/machines/register", json={
                    "account": self.last_account,
                    "machine_id": self.machine_id,
                    "name": self.machine_name,
                })
        except Exception as e:
            # Expected to fail transiently when the relay is briefly unreachable
            # (this runs on a 60s heartbeat); a one-line warning, not a scary
            # traceback. Fail-open: the next heartbeat/pair re-registers.
            logging.warning("machine register via cloud skipped (relay unreachable): %s", e)

    async def cancel_via_cloud(self) -> None:
        relay = os.environ.get("VOXA_RELAY_URL", "").strip().rstrip("/")
        if not relay or not self.last_account:
            return
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as c:
                await c.post(f"{relay}/notify",
                             json={"account": self.last_account, "cancel": True})
        except Exception:
            logging.exception("cancel via cloud failed")
