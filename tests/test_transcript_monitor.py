import asyncio
import json
import os
import time

from server.transcript_monitor import TranscriptMonitor, transcript_state


def _write(path, entries):
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _proj(tmp_path, cwd="/Users/dev/proj"):
    d = tmp_path / cwd.replace("/", "-").replace(".", "-")
    d.mkdir(parents=True, exist_ok=True)
    return str(tmp_path), str(d / "s1.jsonl")


A_TEXT = {"type": "assistant", "message": {"role": "assistant",
          "content": [{"type": "text", "text": "All tests pass now."}]}}
A_TOOL = {"type": "assistant", "message": {"role": "assistant",
          "content": [{"type": "tool_use", "name": "Bash"}]}}
U_TEXT = {"type": "user", "message": {"role": "user", "content": "run the tests"}}


def test_state_done_when_quiet_and_last_is_assistant_text(tmp_path):
    projects, path = _proj(tmp_path)
    _write(path, [U_TEXT, A_TEXT])
    old = time.time() - 60
    os.utime(path, (old, old))
    kind, text = transcript_state(path, quiet_secs=5.0)
    assert kind == "done" and "All tests pass" in text


def test_state_working_when_file_is_fresh(tmp_path):
    projects, path = _proj(tmp_path)
    _write(path, [U_TEXT, A_TEXT])          # mtime = now
    kind, _ = transcript_state(path, quiet_secs=5.0)
    assert kind == "working"


def test_state_needs_input_when_quiet_mid_tool_call(tmp_path):
    projects, path = _proj(tmp_path)
    _write(path, [U_TEXT, A_TOOL])
    old = time.time() - 60
    os.utime(path, (old, old))
    kind, _ = transcript_state(path, quiet_secs=5.0)
    assert kind == "needs_input"


async def test_monitor_emits_once_after_work_burst(tmp_path):
    projects, path = _proj(tmp_path)
    _write(path, [U_TEXT])                   # session exists, no answer yet
    got = []
    mon = TranscriptMonitor("/Users/dev/proj", lambda t: got.append(t),
                            poll_interval=0.01, quiet_secs=0.05,
                            projects_dir=projects)
    await mon.start()
    await asyncio.sleep(0.05)
    _write(path, [U_TEXT, A_TEXT])           # Claude answers (mtime advances)
    await asyncio.sleep(0.3)                 # quiet period passes
    await mon.stop()
    assert len(got) == 1 and "All tests pass" in got[0]


async def test_monitor_silent_for_idle_session(tmp_path):
    projects, path = _proj(tmp_path)
    _write(path, [U_TEXT, A_TEXT])
    old = time.time() - 60
    os.utime(path, (old, old))               # already settled before we attach
    got = []
    mon = TranscriptMonitor("/Users/dev/proj", lambda t: got.append(t),
                            poll_interval=0.01, quiet_secs=0.05,
                            projects_dir=projects)
    await mon.start()
    await asyncio.sleep(0.2)
    await mon.stop()
    assert got == []                          # attaching must not ring the phone
