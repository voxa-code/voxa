"""Tests for the disk-backed per-project task queue store.

Mirrors the session_state / notify_rules pytest style: tmp_path for the backing
file, monkeypatch for the env override and the fail-open write case. These lock
the store's contract before the QueueRunner (Task 2) drives it, so the semantics
that matter to the runner (insertion order, single running item, bounded
history, restart-as-pending) are pinned independently of the wiring.
"""
from server.task_queue import TaskQueue


def test_add_returns_item_and_preserves_order(tmp_path):
    q = TaskQueue(str(tmp_path / "q.json"))
    a = q.add("/p/loop", "first")
    b = q.add("/p/loop", "second")
    assert a["state"] == "queued"
    assert a["text"] == "first"
    assert len(a["id"]) == 12
    assert isinstance(a["created_at"], float)
    assert a["id"] != b["id"]
    assert [i["text"] for i in q.items("/p/loop")] == ["first", "second"]


def test_items_isolated_by_cwd(tmp_path):
    q = TaskQueue(str(tmp_path / "q.json"))
    q.add("/p/one", "x")
    q.add("/p/two", "y")
    assert [i["text"] for i in q.items("/p/one")] == ["x"]
    assert [i["text"] for i in q.items("/p/two")] == ["y"]


def test_pop_next_marks_running_oldest_first(tmp_path):
    q = TaskQueue(str(tmp_path / "q.json"))
    q.add("/p/loop", "first")
    q.add("/p/loop", "second")
    popped = q.pop_next("/p/loop")
    assert popped["text"] == "first"
    assert popped["state"] == "running"
    # Still visible via items() (queued + running), still first.
    states = {i["text"]: i["state"] for i in q.items("/p/loop")}
    assert states == {"first": "running", "second": "queued"}


def test_pop_next_returns_none_when_no_queued(tmp_path):
    q = TaskQueue(str(tmp_path / "q.json"))
    assert q.pop_next("/p/loop") is None
    q.add("/p/loop", "only")
    q.pop_next("/p/loop")            # now running
    # No further queued items -> a running item is not popped again.
    assert q.pop_next("/p/loop") is None


def test_finish_moves_to_outcomes_and_drain_clears(tmp_path):
    q = TaskQueue(str(tmp_path / "q.json"))
    item = q.add("/p/loop", "task")
    q.pop_next("/p/loop")
    q.finish(item["id"], "done", "shipped it")
    # No longer in the live queue.
    assert q.items("/p/loop") == []
    drained = q.drain_outcomes("/p/loop")
    assert len(drained) == 1
    assert drained[0]["outcome"] == "done"
    assert drained[0]["summary"] == "shipped it"
    assert drained[0]["text"] == "task"
    # Drain clears: a second drain is empty.
    assert q.drain_outcomes("/p/loop") == []


def test_drain_outcomes_does_not_touch_queue(tmp_path):
    q = TaskQueue(str(tmp_path / "q.json"))
    done = q.add("/p/loop", "done-one")
    q.pop_next("/p/loop")
    q.finish(done["id"], "done")
    q.add("/p/loop", "still-queued")
    assert q.drain_outcomes("/p/loop")[0]["text"] == "done-one"
    # The queued item survives the outcome drain.
    assert [i["text"] for i in q.items("/p/loop")] == ["still-queued"]


def test_history_bounded_at_20(tmp_path):
    q = TaskQueue(str(tmp_path / "q.json"))
    for n in range(25):
        item = q.add("/p/loop", f"t{n}")
        q.pop_next("/p/loop")
        q.finish(item["id"], "done")
    drained = q.drain_outcomes("/p/loop")
    assert len(drained) == 20
    # The most recent 20 survive (t5..t24), oldest evicted.
    assert [d["text"] for d in drained] == [f"t{n}" for n in range(5, 25)]


def test_finish_rejects_bad_outcome(tmp_path):
    import pytest
    q = TaskQueue(str(tmp_path / "q.json"))
    item = q.add("/p/loop", "task")
    q.pop_next("/p/loop")
    with pytest.raises(ValueError):
        q.finish(item["id"], "maybe")


def test_remove_only_affects_queued(tmp_path):
    q = TaskQueue(str(tmp_path / "q.json"))
    running = q.add("/p/loop", "running-one")
    queued = q.add("/p/loop", "queued-one")
    q.pop_next("/p/loop")            # running-one -> running
    assert q.remove(running["id"]) is False   # cannot remove running
    assert q.remove(queued["id"]) is True
    assert q.remove("nonexistent") is False
    assert [i["text"] for i in q.items("/p/loop")] == ["running-one"]


def test_remove_of_finished_returns_false(tmp_path):
    q = TaskQueue(str(tmp_path / "q.json"))
    item = q.add("/p/loop", "task")
    q.pop_next("/p/loop")
    q.finish(item["id"], "done")
    assert q.remove(item["id"]) is False


def test_move_reorders_queued_only(tmp_path):
    q = TaskQueue(str(tmp_path / "q.json"))
    a = q.add("/p/loop", "a")
    q.add("/p/loop", "b")
    c = q.add("/p/loop", "c")
    assert q.move(c["id"], 0) is True
    assert [i["text"] for i in q.items("/p/loop")] == ["c", "a", "b"]
    # Index clamps into range.
    assert q.move(c["id"], 99) is True
    assert [i["text"] for i in q.items("/p/loop")] == ["a", "b", "c"]
    assert q.move("nonexistent", 0) is False
    # A running item is not reorderable.
    q.pop_next("/p/loop")            # a -> running (a is first now)
    assert q.move(a["id"], 2) is False


def test_flush_returns_count_and_empties(tmp_path):
    q = TaskQueue(str(tmp_path / "q.json"))
    q.add("/p/loop", "a")
    q.add("/p/loop", "b")
    q.pop_next("/p/loop")            # one running, one queued
    assert q.flush("/p/loop") == 2   # both queued + running dropped
    assert q.items("/p/loop") == []
    assert q.flush("/p/loop") == 0


def test_flush_leaves_other_cwds(tmp_path):
    q = TaskQueue(str(tmp_path / "q.json"))
    q.add("/p/one", "x")
    q.add("/p/two", "y")
    assert q.flush("/p/one") == 1
    assert [i["text"] for i in q.items("/p/two")] == ["y"]


def test_pending_counts_shape(tmp_path):
    q = TaskQueue(str(tmp_path / "q.json"))
    q.add("/p/one", "a")
    q.add("/p/one", "b")
    q.pop_next("/p/one")             # queued + running both count
    q.add("/p/two", "c")
    # A cwd whose items all finished contributes nothing.
    done = q.add("/p/three", "d")
    q.pop_next("/p/three")
    q.finish(done["id"], "done")
    counts = q.pending_counts()
    assert counts == {"/p/one": 2, "/p/two": 1}


def test_restart_roundtrip_running_demoted_to_queued(tmp_path):
    # A running item on disk has nothing driving it after a restart, so a fresh
    # instance must treat it as pending (queued) again, not stuck-running.
    p = str(tmp_path / "q.json")
    q = TaskQueue(p)
    q.add("/p/loop", "first")
    q.add("/p/loop", "second")
    q.pop_next("/p/loop")            # first -> running, persisted
    reborn = TaskQueue(p)
    states = {i["text"]: i["state"] for i in reborn.items("/p/loop")}
    assert states == {"first": "queued", "second": "queued"}
    assert reborn.pending_counts() == {"/p/loop": 2}


def test_restart_preserves_undrained_outcomes(tmp_path):
    p = str(tmp_path / "q.json")
    q = TaskQueue(p)
    item = q.add("/p/loop", "task")
    q.pop_next("/p/loop")
    q.finish(item["id"], "needs_input", "which file?")
    reborn = TaskQueue(p)
    drained = reborn.drain_outcomes("/p/loop")
    assert drained[0]["outcome"] == "needs_input"
    assert drained[0]["summary"] == "which file?"


def test_corrupt_file_yields_empty_store(tmp_path):
    p = tmp_path / "q.json"
    p.write_text("{not json")
    q = TaskQueue(str(p))
    assert q.items("/p/loop") == []
    assert q.pending_counts() == {}
    # And it can still take new work without raising.
    q.add("/p/loop", "recovered")
    assert [i["text"] for i in q.items("/p/loop")] == ["recovered"]


def test_missing_file_yields_empty_store(tmp_path):
    q = TaskQueue(str(tmp_path / "does-not-exist.json"))
    assert q.items("/p/loop") == []
    assert q.pending_counts() == {}


def test_env_var_path_override(tmp_path, monkeypatch):
    p = tmp_path / "env-q.json"
    monkeypatch.setenv("VOXA_TASK_QUEUE_FILE", str(p))
    TaskQueue().add("/p/loop", "via-env")
    assert p.exists()
    # A second env-configured instance reads the same file.
    assert [i["text"] for i in TaskQueue().items("/p/loop")] == ["via-env"]


def test_fail_open_write_does_not_raise(tmp_path):
    # Block persistence with a FILE where the parent directory must go; mutations
    # must not raise and in-memory state must still be consistent for the caller.
    (tmp_path / "blocker").write_text("")
    q = TaskQueue(str(tmp_path / "blocker" / "q.json"))
    item = q.add("/p/loop", "task")          # must not raise
    assert [i["text"] for i in q.items("/p/loop")] == ["task"]
    assert q.pop_next("/p/loop")["state"] == "running"
    q.finish(item["id"], "done")             # must not raise
    assert q.drain_outcomes("/p/loop")[0]["outcome"] == "done"


def test_trailing_slash_normalized(tmp_path):
    q = TaskQueue(str(tmp_path / "q.json"))
    q.add("/p/loop/", "x")
    assert [i["text"] for i in q.items("/p/loop")] == ["x"]
    assert q.pop_next("/p/loop")["text"] == "x"
    q.add("/p/loop", "y")
    assert q.flush("/p/loop/") == 2
    assert q.pending_counts() == {}
