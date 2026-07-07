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
"""

from __future__ import annotations

from fastapi import Request


def add_push_routes(app, registry, call_manager, auth_check=None) -> None:
    @app.post("/register")
    async def register(request: Request):
        body = await request.json() or {}
        registry.register(body.get("token", ""), body.get("account", ""))
        return {"ok": True}

    @app.post("/unregister")
    async def unregister(request: Request):
        body = await request.json() or {}
        registry.remove(body.get("token", ""))
        return {"ok": True}

    @app.post("/notify")
    async def notify(request: Request):
        """Ring or cancel an account's registered phone(s). Called by the laptop:
        ring when a Claude terminal finishes, cancel when it is no longer relevant
        (the user already handled it)."""
        body = await request.json() or {}
        account = body.get("account", "")
        if not account:
            return {"ok": True}
        if body.get("cancel"):
            await call_manager.cancel(account)
        else:
            await call_manager.ring(account, body.get("summary", "A task finished."),
                                     approval=body.get("approval"))
        return {"ok": True}

    @app.post("/call/decline")
    async def decline(request: Request):
        await call_manager.decline((await request.json() or {}).get("call_id", ""))
        return {"ok": True}
