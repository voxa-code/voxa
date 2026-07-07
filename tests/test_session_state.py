import json

from starlette.testclient import TestClient

from server.app import create_app
from server.config import Config
from server.session_state import SessionStateFile
from tests.test_app import fake_factory


def test_save_load_roundtrip(tmp_path):
    f = SessionStateFile(str(tmp_path / "s.json"))
    assert f.load() is None
    f.save("/p/proj")
    assert f.load() == {"cwd": "/p/proj"}
    f.clear()
    assert f.load() is None


def test_load_tolerates_corrupt_file(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{not json")
    assert SessionStateFile(str(p)).load() is None


def test_save_failure_is_silent(tmp_path):
    # Fail-open: an unwritable path must not raise into the caller. A missing
    # parent directory is created (save() makedirs), so block with a FILE where
    # the parent directory would have to go.
    (tmp_path / "blocker").write_text("")
    f = SessionStateFile(str(tmp_path / "blocker" / "s.json"))
    f.save("/p/proj")            # must not raise
    assert f.load() is None


def test_save_creates_missing_parent_dir(tmp_path):
    # The default path lives under ~/.voxa, which may not exist yet on a fresh
    # machine; save() must create it rather than silently dropping the state.
    f = SessionStateFile(str(tmp_path / "newdir" / "s.json"))
    f.save("/p/proj")
    assert f.load() == {"cwd": "/p/proj"}


def test_startup_seeds_pending_source_from_state_file(tmp_path, monkeypatch):
    state = tmp_path / "s.json"
    state.write_text(json.dumps({"cwd": "/p/lastproj"}))
    monkeypatch.setenv("VOXA_SESSION_STATE_FILE", str(state))
    monkeypatch.setenv("VOXA_DEVICES_FILE", str(tmp_path / "devices.json"))
    app = create_app(Config("k", "m", "secret", "127.0.0.1", 8787),
                     operator_factory=fake_factory)
    assert app.state.sessions.pending_source == {"cwd": "/p/lastproj"}


def test_disconnect_saves_driven_cwd(tmp_path, monkeypatch):
    state = tmp_path / "s.json"
    monkeypatch.setenv("VOXA_SESSION_STATE_FILE", str(state))
    monkeypatch.setenv("VOXA_DEVICES_FILE", str(tmp_path / "devices.json"))
    app = create_app(Config("k", "m", "secret", "127.0.0.1", 8787),
                     operator_factory=fake_factory)
    client = TestClient(app)
    with client.websocket_connect("/ws?token=secret") as ws:
        assert ws.receive_json()["status"] == "ready"
        # Simulate a session that attached to a folder mid-connection.
        app.state.sessions.default().controller.working_dir = "/p/driven"
    assert json.loads(state.read_text())["cwd"] == "/p/driven"
