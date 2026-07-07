"""Behavior of the push/CallKit routes. These are account-scoped (the unguessable
account id is the authorization) and do not require a shared token in the hosted
model, so the tests assert function, not token gating."""

from fastapi import FastAPI
from starlette.testclient import TestClient

from server.push_routes import add_push_routes


class FakeRegistry:
    def __init__(self):
        self.registered = []
        self.removed = []

    def register(self, token, account):
        self.registered.append((token, account))

    def remove(self, token):
        self.removed.append(token)

    def tokens(self, account=None):
        return ["DEV1"]


class FakeCallManager:
    def __init__(self):
        self.rung = []
        self.cancelled = []
        self.declined = []
        self.last_approval = None

    async def ring(self, account, summary, approval=None):
        self.rung.append((account, summary))
        self.last_approval = approval

    async def cancel(self, account=None):
        self.cancelled.append(account)

    async def decline(self, call_id):
        self.declined.append(call_id)


def _client():
    app = FastAPI()
    reg, cm = FakeRegistry(), FakeCallManager()
    add_push_routes(app, reg, cm)
    return TestClient(app), reg, cm


def test_register_stores_token_by_account():
    c, reg, _ = _client()
    r = c.post("/register", json={"token": "t", "account": "a"})
    assert r.status_code == 200
    assert reg.registered == [("t", "a")]


def test_unregister_removes_token():
    c, reg, _ = _client()
    assert c.post("/unregister", json={"token": "t"}).status_code == 200
    assert reg.removed == ["t"]


def test_notify_rings_account():
    c, _, cm = _client()
    assert c.post("/notify", json={"account": "a", "summary": "hi"}).status_code == 200
    assert cm.rung == [("a", "hi")]


def test_notify_cancel():
    c, _, cm = _client()
    assert c.post("/notify", json={"account": "a", "cancel": True}).status_code == 200
    assert cm.cancelled == ["a"]


def test_notify_without_account_is_noop():
    c, _, cm = _client()
    assert c.post("/notify", json={}).status_code == 200
    assert cm.rung == [] and cm.cancelled == []


def test_decline_calls_manager():
    c, _, cm = _client()
    assert c.post("/call/decline", json={"call_id": "c1"}).status_code == 200
    assert cm.declined == ["c1"]


def test_notify_passes_approval_through():
    c, _, cm = _client()
    r = c.post("/notify", json={"account": "a", "summary": "s",
                                 "approval": {"approval_id": "z9"}})
    assert r.status_code == 200
    assert cm.rung == [("a", "s")]
    assert cm.last_approval["approval_id"] == "z9"
