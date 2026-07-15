# server/machine_routes.py
from __future__ import annotations

import logging

from fastapi import Request

logger = logging.getLogger(__name__)


def add_machine_routes(app, machines, auth_check=None) -> None:
    """/machines register/list/update/remove. Account-scoped, fail-open,
    authed by the pairing-token query param (accepted, not strictly enforced)."""

    @app.post("/machines/register")
    async def register(request: Request):
        try:
            b = await request.json() or {}
            machines.upsert(b.get("account", ""), b.get("machine_id", ""), b.get("name", ""))
        except Exception:
            logger.exception("machines/register failed")
        return {"ok": True}

    @app.get("/machines")
    async def list_machines(account: str = ""):
        if not account:
            return {"machines": []}
        try:
            return {"machines": machines.list(account)}
        except Exception:
            logger.exception("machines list failed")
            return {"machines": []}

    @app.post("/machines/update")
    async def update(request: Request):
        try:
            b = await request.json() or {}
            machines.update(b.get("account", ""), b.get("machine_id", ""),
                            name=b.get("name"), can_ring=b.get("can_ring"))
        except Exception:
            logger.exception("machines/update failed")
        return {"ok": True}

    @app.post("/machines/remove")
    async def remove(request: Request):
        try:
            b = await request.json() or {}
            machines.remove(b.get("account", ""), b.get("machine_id", ""))
        except Exception:
            logger.exception("machines/remove failed")
        return {"ok": True}
