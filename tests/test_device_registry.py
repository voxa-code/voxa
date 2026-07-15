import json

from server.device_registry import DeviceRegistry


def test_register_dedup_and_persist(tmp_path):
    p = str(tmp_path / "devices.json")
    r = DeviceRegistry(p)
    r.register("AA")
    r.register("AA")
    r.register("BB")
    assert sorted(r.tokens()) == ["AA", "BB"]
    # reload from disk
    assert sorted(DeviceRegistry(p).tokens()) == ["AA", "BB"]

def test_remove(tmp_path):
    p = str(tmp_path / "devices.json")
    r = DeviceRegistry(p)
    r.register("AA")
    r.remove("AA")
    assert r.tokens() == []


def test_reregister_moves_token_between_accounts(tmp_path):
    # Simulates the phone signing in: same token re-registers under the Apple
    # account and must NOT remain under the old anonymous account, or ring(None)
    # would push it twice (the second short "ghost" call bug).
    p = str(tmp_path / "devices.json")
    r = DeviceRegistry(p)
    r.register("TOK", account="d-anon")
    r.register("TOK", account="user_123")
    assert r.tokens("d-anon") == []
    assert r.tokens("user_123") == ["TOK"]
    # ring(None) fan-out sees the token exactly once
    assert r.tokens() == ["TOK"]
    # survives reload
    assert sorted(DeviceRegistry(p).tokens()) == ["TOK"]


def test_tokens_none_is_deduped(tmp_path):
    # Defensive: even if the same token somehow lands under two accounts, the
    # all-accounts fan-out list returns it once so the phone rings once.
    p = str(tmp_path / "devices.json")
    r = DeviceRegistry(p)
    r._by_account = {"a": {"voip": {"TOK"}, "alert": set()},
                      "b": {"voip": {"TOK"}, "alert": set()}}
    assert r.tokens() == ["TOK"]


# --- kind ("voip" vs "alert") -------------------------------------------------

def test_register_defaults_to_voip_kind(tmp_path):
    p = str(tmp_path / "devices.json")
    r = DeviceRegistry(p)
    r.register("TOK", account="a")
    assert r.tokens("a") == ["TOK"]
    assert r.alert_tokens("a") == []


def test_register_alert_kind(tmp_path):
    p = str(tmp_path / "devices.json")
    r = DeviceRegistry(p)
    r.register("ATOK", account="a", kind="alert")
    assert r.alert_tokens("a") == ["ATOK"]
    assert r.tokens("a") == []
    # survives reload
    r2 = DeviceRegistry(p)
    assert r2.alert_tokens("a") == ["ATOK"]
    assert r2.tokens("a") == []


def test_invalid_kind_falls_back_to_voip(tmp_path):
    p = str(tmp_path / "devices.json")
    r = DeviceRegistry(p)
    r.register("TOK", account="a", kind="bogus")
    assert r.tokens("a") == ["TOK"]
    assert r.alert_tokens("a") == []


def test_reregister_same_token_different_kind_moves_it(tmp_path):
    # A token that re-registers as "alert" must stop counting as "voip" (and
    # vice versa), same one-slot-per-token rule extended across kinds.
    p = str(tmp_path / "devices.json")
    r = DeviceRegistry(p)
    r.register("TOK", account="a", kind="voip")
    r.register("TOK", account="a", kind="alert")
    assert r.tokens("a") == []
    assert r.alert_tokens("a") == ["TOK"]


def test_alert_tokens_none_is_deduped_across_accounts(tmp_path):
    p = str(tmp_path / "devices.json")
    r = DeviceRegistry(p)
    r.register("ATOK", account="a", kind="alert")
    r.register("ATOK", account="a", kind="alert")  # idempotent
    assert r.alert_tokens() == ["ATOK"]


def test_remove_deletes_from_either_kind(tmp_path):
    p = str(tmp_path / "devices.json")
    r = DeviceRegistry(p)
    r.register("VTOK", account="a", kind="voip")
    r.register("ATOK", account="a", kind="alert")
    r.remove("VTOK")
    r.remove("ATOK")
    assert r.tokens("a") == []
    assert r.alert_tokens("a") == []


# --- legacy on-disk shape migration -------------------------------------------

def test_migrates_legacy_flat_list(tmp_path):
    p = tmp_path / "devices.json"
    p.write_text(json.dumps(["AA", "BB"]))
    r = DeviceRegistry(str(p))
    assert sorted(r.tokens("")) == ["AA", "BB"]
    assert r.alert_tokens("") == []


def test_migrates_legacy_account_list_shape(tmp_path):
    p = tmp_path / "devices.json"
    p.write_text(json.dumps({"acct1": ["AA"], "acct2": ["BB", "CC"]}))
    r = DeviceRegistry(str(p))
    assert r.tokens("acct1") == ["AA"]
    assert sorted(r.tokens("acct2")) == ["BB", "CC"]
    assert r.alert_tokens("acct1") == []


def test_loads_new_shape_with_both_kinds(tmp_path):
    p = tmp_path / "devices.json"
    p.write_text(json.dumps({"acct1": {"voip": ["VV"], "alert": ["AA"]}}))
    r = DeviceRegistry(str(p))
    assert r.tokens("acct1") == ["VV"]
    assert r.alert_tokens("acct1") == ["AA"]
