import asyncio

import pytest

from server.terminal_watcher import (
    TerminalWatcher, _PassiveWatch, _first_meaningful_line, classify_screen,
)
from server.tmux_controller import monitor_loop


def test_first_meaningful_line_skips_blanks():
    assert _first_meaningful_line("\n  \n  Done refactoring auth\n") == "Done refactoring auth"
    assert _first_meaningful_line("") == ""


def test_classify_numbered_menu_is_needs_input():
    kind, summary = classify_screen(
        "Do you trust the files in this folder?\n  1. Yes, proceed\n  2. No, exit"
    )
    assert kind == "needs_input"
    assert "trust" in summary.lower()


def test_classify_yes_no_prompt_is_needs_input():
    kind, _ = classify_screen("Allow edit to config.py? (y/n)")
    assert kind == "needs_input"


def test_classify_question_is_needs_input():
    kind, _ = classify_screen("Which port should the dev server use?")
    assert kind == "needs_input"


def test_classify_plain_result_is_finished():
    kind, summary = classify_screen("Created index.html and wired up the button.")
    assert kind == "finished"
    assert summary == "Created index.html and wired up the button."


async def test_passive_watch_emits_on_completion():
    """A session that WORKS (shows the running marker) then stabilises fires _emit."""
    # baseline idle -> working (esc to interrupt) -> a result that stabilises -> emit.
    frames = iter(["> ready", "Building it\nesc to interrupt",
                   "Created page.html", "Created page.html", "Created page.html"])
    last = {"v": "> ready"}

    def fake_run(args):
        try:
            last["v"] = next(frames)
        except StopIteration:
            pass
        return last["v"]

    emitted = []
    sess = {"id": "tmux::s", "raw_id": "s", "backend": "tmux", "label": "proj", "cwd": "/p"}

    def on_emit(session, text):
        emitted.append((session["label"], text))

    w = _PassiveWatch(sess, on_emit, run=fake_run, poll_interval=0.01, idle_polls=2)
    await asyncio.wait_for(_run_until(lambda: emitted, w), timeout=2)
    assert emitted and emitted[0][0] == "proj"


async def test_passive_watch_ignores_fresh_boot():
    """A session that just boots to its idle prompt (never works, no prompt) must NOT
    emit, so starting a session does not ring the phone."""
    frames = iter(["", "Welcome to Claude Code", "> ready to help",
                   "> ready to help", "> ready to help", "> ready to help"])
    last = {"v": ""}

    def fake_run(args):
        try:
            last["v"] = next(frames)
        except StopIteration:
            pass
        return last["v"]

    emitted = []
    sess = {"id": "tmux::b", "raw_id": "b", "backend": "tmux", "label": "boot", "cwd": "/b"}
    w = _PassiveWatch(sess, lambda s, t: emitted.append(t), run=fake_run,
                      poll_interval=0.01, idle_polls=2)
    task = asyncio.ensure_future(monitor_loop(w))
    await asyncio.sleep(0.2)
    w._started = False
    task.cancel()
    assert emitted == []


async def _run_until(cond, watch):
    task = asyncio.ensure_future(monitor_loop(watch))
    try:
        while not cond():
            await asyncio.sleep(0.01)
    finally:
        watch._started = False
        task.cancel()


async def test_reconcile_spawns_and_skips():
    sessions = [
        {"id": "tmux::a", "raw_id": "a", "backend": "tmux", "label": "a", "cwd": "/a"},
        {"id": "tmux::b", "raw_id": "b", "backend": "tmux", "label": "b", "cwd": "/b"},
    ]
    spawned = []

    async def fake_monitor(watch):
        spawned.append(watch._session["id"])
        await asyncio.sleep(10)

    w = TerminalWatcher(lambda *a: None, discover=lambda run, osa: sessions,
                        monitor=fake_monitor)
    await w.reconcile_once()
    await asyncio.sleep(0.02)      # let the spawned monitor tasks start
    assert set(spawned) == {"tmux::a", "tmux::b"}
    # Idempotent: a second pass with same sessions spawns nothing new.
    await w.reconcile_once()
    await asyncio.sleep(0.02)
    assert len(spawned) == 2
    for t in w._watchers.values():
        t.cancel()


async def test_resume_watch_fires_cancel_when_working_returns():
    sess = {"id": "tmux::r", "raw_id": "r", "backend": "tmux", "label": "r", "cwd": "/r"}

    def fake_run(args):
        return "esc to interrupt, working"   # working marker present

    resumed = []
    w = TerminalWatcher(
        lambda l, c, s: None,
        on_resumed=lambda l, c: resumed.append((l, c)),
        run=fake_run,
        resume_window=0.05, resume_poll=0.01,
    )
    await w._watch_resume(sess)
    assert resumed == [("r", "/r")]


async def test_resume_watch_no_cancel_when_idle():
    sess = {"id": "tmux::i", "raw_id": "i", "backend": "tmux", "label": "i", "cwd": "/i"}
    w = TerminalWatcher(
        lambda l, c, s: None,
        on_resumed=lambda l, c: (_ for _ in ()).throw(AssertionError("should not fire")),
        run=lambda args: "all done, nothing running",
        resume_window=0.03, resume_poll=0.01,
    )
    await w._watch_resume(sess)  # screen stays idle -> no resume


async def test_skipped_session_does_not_report():
    sess = {"id": "tmux::x", "raw_id": "x", "backend": "tmux", "label": "x", "cwd": "/x"}
    reported = []
    w = TerminalWatcher(lambda l, c, s: reported.append(l),
                        should_skip=lambda session: session["cwd"] == "/x")
    await w._handle_emit(sess, "all done")
    assert reported == []          # skipped (actively driven elsewhere)
    w2 = TerminalWatcher(lambda l, c, s: reported.append(l))
    await w2._handle_emit(sess, "all done")
    assert reported == ["x"]       # not skipped -> reported


async def test_watcher_uses_transcript_monitor_for_ax_sessions(tmp_path):
    import json, time, os
    from server.terminal_watcher import TerminalWatcher
    import server.terminal_watcher as tw

    cwd = "/Users/dev/proj"
    d = tmp_path / cwd.replace("/", "-").replace(".", "-")
    d.mkdir(parents=True)
    path = d / "s1.jsonl"
    path.write_text("")

    ax = {"id": "ax:ttys010", "raw_id": "ttys010", "cwd": cwd, "label": "veil",
          "app": "Ghostty", "backend": "ax", "app_pid": "700",
          "controllable": True}
    done = []
    w = TerminalWatcher(lambda label, c, s: done.append((label, s)),
                        discover=lambda run, osa: [ax],
                        poll_interval=0.01,
                        projects_dir=str(tmp_path))
    w._quiet = 0.05
    await w.reconcile_once()
    await asyncio.sleep(0.05)
    entry = {"type": "assistant", "message": {"role": "assistant",
             "content": [{"type": "text", "text": "Deployed the fix."}]}}
    path.write_text(json.dumps(entry) + "\n")
    await asyncio.sleep(0.4)
    for t in w._watchers.values():
        t.cancel()
    assert done and "Deployed the fix" in done[0][1]
