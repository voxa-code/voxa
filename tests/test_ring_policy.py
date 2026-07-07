"""RingScheduler: finish rings only after a quiet window; activity cancels."""
from __future__ import annotations

import asyncio

import pytest

from server.ring_policy import RingScheduler, pane_is_busy


class Recorder:
    def __init__(self):
        self.calls = []

    async def report(self, msg, *, kind="finish", cwd="", approval=None):
        self.calls.append({"msg": msg, "kind": kind, "cwd": cwd, "approval": approval})


async def test_finish_rings_after_quiet_window():
    r = Recorder()
    s = RingScheduler(r.report, quiet_seconds=0.05)
    await s.finish("sess1", "loop finished: done", "/p/loop")
    assert r.calls == []          # not yet: quiet window pending
    await asyncio.sleep(0.12)
    assert len(r.calls) == 1 and r.calls[0]["kind"] == "finish"


async def test_activity_inside_window_cancels_the_ring():
    r = Recorder()
    s = RingScheduler(r.report, quiet_seconds=0.05)
    await s.finish("sess1", "turn boundary", "/p/loop")
    s.note_activity("sess1")      # a PreToolUse arrived: still working
    await asyncio.sleep(0.12)
    assert r.calls == []


async def test_second_finish_replaces_the_first():
    r = Recorder()
    s = RingScheduler(r.report, quiet_seconds=0.05)
    await s.finish("sess1", "older summary", "/p/loop")
    await s.finish("sess1", "newer summary", "/p/loop")
    await asyncio.sleep(0.12)
    assert [c["msg"] for c in r.calls] == ["newer summary"]


async def test_activity_during_the_report_does_not_cancel_an_in_flight_ring():
    # Once the quiet window elapses and the ring has STARTED dialing, a late
    # note_activity (the session resuming at that exact moment) must NOT cancel
    # it: the call was already committed and dropping it is the mirror of the
    # bug this scheduler fixes. Real report() has await points (on_update, the
    # cloud POST); a report with no yield could never expose this race.
    started = asyncio.Event()
    release = asyncio.Event()
    delivered = []

    async def slow_report(msg, *, kind="finish", cwd="", approval=None):
        started.set()
        await release.wait()      # hold the ring open, mid-dial
        delivered.append(msg)

    s = RingScheduler(slow_report, quiet_seconds=0.05)
    await s.finish("sess1", "done", "/p/loop")
    await asyncio.sleep(0.12)      # window elapses, the ring begins dialing
    await asyncio.wait_for(started.wait(), 1.0)   # confirm we are mid-report
    s.note_activity("sess1")      # late activity arrives during the dial
    release.set()                 # let the ring complete
    await asyncio.sleep(0.02)
    assert delivered == ["done"]  # ring still delivered, not dropped


async def test_sessions_are_independent():
    r = Recorder()
    s = RingScheduler(r.report, quiet_seconds=0.05)
    await s.finish("sess1", "a finished", "/p/a")
    await s.finish("sess2", "b finished", "/p/b")
    s.note_activity("sess1")
    await asyncio.sleep(0.12)
    assert [c["msg"] for c in r.calls] == ["b finished"]


async def test_needs_input_rings_immediately_and_cancels_pending_finish():
    r = Recorder()
    s = RingScheduler(r.report, quiet_seconds=0.05)
    await s.finish("sess1", "turn boundary", "/p/loop")
    await s.needs_input("sess1", "loop needs input: pick one", "/p/loop")
    assert len(r.calls) == 1 and r.calls[0]["kind"] == "needs_input"
    await asyncio.sleep(0.12)
    assert len(r.calls) == 1      # the pending finish never fired


async def test_quiet_zero_reports_immediately():
    r = Recorder()
    s = RingScheduler(r.report, quiet_seconds=0)
    await s.finish("sess1", "done", "/p/loop")
    assert len(r.calls) == 1


def test_pane_is_busy_matches_default_markers_case_insensitively():
    assert pane_is_busy("... 2 Background Tasks ...")
    assert pane_is_busy("thinking (Esc to interrupt)")
    assert not pane_is_busy("$ waiting at a shell prompt")
    assert not pane_is_busy("")


def test_pane_is_busy_env_override(monkeypatch):
    monkeypatch.setenv("VOXA_BUSY_MARKERS", "my-marker, other")
    assert pane_is_busy("xx MY-MARKER yy")
    assert not pane_is_busy("background task")   # default set replaced
