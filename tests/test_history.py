import json
import os

import pytest

import server.history as history
from server.history import list_sessions, record_voice, session_detail


@pytest.fixture(autouse=True)
def _clear_scan_cache(monkeypatch, tmp_path_factory):
    # Every test builds its own tmp_path transcripts; a stale module-level
    # cache from a prior test (or prior run) must never leak in.
    monkeypatch.setattr(history, "_scan_cache", {})
    # Pin the persisted list-row index to a throwaway file so tests never read
    # or clobber the real ~/.voxa/history/index.json on the developer's machine.
    idx = tmp_path_factory.mktemp("hist_index") / "index.json"
    monkeypatch.setenv("VOXA_HISTORY_INDEX_FILE", str(idx))


def _write_transcript(projects_dir, enc_dir, name, entries):
    d = projects_dir / enc_dir
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return p


def _entry(etype, text, ts, cwd="/p/proj"):
    return {"type": etype, "timestamp": ts, "cwd": cwd,
            "message": {"role": etype, "content": text}}


def test_list_sessions_metadata_and_order(tmp_path):
    _write_transcript(tmp_path, "-p-proj", "s1", [
        _entry("user", "build the picker", "2026-07-01T10:00:00Z"),
        _entry("assistant", "done", "2026-07-01T10:05:00Z"),
    ])
    _write_transcript(tmp_path, "-p-other", "s2", [
        _entry("user", "fix the bug", "2026-07-02T09:00:00Z", cwd="/p/other"),
        _entry("assistant", "fixed", "2026-07-02T09:30:00Z", cwd="/p/other"),
    ])
    rows = list_sessions(projects_dir=str(tmp_path))
    assert [r["label"] for r in rows] == ["other", "proj"]   # newest ended first
    r = rows[0]
    assert r["id"] == "-p-other/s2"
    assert r["cwd"] == "/p/other"
    assert r["preview"] == "fix the bug"
    assert r["msg_count"] == 2
    assert r["started"] < r["ended"]


def test_list_sessions_skips_empty_and_corrupt(tmp_path):
    (tmp_path / "-p-junk").mkdir(parents=True)
    (tmp_path / "-p-junk" / "bad.jsonl").write_text("not json\n{}\n")
    _write_transcript(tmp_path, "-p-real", "ok", [
        _entry("user", "hi", "2026-07-01T10:00:00Z", cwd="/p/real")])
    rows = list_sessions(projects_dir=str(tmp_path))
    assert [r["id"] for r in rows] == ["-p-real/ok"]


def test_preview_skips_harness_injected_wrappers(tmp_path):
    # Real transcripts often start with <local-command-caveat>/<system-reminder>
    # noise; the preview must show the first HUMAN message instead.
    _write_transcript(tmp_path, "-p-proj", "s1", [
        _entry("user", "<local-command-caveat>Caveat: ...</local-command-caveat>",
               "2026-07-01T10:00:00Z"),
        _entry("user", "build the picker", "2026-07-01T10:00:05Z"),
    ])
    rows = list_sessions(projects_dir=str(tmp_path))
    assert rows[0]["preview"] == "build the picker"


def test_list_sessions_skips_invalid_utf8_file(tmp_path):
    # UnicodeDecodeError is a ValueError, not an OSError: one binary-corrupt
    # transcript must not take down the whole listing.
    d = tmp_path / "-p-bin"
    d.mkdir(parents=True)
    (d / "bad.jsonl").write_bytes(b'{"type": "user"}' + b"\xff\xfe\x00" * 40)
    _write_transcript(tmp_path, "-p-real", "ok", [
        _entry("user", "hi", "2026-07-01T10:00:00Z", cwd="/p/real")])
    rows = list_sessions(projects_dir=str(tmp_path))
    assert [r["id"] for r in rows] == ["-p-real/ok"]


def test_list_sessions_respects_limit(tmp_path):
    for i in range(5):
        _write_transcript(tmp_path, "-p-a", f"s{i}", [
            _entry("user", f"m{i}", f"2026-07-0{i + 1}T10:00:00Z", cwd="/p/a")])
    assert len(list_sessions(limit=3, projects_dir=str(tmp_path))) == 3


def test_record_voice_appends_and_is_fail_open(tmp_path):
    p = tmp_path / "voice.jsonl"
    record_voice("/p/proj", "user", "hello there", path=str(p))
    record_voice("/p/proj", "agent", "hi!", path=str(p))
    rows = [json.loads(l) for l in p.read_text().splitlines()]
    assert rows[0]["role"] == "user" and rows[0]["text"] == "hello there"
    assert rows[0]["cwd"] == "/p/proj" and "ts" in rows[0]
    # Fail-open: unwritable path (parent is a file) must not raise.
    (tmp_path / "blocker").write_text("")
    record_voice("/p/proj", "user", "x", path=str(tmp_path / "blocker" / "v.jsonl"))


def test_session_detail_interleaves_voice_by_window_and_cwd(tmp_path):
    _write_transcript(tmp_path, "-p-proj", "s1", [
        _entry("user", "build it", "2026-07-01T10:00:00Z"),
        _entry("assistant", "building", "2026-07-01T10:02:00Z"),
    ])
    voice = tmp_path / "voice.jsonl"
    # In-window, same cwd: appears. Other cwd or far outside window: filtered.
    from datetime import datetime, timezone
    t0 = datetime(2026, 7, 1, 10, 1, tzinfo=timezone.utc).timestamp()
    voice.write_text("\n".join(json.dumps(r) for r in [
        {"ts": t0, "cwd": "/p/proj", "role": "user", "text": "spoken ask"},
        {"ts": t0, "cwd": "/p/other", "role": "user", "text": "wrong cwd"},
        {"ts": t0 + 90000, "cwd": "/p/proj", "role": "agent", "text": "next day"},
    ]) + "\n")
    d = session_detail("-p-proj/s1", projects_dir=str(tmp_path), voice_path=str(voice))
    roles = [m["role"] for m in d["messages"]]
    assert roles == ["user", "voice_user", "assistant"]   # sorted by ts
    assert d["truncated"] is False
    texts = " ".join(m["text"] for m in d["messages"])
    assert "wrong cwd" not in texts and "next day" not in texts


def test_session_detail_caps_and_flags_truncation(tmp_path):
    entries = [_entry("user" if i % 2 == 0 else "assistant", f"m{i}",
                      f"2026-07-01T10:{i:02d}:00Z") for i in range(30)]
    _write_transcript(tmp_path, "-p-proj", "big", entries)
    d = session_detail("-p-proj/big", limit=10, projects_dir=str(tmp_path))
    assert len(d["messages"]) == 10 and d["truncated"] is True
    assert d["messages"][-1]["text"] == "m29"


def test_session_detail_rejects_traversal(tmp_path):
    assert "error" in session_detail("../../etc/passwd", projects_dir=str(tmp_path))
    assert "error" in session_detail("-p-x/nope", projects_dir=str(tmp_path))


def test_list_sessions_uses_cache(tmp_path, monkeypatch):
    _write_transcript(tmp_path, "-p-proj", "s1", [
        _entry("user", "hello", "2026-07-01T10:00:00Z")])
    first = list_sessions(projects_dir=str(tmp_path))
    assert len(first) == 1

    def _boom(path):
        raise AssertionError(f"_scan should not be called again for {path}")

    monkeypatch.setattr(history, "_scan", _boom)
    second = list_sessions(projects_dir=str(tmp_path))
    assert second == first


def test_list_sessions_rescans_on_mtime_change(tmp_path):
    p = _write_transcript(tmp_path, "-p-proj", "s1", [
        _entry("user", "hello", "2026-07-01T10:00:00Z")])
    first = list_sessions(projects_dir=str(tmp_path))
    assert first[0]["preview"] == "hello"

    p.write_text(json.dumps(_entry("user", "updated", "2026-07-01T10:05:00Z")) + "\n")
    os.utime(p, (os.path.getmtime(p) + 10, os.path.getmtime(p) + 10))
    second = list_sessions(projects_dir=str(tmp_path))
    assert second[0]["preview"] == "updated"


def test_list_sessions_early_stops(tmp_path, monkeypatch):
    for i in range(6):
        _write_transcript(tmp_path, "-p-a", f"s{i}", [
            _entry("user", f"m{i}", f"2026-07-0{i + 1}T10:00:00Z", cwd="/p/a")])
        # Stagger mtimes so newest-first order is deterministic.
        p = tmp_path / "-p-a" / f"s{i}.jsonl"
        os.utime(p, (i, i))

    calls = []
    real_scan = history._scan

    def counting_scan(path):
        calls.append(path)
        return real_scan(path)

    monkeypatch.setattr(history, "_scan", counting_scan)
    rows = list_sessions(limit=2, projects_dir=str(tmp_path))
    assert len(rows) == 2
    assert len(calls) <= 2


def test_scan_row_does_not_fully_parse_transcript(tmp_path, monkeypatch):
    # The listing only needs cwd + preview + first ts; it must NOT json-parse
    # every line of a multi-thousand-line transcript (the old full _scan did,
    # which is what made listing slow). Preview is on line 2, so parsing must
    # stop there regardless of how many lines follow.
    entries = [_entry("user", "<system-reminder>noise</system-reminder>",
                      "2026-07-01T10:00:00Z"),
               _entry("user", "the real ask", "2026-07-01T10:00:01Z")]
    entries += [_entry("assistant", f"reply {i}", "2026-07-01T10:00:02Z")
                for i in range(2000)]
    _write_transcript(tmp_path, "-p-proj", "big", entries)

    real_loads = history.json.loads
    calls = {"n": 0}

    def counting_loads(s, *a, **k):
        calls["n"] += 1
        return real_loads(s, *a, **k)

    monkeypatch.setattr(history.json, "loads", counting_loads)
    rows = history.list_sessions(projects_dir=str(tmp_path))
    assert rows[0]["preview"] == "the real ask"
    # Only the lines up to and including the preview line should be parsed,
    # never the full 2002-line file.
    assert calls["n"] <= 5


def test_scan_row_matches_row_shape(tmp_path):
    _write_transcript(tmp_path, "-p-proj", "s1", [
        _entry("user", "hello", "2026-07-01T10:00:00Z")])
    rows = list_sessions(projects_dir=str(tmp_path))
    assert set(rows[0]) == {
        "id", "cwd", "label", "started", "ended", "msg_count", "preview"}


def test_list_sessions_reuses_persisted_index_after_restart(tmp_path, monkeypatch):
    _write_transcript(tmp_path, "-p-proj", "s1", [
        _entry("user", "hello", "2026-07-01T10:00:00Z")])
    first = list_sessions(projects_dir=str(tmp_path))
    assert first[0]["preview"] == "hello"

    # Simulate a server restart: the in-memory cache is gone, but the on-disk
    # index survives, so an unchanged transcript must not be scanned again.
    history._scan_cache.clear()

    def _boom(path):
        raise AssertionError(f"_scan_row should not run for cached {path}")

    monkeypatch.setattr(history, "_scan_row", _boom)
    second = list_sessions(projects_dir=str(tmp_path))
    assert second == first


def test_persisted_index_invalidated_on_mtime_change(tmp_path, monkeypatch):
    p = _write_transcript(tmp_path, "-p-proj", "s1", [
        _entry("user", "hello", "2026-07-01T10:00:00Z")])
    list_sessions(projects_dir=str(tmp_path))

    p.write_text(json.dumps(_entry("user", "changed", "2026-07-01T10:05:00Z")) + "\n")
    os.utime(p, (os.path.getmtime(p) + 10, os.path.getmtime(p) + 10))
    history._scan_cache.clear()   # force the disk index to be consulted

    rescanned = []
    real = history._scan_row

    def spy(path):
        rescanned.append(path)
        return real(path)

    monkeypatch.setattr(history, "_scan_row", spy)
    rows = list_sessions(projects_dir=str(tmp_path))
    assert rows[0]["preview"] == "changed"
    assert len(rescanned) == 1   # the changed mtime forced a fresh scan


def test_corrupt_index_file_rebuilds_without_raising(tmp_path):
    idx = os.environ["VOXA_HISTORY_INDEX_FILE"]
    os.makedirs(os.path.dirname(idx), exist_ok=True)
    with open(idx, "w") as f:
        f.write("{ this is not valid json ]")
    _write_transcript(tmp_path, "-p-proj", "s1", [
        _entry("user", "hello", "2026-07-01T10:00:00Z")])
    rows = list_sessions(projects_dir=str(tmp_path))   # must not raise
    assert rows[0]["preview"] == "hello"


def test_missing_index_dir_write_is_fail_open(tmp_path, monkeypatch):
    # A parent that is a regular file makes the index unwritable; listing must
    # still succeed (fail-open) rather than propagate the OSError.
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    monkeypatch.setenv("VOXA_HISTORY_INDEX_FILE", str(blocker / "index.json"))
    _write_transcript(tmp_path, "-p-proj", "s1", [
        _entry("user", "hello", "2026-07-01T10:00:00Z")])
    rows = list_sessions(projects_dir=str(tmp_path))
    assert rows[0]["preview"] == "hello"


def test_voice_rows_coalesces_consecutive_same_role_deltas(monkeypatch):
    rows = [
        {"cwd": "/p/proj", "role": "user", "ts": 100.0, "text": "hel"},
        {"cwd": "/p/proj", "role": "user", "ts": 100.5, "text": "lo "},
        {"cwd": "/p/proj", "role": "user", "ts": 101.0, "text": "there"},
        {"cwd": "/p/proj", "role": "agent", "ts": 111.0, "text": "hi!"},
    ]

    class _FakeLog:
        def __init__(self, path):
            pass

        def read_all(self):
            return rows

    monkeypatch.setattr(history, "RotatingJsonlLog", _FakeLog)
    out = history._voice_rows("unused.jsonl", "/p/proj", 0.0, 1000.0)

    assert [r["role"] for r in out] == ["voice_user", "voice_agent"]
    assert out[0]["text"] == "hello there"
    assert out[0]["ts"] == 100.0
