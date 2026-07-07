from server.session import Session, SessionRegistry


def _mk(sid="s1", cwd=None):
    controller = object()
    if cwd is not None:
        controller = type("Ctrl", (), {"working_dir": cwd})()
    return Session(sid, controller=controller, hub=object(), call_manager=object())


def test_add_get_and_default():
    reg = SessionRegistry()
    assert reg.default() is None
    s = reg.add(_mk("abc"))
    assert reg.get("abc") is s
    assert reg.default() is s
    assert reg.all() == [s]


def test_default_is_first_added():
    reg = SessionRegistry()
    first = reg.add(_mk("one"))
    reg.add(_mk("two"))
    assert reg.default() is first


def test_remove_is_idempotent():
    reg = SessionRegistry()
    reg.add(_mk("gone"))
    reg.remove("gone")
    reg.remove("gone")          # second remove must not raise
    assert reg.get("gone") is None
    assert reg.default() is None


def test_pending_source_starts_none_and_holds_dict():
    reg = SessionRegistry()
    assert reg.pending_source is None
    reg.push_pending("/p/proj")
    assert reg.pending_source == {"cwd": "/p/proj"}


def test_session_controller_is_mutable():
    # The driven controller swaps in place when the user attaches to a different
    # terminal; the session must follow the swap.
    s = _mk()
    new_ctrl = object()
    s.controller = new_ctrl
    assert s.controller is new_ctrl


# --- active() / set_active() -----------------------------------------------------

def test_active_falls_back_to_default_when_unset():
    reg = SessionRegistry()
    assert reg.active() is None
    s = reg.add(_mk("one"))
    assert reg.active() is s   # falls back to default() (first added)


def test_set_active_wins_over_default():
    reg = SessionRegistry()
    first = reg.add(_mk("one"))
    second = reg.add(_mk("two"))
    reg.set_active(second.id)
    assert reg.active() is second
    assert reg.default() is first   # default() semantics unchanged


def test_active_id_stored_on_registry():
    reg = SessionRegistry()
    reg.add(_mk("one"))
    assert reg.active_id is None
    reg.set_active("one")
    assert reg.active_id == "one"


def test_active_falls_back_when_active_id_points_nowhere():
    # e.g. the active session was removed; active() must not raise or return None
    # while another session exists.
    reg = SessionRegistry()
    first = reg.add(_mk("one"))
    reg.set_active("gone")
    assert reg.active() is first


# --- find_by_cwd -------------------------------------------------------------------

def test_find_by_cwd_matches_rstrip_normalized():
    reg = SessionRegistry()
    s = reg.add(_mk("one", cwd="/p/proj/"))
    assert reg.find_by_cwd("/p/proj") is s
    assert reg.find_by_cwd("/p/proj/") is s


def test_find_by_cwd_no_match_returns_none():
    reg = SessionRegistry()
    reg.add(_mk("one", cwd="/p/proj"))
    assert reg.find_by_cwd("/p/other") is None
    assert reg.find_by_cwd("") is None


def test_find_by_cwd_picks_the_right_one_of_several():
    reg = SessionRegistry()
    reg.add(_mk("one", cwd="/p/one"))
    two = reg.add(_mk("two", cwd="/p/two"))
    assert reg.find_by_cwd("/p/two") is two


def test_find_by_cwd_ignores_session_without_working_dir():
    reg = SessionRegistry()
    reg.add(_mk("one"))   # controller = object(), no working_dir attribute
    assert reg.find_by_cwd("/p/proj") is None


# --- push_pending / pop_pending: per-cwd, no-clobber -------------------------------

def test_pop_pending_returns_and_clears():
    reg = SessionRegistry()
    reg.push_pending("/p/a")
    assert reg.pop_pending() == {"cwd": "/p/a"}
    assert reg.pop_pending() is None


def test_pop_pending_on_empty_returns_none():
    reg = SessionRegistry()
    assert reg.pop_pending() is None


def test_two_cwd_pending_no_clobber_most_recent_pops_first():
    # A second hook (different cwd) firing while the first is still unanswered must
    # NOT wipe out the first: each cwd keeps its own slot until popped.
    reg = SessionRegistry()
    reg.push_pending("/p/a")
    reg.push_pending("/p/b")
    assert reg.pop_pending() == {"cwd": "/p/b"}   # most recent first
    assert reg.pop_pending() == {"cwd": "/p/a"}   # older one survived
    assert reg.pop_pending() is None


def test_push_pending_same_cwd_twice_moves_to_most_recent():
    reg = SessionRegistry()
    reg.push_pending("/p/a")
    reg.push_pending("/p/b")
    reg.push_pending("/p/a")   # re-push moves it back to "most recent"
    assert reg.pop_pending() == {"cwd": "/p/a"}
    assert reg.pop_pending() == {"cwd": "/p/b"}


def test_pending_source_peeks_most_recent_without_clearing():
    # Back-compat read-only view used by callers/tests that inspect
    # `.pending_source` directly instead of popping it.
    reg = SessionRegistry()
    reg.push_pending("/p/a")
    reg.push_pending("/p/b")
    assert reg.pending_source == {"cwd": "/p/b"}
    assert reg.pending_source == {"cwd": "/p/b"}   # peek: unchanged, not cleared
    assert reg.pop_pending() == {"cwd": "/p/b"}
