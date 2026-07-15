"""RingScheduler: finish rings only after a quiet window; activity cancels."""
from __future__ import annotations

import asyncio

import pytest

from server.ring_policy import ErrorBurst, RingScheduler, looks_like_error, pane_is_busy


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


def test_default_quiet_seconds_is_eight_when_env_unset(monkeypatch):
    # Hook traffic (UserPromptSubmit/PreToolUse) arrives within a couple of
    # seconds when work continues, and note_activity already cancels pending
    # rings, so the old 25s window only delayed real finishes.
    monkeypatch.delenv("VOXA_RING_QUIET_SECONDS", raising=False)
    r = Recorder()
    s = RingScheduler(r.report)
    assert s._quiet == 8.0


async def test_quiet_zero_reports_immediately():
    r = Recorder()
    s = RingScheduler(r.report, quiet_seconds=0)
    await s.finish("sess1", "done", "/p/loop")
    assert len(r.calls) == 1


# --- Instant ring mode (VOXA_RING_INSTANT) ---------------------------------

class CancelRecorder:
    """Fake cancel callable for RingScheduler(cancel=...): async, no args,
    just counts how many times it was actually awaited."""
    def __init__(self):
        self.calls = 0

    async def cancel(self):
        self.calls += 1


async def test_instant_mode_finish_reports_immediately(monkeypatch):
    monkeypatch.setenv("VOXA_RING_INSTANT", "1")
    r = Recorder()
    s = RingScheduler(r.report)
    await s.finish("sess1", "done", "/p/loop")
    assert len(r.calls) == 1 and r.calls[0]["kind"] == "finish"   # no sleep needed


async def test_instant_mode_activity_within_window_fires_cancel(monkeypatch):
    monkeypatch.setenv("VOXA_RING_INSTANT", "1")
    r = Recorder()
    cancel = CancelRecorder()
    s = RingScheduler(r.report, cancel=cancel.cancel)
    await s.finish("sess1", "done", "/p/loop")
    s.note_activity("sess1")      # follow-on hook activity: the finish was premature
    for _ in range(3):
        await asyncio.sleep(0)    # let the fire-and-forget ensure_future run
    assert cancel.calls == 1


async def test_instant_mode_activity_other_session_does_not_cancel(monkeypatch):
    monkeypatch.setenv("VOXA_RING_INSTANT", "1")
    r = Recorder()
    cancel = CancelRecorder()
    s = RingScheduler(r.report, cancel=cancel.cancel)
    await s.finish("sess1", "done", "/p/loop")
    s.note_activity("sess2")      # a DIFFERENT session: must not cancel sess1's ring
    for _ in range(3):
        await asyncio.sleep(0)
    assert cancel.calls == 0


async def test_instant_mode_activity_after_cancel_window_does_not_cancel(monkeypatch):
    monkeypatch.setenv("VOXA_RING_INSTANT", "1")
    monkeypatch.setenv("VOXA_RING_CANCEL_WINDOW", "0.05")
    r = Recorder()
    cancel = CancelRecorder()
    s = RingScheduler(r.report, cancel=cancel.cancel)
    await s.finish("sess1", "done", "/p/loop")
    await asyncio.sleep(0.12)     # well past the (tiny) cancel window
    s.note_activity("sess1")
    for _ in range(3):
        await asyncio.sleep(0)
    assert cancel.calls == 0


async def test_instant_mode_needs_input_after_finish_never_cancels_and_still_reports(monkeypatch):
    # finish() then needs_input() for the SAME session must not fire the
    # cancel (needs_input pops the recent-ring record silently instead of
    # going through note_activity's cancel-firing path), and the needs_input
    # report itself must still go out.
    monkeypatch.setenv("VOXA_RING_INSTANT", "1")
    r = Recorder()
    cancel = CancelRecorder()
    s = RingScheduler(r.report, cancel=cancel.cancel)
    await s.finish("sess1", "done", "/p/loop")
    await s.needs_input("sess1", "needs input: pick one", "/p/loop")
    for _ in range(3):
        await asyncio.sleep(0)
    assert cancel.calls == 0
    assert [c["kind"] for c in r.calls] == ["finish", "needs_input"]


async def test_default_mode_finish_still_waits_for_the_quiet_window(monkeypatch):
    # Instant mode is opt-in: with the env unset, finish() must keep today's
    # behavior of waiting behind a pending task instead of reporting right away.
    monkeypatch.delenv("VOXA_RING_INSTANT", raising=False)
    r = Recorder()
    s = RingScheduler(r.report, quiet_seconds=0.05)
    await s.finish("sess1", "done", "/p/loop")
    assert r.calls == []
    assert "sess1" in s._pending


def test_pane_is_busy_matches_default_markers_case_insensitively():
    assert pane_is_busy("... 2 Background Tasks ...")
    assert pane_is_busy("thinking (Esc to interrupt)")
    assert not pane_is_busy("$ waiting at a shell prompt")
    assert not pane_is_busy("")


def test_pane_is_busy_env_override(monkeypatch):
    monkeypatch.setenv("VOXA_BUSY_MARKERS", "my-marker, other")
    assert pane_is_busy("xx MY-MARKER yy")
    assert not pane_is_busy("background task")   # default set replaced


# --- ErrorBurst -----------------------------------------------------------

class FakeClock:
    """A controllable clock for ErrorBurst's now_fn: .t is the current time,
    advanced explicitly by the test instead of sleeping real seconds."""
    def __init__(self, start: float = 0.0):
        self.t = start

    def __call__(self) -> float:
        return self.t


def test_error_burst_not_triggered_below_threshold():
    clock = FakeClock()
    burst = ErrorBurst(threshold=3, window_seconds=60, now_fn=clock)
    burst.note_error()
    burst.note_error()
    assert burst.triggered() is False   # only 2 of 3 needed


def test_error_burst_triggers_once_threshold_reached_within_window():
    clock = FakeClock()
    burst = ErrorBurst(threshold=3, window_seconds=60, now_fn=clock)
    burst.note_error(); clock.t += 1
    burst.note_error(); clock.t += 1
    burst.note_error()
    assert burst.triggered() is True
    # Same burst, immediately re-checked: cooldown suppresses the repeat.
    assert burst.triggered() is False


def test_error_burst_events_spread_beyond_window_do_not_trigger():
    clock = FakeClock()
    burst = ErrorBurst(threshold=3, window_seconds=10, now_fn=clock)
    burst.note_error()          # t=0
    clock.t += 20
    burst.note_error()          # t=20, the t=0 event has aged out of the 10s window
    clock.t += 1
    burst.note_error()          # t=21
    # Only 2 events remain inside the trailing 10s window (t=20 and t=21).
    assert burst.triggered() is False


def test_error_burst_cooldown_suppresses_repeats_then_allows_a_new_burst():
    clock = FakeClock()
    burst = ErrorBurst(threshold=2, window_seconds=10, cooldown_seconds=5, now_fn=clock)
    burst.note_error(); burst.note_error()
    assert burst.triggered() is True     # first burst reports
    clock.t += 1
    burst.note_error(); burst.note_error()
    assert burst.triggered() is False    # still within cooldown: suppressed
    # Let the original events age out of the window entirely (burst over), then
    # a genuinely fresh burst must be able to report right away.
    clock.t += 30
    burst.note_error(); burst.note_error()
    assert burst.triggered() is True


# --- looks_like_error -------------------------------------------------------

def test_looks_like_error_hits():
    assert looks_like_error("Traceback (most recent call last):\n  File x")
    assert looks_like_error("ValueError: invalid literal for int()")
    assert looks_like_error("error: could not compile foo.c")
    assert looks_like_error("fatal: not a git repository")
    assert looks_like_error("npm ERR! code ENOENT")
    assert looks_like_error("zsh: command not found: fooo")
    assert looks_like_error("Process exited with code 1")
    assert looks_like_error("2 tests FAILED, 8 passed")


def test_looks_like_error_misses_normal_prose():
    assert not looks_like_error("")
    assert not looks_like_error("There was an error in my earlier approach, fixing it now")
    assert not looks_like_error("This function handles error recovery gracefully")
    assert not looks_like_error("All 10 tests passed, no failures")
    assert not looks_like_error("Let's terraform the error handling module")
