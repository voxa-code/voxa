import asyncio
import json

import pytest

from server.ax_controller import AXController, AX_PERMISSION_ERROR


def _seams(trusted=True, screen=""):
    posted, opened, keys = [], [], []
    return {
        "poster": lambda pid, text: posted.append((pid, text)),
        "key_poster": lambda pid, code: keys.append((pid, code)),
        "capturer": lambda pid: screen,
        "trusted": lambda: trusted,
        "opener": lambda url: opened.append(url),
    }, posted, opened, keys


async def test_send_mirrors_screen_false_posts_best_effort_second_return():
    # screen="" -> the first capture inside start() is empty, so mirrors_screen is
    # False (a GPU terminal with no AX text): send() has nothing to verify against,
    # so it mirrors the tmux no-capture fallback and posts one best-effort EXTRA
    # Return, reporting the send optimistically.
    seams, posted, _, keys = _seams()
    c = AXController(700, "/Users/dev/proj", poll_interval=0.005, **seams)
    await c.start()
    ok = await c.send("fix the bug")
    await c.stop()
    assert posted == [(700, "fix the bug")]     # poster types ONLY the text now
    assert keys == [(700, 36), (700, 36)]       # first Return + one best-effort extra
    assert ok is True


async def test_untrusted_start_raises_and_opens_settings():
    seams, _, opened, _ = _seams(trusted=False)
    c = AXController(700, "/x", poll_interval=0.005, **seams)
    with pytest.raises(PermissionError) as e:
        await c.start()
    assert AX_PERMISSION_ERROR in str(e.value)
    assert opened and "Privacy_Accessibility" in opened[0]


async def test_send_before_start_raises():
    seams, posted, _, _ = _seams()
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
    seams, _, _, _ = _seams(screen="")
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


async def test_ax_interrupt_posts_escape_keycode():
    # The screen confirms the interrupt, so exactly ONE bare Escape is posted
    # (no text/Return, no retry).
    keys = []
    c = AXController(42, "/p/loop",
                     poster=lambda pid, text: None,
                     key_poster=lambda pid, code: keys.append((pid, code)),
                     capturer=lambda pid: "⎿ Interrupted · What should Claude do instead?",
                     trusted=lambda: True,
                     poll_interval=0.005, idle_polls=2)
    await c.start()
    c.status = "working"
    await c.interrupt()
    assert keys == [(42, 53)]                 # bare Escape, no text/Return
    assert c._started is True and c.status == "idle"
    await c.stop()


async def test_ax_interrupt_retries_while_unconfirmed():
    # A static, promptless screen never confirms the interrupt (vim INSERT mode
    # can eat the first Escape), so the press is retried up to twice more.
    keys = []
    c = AXController(42, "/p/loop",
                     poster=lambda pid, text: None,
                     key_poster=lambda pid, code: keys.append((pid, code)),
                     capturer=lambda pid: "screen",
                     trusted=lambda: True,
                     poll_interval=0.005, idle_polls=2)
    await c.start()
    c.status = "working"
    await c.interrupt()
    assert keys == [(42, 53)] * 3
    assert c._started is True and c.status == "idle"
    await c.stop()


async def test_ax_interrupt_noop_before_attach():
    keys = []
    c = AXController(42, "/p/loop",
                     poster=lambda pid, text: None,
                     key_poster=lambda pid, code: keys.append((pid, code)),
                     capturer=lambda pid: "screen",
                     trusted=lambda: True)
    await c.interrupt()
    assert keys == []


# --- reliable submit: type/Return split, verify + retry (the send-reliability bug) -


async def test_send_types_text_then_posts_return_separately(monkeypatch):
    # Typing must not bake Return into the same burst (that's the send-reliability
    # bug: a busy TUI can absorb a Return arriving mid-burst as a newline instead of
    # a submit). Return goes through key_poster, and the pane already shows no trace
    # of the typed text, so a single Return is enough to confirm submission.
    monkeypatch.setenv("VOXA_SEND_SETTLE_SECONDS", "0")
    posted, keys = [], []
    c = AXController(700, "/Users/dev/proj", poll_interval=5.0,
                     poster=lambda pid, text: posted.append((pid, text)),
                     key_poster=lambda pid, code: keys.append((pid, code)),
                     capturer=lambda pid: "already idle, nothing typed",
                     trusted=lambda: True, opener=lambda url: None)
    await c.start()
    ok = await c.send("fix the bug")
    await c.stop()
    assert posted == [(700, "fix the bug")]   # poster typed ONLY the text
    assert keys == [(700, 36)]                # Return posted separately, exactly once
    assert ok is True


async def test_send_retries_return_until_limit_then_reports_false(monkeypatch):
    # The typed tail sits in the bottom input region FOREVER (the Return keeps
    # getting swallowed): every retry fires another Return, and once the budget
    # is exhausted send() reports False so a silently-stuck instruction can be
    # noticed instead of assumed sent.
    monkeypatch.setenv("VOXA_SEND_SETTLE_SECONDS", "0")
    monkeypatch.setenv("VOXA_SEND_ENTER_RETRIES", "2")
    keys = []
    typed = "push the approved change to the remote"
    pending_screen = "> " + typed
    c = AXController(700, "/Users/dev/proj", poll_interval=5.0,
                     poster=lambda pid, text: None,
                     key_poster=lambda pid, code: keys.append((pid, code)),
                     capturer=lambda pid: pending_screen,
                     trusted=lambda: True, opener=lambda url: None)
    await c.start()
    ok = await c.send(typed)
    await c.stop()
    assert ok is False
    # first Return + 2 retries (VOXA_SEND_ENTER_RETRIES=2), still pending every time
    assert keys == [(700, 36)] * 3


async def test_send_returns_true_once_pane_clears_after_one_retry(monkeypatch):
    # The pane clears only once the SECOND Return has actually landed: the fake
    # capturer keys off how many Returns key_poster has recorded so far, which is
    # robust to the live monitor task (started by start()) sneaking in extra reads
    # of the pane in the background (it never posts keys, only reads).
    monkeypatch.setenv("VOXA_SEND_SETTLE_SECONDS", "0")
    monkeypatch.setenv("VOXA_SEND_ENTER_RETRIES", "3")
    keys = []
    typed = "run the release checklist"

    def capturer(pid):
        return ("idle, ready for the next command" if len(keys) >= 2
                else f"> {typed}")

    c = AXController(700, "/Users/dev/proj", poll_interval=5.0,
                     poster=lambda pid, text: None,
                     key_poster=lambda pid, code: keys.append((pid, code)),
                     capturer=capturer,
                     trusted=lambda: True, opener=lambda url: None)
    await c.start()
    ok = await c.send(typed)
    await c.stop()
    assert ok is True
    assert keys == [(700, 36), (700, 36)]   # exactly 2 Returns: initial + one retry


# --- press(): actuate an approval/menu keypress in an AX-driven terminal -------


async def test_press_digit_posts_via_poster_text_path():
    # A single approval-option digit ("1") is not a named special key: it goes
    # through the TEXT poster (typed, no Return), not key_poster.
    seams, posted, _, keys = _seams(screen="anything")
    c = AXController(700, "/x", poll_interval=0.005, **seams)
    await c.start()
    c.mirrors_screen = False   # isolate the posting path from the verify/retry logic
    await c.press("1")
    await c.stop()
    assert posted == [(700, "1")]
    assert keys == []


async def test_press_enter_posts_keycode_via_key_poster():
    seams, posted, _, keys = _seams(screen="anything")
    c = AXController(700, "/x", poll_interval=0.005, **seams)
    await c.start()
    c.mirrors_screen = False
    await c.press("enter")
    await c.stop()
    assert keys == [(700, 36)]
    assert posted == []


async def test_press_unmappable_named_key_raises_valueerror():
    # "ctrl-c" is a real tmux PRESS_KEY_NAMES entry, but it needs a modifier
    # flag AX keystroke injection can't post: press() must reject it instead
    # of posting the wrong thing.
    seams, posted, _, keys = _seams()
    c = AXController(700, "/x", poll_interval=0.005, **seams)
    await c.start()
    with pytest.raises(ValueError):
        await c.press("ctrl-c")
    assert posted == [] and keys == []
    await c.stop()


async def test_press_before_start_raises():
    seams, posted, _, keys = _seams()
    c = AXController(700, "/x", **seams)
    with pytest.raises(ValueError):
        await c.press("1")
    assert posted == [] and keys == []


async def test_press_unchanged_pane_triggers_exactly_one_repost():
    # A static, promptless pane never confirms delivery, so press() posts the
    # key ONE extra time (never more: a changed pane means it landed).
    keys = []
    c = AXController(42, "/p/loop",
                     poster=lambda pid, text: None,
                     key_poster=lambda pid, code: keys.append((pid, code)),
                     capturer=lambda pid: "static screen, never changes",
                     trusted=lambda: True, opener=lambda url: None,
                     poll_interval=0.005)
    c._press_verify_secs = 0
    await c.start()
    await c.press("enter")
    await c.stop()
    assert keys == [(42, 36), (42, 36)]   # initial press + exactly one repost


async def test_press_changed_pane_does_not_repost():
    # The pane text changes between the pre- and post-press captures (the key
    # landed and moved the menu on): NEVER repost, or a second "1" could act
    # on whatever screen came next.
    keys = []

    def capturer(pid):
        return "before the press" if len(keys) < 1 else "after the press"

    c = AXController(42, "/p/loop",
                     poster=lambda pid, text: None,
                     key_poster=lambda pid, code: keys.append((pid, code)),
                     capturer=capturer,
                     trusted=lambda: True, opener=lambda url: None,
                     poll_interval=0.005)
    c._press_verify_secs = 0
    await c.start()
    await c.press("enter")
    await c.stop()
    assert keys == [(42, 36)]   # exactly one press, no repost


async def test_press_mirrors_screen_false_posts_once_no_capture_retry():
    # A GPU terminal with no AX text to verify against: post once, and never
    # touch the capturer from inside press() at all.
    capture_calls = []
    posted = []

    def capturer(pid):
        capture_calls.append(pid)
        return ""   # empty -> mirrors_screen becomes False in start()

    c = AXController(700, "/x", poll_interval=0.005,
                     poster=lambda pid, text: posted.append((pid, text)),
                     key_poster=lambda pid, code: None,
                     capturer=capturer,
                     trusted=lambda: True, opener=lambda url: None)
    await c.start()
    assert c.mirrors_screen is False
    calls_after_start = len(capture_calls)
    await c.press("1")
    await c.stop()
    assert posted == [(700, "1")]
    assert len(capture_calls) == calls_after_start   # press() never re-captured


async def test_send_honors_settle_seconds_before_first_return(monkeypatch):
    # A non-zero settle window must actually be awaited before the FIRST Return is
    # posted (the whole point: give the TUI time to render the typed text so Return
    # lands as a submit, not a newline).
    monkeypatch.setenv("VOXA_SEND_SETTLE_SECONDS", "0.05")
    import time
    keys, times = [], []

    def key_poster(pid, code):
        times.append(time.monotonic())
        keys.append((pid, code))

    c = AXController(700, "/Users/dev/proj", poll_interval=5.0,
                     poster=lambda pid, text: None,
                     key_poster=key_poster,
                     capturer=lambda pid: "idle, nothing pending",
                     trusted=lambda: True, opener=lambda url: None)
    await c.start()
    t0 = time.monotonic()
    ok = await c.send("check the settle window")
    await c.stop()
    assert ok is True
    assert times and (times[0] - t0) >= 0.04


# --- verify_working: verify-on-read for AX-driven terminals ------------------

def _vw_ax(capturer):
    import time as _t
    ax = AXController(1, "/p/x", poster=lambda pid, t: None,
                      key_poster=lambda pid, k: None, capturer=capturer,
                      trusted=lambda: True, opener=lambda u: None)
    ax._started = True
    ax.mirrors_screen = True
    ax.status = "working"
    ax._last_send_at = _t.monotonic()
    return ax


async def test_ax_verify_working_true_while_generating(monkeypatch):
    monkeypatch.setenv("VOXA_BUSY_GRACE_SECONDS", "0")
    ax = _vw_ax(lambda pid: "out\n* Brewing (3s - esc to interrupt)\n")
    assert await ax.verify_working() is True
    assert ax.status == "working"


async def test_ax_verify_working_heals_stale_flag(monkeypatch):
    monkeypatch.setenv("VOXA_BUSY_GRACE_SECONDS", "0")
    ax = _vw_ax(lambda pid: "done.\n> \n")
    assert await ax.verify_working() is False
    assert ax.status == "idle"


async def test_ax_verify_working_no_screen_trusted_before_decay(monkeypatch):
    monkeypatch.setenv("VOXA_BUSY_GRACE_SECONDS", "0")
    monkeypatch.setenv("VOXA_BUSY_DECAY_SECONDS", "300")
    ax = _vw_ax(lambda pid: "")
    ax.mirrors_screen = False
    assert await ax.verify_working() is True
    assert ax.status == "working"


async def test_ax_verify_working_no_screen_decays_to_idle(monkeypatch):
    import time as _t
    monkeypatch.setenv("VOXA_BUSY_GRACE_SECONDS", "0")
    monkeypatch.setenv("VOXA_BUSY_DECAY_SECONDS", "0.01")
    ax = _vw_ax(lambda pid: "")
    ax.mirrors_screen = False
    ax._last_send_at = _t.monotonic() - 1
    assert await ax.verify_working() is False
    assert ax.status == "idle"
