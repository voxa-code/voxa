"""Device registration + ring + call-decline endpoints, shared by the laptop
server and the cloud. In production these live on the cloud (so your APNs key
isn't on every customer's laptop).

Security model: these routes are scoped by the ACCOUNT id (an unguessable UUID),
NOT by a shared secret. In the hosted/relay model the phone and each laptop hold
the laptop's per-machine pairing token, which the cloud cannot verify (it never
sees it), so requiring a shared token here would break zero-config pairing. That
is why `auth_check` is accepted for compatibility but not enforced. The intended
hardening is per-ACCOUNT authentication (device attestation / a cloud-issued,
cloud-verifiable account token), which does not exist yet, see SECURITY.md.

Free-tier call metering: see add_push_routes' docstring.
"""

from __future__ import annotations

import logging
import os
import time

from fastapi import Request

logger = logging.getLogger(__name__)


def add_push_routes(app, registry, call_manager, auth_check=None,
                     meter=None, is_paying=None, free_call_limit=None,
                     apns=None, machines=None) -> None:
    """Register /register, /unregister, /notify, /call/decline.

    Free-tier call metering (all-optional; omitting `meter`/`is_paying` leaves
    behavior exactly as before, i.e. always ring): when both are given, /notify
    checks whether `account` is a never-paid account that has already used its
    monthly free-call quota (`free_call_limit`, default from env
    VOXA_FREE_CALLS_PER_MONTH, default 3). If so, it sends a plain APNs alert
    push ("Claude finished... Voxa Pro would have called you") to the account's
    alert_tokens() instead of ringing, and does not touch the meter further.
    Paying accounts (is_paying(account) is True) and accounts under quota always
    ring as before, and each ring increments the meter.

    If the account has no alert tokens registered (or no pusher can be found to
    send one), /notify falls back to ringing anyway rather than silently
    dropping the only signal available for that account.

    Everything here fails open: any error while checking the quota or sending
    an alert is logged and treated as "just ring", never as "drop the call"."""
    _free_limit = (free_call_limit if free_call_limit is not None
                   else int(os.environ.get("VOXA_FREE_CALLS_PER_MONTH", "3")))

    @app.post("/register")
    async def register(request: Request):
        body = await request.json() or {}
        registry.register(body.get("token", ""), body.get("account", ""),
                          body.get("kind", "voip"))
        return {"ok": True}

    @app.post("/unregister")
    async def unregister(request: Request):
        body = await request.json() or {}
        registry.remove(body.get("token", ""))
        return {"ok": True}

    def _over_free_quota(account: str) -> bool:
        """True only when metering is wired up AND the account has never paid
        AND it has already used this month's free calls. Fails open to False
        (i.e. "just ring") on any error."""
        if meter is None or is_paying is None:
            return False
        try:
            if is_paying(account):
                return False
            return meter.count(account, time.strftime("%Y%m", time.gmtime())) >= _free_limit
        except Exception:
            logger.exception("call-quota check failed for %s; failing open", account)
            return False

    async def _send_alert_fallback(account: str, summary: str) -> bool:
        """Try to send an alert push in place of a ring. Returns True if at
        least one alert was actually sent (the caller should skip ringing);
        False means there was no alert token or no usable pusher, so the caller
        must ring instead rather than silently drop the only signal."""
        try:
            alert_tokens = registry.alert_tokens(account)
        except Exception:
            logger.exception("alert_tokens lookup failed for %s", account)
            return False
        if not alert_tokens:
            return False
        pusher = apns if apns is not None else getattr(call_manager, "_pusher", None)
        if pusher is None or not hasattr(pusher, "send_alert"):
            return False
        title = "Claude finished"
        body_text = f"{summary[:120]} (Voxa Pro would have called you)"
        sent_any = False
        for token in alert_tokens:
            try:
                await pusher.send_alert(token, title, body_text)
                sent_any = True
            except Exception:
                logger.exception("alert push failed for token %s", token[:8])
        return sent_any

    @app.post("/notify")
    async def notify(request: Request):
        """Ring or cancel an account's registered phone(s). Called by the laptop:
        ring when a Claude terminal finishes, cancel when it is no longer relevant
        (the user already handled it). A free-tier account past its monthly call
        quota gets a plain alert push instead of a ring (see module docstring)."""
        body = await request.json() or {}
        account = body.get("account", "")
        if not account:
            return {"ok": True}
        if body.get("cancel"):
            await call_manager.cancel(account)
            return {"ok": True}

        summary = body.get("summary", "A task finished.")
        approval = body.get("approval")

        # Per-machine ring control: a muted Mac (can_ring False) sends the plain
        # alert banner instead of a CallKit ring. Unknown/absent machine_id
        # defaults to ringing (fleet-wide default). Refresh last_seen so a Mac
        # that just finished counts as online. Fail-open to ringing.
        machine_id = body.get("machine_id", "")
        if machines is not None and machine_id:
            try:
                machines.upsert(account, machine_id, "")
            except Exception:
                logger.exception("machine upsert on notify failed for %s", account)
            try:
                muted = not machines.can_ring(account, machine_id)
            except Exception:
                muted = False
            if muted:
                if await _send_alert_fallback(account, summary):
                    return {"ok": True}
                # No alert token reachable: ring anyway rather than drop.

        if _over_free_quota(account):
            if await _send_alert_fallback(account, summary):
                return {"ok": True}
            # No alert tokens (or no pusher) reachable: never silently drop the
            # only signal, ring anyway even though the free quota is used up.

        await call_manager.ring(account, summary, approval=approval)
        if meter is not None:
            try:
                meter.increment(account)
            except Exception:
                logger.exception("call meter increment failed for %s", account)
        return {"ok": True}

    @app.post("/call/decline")
    async def decline(request: Request):
        await call_manager.decline((await request.json() or {}).get("call_id", ""))
        return {"ok": True}
