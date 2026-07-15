import json
from server.machine_registry import MachineRegistry


def _reg(tmp_path, **kw):
    return MachineRegistry(str(tmp_path / "machines.json"), **kw)


def test_upsert_creates_with_defaults(tmp_path):
    clock = [1000.0]
    r = _reg(tmp_path, now_fn=lambda: clock[0])
    r.upsert("acct", "m1", "Studio")
    rows = r.list("acct")
    assert len(rows) == 1
    row = rows[0]
    assert row["machine_id"] == "m1"
    assert row["name"] == "Studio"
    assert row["can_ring"] is True
    assert row["last_seen"] == 1000.0
    assert row["online"] is True


def test_upsert_refreshes_last_seen_keeps_name_and_flag(tmp_path):
    clock = [1000.0]
    r = _reg(tmp_path, now_fn=lambda: clock[0])
    r.upsert("acct", "m1", "Studio")
    r.update("acct", "m1", can_ring=False)
    clock[0] = 2000.0
    r.upsert("acct", "m1", "")          # empty name must not clobber
    row = r.list("acct")[0]
    assert row["name"] == "Studio"
    assert row["can_ring"] is False
    assert row["last_seen"] == 2000.0


def test_update_rename_and_toggle(tmp_path):
    r = _reg(tmp_path)
    r.upsert("acct", "m1", "Old")
    r.update("acct", "m1", name="New", can_ring=False)
    row = r.list("acct")[0]
    assert row["name"] == "New"
    assert row["can_ring"] is False


def test_remove(tmp_path):
    r = _reg(tmp_path)
    r.upsert("acct", "m1", "A")
    r.remove("acct", "m1")
    assert r.list("acct") == []


def test_can_ring_defaults_true_for_unknown(tmp_path):
    r = _reg(tmp_path)
    assert r.can_ring("acct", "never-seen") is True
    r.upsert("acct", "m1", "A")
    r.update("acct", "m1", can_ring=False)
    assert r.can_ring("acct", "m1") is False


def test_online_derivation(tmp_path):
    clock = [1000.0]
    r = _reg(tmp_path, online_window=120.0, now_fn=lambda: clock[0])
    r.upsert("acct", "m1", "A")
    clock[0] = 1000.0 + 119
    assert r.list("acct")[0]["online"] is True
    clock[0] = 1000.0 + 121
    assert r.list("acct")[0]["online"] is False


def test_ttl_prune_on_list(tmp_path):
    clock = [1000.0]
    r = _reg(tmp_path, ttl_days=30, now_fn=lambda: clock[0])
    r.upsert("acct", "m1", "A")
    clock[0] = 1000.0 + 31 * 86400
    assert r.list("acct") == []          # pruned


def test_persists_across_instances(tmp_path):
    p = str(tmp_path / "machines.json")
    MachineRegistry(p).upsert("acct", "m1", "A")
    assert MachineRegistry(p).list("acct")[0]["machine_id"] == "m1"


def test_corrupt_file_is_empty(tmp_path):
    p = tmp_path / "machines.json"
    p.write_text("{not json")
    assert MachineRegistry(str(p)).list("acct") == []
