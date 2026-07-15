"""Behavior of the push/CallKit routes. These are account-scoped (the unguessable
account id is the authorization) and do not require a shared token in the hosted
model, so the tests assert function, not token gating."""

from fastapi import FastAPI
from starlette.testclient import TestClient

from server.push_routes import add_push_routes


class FakeRegistry:
    def __init__(self, alert_tokens=None):
        self.registered = []
        self.removed = []
        self._alert_tokens = alert_tokens if alert_tokens is not None else ["ALERT1"]

    def register(self, token, account, kind="voip"):
        self.registered.append((token, account, kind))

    def remove(self, token):
        self.removed.append(token)

    def tokens(self, account=None):
        return ["DEV1"]

    def alert_tokens(self, account=None):
        return list(self._alert_tokens)


class FakeCallManager:
    def __init__(self, pusher=None):
        self.rung = []
        self.cancelled = []
        self.declined = []
        self.last_approval = None
        self._pusher = pusher

    async def ring(self, account, summary, approval=None):
        self.rung.append((account, summary))
        self.last_approval = approval

    async def cancel(self, account=None):
        self.cancelled.append(account)

    async def decline(self, call_id):
        self.declined.append(call_id)


class FakeMeter:
    def __init__(self, counts=None):
        self._counts = counts or {}
        self.incremented = []

    def count(self, account, yyyymm=None):
        return self._counts.get(account, 0)

    def increment(self, account):
        self.incremented.append(account)
        self._counts[account] = self._counts.get(account, 0) + 1


class FakePusher:
    def __init__(self):
        self.alerts = []

    async def send_alert(self, token, title, body):
        self.alerts.append((token, title, body))


def _client(registry=None, call_manager=None, **kwargs):
    app = FastAPI()
    reg = registry or FakeRegistry()
    cm = call_manager or FakeCallManager()
    add_push_routes(app, reg, cm, **kwargs)
    return TestClient(app), reg, cm


def test_register_stores_token_by_account():
    c, reg, _ = _client()
    r = c.post("/register", json={"token": "t", "account": "a"})
    assert r.status_code == 200
    assert reg.registered == [("t", "a", "voip")]


def test_register_alert_kind_is_passed_through():
    c, reg, _ = _client()
    r = c.post("/register", json={"token": "t", "account": "a", "kind": "alert"})
    assert r.status_code == 200
    assert reg.registered == [("t", "a", "alert")]


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


# --- free-tier call metering gate --------------------------------------------

def test_notify_without_metering_kwargs_rings_as_before():
    # Omitting meter/is_paying must leave behavior exactly as it was.
    c, _, cm = _client()
    c.post("/notify", json={"account": "a", "summary": "s"})
    assert cm.rung == [("a", "s")]


def test_notify_free_account_under_limit_rings_and_increments():
    meter = FakeMeter(counts={"a": 1})   # 1 used, limit 3
    pusher = FakePusher()
    cm = FakeCallManager(pusher=pusher)
    c, _, cm = _client(call_manager=cm, meter=meter, is_paying=lambda a: False,
                       free_call_limit=3)
    r = c.post("/notify", json={"account": "a", "summary": "s"})
    assert r.status_code == 200
    assert cm.rung == [("a", "s")]
    assert meter.incremented == ["a"]
    assert pusher.alerts == []


def test_notify_free_account_at_limit_sends_alert_and_does_not_ring():
    meter = FakeMeter(counts={"a": 3})   # already at the limit
    pusher = FakePusher()
    cm = FakeCallManager(pusher=pusher)
    reg = FakeRegistry(alert_tokens=["ALERT1"])
    c, reg, cm = _client(registry=reg, call_manager=cm, meter=meter,
                         is_paying=lambda a: False, free_call_limit=3)
    r = c.post("/notify", json={"account": "a", "summary": "x" * 200})
    assert r.status_code == 200
    assert cm.rung == []
    assert meter.incremented == []
    assert len(pusher.alerts) == 1
    token, title, body = pusher.alerts[0]
    assert token == "ALERT1"
    assert title == "Claude finished"
    assert body.startswith("x" * 120)
    assert body.endswith("(Voxa Pro would have called you)")
    assert len(body) < 250   # summary was truncated, not the full 200 chars


def test_notify_paying_account_always_rings_even_at_high_count():
    meter = FakeMeter(counts={"a": 999})
    pusher = FakePusher()
    cm = FakeCallManager(pusher=pusher)
    c, _, cm = _client(call_manager=cm, meter=meter, is_paying=lambda a: True,
                       free_call_limit=3)
    r = c.post("/notify", json={"account": "a", "summary": "s"})
    assert r.status_code == 200
    assert cm.rung == [("a", "s")]
    assert meter.incremented == ["a"]
    assert pusher.alerts == []


def test_notify_no_alert_tokens_falls_back_to_ringing():
    meter = FakeMeter(counts={"a": 5})
    pusher = FakePusher()
    cm = FakeCallManager(pusher=pusher)
    reg = FakeRegistry(alert_tokens=[])   # no alert tokens registered
    c, reg, cm = _client(registry=reg, call_manager=cm, meter=meter,
                         is_paying=lambda a: False, free_call_limit=3)
    r = c.post("/notify", json={"account": "a", "summary": "s"})
    assert r.status_code == 200
    assert cm.rung == [("a", "s")]     # never silently dropped
    assert pusher.alerts == []
    assert meter.incremented == ["a"]


def test_notify_cancel_path_unaffected_by_metering():
    meter = FakeMeter(counts={"a": 999})
    c, _, cm = _client(meter=meter, is_paying=lambda a: False, free_call_limit=3)
    r = c.post("/notify", json={"account": "a", "cancel": True})
    assert r.status_code == 200
    assert cm.cancelled == ["a"]
    assert cm.rung == []
    assert meter.incremented == []


def test_notify_uses_apns_kwarg_over_call_manager_pusher():
    meter = FakeMeter(counts={"a": 3})
    cm_pusher = FakePusher()
    explicit_pusher = FakePusher()
    cm = FakeCallManager(pusher=cm_pusher)
    reg = FakeRegistry(alert_tokens=["ALERT1"])
    c, reg, cm = _client(registry=reg, call_manager=cm, meter=meter,
                         is_paying=lambda a: False, free_call_limit=3,
                         apns=explicit_pusher)
    c.post("/notify", json={"account": "a", "summary": "s"})
    assert len(explicit_pusher.alerts) == 1
    assert cm_pusher.alerts == []


def test_notify_metering_error_fails_open_and_still_rings():
    class BrokenMeter:
        def count(self, account, yyyymm=None):
            raise RuntimeError("boom")

        def increment(self, account):
            raise RuntimeError("boom")

    c, _, cm = _client(meter=BrokenMeter(), is_paying=lambda a: False,
                       free_call_limit=3)
    r = c.post("/notify", json={"account": "a", "summary": "s"})
    assert r.status_code == 200
    assert cm.rung == [("a", "s")]   # metering failure never blocks the ring
