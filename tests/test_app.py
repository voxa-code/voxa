import pytest
from contextlib import asynccontextmanager
from starlette.testclient import TestClient
from server.app import create_app
from server.config import Config


class FakeOperator:
    def __init__(self, config, handle_tool_call):
        self.handle = handle_tool_call
        self._out = None
    def set_audio_out(self, cb): self._out = cb
    def set_text_out(self, cb): self._text_out = cb
    async def send_audio(self, pcm): self.received = pcm
    async def speak(self, text, immediate=False): self.spoke = text
    def suppress_greeting(self): self.suppressed = True
    async def send_text(self, text): self.said = text
    async def run(self):
        import asyncio
        await asyncio.sleep(0.01)  # idle until disconnect

@asynccontextmanager
async def fake_factory(config, handle_tool_call, voice=""):
    yield FakeOperator(config, handle_tool_call)


def make_client():
    cfg = Config("k", "model", "secret", "127.0.0.1", 8787)
    return TestClient(create_app(cfg, operator_factory=fake_factory))


def test_healthz():
    assert make_client().get("/healthz").json() == {"ok": True}

def test_index_served():
    r = make_client().get("/")
    assert r.status_code == 200
    assert "<html" in r.text.lower()

def test_ws_rejects_bad_token():
    client = make_client()
    with pytest.raises(Exception):
        with client.websocket_connect("/ws?token=wrong"):
            pass

def test_ws_accepts_good_token():
    client = make_client()
    with client.websocket_connect("/ws?token=secret") as ws:
        ws.send_bytes(b"\x00\x01")   # should not raise


def _cfg():
    return Config(gemini_api_key="k", gemini_live_model="m",
                  auth_token="t", host="127.0.0.1", port=8787)

def test_register_account_scoped():
    # Account-scoped, no shared token needed (zero-config client): the unguessable
    # account id is the scope, and the cloud cannot verify a per-laptop token.
    app = create_app(_cfg(), operator_factory=lambda c, h, voice="": object())
    client = TestClient(app)
    assert client.post("/register", json={"token": "X", "account": "a"}).status_code == 200
    assert "X" in app.state.registry.tokens("a")

def test_decline_ok():
    app = create_app(_cfg(), operator_factory=lambda c, h, voice="": object())
    client = TestClient(app)
    assert client.post("/call/decline", json={"call_id": "c1"}).status_code == 200

def test_unregister_removes_token(tmp_path, monkeypatch):
    monkeypatch.setenv("VOXA_DEVICES_FILE", str(tmp_path / "devices.json"))
    app = create_app(_cfg(), operator_factory=lambda c, h, voice="": object())
    client = TestClient(app)
    client.post("/register", json={"token": "AA", "account": "a"})
    assert "AA" in app.state.registry.tokens()
    assert client.post("/unregister", json={"token": "AA"}).status_code == 200
    assert "AA" not in app.state.registry.tokens()

def test_voice_param_reaches_factory(tmp_path, monkeypatch):
    monkeypatch.setenv("VOXA_DEVICES_FILE", str(tmp_path / "devices.json"))
    captured = {}
    @asynccontextmanager
    async def cap_factory(config, handle, voice=""):
        captured["voice"] = voice
        yield FakeOperator(config, handle)
    cfg = Config("k", "m", "secret", "127.0.0.1", 8787)
    client = TestClient(create_app(cfg, operator_factory=cap_factory))
    with client.websocket_connect("/ws?token=secret&voice=Kore") as ws:
        ws.send_bytes(b"\x00")
    assert captured.get("voice") == "Kore"

def test_account_param_reaches_factory(tmp_path, monkeypatch):
    """The paired phone's account (forwarded by the bridge as ?account=) reaches
    a factory that accepts it, so the metered session bills that balance."""
    monkeypatch.setenv("VOXA_DEVICES_FILE", str(tmp_path / "devices.json"))
    captured = {}
    @asynccontextmanager
    async def cap_factory(config, handle, voice="", account=""):
        captured["account"] = account
        yield FakeOperator(config, handle)
    cfg = Config("k", "m", "secret", "127.0.0.1", 8787)
    client = TestClient(create_app(cfg, operator_factory=cap_factory))
    with client.websocket_connect("/ws?token=secret&account=user-77") as ws:
        ws.send_bytes(b"\x00")
    assert captured.get("account") == "user-77"

def test_session_gated_until_begin(tmp_path, monkeypatch):
    """The metered operator must not be created until the client sends `begin`
    (or starts talking). On connect the server first sends a `ready` status."""
    monkeypatch.setenv("VOXA_DEVICES_FILE", str(tmp_path / "devices.json"))
    created = {"n": 0}
    @asynccontextmanager
    async def counting_factory(config, handle, voice="", account=""):
        created["n"] += 1
        yield FakeOperator(config, handle)
    cfg = Config("k", "m", "secret", "127.0.0.1", 8787)
    client = TestClient(create_app(cfg, operator_factory=counting_factory))
    with client.websocket_connect("/ws?token=secret") as ws:
        first = ws.receive_json()                 # server announces readiness
        assert first["status"] == "ready"
        assert created["n"] == 0                  # nothing metered yet
        ws.send_text('{"type":"begin"}')          # user taps Start
        ws.send_bytes(b"\x00")
    assert created["n"] == 1                       # operator opened only after begin

async def test_say_control_calls_send_text():
    from server.app import handle_client_control
    sent = []
    class FakeOrch:
        async def handle_tool_call(self, n, a): return {}
        def set_terminal_app(self, a): pass
    class FakeWS:
        async def send_json(self, m): pass
    class FakeOp:
        async def send_text(self, t): sent.append(t)
    await handle_client_control('{"type":"say","text":"hello"}', FakeOrch(), FakeWS(), FakeOp())
    assert sent == ["hello"]


async def test_list_dirs_control_returns_subfolders(tmp_path):
    import json
    from server.app import handle_client_control
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / ".hidden").mkdir()          # dotfiles are omitted
    sent = []

    class FakeOrch:
        async def handle_tool_call(self, n, a): return {}
        def set_terminal_app(self, a): pass

    class FakeWS:
        async def send_json(self, m): sent.append(m)

    await handle_client_control(
        json.dumps({"type": "list_dirs", "path": str(tmp_path)}), FakeOrch(), FakeWS())
    assert len(sent) == 1
    msg = sent[0]
    assert msg["type"] == "dirs"
    assert msg["path"] == str(tmp_path)
    assert msg["dirs"] == ["alpha", "beta"]   # sorted, hidden excluded


async def test_list_dirs_nonexistent_falls_back_to_ancestor(tmp_path):
    import json
    from server.app import handle_client_control
    (tmp_path / "real").mkdir()
    sent = []

    class FakeOrch:
        async def handle_tool_call(self, n, a): return {}
        def set_terminal_app(self, a): pass

    class FakeWS:
        async def send_json(self, m): sent.append(m)

    # Ask for a path whose tail doesn't exist -> resolves to the deepest real ancestor.
    await handle_client_control(
        json.dumps({"type": "list_dirs", "path": str(tmp_path / "nope" / "deeper")}),
        FakeOrch(), FakeWS())
    assert sent[0]["path"] == str(tmp_path)
    assert "real" in sent[0]["dirs"]


async def test_stop_control_calls_stop_claude():
    import json
    from server.app import handle_client_control
    calls = []
    class FakeOrch:
        async def handle_tool_call(self, n, a): calls.append((n, a)); return {}
        def set_terminal_app(self, a): pass
    class FakeWS:
        async def send_json(self, m): pass
    await handle_client_control(json.dumps({"type": "stop"}), FakeOrch(), FakeWS())
    assert ("stop_claude", {}) in calls


async def test_screenshot_control_calls_take_screenshot():
    import json
    from server.app import handle_client_control
    calls = []
    class FakeOrch:
        async def handle_tool_call(self, n, a): calls.append((n, a)); return {}
        def set_terminal_app(self, a): pass
    class FakeWS:
        async def send_json(self, m): pass
    await handle_client_control(json.dumps({"type": "screenshot"}), FakeOrch(), FakeWS())
    assert ("take_screenshot", {}) in calls


async def test_claude_key_control_presses_named_key():
    import json
    from server.app import handle_client_control
    pressed = []

    class FakeOrch:
        async def handle_tool_call(self, n, a): return {}
        def set_terminal_app(self, a): pass
        async def press_key(self, k): pressed.append(k); return {"pressed": k}

    class FakeWS:
        def __init__(self): self.sent = []
        async def send_json(self, m): self.sent.append(m)

    ws = FakeWS()
    await handle_client_control(
        json.dumps({"type": "claude_key", "key": "up"}), FakeOrch(), ws)
    assert pressed == ["up"]
    assert ws.sent == []            # minimal-reply idiom: no status on success


async def test_claude_key_control_unknown_key_is_ignored_with_status():
    import json
    from server.app import handle_client_control
    pressed = []

    class FakeOrch:
        async def handle_tool_call(self, n, a): return {}
        def set_terminal_app(self, a): pass
        async def press_key(self, k): pressed.append(k); return {"pressed": k}

    class FakeWS:
        def __init__(self): self.sent = []
        async def send_json(self, m): self.sent.append(m)

    ws = FakeWS()
    await handle_client_control(
        json.dumps({"type": "claude_key", "key": "pagedown"}), FakeOrch(), ws)
    assert pressed == []            # never reached orchestrator.press_key
    assert ws.sent[-1]["type"] == "status"
    assert "ignored" in ws.sent[-1]["status"].lower()


async def test_claude_key_control_does_not_go_through_approval_validation():
    # claude_key has no approval_id/options concept: it must not be reachable
    # via, or interfere with, the approval_decision branch's option check.
    import json
    from server.app import handle_client_control
    from server.approvals import build_approval
    from server.notifier import Notifier
    pressed = []

    class FakeOrch:
        controller = type("C", (), {"working_dir": "/p/loop"})()
        async def handle_tool_call(self, n, a): return {}
        def set_terminal_app(self, a): pass
        async def press_key(self, k): pressed.append(k); return {"pressed": k}

    class FakeWS:
        def __init__(self): self.sent = []
        async def send_json(self, m): self.sent.append(m)

    n = Notifier(_FakeCM(), push_enabled=True, ring_debounce=0)
    a = build_approval("/p/loop", "s", "> 1. Yes\n  2. No")
    n.approvals.put(a)
    ws = FakeWS()
    # "enter" is a valid claude_key name but is NOT one of this approval's option
    # keys ("1"/"2"); it must still be pressed (claude_key skips option validation)
    # and the approval itself must be left untouched (not resolved).
    await handle_client_control(
        json.dumps({"type": "claude_key", "key": "enter"}), FakeOrch(), ws, notifier=n)
    assert pressed == ["enter"]
    assert ws.sent == []
    assert n.approvals.get(a["approval_id"]) is not None   # untouched, not resolved


# --- /notify ring vs cancel routing -------------------------------------------
from fastapi import FastAPI
from server.push_routes import add_push_routes


class _FakeCM:
    def __init__(self):
        self.rang = []
        self.cancelled = []
    async def ring(self, account, summary, approval=None):
        self.rang.append((account, summary))
    async def cancel(self, account):
        self.cancelled.append(account)


class _FakeReg:
    def register(self, *a, **k): pass
    def remove(self, *a, **k): pass


def test_notify_cancel_calls_cancel():
    app = FastAPI()
    cm = _FakeCM()
    add_push_routes(app, _FakeReg(), cm)
    client = TestClient(app)
    r = client.post("/notify", json={"account": "acct", "cancel": True})
    assert r.json() == {"ok": True}
    assert cm.cancelled == ["acct"]
    assert cm.rang == []


def test_notify_summary_still_rings():
    app = FastAPI()
    cm = _FakeCM()
    add_push_routes(app, _FakeReg(), cm)
    client = TestClient(app)
    client.post("/notify", json={"account": "acct", "summary": "done"})
    assert cm.rang == [("acct", "done")]


from server.app import (should_suppress_greeting, apply_greeting_suppression,
                        compose_opening, _strip_finished_prefix)


def test_strip_finished_prefix():
    assert _strip_finished_prefix("loop finished: added the picker") == "added the picker"
    assert _strip_finished_prefix("loop finished") == ""           # no result detail
    assert _strip_finished_prefix("loop needs input: pick 1 or 2") == "loop needs input: pick 1 or 2"


def test_compose_opening_leads_with_project_and_result():
    o = compose_opening("loop", ["loop finished: added the folder picker"])
    assert o == ("Hi. Your last task in loop just finished. "
                 "Here's what it did: added the folder picker. What would you like to do next?")
    # Doesn't start with the old generic greeting.
    assert "what would you like to do" not in o.lower().split("finished")[0]


def test_compose_opening_project_without_detail():
    o = compose_opening("veil", ["veil finished"])
    assert o == ("Hi. You're back in veil. Your last task there just finished. "
                 "What would you like to do next?")


def test_compose_opening_project_with_no_updates_claims_no_finish():
    # A fresh connect with NOTHING pending must not fabricate "your last task
    # just finished" (a brand-new session heard that lie on its first hello).
    o = compose_opening("veil", [])
    assert o == "Hi. You're back in veil. What would you like to do next?"
    assert "finished" not in o


def test_compose_opening_no_project_with_detail():
    o = compose_opening("", ["app finished: shipped the fix"])
    assert o.startswith("Hi. Your last task just finished. Here's what it did: shipped the fix.")


def test_compose_opening_empty_falls_back():
    assert compose_opening("", []) == "Hi. You're back. What would you like to do next?"


def test_suppress_greeting_when_pending():
    assert should_suppress_greeting(["a finished"]) is True


def test_no_suppress_when_no_pending():
    assert should_suppress_greeting([]) is False


class _OpWithSuppress:
    def __init__(self): self.suppressed = False
    def suppress_greeting(self): self.suppressed = True


class _OpNoSuppress:
    """Stand-in for the metered RemoteOperator, which greets cloud-side."""


def test_apply_suppression_calls_when_available():
    op = _OpWithSuppress()
    assert apply_greeting_suppression(op, ["update"]) is True
    assert op.suppressed is True


def test_apply_suppression_safe_without_method():
    # Must NOT raise when the operator has no suppress_greeting (metered mode).
    assert apply_greeting_suppression(_OpNoSuppress(), ["update"]) is False


def test_apply_suppression_noop_without_pending():
    op = _OpWithSuppress()
    assert apply_greeting_suppression(op, []) is False
    assert op.suppressed is False


# --- /hook (Claude Code hook -> call only when app is closed) ------------------

def test_hook_requires_token():
    client = make_client()
    r = client.post("/hook", json={"hook_event_name": "Stop"})
    assert r.status_code == 401


def test_hook_stop_rings_when_app_closed(monkeypatch):
    # quiet=0 restores immediate reporting (the ring-gating window is exercised
    # separately in the quiet-window tests below).
    monkeypatch.setenv("VOXA_RING_QUIET_SECONDS", "0")
    app = create_app(Config("k", "model", "secret", "127.0.0.1", 8787),
                     operator_factory=fake_factory)
    cm = app.state.call_manager
    client = TestClient(app)
    # No prior UserPromptSubmit -> unknown duration -> err toward calling.
    r = client.post("/hook?token=secret",
                    json={"hook_event_name": "Stop", "session_id": "s2", "cwd": "/p/app"})
    assert r.json() == {"ok": True}
    assert any("finished" in m for m in cm._pending)   # a call was queued


def test_hook_still_rings_when_app_open(monkeypatch):
    # App open (connected, but no metered line yet): a terminals-first app is
    # "connected" whenever it's simply open, so the finish must still ring the
    # phone, AND still be queued so it's spoken/rendered when the user taps
    # Start or attaches (not silently lost).
    monkeypatch.setenv("VOXA_RING_QUIET_SECONDS", "0")   # immediate report
    app = create_app(Config("k", "model", "secret", "127.0.0.1", 8787),
                     operator_factory=fake_factory)
    cm = app.state.call_manager
    sent = []

    class _SpyPush:
        async def send_voip(self, *a, **k):
            sent.append(a)
            return True

    cm._pusher = _SpyPush()
    app.state.notifier.note_client_connected()         # app is OPEN/connected
    client = TestClient(app)
    client.post("/hook?token=secret",
                json={"hook_event_name": "Stop", "session_id": "s3", "cwd": "/p/app"})
    assert sent != []                                 # app open -> still rings
    assert any("finished" in m for m in cm._pending)  # ...and queued to speak on Start


def test_hook_finish_held_until_last_open_turn_stops(monkeypatch):
    # A fleet: session "slow" is mid-turn (UserPromptSubmit, no Stop yet) when
    # session "fast" finishes. The fast finish must NOT ring (held); when slow
    # finally stops, ONE ring fires carrying both finishes.
    monkeypatch.setenv("VOXA_RING_QUIET_SECONDS", "0")
    app = create_app(Config("k", "model", "secret", "127.0.0.1", 8787),
                     operator_factory=fake_factory)
    cm = app.state.call_manager
    client = TestClient(app)
    client.post("/hook?token=secret",
                json={"hook_event_name": "UserPromptSubmit", "session_id": "slow"})
    client.post("/hook?token=secret",
                json={"hook_event_name": "Stop", "session_id": "fast", "cwd": "/p/veil"})
    assert cm._pending == []                       # held, not rung mid-fleet
    assert "fast" in app.state.notifier.held_finishes
    client.post("/hook?token=secret",
                json={"hook_event_name": "Stop", "session_id": "slow", "cwd": "/p/loop"})
    assert len(cm._pending) == 1                   # ONE call for the whole fleet
    msg = cm._pending[0]
    assert msg.startswith("All tasks are done.")
    assert "loop finished" in msg and "veil finished" in msg
    assert app.state.notifier.held_finishes == {}  # drained


def test_hook_short_turn_does_not_ring(monkeypatch):
    # With a gate configured (VOXA_HOOK_MIN_SECONDS>0), a quick interactive turn is
    # suppressed. (The default is 0 = call on every finish.)
    monkeypatch.setenv("VOXA_HOOK_MIN_SECONDS", "30")
    app = create_app(Config("k", "model", "secret", "127.0.0.1", 8787),
                     operator_factory=fake_factory)
    cm = app.state.call_manager
    client = TestClient(app)
    client.post("/hook?token=secret",
                json={"hook_event_name": "UserPromptSubmit", "session_id": "q"})
    client.post("/hook?token=secret",
                json={"hook_event_name": "Stop", "session_id": "q", "cwd": "/p/app"})
    assert cm._pending == []                           # quick interactive turn -> no call


def test_hook_short_turn_rings_by_default(monkeypatch):
    # Default (no gate): even a quick turn places a call when the app is closed.
    monkeypatch.setenv("VOXA_RING_QUIET_SECONDS", "0")   # immediate report
    app = create_app(Config("k", "model", "secret", "127.0.0.1", 8787),
                     operator_factory=fake_factory)
    cm = app.state.call_manager
    client = TestClient(app)
    client.post("/hook?token=secret",
                json={"hook_event_name": "UserPromptSubmit", "session_id": "z"})
    client.post("/hook?token=secret",
                json={"hook_event_name": "Stop", "session_id": "z", "cwd": "/p/app"})
    assert any("finished" in m for m in cm._pending)   # called on every finish


def test_duplicate_finish_within_window_rings_once(monkeypatch):
    # The finish-hook and the screen-scraper can both report the SAME finish. Two
    # reports inside the debounce window must collapse to ONE ring (no second short
    # "ghost" call). Different session ids so the per-session hook dedup doesn't
    # mask the cross-source debounce we're testing.
    monkeypatch.setenv("VOXA_RING_QUIET_SECONDS", "0")   # immediate report
    app = create_app(Config("k", "model", "secret", "127.0.0.1", 8787),
                     operator_factory=fake_factory)
    cm = app.state.call_manager
    client = TestClient(app)
    client.post("/hook?token=secret",
                json={"hook_event_name": "Stop", "session_id": "a", "cwd": "/p"})
    client.post("/hook?token=secret",
                json={"hook_event_name": "Stop", "session_id": "b", "cwd": "/p"})
    assert len([m for m in cm._pending if "finished" in m]) == 1


def test_debounce_zero_allows_both(monkeypatch):
    # With the window disabled, two distinct finishes both ring.
    monkeypatch.setenv("VOXA_RING_DEBOUNCE_SECONDS", "0")
    monkeypatch.setenv("VOXA_RING_QUIET_SECONDS", "0")   # immediate report
    app = create_app(Config("k", "model", "secret", "127.0.0.1", 8787),
                     operator_factory=fake_factory)
    cm = app.state.call_manager
    client = TestClient(app)
    client.post("/hook?token=secret",
                json={"hook_event_name": "Stop", "session_id": "a", "cwd": "/p"})
    client.post("/hook?token=secret",
                json={"hook_event_name": "Stop", "session_id": "b", "cwd": "/p"})
    assert len([m for m in cm._pending if "finished" in m]) == 2


def test_hook_remembers_source_cwd(monkeypatch):
    monkeypatch.setenv("VOXA_RING_QUIET_SECONDS", "0")   # no leaked delayed task
    app = create_app(Config("k", "model", "secret", "127.0.0.1", 8787),
                     operator_factory=fake_factory)
    client = TestClient(app)
    client.post("/hook?token=secret",
                json={"hook_event_name": "Stop", "session_id": "s", "cwd": "/p/proj"})
    assert app.state.sessions.pending_source == {"cwd": "/p/proj"}


def test_hook_needs_input_builds_approval():
    app = create_app(Config("k", "model", "secret", "127.0.0.1", 8787),
                     operator_factory=fake_factory)
    # Driven controller pretends to be in /p/loop showing a menu.
    class FakeCtrl:
        working_dir = "/p/loop"
        def capture_text(self):
            return "Do you want to proceed?\n> 1. Yes\n  2. No"
    from server.session import Session
    app.state.sessions.add(Session("x", FakeCtrl(), hub=type("H", (), {
        "set_offline_ring": lambda self, v: None})(), call_manager=app.state.call_manager))
    client = TestClient(app)
    client.post("/hook?token=secret", json={
        "hook_event_name": "Notification", "session_id": "n1", "cwd": "/p/loop",
        "message": "Claude needs your permission"})
    a = app.state.notifier.approvals.active_for("/p/loop")
    assert a and [o["key"] for o in a["options"]] == ["1", "2"]


def test_hook_needs_input_without_driven_session_falls_back_plain():
    # No session mirrors the hook's cwd (or the driven controller can't be
    # scraped): the report must still go out, just without a structured
    # approval attached (today's plain summary).
    app = create_app(Config("k", "model", "secret", "127.0.0.1", 8787),
                     operator_factory=fake_factory)
    client = TestClient(app)
    r = client.post("/hook?token=secret", json={
        "hook_event_name": "Notification", "session_id": "n2", "cwd": "/p/other",
        "message": "Claude needs your permission"})
    assert r.json() == {"ok": True}
    assert app.state.notifier.approvals.active_for("/p/other") is None


def test_hook_scrapes_the_matching_cwd_session_not_the_default_one():
    # A fleet with TWO registered sessions: the hook for the SECOND session's cwd
    # must scrape ITS pane, not fall back to the first-added (default) session.
    app = create_app(Config("k", "model", "secret", "127.0.0.1", 8787),
                     operator_factory=fake_factory)

    class FakeCtrl:
        def __init__(self, cwd, pane):
            self.working_dir = cwd
            self._pane = pane
        def capture_text(self):
            return self._pane

    class FakeHub:
        def set_offline_ring(self, v): pass

    from server.session import Session
    app.state.sessions.add(Session(
        "first", FakeCtrl("/p/first", "Overwrite this file?\n> 1. Yes\n  2. No"),
        hub=FakeHub(), call_manager=app.state.call_manager))
    app.state.sessions.add(Session(
        "second", FakeCtrl("/p/second", "Delete this branch?\n> 1. Yes\n  2. No"),
        hub=FakeHub(), call_manager=app.state.call_manager))

    client = TestClient(app)
    client.post("/hook?token=secret", json={
        "hook_event_name": "Notification", "session_id": "n3", "cwd": "/p/second",
        "message": "Claude needs your permission"})
    # An approval was built for the SECOND session's cwd (its pane was scraped
    # because find_by_cwd resolved to it, not the first-added default session).
    a = app.state.notifier.approvals.active_for("/p/second")
    assert a and [o["key"] for o in a["options"]] == ["1", "2"]
    # The FIRST session's cwd never got an approval (its pane wasn't scraped: the
    # default-session fallback would have failed the cwd match and yielded none).
    assert app.state.notifier.approvals.active_for("/p/first") is None


def test_stand_down_watcher_rings_down_matching_session_not_default(monkeypatch):
    # Two registered sessions; a hook for the SECOND session's cwd must silence
    # THAT session's offline ring, not the first-added (default) one.
    monkeypatch.setenv("VOXA_RING_QUIET_SECONDS", "0")   # no leaked delayed task
    app = create_app(Config("k", "model", "secret", "127.0.0.1", 8787),
                     operator_factory=fake_factory)

    class FakeCtrl:
        def __init__(self, cwd): self.working_dir = cwd

    class FakeHub:
        def __init__(self): self.calls = []
        def set_offline_ring(self, v): self.calls.append(v)

    from server.session import Session
    first_hub, second_hub = FakeHub(), FakeHub()
    app.state.sessions.add(Session("first", FakeCtrl("/p/first"), hub=first_hub,
                                   call_manager=app.state.call_manager))
    app.state.sessions.add(Session("second", FakeCtrl("/p/second"), hub=second_hub,
                                   call_manager=app.state.call_manager))

    client = TestClient(app)
    client.post("/hook?token=secret", json={
        "hook_event_name": "Stop", "session_id": "n4", "cwd": "/p/second"})
    assert second_hub.calls == [False]
    assert first_hub.calls == []


# --- /hook ring-gating: quiet window + busy-pane suppression -------------------
# These drive the RingScheduler through the real endpoint. The delayed finish
# task runs on the TestClient's background event loop, so a short quiet window
# (0.1s) plus a wall-clock sleep is enough to observe (or not observe) the ring.
import time as _time


def _quiet_hook_app(monkeypatch, quiet):
    # The scheduler reads VOXA_RING_QUIET_SECONDS at wire-up, so set it BEFORE
    # create_app builds the /hook route.
    monkeypatch.setenv("VOXA_RING_QUIET_SECONDS", quiet)
    return create_app(Config("k", "model", "secret", "127.0.0.1", 8787),
                      operator_factory=fake_factory)


def test_hook_finish_rings_once_after_quiet_window(monkeypatch):
    # A lone Stop with a quiet pane: nothing rings during the window, exactly ONE
    # report once it elapses.
    app = _quiet_hook_app(monkeypatch, "0.1")
    cm = app.state.call_manager
    # Context manager keeps the background event loop alive so the delayed finish
    # task can fire; a bare TestClient tears the portal down after each request.
    with TestClient(app) as client:
        client.post("/hook?token=secret",
                    json={"hook_event_name": "Stop", "session_id": "q1", "cwd": "/p/x"})
        assert [m for m in cm._pending if "finished" in m] == []   # gated, not yet
        _time.sleep(0.35)
        assert len([m for m in cm._pending if "finished" in m]) == 1


def test_hook_pretooluse_inside_window_cancels_finish(monkeypatch):
    # A PreToolUse arriving inside the quiet window proves the session is still
    # working: the pending finish is cancelled and never rings.
    app = _quiet_hook_app(monkeypatch, "0.2")
    cm = app.state.call_manager
    with TestClient(app) as client:
        client.post("/hook?token=secret",
                    json={"hook_event_name": "Stop", "session_id": "p1", "cwd": "/p/x"})
        client.post("/hook?token=secret",
                    json={"hook_event_name": "PreToolUse", "session_id": "p1",
                          "tool_name": "Bash"})
        _time.sleep(0.4)
        assert [m for m in cm._pending if "finished" in m] == []


def test_hook_busy_pane_suppresses_finish(monkeypatch):
    # The driven pane shows a running background task: the Stop is a turn boundary,
    # not a task end, so no report goes out even after the quiet window.
    app = _quiet_hook_app(monkeypatch, "0.1")
    cm = app.state.call_manager

    class FakeCtrl:
        working_dir = "/p/busy"
        def capture_text(self):
            return "working... 1 background task running (Esc to interrupt)"

    class FakeHub:
        def set_offline_ring(self, v): pass

    from server.session import Session
    app.state.sessions.add(Session("busy", FakeCtrl(), hub=FakeHub(),
                                   call_manager=cm))
    with TestClient(app) as client:
        client.post("/hook?token=secret",
                    json={"hook_event_name": "Stop", "session_id": "b1", "cwd": "/p/busy"})
        _time.sleep(0.35)
        assert [m for m in cm._pending if "finished" in m] == []


def test_hook_notification_rings_immediately_regardless_of_window(monkeypatch):
    # needs_input means a human is blocking: it rings immediately even under a
    # long quiet window (which only gates finish).
    monkeypatch.setenv("VOXA_RING_QUIET_SECONDS", "100")
    app = create_app(Config("k", "model", "secret", "127.0.0.1", 8787),
                     operator_factory=fake_factory)
    cm = app.state.call_manager
    client = TestClient(app)
    client.post("/hook?token=secret", json={
        "hook_event_name": "Notification", "session_id": "ni", "cwd": "/p/x",
        "message": "Claude needs your permission"})
    assert any("needs input" in m for m in cm._pending)   # immediate, no sleep


def test_hook_notification_builds_options_from_non_driven_terminal(monkeypatch):
    # The prompt lives in a terminal Voxa is NOT attached to (the fleet
    # reality): the driven-pane scrape yields nothing, so the discovery
    # fallback captures the matching terminal and the approval still carries
    # the actual choices instead of a bare "needs your permission".
    from server import hook_routes
    menu = ("How should we depict it?\n"
            " \x1b[38;5;153m❯\x1b[39m \x1b[38;5;246m1. Bold pictograms\x1b[39m\n"
            "   2. Phone POV\n"
            "   3. Kinetic type only\n"
            " Enter to select · Tab/Arrow keys to navigate · Esc to cancel\n")
    monkeypatch.setattr(hook_routes, "_discovery_capture",
                        lambda cwd: menu if cwd == "/p/trailer" else "")
    app = create_app(Config("k", "model", "secret", "127.0.0.1", 8787),
                     operator_factory=fake_factory)
    client = TestClient(app)
    client.post("/hook?token=secret", json={
        "hook_event_name": "Notification", "session_id": "nd", "cwd": "/p/trailer",
        "message": "Claude is waiting for your input"})
    approval = app.state.notifier.approvals.active_for("/p/trailer")
    assert approval is not None
    assert [o["key"] for o in approval["options"]] == ["1", "2", "3"]
    assert approval["options"][0]["label"] == "Bold pictograms"


async def test_approval_decision_presses_key_and_resolves():
    from server.app import handle_client_control
    from server.approvals import build_approval
    from server.notifier import Notifier
    pressed = []
    class FakeOrch:
        controller = type("C", (), {"working_dir": "/p/loop"})()
        async def handle_tool_call(self, n, a): return {}
        def set_terminal_app(self, a): pass
        async def press_key(self, k): pressed.append(k); return {"pressed": k}
    class FakeWS:
        def __init__(self): self.sent = []
        async def send_json(self, m): self.sent.append(m)
    n = Notifier(_FakeCM(), push_enabled=True, ring_debounce=0)
    a = build_approval("/p/loop", "s", "> 1. Yes\n  2. No")
    n.approvals.put(a)
    ws = FakeWS()
    import json as _json
    await handle_client_control(_json.dumps(
        {"type": "approval_decision", "approval_id": a["approval_id"], "key": "1"}),
        FakeOrch(), ws, notifier=n)
    assert pressed == ["1"]
    assert ws.sent[-1]["type"] == "approval_resolved"
    assert ws.sent[-1]["outcome"] == "sent"
    assert n.approvals.get(a["approval_id"]) is None


async def test_approval_decision_invalid_key_reports_error():
    from server.app import handle_client_control
    from server.approvals import build_approval
    from server.notifier import Notifier
    pressed = []
    class FakeOrch:
        # Driven terminal matches the approval's cwd, so the swap guard passes
        # and the key validation is what gets exercised.
        controller = type("C", (), {"working_dir": "/p/loop"})()
        async def handle_tool_call(self, n, a): return {}
        def set_terminal_app(self, a): pass
        async def press_key(self, k): pressed.append(k); return {"pressed": k}
    class FakeWS:
        def __init__(self): self.sent = []
        async def send_json(self, m): self.sent.append(m)
    n = Notifier(_FakeCM(), push_enabled=True, ring_debounce=0)
    a = build_approval("/p/loop", "s", "> 1. Yes\n  2. No")
    n.approvals.put(a)
    ws = FakeWS()
    import json as _json
    await handle_client_control(_json.dumps(
        {"type": "approval_decision", "approval_id": a["approval_id"], "key": "9"}),
        FakeOrch(), ws, notifier=n)
    assert pressed == []                                     # never pressed a bogus key
    assert ws.sent[-1]["type"] == "status" and "error" in ws.sent[-1]["status"].lower()
    assert n.approvals.get(a["approval_id"]) is not None      # left active, not resolved


async def test_approval_decision_press_error_does_not_resolve_or_report_sent():
    # press_key can fail even for a validated option (session gone, tmux
    # error): the approval must stay active and the reply must say so,
    # instead of lying "sent" while nothing reached the live pane.
    from server.app import handle_client_control
    from server.approvals import build_approval
    from server.notifier import Notifier
    pressed = []
    class FakeOrch:
        controller = type("C", (), {"working_dir": "/p/loop"})()
        async def handle_tool_call(self, n, a): return {}
        def set_terminal_app(self, a): pass
        async def press_key(self, k): pressed.append(k); return {"error": "no_session"}
    class FakeWS:
        def __init__(self): self.sent = []
        async def send_json(self, m): self.sent.append(m)
    n = Notifier(_FakeCM(), push_enabled=True, ring_debounce=0)
    a = build_approval("/p/loop", "s", "> 1. Yes\n  2. No")
    n.approvals.put(a)
    ws = FakeWS()
    import json as _json
    await handle_client_control(_json.dumps(
        {"type": "approval_decision", "approval_id": a["approval_id"], "key": "1"}),
        FakeOrch(), ws, notifier=n)
    assert pressed == ["1"]
    assert ws.sent[-1]["type"] == "status"
    assert "approval press error" in ws.sent[-1]["status"]
    assert n.approvals.get(a["approval_id"]) is not None      # left active, not resolved
    assert not any(m.get("type") == "approval_resolved" for m in ws.sent)


async def test_stale_approval_decision_reports_stale():
    from server.app import handle_client_control
    from server.notifier import Notifier
    class FakeOrch:
        async def handle_tool_call(self, n, a): return {}
        def set_terminal_app(self, a): pass
    class FakeWS:
        def __init__(self): self.sent = []
        async def send_json(self, m): self.sent.append(m)
    ws = FakeWS()
    import json as _json
    await handle_client_control(_json.dumps(
        {"type": "approval_decision", "approval_id": "gone", "key": "1"}),
        FakeOrch(), ws, notifier=Notifier(_FakeCM(), push_enabled=True))
    assert ws.sent[-1] == {"type": "approval_resolved", "approval_id": "gone",
                           "outcome": "stale"}


async def test_approval_decision_without_notifier_is_a_safe_noop():
    # Every existing call site (and every OLDER test) calls handle_client_control
    # without a notifier kwarg; it must default to None and never raise.
    from server.app import handle_client_control
    class FakeOrch:
        async def handle_tool_call(self, n, a): return {}
        def set_terminal_app(self, a): pass
    class FakeWS:
        def __init__(self): self.sent = []
        async def send_json(self, m): self.sent.append(m)
    ws = FakeWS()
    import json as _json
    await handle_client_control(_json.dumps(
        {"type": "approval_decision", "approval_id": "gone", "key": "1"}), FakeOrch(), ws)
    assert ws.sent == []


async def test_resume_session_control_calls_orchestrator():
    from server.app import handle_client_control
    import json as _json
    calls = {}

    class FakeOrch:
        async def resume_session(self, cwd, session_id):
            calls["cwd"] = cwd; calls["sid"] = session_id
            return {"status": "idle", "working_dir": cwd}
        async def handle_tool_call(self, n, a): return {}
        def set_terminal_app(self, a): pass

    class FakeWS:
        def __init__(self): self.sent = []
        async def send_json(self, m): self.sent.append(m)

    ws = FakeWS()
    await handle_client_control(_json.dumps(
        {"type": "resume_session", "cwd": "/p/loop", "session_id": "enc/stem"}),
        FakeOrch(), ws)
    assert calls == {"cwd": "/p/loop", "sid": "enc/stem"}


async def test_resume_session_control_reports_error():
    from server.app import handle_client_control
    import json as _json

    class FakeOrch:
        async def resume_session(self, cwd, session_id):
            return {"error": "not a directory: /nope"}
        async def handle_tool_call(self, n, a): return {}
        def set_terminal_app(self, a): pass

    class FakeWS:
        def __init__(self): self.sent = []
        async def send_json(self, m): self.sent.append(m)

    ws = FakeWS()
    await handle_client_control(_json.dumps(
        {"type": "resume_session", "cwd": "/nope", "session_id": "s"}),
        FakeOrch(), ws)
    assert ws.sent[-1]["type"] == "status" and "resume error" in ws.sent[-1]["status"]


async def test_attach_source_control_emits_working_dir_on_success():
    from server.app import handle_client_control
    import json as _json
    calls = {}

    class FakeOrch:
        async def attach_source(self, cwd):
            calls["cwd"] = cwd
            return {"attached": "loop", "working_dir": cwd, "recap": "did stuff"}
        async def handle_tool_call(self, n, a): return {}
        def set_terminal_app(self, a): pass

    class FakeWS:
        def __init__(self): self.sent = []
        async def send_json(self, m): self.sent.append(m)

    ws = FakeWS()
    await handle_client_control(_json.dumps(
        {"type": "attach_source", "cwd": "/p/loop"}), FakeOrch(), ws)
    assert calls == {"cwd": "/p/loop"}
    assert {"type": "status", "working_dir": "/p/loop"} in ws.sent


async def test_attach_source_control_reports_error():
    from server.app import handle_client_control
    import json as _json

    class FakeOrch:
        async def attach_source(self, cwd):
            return {"error": "source terminal not open or not discoverable"}
        async def handle_tool_call(self, n, a): return {}
        def set_terminal_app(self, a): pass

    class FakeWS:
        def __init__(self): self.sent = []
        async def send_json(self, m): self.sent.append(m)

    ws = FakeWS()
    await handle_client_control(_json.dumps(
        {"type": "attach_source", "cwd": "/p/gone"}), FakeOrch(), ws)
    assert ws.sent[-1]["type"] == "status"
    assert "not open" in ws.sent[-1]["status"]


async def test_get_notify_rules_control_returns_rules(tmp_path):
    from server.app import handle_client_control
    from server.notify_rules import NotifyRules
    from server.notifier import Notifier
    rules = NotifyRules(str(tmp_path / "rules.json"))
    rules.set_mode("/p/loop", "finish", "silent")
    n = Notifier(_FakeCM(), push_enabled=True, rules=rules)
    class FakeOrch:
        async def handle_tool_call(self, n, a): return {}
        def set_terminal_app(self, a): pass
    class FakeWS:
        def __init__(self): self.sent = []
        async def send_json(self, m): self.sent.append(m)
    ws = FakeWS()
    import json as _json
    await handle_client_control(_json.dumps({"type": "get_notify_rules"}),
                                FakeOrch(), ws, notifier=n)
    assert ws.sent[-1] == {"type": "notify_rules", "rules": rules.overrides(),
                           "default": rules.defaults()}


async def test_set_notify_rule_control_updates_and_replies(tmp_path):
    from server.app import handle_client_control
    from server.notify_rules import NotifyRules
    from server.notifier import Notifier
    rules = NotifyRules(str(tmp_path / "rules.json"))
    n = Notifier(_FakeCM(), push_enabled=True, rules=rules)
    class FakeOrch:
        async def handle_tool_call(self, n, a): return {}
        def set_terminal_app(self, a): pass
    class FakeWS:
        def __init__(self): self.sent = []
        async def send_json(self, m): self.sent.append(m)
    ws = FakeWS()
    import json as _json
    await handle_client_control(_json.dumps(
        {"type": "set_notify_rule", "cwd": "/p/loop", "kind": "finish", "mode": "silent"}),
        FakeOrch(), ws, notifier=n)
    assert ws.sent[-1] == {"type": "notify_rules", "rules": rules.overrides(),
                           "default": rules.defaults()}
    assert rules.mode("/p/loop", "finish") == "silent"


async def test_set_notify_rule_invalid_reports_error_status(tmp_path):
    from server.app import handle_client_control
    from server.notify_rules import NotifyRules
    from server.notifier import Notifier
    rules = NotifyRules(str(tmp_path / "rules.json"))
    n = Notifier(_FakeCM(), push_enabled=True, rules=rules)
    class FakeOrch:
        async def handle_tool_call(self, n, a): return {}
        def set_terminal_app(self, a): pass
    class FakeWS:
        def __init__(self): self.sent = []
        async def send_json(self, m): self.sent.append(m)
    ws = FakeWS()
    import json as _json
    await handle_client_control(_json.dumps(
        {"type": "set_notify_rule", "cwd": "/p/loop", "kind": "bogus", "mode": "silent"}),
        FakeOrch(), ws, notifier=n)
    assert ws.sent[-1]["type"] == "status" and "error" in ws.sent[-1]["status"].lower()


async def test_set_notify_default_control_updates_and_rebroadcasts(tmp_path):
    from server.app import handle_client_control
    from server.notify_rules import NotifyRules
    from server.notifier import Notifier
    import json as _json
    rules = NotifyRules(str(tmp_path / "rules.json"))
    n = Notifier(_FakeCM(), push_enabled=True, rules=rules)

    class FakeOrch:
        async def handle_tool_call(self, n, a): return {}
        def set_terminal_app(self, a): pass

    class FakeWS:
        def __init__(self): self.sent = []
        async def send_json(self, m): self.sent.append(m)

    ws = FakeWS()
    await handle_client_control(_json.dumps(
        {"type": "set_notify_default", "kind": "finish", "mode": "silent"}),
        FakeOrch(), ws, notifier=n)
    assert rules.defaults()["finish"] == "silent"
    assert ws.sent[-1] == {"type": "notify_rules", "rules": rules.overrides(),
                           "default": rules.defaults()}
    # The reserved defaults key must NOT leak into the per-cwd "rules" map, or the
    # phone renders it as a phantom muted project named __default__.
    assert "__default__" not in ws.sent[-1]["rules"]


async def test_set_notify_default_invalid_reports_error_status(tmp_path):
    from server.app import handle_client_control
    from server.notify_rules import NotifyRules
    from server.notifier import Notifier
    import json as _json
    rules = NotifyRules(str(tmp_path / "rules.json"))
    n = Notifier(_FakeCM(), push_enabled=True, rules=rules)

    class FakeOrch:
        async def handle_tool_call(self, n, a): return {}
        def set_terminal_app(self, a): pass

    class FakeWS:
        def __init__(self): self.sent = []
        async def send_json(self, m): self.sent.append(m)

    ws = FakeWS()
    await handle_client_control(_json.dumps(
        {"type": "set_notify_default", "kind": "bogus", "mode": "silent"}),
        FakeOrch(), ws, notifier=n)
    assert ws.sent[-1]["type"] == "status" and "error" in ws.sent[-1]["status"].lower()


def test_suppress_greeting_if_supported_is_safe_without_method():
    from server.app import suppress_greeting_if_supported
    class _NoSuppress:  # stands in for the metered RemoteOperator
        pass
    # Must NOT raise (this exact path crashed the answer flow in metered mode).
    assert suppress_greeting_if_supported(_NoSuppress()) is False
    class _WithSuppress:
        def __init__(self): self.hit = False
        def suppress_greeting(self): self.hit = True
    op = _WithSuppress()
    assert suppress_greeting_if_supported(op) is True and op.hit is True


# --- session registry -----------------------------------------------------------

def test_session_registry_created_and_persists_across_connections(tmp_path, monkeypatch):
    """The Claude session must survive phone reconnects: the SAME Session object
    (and its controller) is reused on the second connection instead of a fresh one."""
    monkeypatch.setenv("VOXA_DEVICES_FILE", str(tmp_path / "devices.json"))
    app = create_app(Config("k", "m", "secret", "127.0.0.1", 8787),
                     operator_factory=fake_factory)
    client = TestClient(app)
    with client.websocket_connect("/ws?token=secret") as ws:
        assert ws.receive_json()["status"] == "ready"
    first = app.state.sessions.default()
    assert first is not None
    with client.websocket_connect("/ws?token=secret") as ws:
        assert ws.receive_json()["status"] == "ready"
    assert app.state.sessions.default() is first


def test_ws_connect_sets_the_new_session_active(tmp_path, monkeypatch):
    """serve_ws's create-flow must mark the freshly created session ACTIVE (not
    just added), so registry.active() -- not only default() -- finds it."""
    monkeypatch.setenv("VOXA_DEVICES_FILE", str(tmp_path / "devices.json"))
    app = create_app(Config("k", "m", "secret", "127.0.0.1", 8787),
                     operator_factory=fake_factory)
    client = TestClient(app)
    with client.websocket_connect("/ws?token=secret") as ws:
        assert ws.receive_json()["status"] == "ready"
    session = app.state.sessions.default()
    assert app.state.sessions.active_id == session.id
    assert app.state.sessions.active() is session


def test_default_session_uses_scoped_tmux_name(tmp_path, monkeypatch):
    """New sessions get a voxa-scoped tmux name (voxa or voxa-<id> when adopting
    a leftover), never an arbitrary one."""
    monkeypatch.setenv("VOXA_DEVICES_FILE", str(tmp_path / "devices.json"))
    app = create_app(Config("k", "m", "secret", "127.0.0.1", 8787),
                     operator_factory=fake_factory)
    client = TestClient(app)
    with client.websocket_connect("/ws?token=secret") as ws:
        ws.receive_json()
    session = app.state.sessions.default()
    assert session.controller._session.startswith("voxa")
    assert session.id in session.controller._session or session.controller._session == "voxa" \
        or session.controller._session.startswith("voxa-")


# --- session history controls ---------------------------------------------------

async def test_history_list_control_returns_sessions(tmp_path, monkeypatch):
    import json as _json
    from server.app import handle_client_control
    d = tmp_path / "-p-proj"
    d.mkdir(parents=True)
    (d / "s1.jsonl").write_text(_json.dumps({
        "type": "user", "timestamp": "2026-07-01T10:00:00Z", "cwd": "/p/proj",
        "message": {"role": "user", "content": "hello"}}) + "\n")
    monkeypatch.setattr("server.history.PROJECTS_DIR", str(tmp_path))
    sent = []

    class FakeOrch:
        async def handle_tool_call(self, n, a): return {}
        def set_terminal_app(self, a): pass

    class FakeWS:
        async def send_json(self, m): sent.append(m)

    await handle_client_control('{"type":"history_list"}', FakeOrch(), FakeWS())
    assert sent and sent[0]["type"] == "history_sessions"
    assert sent[0]["sessions"][0]["id"] == "-p-proj/s1"


async def test_history_get_control_returns_detail(tmp_path, monkeypatch):
    import json as _json
    from server.app import handle_client_control
    d = tmp_path / "-p-proj"
    d.mkdir(parents=True)
    (d / "s1.jsonl").write_text(_json.dumps({
        "type": "user", "timestamp": "2026-07-01T10:00:00Z", "cwd": "/p/proj",
        "message": {"role": "user", "content": "hello"}}) + "\n")
    monkeypatch.setattr("server.history.PROJECTS_DIR", str(tmp_path))
    sent = []

    class FakeOrch:
        async def handle_tool_call(self, n, a): return {}
        def set_terminal_app(self, a): pass

    class FakeWS:
        async def send_json(self, m): sent.append(m)

    await handle_client_control(
        _json.dumps({"type": "history_get", "id": "-p-proj/s1"}), FakeOrch(), FakeWS())
    assert sent[0]["type"] == "history_session" and sent[0]["id"] == "-p-proj/s1"
    assert sent[0]["messages"][0]["text"] == "hello"
    assert sent[0]["truncated"] is False


async def test_history_get_bad_id_reports_error_status(monkeypatch, tmp_path):
    import json as _json
    from server.app import handle_client_control
    monkeypatch.setattr("server.history.PROJECTS_DIR", str(tmp_path))
    sent = []

    class FakeOrch:
        async def handle_tool_call(self, n, a): return {}
        def set_terminal_app(self, a): pass

    class FakeWS:
        async def send_json(self, m): sent.append(m)

    await handle_client_control(
        _json.dumps({"type": "history_get", "id": "../evil"}), FakeOrch(), FakeWS())
    assert sent[0]["type"] == "status" and "error" in sent[0]["status"].lower()


async def test_approval_decision_after_terminal_swap_is_stale():
    # The driven controller swapped (attach_terminal mid-call) after the prompt
    # appeared: pressing would type into a DIFFERENT live pane, so the decision
    # must be refused as stale, not injected.
    from server.app import handle_client_control
    from server.approvals import build_approval
    from server.notifier import Notifier
    pressed = []

    class FakeOrch:
        controller = type("C", (), {"working_dir": "/p/OTHER"})()
        async def handle_tool_call(self, n, a): return {}
        def set_terminal_app(self, a): pass
        async def press_key(self, k): pressed.append(k); return {"pressed": k}

    class FakeWS:
        def __init__(self): self.sent = []
        async def send_json(self, m): self.sent.append(m)

    n = Notifier(_FakeCM(), push_enabled=True, ring_debounce=0)
    a = build_approval("/p/loop", "s", "> 1. Yes\n  2. No")
    n.approvals.put(a)
    ws = FakeWS()
    import json as _json
    await handle_client_control(_json.dumps(
        {"type": "approval_decision", "approval_id": a["approval_id"], "key": "1"}),
        FakeOrch(), ws, notifier=n)
    assert pressed == []
    assert ws.sent[-1]["outcome"] == "stale"


# --- fleet: WS controls, connect push, answer routing, watcher skip --------------

async def test_list_sessions_control_replies_with_sessions():
    from server.app import handle_client_control
    calls = []
    payload = [{"id": "a", "label": "alpha", "cwd": "/p/alpha",
                "status": "idle", "active": True}]

    class FakeOrch:
        async def handle_tool_call(self, n, a):
            calls.append((n, a)); return {"sessions": payload}
        def set_terminal_app(self, a): pass

    class FakeWS:
        def __init__(self): self.sent = []
        async def send_json(self, m): self.sent.append(m)

    ws = FakeWS()
    await handle_client_control('{"type":"list_sessions"}', FakeOrch(), ws)
    assert ("list_sessions", {}) in calls
    assert ws.sent[-1] == {"type": "sessions", "sessions": payload}


async def test_switch_session_control_calls_tool_and_reports_error():
    import json as _json
    from server.app import handle_client_control
    calls = []

    class FakeOrch:
        async def handle_tool_call(self, n, a):
            calls.append((n, a)); return {"error": "no session matching 'x'"}
        def set_terminal_app(self, a): pass

    class FakeWS:
        def __init__(self): self.sent = []
        async def send_json(self, m): self.sent.append(m)

    ws = FakeWS()
    await handle_client_control(
        _json.dumps({"type": "switch_session", "session_id": "x"}), FakeOrch(), ws)
    assert ("switch_session", {"target": "x"}) in calls
    assert ws.sent[-1]["type"] == "status"
    assert "no session" in ws.sent[-1]["status"]


async def test_switch_session_control_success_is_quiet():
    import json as _json
    from server.app import handle_client_control
    calls = []

    class FakeOrch:
        async def handle_tool_call(self, n, a):
            calls.append((n, a)); return {"switched": "beta"}
        def set_terminal_app(self, a): pass

    class FakeWS:
        def __init__(self): self.sent = []
        async def send_json(self, m): self.sent.append(m)

    ws = FakeWS()
    await handle_client_control(
        _json.dumps({"type": "switch_session", "session_id": "bid"}), FakeOrch(), ws)
    assert ("switch_session", {"target": "bid"}) in calls
    assert ws.sent == []      # minimal-reply idiom: the tool pushes `sessions` itself


async def test_new_session_control_calls_tool():
    import json as _json
    from server.app import handle_client_control
    calls = []

    class FakeOrch:
        async def handle_tool_call(self, n, a):
            calls.append((n, a)); return {"created": "gamma"}
        def set_terminal_app(self, a): pass

    class FakeWS:
        def __init__(self): self.sent = []
        async def send_json(self, m): self.sent.append(m)

    ws = FakeWS()
    await handle_client_control(
        _json.dumps({"type": "new_session", "path": "/p/gamma"}), FakeOrch(), ws)
    assert ("new_session", {"path": "/p/gamma"}) in calls
    assert ws.sent == []


async def test_new_session_control_reports_error_status():
    import json as _json
    from server.app import handle_client_control

    class FakeOrch:
        async def handle_tool_call(self, n, a): return {"error": "not a folder"}
        def set_terminal_app(self, a): pass

    class FakeWS:
        def __init__(self): self.sent = []
        async def send_json(self, m): self.sent.append(m)

    ws = FakeWS()
    await handle_client_control(
        _json.dumps({"type": "new_session", "path": "/p/ghost"}), FakeOrch(), ws)
    assert ws.sent[-1]["type"] == "status"
    assert "not a folder" in ws.sent[-1]["status"]


def test_ws_pushes_sessions_right_after_ready(tmp_path, monkeypatch):
    """On connect the phone gets the fleet snapshot unsolicited, immediately
    after `ready`, so the session card renders without having to ask."""
    monkeypatch.setenv("VOXA_DEVICES_FILE", str(tmp_path / "devices.json"))
    app = create_app(Config("k", "m", "secret", "127.0.0.1", 8787),
                     operator_factory=fake_factory)
    client = TestClient(app)
    with client.websocket_connect("/ws?token=secret") as ws:
        first = ws.receive_json()
        assert first["status"] == "ready"
        second = ws.receive_json()
        assert second["type"] == "sessions"
        assert len(second["sessions"]) == 1
        entry = second["sessions"][0]
        assert entry["id"] == app.state.sessions.default().id
        assert entry["active"] is True
        assert set(entry) == {"id", "label", "cwd", "status", "active"}


def test_answer_attach_sets_matching_fleet_member_active(tmp_path, monkeypatch):
    """Answering a call whose cwd belongs to a registered fleet member must make
    THAT member the active one (and persist the swapped controller onto it), so
    the fleet card and later reconnects follow the answer."""
    monkeypatch.setenv("VOXA_DEVICES_FILE", str(tmp_path / "devices.json"))
    app = create_app(Config("k", "m", "secret", "127.0.0.1", 8787),
                     operator_factory=fake_factory)
    client = TestClient(app)
    # First connect creates member A and marks it active.
    with client.websocket_connect("/ws?token=secret") as ws:
        assert ws.receive_json()["status"] == "ready"
    first = app.state.sessions.default()
    assert app.state.sessions.active_id == first.id

    class FakeCtrl:
        working_dir = "/p/beta"
        status = "idle"
        def on_final(self, cb): pass

    class FakeHub:
        def set_offline_ring(self, v): pass

    from server.session import Session
    app.state.sessions.add(Session("beta1", FakeCtrl(), FakeHub(),
                                   app.state.call_manager))
    app.state.sessions.push_pending("/p/beta")

    from server.orchestrator import Orchestrator
    async def fake_attach_source(self, cwd):
        return {"attached": "beta", "working_dir": cwd}
    monkeypatch.setattr(Orchestrator, "attach_source", fake_attach_source)

    with client.websocket_connect("/ws?token=secret") as ws:
        assert ws.receive_json()["status"] == "ready"
        ws.send_text('{"type":"begin"}')
    assert app.state.sessions.active_id == "beta1"
    # The swap persisted onto the MEMBER (fake attach kept the original
    # controller, so that is what lands there), not onto session A only.
    assert app.state.sessions.get("beta1").controller is first.controller


def test_watcher_skip_is_fleet_aware(monkeypatch):
    """The background watcher's skip closure must recognize EVERY registered
    session as 'already driven' while a line is open, not just the default
    one; terminals outside the fleet must still be reported."""
    monkeypatch.setenv("VOXA_WATCH_TERMINALS", "1")
    captured = {}

    class FakeWatcher:
        def __init__(self, on_done, **kw):
            captured["skip"] = kw.get("should_skip")
        async def run(self): pass

    import server.terminal_watcher as tw
    monkeypatch.setattr(tw, "TerminalWatcher", FakeWatcher)
    app = create_app(Config("k", "m", "secret", "127.0.0.1", 8787),
                     operator_factory=fake_factory)

    class FakeCtrl:
        def __init__(self, cwd): self.working_dir = cwd
        def on_final(self, cb): pass

    class FakeHub:
        def set_offline_ring(self, v): pass

    from server.session import Session
    app.state.sessions.add(Session("first", FakeCtrl("/p/first"), FakeHub(),
                                   app.state.call_manager))
    app.state.sessions.add(Session("second", FakeCtrl("/p/second"), FakeHub(),
                                   app.state.call_manager))
    skip = captured["skip"]
    # No line open: nothing is skipped (the watcher reports everything).
    assert skip({"cwd": "/p/second"}) is False
    app.state.call_manager.attach()          # a phone line is now open
    assert skip({"cwd": "/p/second"}) is True    # any fleet member, not just default
    assert skip({"cwd": "/p/first"}) is True
    assert skip({"cwd": "/p/elsewhere"}) is False  # non-member still reported


# --- Task 2: read pending approval options on answer; speak foreign prompts ------
# The notifier calls on_approval_speak (a line-scoped callback) right after
# on_approval, both fail-open. serve_ws wires it so a FRESH approval for a session
# OTHER than the driven one is read aloud, while the driven session's own prompt
# stays pane-narrated (no double speech).
import asyncio as _asyncio2
from contextlib import asynccontextmanager as _acm2


class _LineOpenCM:
    """A CallManager stand-in whose line is open, so Notifier.report reaches the
    on_approval / on_approval_speak callbacks then returns without ringing."""
    line_open = True


async def test_report_calls_on_approval_speak_after_on_approval():
    from server.notifier import Notifier
    n = Notifier(_LineOpenCM(), push_enabled=True, ring_debounce=0)
    order = []
    async def on_appr(a): order.append("push")
    async def on_speak(a): order.append("speak")
    n.on_approval = on_appr
    n.on_approval_speak = on_speak
    await n.report("s", kind="needs_input", cwd="/p/x",
                   approval={"approval_id": "a1", "options": []})
    assert order == ["push", "speak"]


async def test_report_on_approval_speak_failure_is_swallowed():
    # Fail-open: a narration error inside on_approval_speak must never break the call.
    from server.notifier import Notifier
    n = Notifier(_LineOpenCM(), push_enabled=True, ring_debounce=0)
    async def boom(a): raise RuntimeError("no speak")
    n.on_approval_speak = boom
    await n.report("s", kind="needs_input", cwd="/p/x",
                   approval={"approval_id": "a1", "options": []})   # must not raise


async def test_report_without_approval_does_not_speak():
    from server.notifier import Notifier
    n = Notifier(_LineOpenCM(), push_enabled=True, ring_debounce=0)
    spoke = []
    async def on_speak(a): spoke.append(a)
    n.on_approval_speak = on_speak
    await n.report("plain finish", kind="finish", cwd="/p/x")
    assert spoke == []


class _BlockingOperator:
    """FakeOperator whose run() blocks until released, so the line stays attached
    long enough to inject a mid-call approval report and observe the narration."""
    def __init__(self, config, handle_tool_call):
        self.handle = handle_tool_call
        self.spoke = []
        self.audio = []            # mic frames actually forwarded to Gemini
        self._release = _asyncio2.Event()
    def set_audio_out(self, cb): pass
    def set_text_out(self, cb): pass
    async def send_audio(self, pcm): self.audio.append(pcm)
    async def speak(self, text, immediate=False): self.spoke.append(text)
    def suppress_greeting(self): pass
    async def send_text(self, text): pass
    async def run(self): await self._release.wait()


class _FakeDrivenCtrl:
    status = "idle"
    def __init__(self, cwd):
        self.working_dir = cwd
        self._started = True          # skip serve_ws's reattach path
    def on_final(self, cb): pass
    def capture_text(self): return ""


class _FakeWSChannel:
    """Minimal WebSocket for driving serve_ws directly: a queue feeds receive(),
    sends are recorded. Avoids launching a real terminal (a pre-added session in
    the registry means serve_ws never constructs a controller)."""
    def __init__(self, incoming):
        self.query_params = {}
        self.sent = []
        self._in = incoming
    async def send_json(self, m): self.sent.append(m)
    async def send_bytes(self, b): pass
    async def receive(self): return await self._in.get()


@_acm2
async def _attached_serve_ws(driven_cwd, monkeypatch, seed_approval=None, seed_pending=None):
    """Drive serve_ws to the point where a metered line is attached and yield
    (notifier, operator, ws, sessions). Tears the connection down on exit.
    ``seed_pending`` (a list of summary strings) queues updates onto the call
    manager BEFORE the line attaches, mirroring how a background finish queues
    while no phone is connected."""
    from server.ws_session import serve_ws
    from server.session import SessionRegistry, Session
    from server.session_hub import SessionHub
    from server.call_manager import CallManager

    # Keep the async terminal-discovery side task fast and side-effect free.
    monkeypatch.setattr("server.terminals.discover_claude_sessions", lambda: [])

    captured = {}

    @_acm2
    async def factory(config, handle_tool_call, voice=""):
        op = _BlockingOperator(config, handle_tool_call)
        captured["op"] = op
        yield op

    class _NoPush:
        async def send_voip(self, *a, **k): return False

    cm = CallManager(_NoPush(), _FakeReg())
    for text in (seed_pending or []):
        cm.queue(text)
    from server.notifier import Notifier
    notifier = Notifier(cm, push_enabled=True)
    if seed_approval is not None:
        notifier.approvals.put(seed_approval)

    sessions = SessionRegistry()
    ctrl = _FakeDrivenCtrl(driven_cwd)
    hub = SessionHub(ctrl, cm)
    sessions.add(Session("drv", ctrl, hub, cm))
    sessions.set_active("drv")

    incoming = _asyncio2.Queue()
    ws = _FakeWSChannel(incoming)
    cfg = Config("k", "m", "secret", "127.0.0.1", 8787)
    task = _asyncio2.ensure_future(serve_ws(
        ws, config=cfg, mode="attach", sessions=sessions, notifier=notifier,
        operator_factory=factory))
    # Cross the pre-session gate: `begin` attaches the metered line.
    await incoming.put({"type": "websocket.receive", "text": '{"type":"begin"}'})
    # Wait until the line is attached (callback wired) and the opening was spoken.
    for _ in range(400):
        if notifier.on_approval_speak is not None and captured.get("op") and captured["op"].spoke:
            break
        await _asyncio2.sleep(0.005)
    assert notifier.on_approval_speak is not None, "line never attached"
    try:
        yield notifier, captured["op"], ws, sessions
    finally:
        await incoming.put({"type": "websocket.disconnect"})
        try:
            await _asyncio2.wait_for(task, 1.0)
        except Exception:
            task.cancel()


async def test_answer_speaks_active_approval_options(monkeypatch):
    # Answering with an approval already ACTIVE for the attached cwd: the spoken
    # opening reads the question and every option (the user's core bug).
    from server.approvals import build_approval
    a = build_approval("/p/loop", "Overwrite file?", "Overwrite file?\n> 1. Yes\n  2. No")
    async with _attached_serve_ws("/p/loop", monkeypatch, seed_approval=a) as (n, op, ws, sessions):
        opening = op.spoke[0]
        assert "1: Yes" in opening and "2: No" in opening
        assert any(m.get("type") == "approval" for m in ws.sent)   # card also pushed


async def test_missed_updates_control_sent_when_updates_queued_before_connect(monkeypatch):
    # Updates that queued while no phone was connected (background finishes) are
    # spoken in the opening AND pushed as a structured control so the phone UI can
    # render them, not just have them read aloud.
    async with _attached_serve_ws(
            "/p/loop", monkeypatch,
            seed_pending=["loop finished", "veil needs input"]) as (n, op, ws, sessions):
        missed = [m for m in ws.sent if m.get("type") == "missed_updates"]
        assert len(missed) == 1
        assert missed[0]["items"] == [{"text": "loop finished"},
                                      {"text": "veil needs input"}]
        # Spoken opening is unchanged (still mentions the missed updates).
        assert op.spoke, "opening was never spoken"


async def test_no_missed_updates_control_when_nothing_queued(monkeypatch):
    async with _attached_serve_ws("/p/loop", monkeypatch) as (n, op, ws, sessions):
        assert not any(m.get("type") == "missed_updates" for m in ws.sent)


async def test_midcall_foreign_approval_is_spoken_and_carded(monkeypatch):
    # A fresh approval mid-call for a DIFFERENT cwd than the driven one: the pane
    # monitor cannot narrate it, so on_approval_speak reads the options aloud AND
    # the card is still pushed.
    from server.approvals import build_approval
    async with _attached_serve_ws("/p/driven", monkeypatch) as (n, op, ws, sessions):
        before = len(op.spoke)
        foreign = build_approval("/p/other", "Delete branch?", "Delete branch?\n> 1. Yes\n  2. No")
        await n.report("other needs input", kind="needs_input", cwd="/p/other", approval=foreign)
        spoken = op.spoke[before:]
        assert any("1: Yes" in t and "2: No" in t for t in spoken)
        assert any(m.get("type") == "approval" and m.get("cwd") == "/p/other" for m in ws.sent)


async def test_midcall_driven_approval_is_carded_not_spoken(monkeypatch):
    # A fresh approval mid-call for the DRIVEN cwd: the driven pane's own monitor
    # narrates it, so on_approval_speak must NOT speak it (no double narration),
    # but the card is still pushed.
    from server.approvals import build_approval
    async with _attached_serve_ws("/p/driven", monkeypatch) as (n, op, ws, sessions):
        before = len(op.spoke)
        driven = build_approval("/p/driven", "Overwrite?", "Overwrite?\n> 1. Yes\n  2. No")
        await n.report("driven needs input", kind="needs_input", cwd="/p/driven", approval=driven)
        assert len(op.spoke) == before   # not spoken through on_approval_speak
        assert any(m.get("type") == "approval" and m.get("cwd") == "/p/driven" for m in ws.sent)


# --- Task 2: queue WS controls + restart announcement ----------------------------

class _QueueOrch:
    """Records handle_tool_call and queue mutations for the queue WS controls."""
    controller = type("C", (), {"working_dir": "/p/loop"})()
    def __init__(self):
        self.calls = []
        self.removed = []
        self.moved = []
    async def handle_tool_call(self, n, a):
        self.calls.append((n, a))
        return {}
    def set_terminal_app(self, a): pass
    async def queue_remove(self, item_id): self.removed.append(item_id)
    async def queue_move(self, item_id, index): self.moved.append((item_id, index))


class _CollectWS:
    def __init__(self): self.sent = []
    async def send_json(self, m): self.sent.append(m)


async def test_queue_task_control_calls_tool():
    import json as _json
    from server.app import handle_client_control
    orch = _QueueOrch()
    await handle_client_control(
        _json.dumps({"type": "queue_task", "text": "bump the deps"}), orch, _CollectWS())
    assert ("queue_task", {"text": "bump the deps"}) in orch.calls


async def test_queue_remove_control_calls_orchestrator():
    import json as _json
    from server.app import handle_client_control
    orch = _QueueOrch()
    await handle_client_control(
        _json.dumps({"type": "queue_remove", "id": "abc123"}), orch, _CollectWS())
    assert orch.removed == ["abc123"]


async def test_queue_move_control_calls_orchestrator():
    import json as _json
    from server.app import handle_client_control
    orch = _QueueOrch()
    await handle_client_control(
        _json.dumps({"type": "queue_move", "id": "abc123", "index": 2}), orch, _CollectWS())
    assert orch.moved == [("abc123", 2)]


async def test_stop_control_reports_dropped_count():
    import json as _json
    from server.app import handle_client_control

    class _DropOrch:
        async def handle_tool_call(self, n, a): return {"status": "idle", "dropped": 3}
        def set_terminal_app(self, a): pass

    ws = _CollectWS()
    await handle_client_control(_json.dumps({"type": "stop"}), _DropOrch(), ws)
    assert ws.sent[-1]["type"] == "status"
    assert "dropped 3 queued tasks" in ws.sent[-1]["status"]


async def test_stop_control_without_drops_says_stopped():
    import json as _json
    from server.app import handle_client_control

    class _NoDropOrch:
        async def handle_tool_call(self, n, a): return {"status": "idle"}
        def set_terminal_app(self, a): pass

    ws = _CollectWS()
    await handle_client_control(_json.dumps({"type": "stop"}), _NoDropOrch(), ws)
    assert ws.sent[-1] == {"type": "status", "status": "stopped"}


async def test_restart_announcement_mentions_pending_queue(tmp_path, monkeypatch):
    """A session that boots with pending queued tasks for the driven project
    announces them in the spoken opening (without auto-running them)."""
    from server.task_queue import TaskQueue
    qfile = str(tmp_path / "q.json")
    monkeypatch.setenv("VOXA_TASK_QUEUE_FILE", qfile)
    # Seed two pending items for /p/loop, then let a fresh serve_ws pick them up.
    seed = TaskQueue(qfile)
    seed.add("/p/loop", "bump the deps")
    seed.add("/p/loop", "run the tests")
    async with _attached_serve_ws("/p/loop", monkeypatch) as (n, op, ws, sessions):
        opening = op.spoke[0]
        assert "2 queued tasks for loop" in opening
        assert "say run them to continue" in opening


# --- Busy mode: the mic stays open while Claude works -----------------------------

async def test_mic_forwards_while_working_when_queue_engaged(tmp_path, monkeypatch):
    """While Claude is working, mic audio keeps flowing to Gemini so the user can
    stack another instruction by voice."""
    from server.task_queue import TaskQueue
    qfile = str(tmp_path / "q.json")
    monkeypatch.setenv("VOXA_TASK_QUEUE_FILE", qfile)
    TaskQueue(qfile).add("/p/loop", "queued item")   # non-empty queue for /p/loop
    async with _attached_serve_ws("/p/loop", monkeypatch) as (n, op, ws, sessions):
        sessions.active().controller.status = "working"
        before = len(op.audio)
        await ws._in.put({"type": "websocket.receive", "bytes": b"\x01\x02"})
        for _ in range(200):
            if len(op.audio) > before:
                break
            await _asyncio2.sleep(0.005)
        assert op.audio[before:] == [b"\x01\x02"]   # forwarded despite working


async def test_mic_forwards_while_working_even_with_empty_queue(tmp_path, monkeypatch):
    """Busy mode: the mic stays OPEN while Claude works even with no queue, so a
    spoken "stop" / "status?" can reach Gemini mid-task (this replaced the old
    cost-saving pause; a voice interrupt requires listening)."""
    from server.task_queue import TaskQueue
    qfile = str(tmp_path / "q.json")
    monkeypatch.setenv("VOXA_TASK_QUEUE_FILE", qfile)
    TaskQueue(qfile)   # empty queue file so the default path is never read
    async with _attached_serve_ws("/p/loop", monkeypatch) as (n, op, ws, sessions):
        sessions.active().controller.status = "working"
        before = len(op.audio)
        await ws._in.put({"type": "websocket.receive", "bytes": b"\x01\x02"})
        for _ in range(200):
            if len(op.audio) > before:
                break
            await _asyncio2.sleep(0.005)
        assert op.audio[before:] == [b"\x01\x02"]   # forwarded: mic open while busy


async def test_approval_decision_git_action_executes_instead_of_pressing():
    from server.app import handle_client_control
    from server.approvals import build_action_approval
    from server.notifier import Notifier
    pressed, executed = [], []

    class FakeOrch:
        controller = type("C", (), {"working_dir": "/p/loop"})()
        async def handle_tool_call(self, n, a): return {}
        def set_terminal_app(self, a): pass
        async def press_key(self, k): pressed.append(k); return {"pressed": k}
        async def execute_approved_action(self, approval):
            executed.append(approval["action"])
            return {"summary": "Committed on main: fix bug."}

    class FakeWS:
        def __init__(self): self.sent = []
        async def send_json(self, m): self.sent.append(m)

    n = Notifier(_FakeCM(), push_enabled=True, ring_debounce=0)
    a = build_action_approval("/p/loop", "Commit 1 change(s): fix bug",
                              tool="git_commit",
                              action={"kind": "git_commit", "cwd": "/p/loop",
                                      "message": "fix bug"})
    n.approvals.put(a)
    ws = FakeWS()
    import json as _json
    await handle_client_control(_json.dumps(
        {"type": "approval_decision", "approval_id": a["approval_id"], "key": "y"}),
        FakeOrch(), ws, notifier=n)
    assert pressed == []                      # a git approval never presses a pane
    assert executed == [{"kind": "git_commit", "cwd": "/p/loop",
                         "message": "fix bug"}]
    assert any(m.get("type") == "approval_resolved" and m["outcome"] == "sent"
               for m in ws.sent)
    assert n.approvals.get(a["approval_id"]) is None
    assert ws.sent[-1] == {"type": "status",
                           "status": "Committed on main: fix bug."}


async def test_approval_decision_git_action_decline_skips_execution():
    from server.app import handle_client_control
    from server.approvals import build_action_approval
    from server.notifier import Notifier
    executed = []

    class FakeOrch:
        controller = type("C", (), {"working_dir": "/p/loop"})()
        async def handle_tool_call(self, n, a): return {}
        def set_terminal_app(self, a): pass
        async def press_key(self, k): return {"pressed": k}
        async def execute_approved_action(self, approval):
            executed.append(approval)
            return {"summary": "should not happen"}

    class FakeWS:
        def __init__(self): self.sent = []
        async def send_json(self, m): self.sent.append(m)

    n = Notifier(_FakeCM(), push_enabled=True, ring_debounce=0)
    a = build_action_approval("/p/loop", "s", tool="git_push",
                              action={"kind": "git_push", "cwd": "/p/loop"})
    n.approvals.put(a)
    ws = FakeWS()
    import json as _json
    await handle_client_control(_json.dumps(
        {"type": "approval_decision", "approval_id": a["approval_id"], "key": "n"}),
        FakeOrch(), ws, notifier=n)
    assert executed == []
    assert n.approvals.get(a["approval_id"]) is None
    assert any(m.get("type") == "approval_resolved" for m in ws.sent)
    assert ws.sent[-1]["type"] == "status" and "cancel" in ws.sent[-1]["status"].lower()


async def test_approval_decision_git_action_error_is_reported():
    from server.app import handle_client_control
    from server.approvals import build_action_approval
    from server.notifier import Notifier

    class FakeOrch:
        controller = type("C", (), {"working_dir": "/p/loop"})()
        async def handle_tool_call(self, n, a): return {}
        def set_terminal_app(self, a): pass
        async def press_key(self, k): return {"pressed": k}
        async def execute_approved_action(self, approval):
            return {"error": "Push failed: rejected."}

    class FakeWS:
        def __init__(self): self.sent = []
        async def send_json(self, m): self.sent.append(m)

    n = Notifier(_FakeCM(), push_enabled=True, ring_debounce=0)
    a = build_action_approval("/p/loop", "s", tool="git_push",
                              action={"kind": "git_push", "cwd": "/p/loop"})
    n.approvals.put(a)
    ws = FakeWS()
    import json as _json
    await handle_client_control(_json.dumps(
        {"type": "approval_decision", "approval_id": a["approval_id"], "key": "y"}),
        FakeOrch(), ws, notifier=n)
    # The action FAILED but the decision itself was delivered: the card clears
    # and the failure is reported as a status line the user can act on.
    assert n.approvals.get(a["approval_id"]) is None
    assert ws.sent[-1] == {"type": "status", "status": "Push failed: rejected."}


def test_lang_param_reaches_factory(tmp_path, monkeypatch):
    """The phone's ?lang= (forwarded by the bridge) reaches a factory that
    accepts it, so the Gemini session is built in the user's language."""
    monkeypatch.setenv("VOXA_DEVICES_FILE", str(tmp_path / "devices.json"))
    captured = {}
    @asynccontextmanager
    async def cap_factory(config, handle, voice="", lang=""):
        captured["lang"] = lang
        yield FakeOperator(config, handle)
    cfg = Config("k", "m", "secret", "127.0.0.1", 8787)
    client = TestClient(create_app(cfg, operator_factory=cap_factory))
    with client.websocket_connect("/ws?token=secret&lang=ar") as ws:
        ws.send_bytes(b"\x00")
    assert captured.get("lang") == "ar"
