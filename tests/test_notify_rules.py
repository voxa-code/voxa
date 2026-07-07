from server.notify_rules import NotifyRules


def test_default_is_ring(tmp_path):
    r = NotifyRules(str(tmp_path / "rules.json"))
    assert r.mode("/p/loop", "finish") == "ring"


def test_set_and_persist(tmp_path):
    p = str(tmp_path / "rules.json")
    NotifyRules(p).set_mode("/p/loop", "finish", "silent")
    r2 = NotifyRules(p)
    assert r2.mode("/p/loop", "finish") == "silent"
    assert r2.mode("/p/loop", "needs_input") == "ring"
    assert r2.all() == {"/p/loop": {"finish": "silent"}}


def test_invalid_values_rejected(tmp_path):
    import pytest
    r = NotifyRules(str(tmp_path / "rules.json"))
    with pytest.raises(ValueError):
        r.set_mode("/p/loop", "bogus", "silent")
    with pytest.raises(ValueError):
        r.set_mode("/p/loop", "finish", "maybe")


def test_corrupt_file_is_tolerated(tmp_path):
    p = tmp_path / "rules.json"
    p.write_text("{broken")
    assert NotifyRules(str(p)).mode("/p/loop", "finish") == "ring"


def test_save_failure_is_silent(tmp_path):
    (tmp_path / "blocker").write_text("")
    r = NotifyRules(str(tmp_path / "blocker" / "rules.json"))
    r.set_mode("/p/loop", "finish", "silent")   # must not raise (fail-open write)


def test_trailing_slash_normalized(tmp_path):
    r = NotifyRules(str(tmp_path / "rules.json"))
    r.set_mode("/p/loop/", "finish", "silent")
    assert r.mode("/p/loop", "finish") == "silent"


# --- global default (per-cwd silent stays the mute list) -------------------------

def test_global_default_falls_back_to_ring(tmp_path):
    r = NotifyRules(str(tmp_path / "rules.json"))
    assert r.mode("/p/x", "finish") == "ring"
    assert r.defaults() == {"finish": "ring", "needs_input": "ring"}


def test_set_default_applies_when_no_per_cwd_override(tmp_path):
    p = str(tmp_path / "rules.json")
    NotifyRules(p).set_default("finish", "silent")
    r2 = NotifyRules(p)
    assert r2.mode("/p/any", "finish") == "silent"        # global default resolves
    assert r2.mode("/p/any", "needs_input") == "ring"     # other kind untouched
    assert r2.defaults() == {"finish": "silent", "needs_input": "ring"}


def test_per_cwd_override_beats_global_default(tmp_path):
    r = NotifyRules(str(tmp_path / "rules.json"))
    r.set_default("finish", "silent")
    r.set_mode("/p/loud", "finish", "ring")               # per-cwd override wins
    assert r.mode("/p/loud", "finish") == "ring"
    assert r.mode("/p/other", "finish") == "silent"       # global still applies


def test_set_default_rejects_invalid(tmp_path):
    import pytest
    r = NotifyRules(str(tmp_path / "rules.json"))
    with pytest.raises(ValueError):
        r.set_default("bogus", "silent")
    with pytest.raises(ValueError):
        r.set_default("finish", "maybe")


def test_default_key_cannot_collide_with_a_real_cwd(tmp_path):
    # A real cwd is an absolute path (normalizes with a leading "/"), so it can never
    # equal the reserved "__default__" key. Even cwd "/" (normalizes to "") stays
    # distinct from the global-default section.
    r = NotifyRules(str(tmp_path / "rules.json"))
    r.set_default("finish", "silent")
    r.set_mode("/", "finish", "ring")
    assert r.mode("/", "finish") == "ring"                # the "/" per-cwd rule
    assert r.mode("/p/anything", "finish") == "silent"    # global default intact
    assert r.all().get("__default__") == {"finish": "silent"}
