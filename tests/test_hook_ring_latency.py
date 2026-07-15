# tests/test_hook_ring_latency.py
"""Task A4: needs_input must ring promptly even when the pane scrape that
builds the approval payload is slow, instead of blocking the ring on it. The
slow scrape keeps running in the background and its approval is still stored
for late delivery to the answer path (server/hook_routes._build_approval_for_hook).
"""
from __future__ import annotations

import asyncio
import time as _time

from starlette.testclient import TestClient

from server import hook_routes
from server.app import create_app
from server.config import Config

from tests.test_app import fake_factory   # the shared fake operator factory


def _cfg():
    return Config("k", "model", "secret", "127.0.0.1", 8787)


def test_needs_input_rings_promptly_without_waiting_on_slow_approval_scrape(monkeypatch):
    # A tight timeout so the ring must fire long before the fake scrape's 0.2s sleep.
    monkeypatch.setenv("VOXA_APPROVAL_SCRAPE_TIMEOUT", "0.05")
    monkeypatch.setenv("VOXA_RING_QUIET_SECONDS", "0")   # immediate report, unrelated to this gate
    app = create_app(_cfg(), operator_factory=fake_factory)
    cm = app.state.call_manager
    notifier = app.state.notifier

    built = {}

    async def slow_build(sessions, notifier_arg, *, cwd, msg, session_id):
        await asyncio.sleep(0.2)     # much slower than the 0.05s scrape timeout
        approval = {"approval_id": "late-1", "cwd": cwd, "options": []}
        notifier_arg.approvals.put(approval)
        built["approval"] = approval
        return approval

    monkeypatch.setattr(hook_routes, "_build_approval_for_hook", slow_build)

    with TestClient(app) as client:
        start = _time.monotonic()
        r = client.post("/hook?token=secret", json={
            "hook_event_name": "Notification", "session_id": "slowscrape",
            "cwd": "/p/slow", "message": "Claude needs your permission"})
        elapsed = _time.monotonic() - start
        assert r.json() == {"ok": True}
        # Rang promptly: well under the 0.2s the scrape takes to complete.
        assert elapsed < 0.15
        assert any("needs input" in m for m in cm._pending)
        # No approval attached to the prompt ring: the scrape had not finished yet.
        assert cm.attach_approvals() == []

        # Let the background scrape finish; its approval is stored for late
        # delivery to the answer path (fail-open, never blocks the call).
        _time.sleep(0.3)
        assert built.get("approval") is not None
        assert notifier.approvals.active_for("/p/slow") is not None
