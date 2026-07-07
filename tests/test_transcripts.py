import json

from server.transcripts import recap, latest_transcript


def _write(proj_dir, name, events):
    p = proj_dir / name
    p.write_text("\n".join(json.dumps(e) for e in events))
    return p


def test_recap_reads_recent_messages(tmp_path):
    proj = tmp_path / "-Users-dev-Documents-JOSA"
    proj.mkdir()
    _write(proj, "sess.jsonl", [
        {"type": "permission-mode", "permissionMode": "bypassPermissions"},
        {"type": "user", "message": {"role": "user", "content": "build the landing page"}},
        {"type": "assistant", "message": {"role": "assistant",
         "content": [{"type": "text", "text": "Done, created index.html"}]}},
        {"type": "user", "message": {"role": "user",
         "content": [{"type": "tool_result", "content": "ok"}]}},  # no text -> skipped
    ])
    out = recap("/Users/dev/Documents/JOSA", projects_dir=str(tmp_path))
    assert "build the landing page" in out
    assert "created index.html" in out
    assert "You:" in out and "Claude:" in out


def test_recap_picks_newest_transcript(tmp_path):
    proj = tmp_path / "-Users-dev-app"
    proj.mkdir()
    old = _write(proj, "old.jsonl", [
        {"type": "user", "message": {"role": "user", "content": "old work"}}])
    new = _write(proj, "new.jsonl", [
        {"type": "user", "message": {"role": "user", "content": "newest work"}}])
    import os
    os.utime(old, (1, 1))            # make 'old' older
    assert latest_transcript("/Users/dev/app", projects_dir=str(tmp_path)) == str(new)
    out = recap("/Users/dev/app", projects_dir=str(tmp_path))
    assert "newest work" in out and "old work" not in out


def test_recap_no_transcript(tmp_path):
    assert recap("/no/such/place", projects_dir=str(tmp_path)) == ""


from server.transcripts import read_session


def _mk(tmp_path, msgs, cwd="/Users/dev/proj"):
    import json
    d = tmp_path / cwd.replace("/", "-").replace(".", "-")
    d.mkdir(parents=True, exist_ok=True)
    p = d / "s1.jsonl"
    with open(p, "w") as f:
        for role, text in msgs:
            f.write(json.dumps({"type": role, "message":
                                {"role": role, "content": text}}) + "\n")
    return str(tmp_path)


def test_recap_includes_session_opener(tmp_path):
    msgs = [("user", "build the login page")] + \
           [("assistant", f"step {i}") for i in range(40)]
    projects = _mk(tmp_path, msgs)
    out = recap("/Users/dev/proj", projects_dir=projects)
    assert out.startswith("This session started with: build the login page")
    assert "step 39" in out            # recent messages still present


def test_read_session_last_n(tmp_path):
    msgs = [("user", f"q{i}") for i in range(60)]
    projects = _mk(tmp_path, msgs)
    res = read_session("/Users/dev/proj", last=5, projects_dir=projects)
    assert [m["text"] for m in res["messages"]] == ["q55", "q56", "q57", "q58", "q59"]


def test_read_session_last_capped_at_40(tmp_path):
    msgs = [("user", f"q{i}") for i in range(60)]
    projects = _mk(tmp_path, msgs)
    res = read_session("/Users/dev/proj", last=999, projects_dir=projects)
    assert len(res["messages"]) == 40


def test_read_session_search_returns_hits_with_neighbours(tmp_path):
    msgs = [("user", "alpha"), ("assistant", "the auth token expired"),
            ("user", "beta"), ("assistant", "gamma")]
    projects = _mk(tmp_path, msgs)
    res = read_session("/Users/dev/proj", search="auth token",
                       projects_dir=projects)
    texts = [m["text"] for m in res["messages"]]
    assert "the auth token expired" in texts
    assert "alpha" in texts and "beta" in texts   # neighbours included


def test_read_session_result_capped_by_bytes(tmp_path):
    msgs = [("assistant", "x" * 2000) for _ in range(30)]
    projects = _mk(tmp_path, msgs)
    res = read_session("/Users/dev/proj", last=40, projects_dir=projects,
                       max_bytes=6000)
    import json as j
    assert len(j.dumps(res).encode()) <= 6500     # small envelope slack


def test_read_session_no_transcript(tmp_path):
    res = read_session("/nope", last=5, projects_dir=str(tmp_path))
    assert "error" in res
