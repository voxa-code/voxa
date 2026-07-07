import asyncio
import json

import pytest

from server.ax_controller import AXController, AX_PERMISSION_ERROR


def _seams(trusted=True, screen=""):
    posted, opened = [], []
    return {
        "poster": lambda pid, text: posted.append((pid, text)),
        "capturer": lambda pid: screen,
        "trusted": lambda: trusted,
        "opener": lambda url: opened.append(url),
    }, posted, opened


async def test_send_posts_keystrokes_to_the_app_pid():
    seams, posted, _ = _seams()
    c = AXController(700, "/Users/dev/proj", poll_interval=0.005, **seams)
    await c.start()
    await c.send("fix the bug")
    await c.stop()
    assert posted == [(700, "fix the bug")]


async def test_untrusted_start_raises_and_opens_settings():
    seams, _, opened = _seams(trusted=False)
    c = AXController(700, "/x", poll_interval=0.005, **seams)
    with pytest.raises(PermissionError) as e:
        await c.start()
    assert AX_PERMISSION_ERROR in str(e.value)
    assert opened and "Privacy_Accessibility" in opened[0]


async def test_send_before_start_raises():
    seams, posted, _ = _seams()
    c = AXController(700, "/x", **seams)
    with pytest.raises(ValueError):
        await c.send("hello")
    assert posted == []


async def test_screenless_app_uses_transcript_monitor(tmp_path):
    # capturer returns "" (GPU terminal): monitor must be the transcript one
    cwd = "/Users/dev/proj"
    d = tmp_path / cwd.replace("/", "-").replace(".", "-")
    d.mkdir(parents=True)
    path = d / "s1.jsonl"
    path.write_text("")
    seams, _, _ = _seams(screen="")
    got = []
    c = AXController(700, cwd, poll_interval=0.01, quiet_secs=0.05,
                     projects_dir=str(tmp_path), **seams)
    c.on_final(lambda t: got.append(t))
    await c.start()
    await asyncio.sleep(0.05)
    entry = {"type": "assistant", "message": {"role": "assistant",
             "content": [{"type": "text", "text": "Refactor finished."}]}}
    path.write_text(json.dumps(entry) + "\n")
    await asyncio.sleep(0.3)
    await c.stop()
    assert got and "Refactor finished" in got[0]


async def test_ax_screenfull_streams_output_and_scrollback():
    # seq[0] must be non-empty: the first capture (taken inside start(), before the
    # monitor task ever runs) is what decides mirrors_screen, so it has to look like
    # a readable AX screen already, not the "no AX text" ("") sentinel.
    seq = ["idle prompt", "Claude: refactor done"]
    state = {"i": 0}
    seams = {
        "poster": lambda pid, text: None,
        "capturer": lambda pid: seq[min(state["i"], len(seq) - 1)],
        "trusted": lambda: True,
        "opener": lambda url: None,
    }
    outputs = []
    c = AXController(700, "/x", poll_interval=0.005, idle_polls=2, **seams)
    c.on_output(lambda t: outputs.append(t))
    await c.start()
    # let the monitor loop's own baseline capture run before we move the screen on,
    # so the later poll actually observes a change (it is scheduled, not synchronous).
    await asyncio.sleep(0)
    # advance the capturer so the screen "changes"
    state["i"] = 1
    await asyncio.sleep(0.15)
    await c.stop()
    assert c.mirrors_screen is True
    assert any("refactor done" in o for o in outputs)
    assert "refactor done" in c.capture_scrollback()


async def test_ax_screenless_reports_no_live_view(tmp_path):
    seams = {
        "poster": lambda pid, text: None,
        "capturer": lambda pid: "",          # GPU terminal: no AX text
        "trusted": lambda: True,
        "opener": lambda url: None,
    }
    c = AXController(700, "/Users/dev/proj", poll_interval=0.01,
                     quiet_secs=0.05, projects_dir=str(tmp_path), **seams)
    await c.start()
    await asyncio.sleep(0.05)
    assert c.mirrors_screen is False
    assert c.capture_scrollback() == "Live view isn't available for this terminal."
    await c.stop()
