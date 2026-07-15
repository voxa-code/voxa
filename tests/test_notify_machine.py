"""/notify honoring per-machine can_ring (Connected Macs roster)."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.machine_registry import MachineRegistry
from server.push_routes import add_push_routes


class _Reg:
    def tokens(self, a=None): return ["tok"]
    def alert_tokens(self, a=None): return ["alerttok"]


class _CM:
    def __init__(self): self.rang = []
    async def ring(self, account, summary, approval=None): self.rang.append(account)
    async def cancel(self, account=None): pass


class _Apns:
    def __init__(self): self.alerts = []
    async def send_alert(self, token, title, body): self.alerts.append(token)


def _app(tmp_path):
    app = FastAPI()
    cm, apns = _CM(), _Apns()
    machines = MachineRegistry(str(tmp_path / "m.json"))
    add_push_routes(app, _Reg(), cm, apns=apns, machines=machines)
    return TestClient(app), cm, apns, machines


def test_notify_rings_when_machine_can_ring(tmp_path):
    c, cm, apns, machines = _app(tmp_path)
    machines.upsert("a", "m1", "Mac")           # default can_ring True
    c.post("/notify", json={"account": "a", "machine_id": "m1", "summary": "done"})
    assert cm.rang == ["a"] and apns.alerts == []


def test_notify_unknown_machine_still_rings(tmp_path):
    c, cm, apns, _ = _app(tmp_path)
    c.post("/notify", json={"account": "a", "machine_id": "ghost", "summary": "done"})
    assert cm.rang == ["a"]


def test_notify_muted_machine_sends_alert_not_ring(tmp_path):
    c, cm, apns, machines = _app(tmp_path)
    machines.upsert("a", "m1", "Mac")
    machines.update("a", "m1", can_ring=False)
    c.post("/notify", json={"account": "a", "machine_id": "m1", "summary": "done"})
    assert cm.rang == [] and apns.alerts == ["alerttok"]


def test_notify_muted_but_no_alert_token_still_rings(tmp_path):
    app = FastAPI()
    cm = _CM()

    class _RegNoAlert(_Reg):
        def alert_tokens(self, a=None): return []

    machines = MachineRegistry(str(tmp_path / "m.json"))
    add_push_routes(app, _RegNoAlert(), cm, apns=_Apns(), machines=machines)
    machines.upsert("a", "m1", "Mac")
    machines.update("a", "m1", can_ring=False)
    TestClient(app).post("/notify", json={"account": "a", "machine_id": "m1", "summary": "done"})
    assert cm.rang == ["a"]          # no alert token -> ring anyway, never drop
