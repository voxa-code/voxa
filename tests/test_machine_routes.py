import asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.machine_registry import MachineRegistry
from server.machine_routes import add_machine_routes


def _client(tmp_path):
    app = FastAPI()
    reg = MachineRegistry(str(tmp_path / "m.json"))
    add_machine_routes(app, reg)
    return TestClient(app), reg


def test_register_then_list(tmp_path):
    c, _ = _client(tmp_path)
    assert c.post("/machines/register",
                  json={"account": "a", "machine_id": "m1", "name": "Studio"}).json() == {"ok": True}
    rows = c.get("/machines", params={"account": "a"}).json()["machines"]
    assert [r["machine_id"] for r in rows] == ["m1"]
    assert rows[0]["name"] == "Studio"
    assert rows[0]["can_ring"] is True


def test_update_and_remove(tmp_path):
    c, _ = _client(tmp_path)
    c.post("/machines/register", json={"account": "a", "machine_id": "m1", "name": "Old"})
    c.post("/machines/update", json={"account": "a", "machine_id": "m1", "name": "New", "can_ring": False})
    row = c.get("/machines", params={"account": "a"}).json()["machines"][0]
    assert row["name"] == "New" and row["can_ring"] is False
    c.post("/machines/remove", json={"account": "a", "machine_id": "m1"})
    assert c.get("/machines", params={"account": "a"}).json()["machines"] == []


def test_missing_account_is_noop(tmp_path):
    c, _ = _client(tmp_path)
    assert c.get("/machines", params={"account": ""}).json() == {"machines": []}
