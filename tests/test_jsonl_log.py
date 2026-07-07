import json

from server.jsonl_log import RotatingJsonlLog


def test_append_and_read_all(tmp_path):
    log = RotatingJsonlLog(str(tmp_path / "l.jsonl"))
    log.append({"a": 1})
    log.append({"a": 2})
    assert log.read_all() == [{"a": 1}, {"a": 2}]


def test_read_recent_limits_to_last_n(tmp_path):
    log = RotatingJsonlLog(str(tmp_path / "l.jsonl"))
    for i in range(5):
        log.append({"i": i})
    assert log.read_recent(2) == [{"i": 3}, {"i": 4}]


def test_rotates_when_over_max_bytes(tmp_path):
    path = tmp_path / "l.jsonl"
    log = RotatingJsonlLog(str(path), max_bytes=40)
    log.append({"a": "xxxxxxxxxx"})   # first line, under the cap alone
    log.append({"a": "yyyyyyyyyy"})   # pushes the file over max_bytes -> rotates
    assert path.with_suffix(".jsonl.1").exists()
    # both records still readable across the rotation boundary
    records = log.read_all()
    assert {"a": "xxxxxxxxxx"} in records
    assert {"a": "yyyyyyyyyy"} in records


def test_skips_malformed_lines(tmp_path):
    path = tmp_path / "l.jsonl"
    path.write_text('{"a": 1}\nnot json\n{"a": 2}\n')
    log = RotatingJsonlLog(str(path))
    assert log.read_all() == [{"a": 1}, {"a": 2}]
