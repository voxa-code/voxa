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
    r._by_account = {"a": {"TOK"}, "b": {"TOK"}}
    assert r.tokens() == ["TOK"]
