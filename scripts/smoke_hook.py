#!/usr/bin/env python3
"""End-to-end smoke test: Claude Code hook -> Voxa call pipeline.

Runs a REAL Voxa server (in-process, on a real port), installs the global-style
Claude hook into a temp settings.json, then runs the EXACT shell command Claude Code
would run on a `Stop` event (curl piping the hook JSON), and verifies the server
routed it to the "call" path. No phone, no APNs, no Gemini needed.

Run: .venv/bin/python scripts/smoke_hook.py
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_healthy(port: int, tries: int = 60) -> bool:
    for _ in range(tries):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.1)
    return False


def main() -> int:
    port = _free_port()
    token = "smoketoken"
    tmp = tempfile.mkdtemp(prefix="voxa-smoke-")
    settings = os.path.join(tmp, "settings.json")
    transcript = os.path.join(tmp, "transcript.jsonl")
    with open(transcript, "w") as f:
        f.write(json.dumps({
            "type": "assistant",
            "message": {"role": "assistant",
                        "content": [{"type": "text",
                                     "text": "Created index.html and wired the button."}]},
        }) + "\n")

    # No APNs / no watcher / any turn length counts, so the ring path is exercised
    # purely from the hook with nothing external required.
    for k in ("APNS_KEY", "APNS_KEY_PATH", "APNS_KEY_ID", "APNS_TEAM_ID", "APNS_BUNDLE_ID",
              "VOXA_RELAY_URL", "VOXA_LIVE_PROXY"):
        os.environ.pop(k, None)
    os.environ["VOXA_WATCH_TERMINALS"] = "0"
    os.environ["VOXA_HOOK_MIN_SECONDS"] = "0"
    os.environ["VOXA_DEVICES_FILE"] = os.path.join(tmp, "devices.json")
    # Ring immediately instead of waiting the production 8s quiet window
    # (server/ring_policy.py), so this smoke test stays fast.
    os.environ["VOXA_RING_QUIET_SECONDS"] = "0"

    import uvicorn
    from server.app import create_app
    from server.config import Config
    from server.hooks import install_claude_hook, hook_url

    app = create_app(Config("k", "model", token, "127.0.0.1", port))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()

    ok = False
    try:
        if not _wait_healthy(port):
            print("[smoke] FAIL: server did not come up")
            return 1
        print(f"[smoke] server up on :{port}")

        # 1) Install the hook exactly like the launcher does.
        url = hook_url("127.0.0.1", port, token)
        install_claude_hook(settings, url)
        data = json.load(open(settings))
        cmd = data["hooks"]["Stop"][0]["hooks"][0]["command"]
        assert "/hook" in cmd and token in cmd, "installed command is wrong"
        print(f"[smoke] hook installed -> {cmd}")

        # 2) Run the EXACT command Claude Code runs, piping a Stop-hook payload.
        hook_json = json.dumps({
            "hook_event_name": "Stop", "session_id": "smoke",
            "cwd": "/tmp/myproject", "transcript_path": transcript,
        })
        r = subprocess.run(["/bin/sh", "-c", cmd], input=hook_json,
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            print(f"[smoke] FAIL: hook command exited {r.returncode}: {r.stderr}")
            return 1
        print("[smoke] Stop hook delivered through the installed curl command")

        # 3) Verify the server routed it to the call path (queued for the phone),
        #    with the spoken summary taken from the transcript.
        time.sleep(0.4)
        pending = app.state.call_manager._pending
        if not any("finished" in m for m in pending):
            print(f"[smoke] FAIL: no call was queued. pending={pending!r}")
            return 1
        if not any("Created index.html" in m for m in pending):
            print(f"[smoke] FAIL: summary not taken from transcript. pending={pending!r}")
            return 1
        print(f"[smoke] call queued: {pending[-1]!r}")

        # 4) Verify the app-OPEN rule: a connected-but-not-on-a-line app must
        # still queue AND ring (see server/notifier.py's module docstring and
        # tests/test_app.py::test_hook_still_rings_when_app_open) -- "connected"
        # just means the terminals-first home screen is open, not that the
        # finish is being narrated live.
        app.state.notifier.note_client_connected()
        app.state.call_manager._pending.clear()
        subprocess.run(["/bin/sh", "-c", cmd],
                       input=json.dumps({"hook_event_name": "Stop", "session_id": "smoke2",
                                         "cwd": "/tmp/other", "transcript_path": transcript}),
                       capture_output=True, text=True, timeout=15)
        time.sleep(0.3)
        if not app.state.call_manager._pending:
            print("[smoke] FAIL: app-open (no line) should still ring. pending=[]")
            return 1
        print(f"[smoke] app-open still rang correctly: {app.state.call_manager._pending[-1]!r}")

        ok = True
    finally:
        server.should_exit = True
        t.join(timeout=5)

    print("[smoke] PASS" if ok else "[smoke] FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
