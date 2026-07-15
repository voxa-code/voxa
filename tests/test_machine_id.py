import os
from server import machine_id as mid


def test_machine_id_stable_and_persisted(tmp_path):
    p = str(tmp_path / "machine-id")
    a = mid.machine_id(p)
    b = mid.machine_id(p)
    assert a == b and len(a) >= 8
    assert open(p).read().strip() == a


def test_machine_name_env_override(monkeypatch):
    monkeypatch.setenv("VOXA_MACHINE_NAME", "My Studio")
    assert mid.machine_name() == "My Studio"


def test_machine_name_defaults_to_hostname(monkeypatch):
    monkeypatch.delenv("VOXA_MACHINE_NAME", raising=False)
    assert mid.machine_name()   # non-empty (hostname)
