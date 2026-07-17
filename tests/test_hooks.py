import json

from server.hooks import (
    last_assistant_text, route_hook, merge_hook, remove_hook,
    install_claude_hook, uninstall_claude_hook, hook_command, MARKER, HOOK_EVENTS,
    pre_tool_command_text, pre_tool_deny_response,
)


# --- transcript summary -------------------------------------------------------

def test_last_assistant_text_returns_last(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join([
        json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}),
        json.dumps({"type": "assistant",
                    "message": {"role": "assistant",
                                "content": [{"type": "text", "text": "First answer."}]}}),
        json.dumps({"type": "assistant",
                    "message": {"role": "assistant",
                                "content": [{"type": "text", "text": "Created index.html."}]}}),
    ]))
    assert last_assistant_text(str(p)) == "Created index.html."


def test_last_assistant_text_missing_file_is_empty():
    assert last_assistant_text("/no/such/file.jsonl") == ""


# --- route_hook ---------------------------------------------------------------

def test_userpromptsubmit_records_turn_and_is_silent():
    ts, hl = {}, {}
    out, kind = route_hook({"hook_event_name": "UserPromptSubmit", "session_id": "s"},
                     turn_start=ts, hook_last=hl, now=100.0)
    assert out is None and kind == ""
    assert ts["s"] == 100.0


def test_stop_short_turn_does_not_announce():
    ts, hl = {"s": 100.0}, {}
    out, kind = route_hook({"hook_event_name": "Stop", "session_id": "s", "cwd": "/p/app"},
                     turn_start=ts, hook_last=hl, now=110.0, min_seconds=30.0)
    assert out is None and kind == ""   # 10s turn = interactive, no call


def test_stop_long_turn_announces_finished():
    ts, hl = {"s": 100.0}, {}
    out, kind = route_hook({"hook_event_name": "Stop", "session_id": "s", "cwd": "/p/app",
                      "transcript_path": "x"},
                     turn_start=ts, hook_last=hl, now=200.0, min_seconds=30.0,
                     read_transcript=lambda _p: "Created index.html.")
    assert out == "app finished: Created index.html." and kind == "finish"


def test_stop_unknown_turn_still_announces():
    # No UserPromptSubmit seen (e.g. server started mid-session) -> err toward calling.
    ts, hl = {}, {}
    out, kind = route_hook({"hook_event_name": "Stop", "session_id": "s", "cwd": "/p/app",
                      "transcript_path": "x"},
                     turn_start=ts, hook_last=hl, now=200.0, min_seconds=30.0,
                     read_transcript=lambda _p: "Done.")
    assert out == "app finished: Done." and kind == "finish"


def test_stop_debounced_within_cooldown():
    ts, hl = {}, {"s": 195.0}
    out, kind = route_hook({"hook_event_name": "Stop", "session_id": "s", "cwd": "/p/app"},
                     turn_start=ts, hook_last=hl, now=200.0, cooldown=8.0,
                     read_transcript=lambda _p: "Done.")
    assert out is None and kind == ""   # within 8s of the last announce


def test_notification_announces_needs_input():
    ts, hl = {}, {}
    out, kind = route_hook({"hook_event_name": "Notification", "session_id": "s",
                      "cwd": "/p/app", "message": "Allow Bash?"},
                     turn_start=ts, hook_last=hl, now=10.0)
    assert out == "app needs input: Allow Bash?" and kind == "needs_input"


def test_pre_tool_use_records_context_and_stays_silent():
    pre_tool = {}
    msg, kind = route_hook(
        {"hook_event_name": "PreToolUse", "session_id": "s1", "cwd": "/p/loop",
         "tool_name": "Bash", "tool_input": {"command": "rm -rf build/"}},
        turn_start={}, hook_last={}, now=100.0, min_seconds=0, pre_tool=pre_tool)
    assert msg is None and kind == ""
    assert pre_tool["s1"]["tool_name"] == "Bash"
    assert "rm -rf build/" in pre_tool["s1"]["input_summary"]


def test_pre_tool_use_is_installed():
    from server.hooks import HOOK_EVENTS
    assert "PreToolUse" in HOOK_EVENTS


def test_notification_returns_needs_input_kind():
    msg, kind = route_hook(
        {"hook_event_name": "Notification", "session_id": "s2", "cwd": "/p/loop",
         "message": "Claude needs your permission to use Bash"},
        turn_start={}, hook_last={}, now=100.0, min_seconds=0)
    assert kind == "needs_input" and "needs input" in msg


def test_stop_returns_finish_kind(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text('{"type":"assistant","message":{"role":"assistant","content":"done"}}\n')
    msg, kind = route_hook(
        {"hook_event_name": "Stop", "session_id": "s3", "cwd": "/p/loop",
         "transcript_path": str(t)},
        turn_start={}, hook_last={}, now=100.0, min_seconds=0)
    assert kind == "finish" and "finished" in msg


# --- settings.json install ----------------------------------------------------

def test_merge_is_idempotent_and_preserves_others():
    existing = {"hooks": {"PreToolUse": [{"matcher": "Bash",
                "hooks": [{"type": "command", "command": "atuin hook claude-code"}]}]}}
    once = merge_hook(existing, "http://127.0.0.1:8787/hook?token=t")
    twice = merge_hook(once, "http://127.0.0.1:8787/hook?token=t")
    # Our events present, exactly one of ours each, even after a second merge.
    for ev in HOOK_EVENTS:
        ours = [e for e in twice["hooks"][ev]
                if any(MARKER in h["command"] for h in e["hooks"])]
        assert len(ours) == 1
    # The unrelated PreToolUse hook is preserved.
    assert any("atuin" in h["command"]
               for e in twice["hooks"]["PreToolUse"] for h in e["hooks"])


def test_remove_strips_only_ours():
    s = merge_hook({"hooks": {"PreToolUse": [{"matcher": "Bash",
            "hooks": [{"type": "command", "command": "atuin hook claude-code"}]}]}},
        "http://127.0.0.1:8787/hook?token=t")
    cleaned = remove_hook(s)
    assert "Stop" not in cleaned.get("hooks", {})
    assert any("atuin" in h["command"]
               for e in cleaned["hooks"]["PreToolUse"] for h in e["hooks"])


def test_install_and_uninstall_roundtrip(tmp_path):
    path = str(tmp_path / "settings.json")
    install_claude_hook(path, "http://127.0.0.1:8787/hook?token=abc")
    data = json.loads(open(path).read())
    assert MARKER in data["hooks"]["Stop"][0]["hooks"][0]["command"]
    uninstall_claude_hook(path)
    data = json.loads(open(path).read())
    assert "Stop" not in data.get("hooks", {})


def test_install_refuses_to_clobber_malformed_settings(tmp_path):
    import pytest
    path = tmp_path / "settings.json"
    path.write_text('{ this is not valid json ,,, ')   # a real-but-broken config
    before = path.read_text()
    with pytest.raises(ValueError):
        install_claude_hook(str(path), "http://127.0.0.1:8787/hook?token=t")
    assert path.read_text() == before                  # left untouched, not destroyed


def test_install_starts_fresh_on_empty_file(tmp_path):
    import json as _json
    path = tmp_path / "settings.json"
    path.write_text("")                                 # empty is fine -> start fresh
    install_claude_hook(str(path), "http://127.0.0.1:8787/hook?token=t")
    assert MARKER in _json.loads(path.read_text())["hooks"]["Stop"][0]["hooks"][0]["command"]


def test_hook_command_is_exit_safe_and_marked():
    cmd = hook_command("http://127.0.0.1:8787/hook?token=t")
    assert "curl" in cmd and "--data-binary @-" in cmd
    assert "; true" in cmd            # never blocks Claude if the server is down
    assert MARKER in cmd              # identifiable for idempotent install


# --- per-event hook command shape ----------------------------------------------

def test_hook_command_no_event_swallows_stdout_and_stderr():
    # Backward compatible default (no event arg) stays byte-identical to the
    # original fire-and-forget form.
    cmd = hook_command("http://127.0.0.1:8787/hook?token=t")
    assert cmd == (
        "curl -s -m 5 -X POST 'http://127.0.0.1:8787/hook?token=t' "
        "-H 'Content-Type: application/json' --data-binary @- >/dev/null 2>&1 "
        f"; true  # {MARKER}"
    )


def test_hook_command_stop_notification_userpromptsubmit_swallow_stdout():
    # These three MUST be byte-identical to the original command: UserPromptSubmit
    # especially must keep swallowing stdout since Claude Code injects that hook's
    # stdout into context.
    expected = (
        "curl -s -m 5 -X POST 'http://127.0.0.1:8787/hook?token=t' "
        "-H 'Content-Type: application/json' --data-binary @- >/dev/null 2>&1 "
        f"; true  # {MARKER}"
    )
    for event in ("Stop", "Notification", "UserPromptSubmit"):
        assert hook_command("http://127.0.0.1:8787/hook?token=t", event) == expected


def test_hook_command_pretooluse_preserves_stdout():
    cmd = hook_command("http://127.0.0.1:8787/hook?token=t", "PreToolUse")
    assert cmd == (
        "curl -s -m 5 -X POST 'http://127.0.0.1:8787/hook?token=t' "
        "-H 'Content-Type: application/json' --data-binary @- 2>/dev/null "
        f"; true  # {MARKER}"
    )
    assert "@- 2>/dev/null" in cmd    # stdout stays connected so a deny can reach Claude
    assert "@- >/dev/null" not in cmd
    assert "; true" in cmd            # decision comes from the JSON body, not exit code


def test_merge_hook_installs_per_event_commands():
    settings = merge_hook({}, "http://127.0.0.1:8787/hook?token=t")
    pre_tool_cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    stop_cmd = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert pre_tool_cmd == hook_command("http://127.0.0.1:8787/hook?token=t", "PreToolUse")
    assert stop_cmd == hook_command("http://127.0.0.1:8787/hook?token=t", "Stop")
    assert pre_tool_cmd != stop_cmd
    for event in ("Notification", "UserPromptSubmit"):
        cmd = settings["hooks"][event][0]["hooks"][0]["command"]
        assert cmd == stop_cmd


# --- PreToolUse danger gate -----------------------------------------------------

def test_pre_tool_command_text_from_dict_input():
    body = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}
    assert pre_tool_command_text(body) == "rm -rf /"


def test_pre_tool_command_text_from_dict_without_command_key():
    body = {"tool_name": "Other", "tool_input": {"foo": "bar"}}
    assert pre_tool_command_text(body) == "{'foo': 'bar'}"


def test_pre_tool_command_text_from_string_input():
    body = {"tool_name": "Bash", "tool_input": "rm -rf /"}
    assert pre_tool_command_text(body) == "rm -rf /"


def test_pre_tool_command_text_missing_input_is_empty():
    assert pre_tool_command_text({"tool_name": "Bash"}) == ""


def test_pre_tool_deny_response_for_dangerous_command():
    body = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}
    deny = pre_tool_deny_response(body)
    assert deny == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "Voxa blocked this: recursively deletes files. "
                "It recursively deletes files; ask the user to confirm before running it."
            ),
        }
    }


def test_pre_tool_deny_response_for_force_push():
    body = {"tool_name": "Bash", "tool_input": {"command": "git push --force origin main"}}
    deny = pre_tool_deny_response(body)
    assert deny["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "force-pushes over remote history" in (
        deny["hookSpecificOutput"]["permissionDecisionReason"])


def test_pre_tool_deny_response_none_for_safe_command():
    body = {"tool_name": "Bash", "tool_input": {"command": "ls -la"}}
    assert pre_tool_deny_response(body) is None


def test_pre_tool_deny_response_none_for_missing_command():
    assert pre_tool_deny_response({"tool_name": "Bash", "tool_input": {}}) is None


# --- others_mid_turn / drain_held_finishes: one call at the END of a fleet -----

from server.hooks import others_mid_turn, drain_held_finishes


def test_others_mid_turn_true_when_another_session_is_open():
    turns = {"a": 100.0, "b": 150.0}
    assert others_mid_turn(turns, "a", now=200.0, max_age=3600) is True


def test_others_mid_turn_false_when_only_self_is_open():
    turns = {"a": 100.0}
    assert others_mid_turn(turns, "a", now=200.0, max_age=3600) is False
    assert others_mid_turn({}, "a", now=200.0, max_age=3600) is False


def test_others_mid_turn_prunes_stale_entries():
    # A session killed mid-turn must not silence finish rings forever.
    turns = {"dead": 0.0, "self": 5000.0}
    assert others_mid_turn(turns, "self", now=5000.0, max_age=3600) is False
    assert "dead" not in turns


def test_drain_held_finishes_combines_and_clears():
    held = {"s1": ("loop finished: tests green", "/p/loop"),
            "s2": ("dorak finished", "/p/dorak")}
    out = drain_held_finishes(held, "veil finished: shipped it")
    assert out.startswith("All tasks are done. veil finished: shipped it. Earlier: ")
    assert "loop finished: tests green" in out and "dorak finished" in out
    assert held == {}


def test_drain_held_finishes_noop_when_nothing_held():
    assert drain_held_finishes({}, "loop finished") == "loop finished"


def test_notification_reads_claudes_actual_question(tmp_path):
    # The user answering this ring may have NO screen in sight: after the
    # generic notification text, the ring must carry Claude's actual question
    # (its last transcript message holds the options it just offered), so the
    # call presents the real choice instead of "what would you like to do?".
    t = tmp_path / "t.jsonl"
    t.write_text('{"type":"assistant","message":{"role":"assistant","content":'
                 '"The disk erase was blocked. Should I (1) skip it or (2) run it anyway?"}}\n')
    msg, kind = route_hook(
        {"hook_event_name": "Notification", "session_id": "s9", "cwd": "/p/loop",
         "message": "Claude is waiting for your input", "transcript_path": str(t)},
        turn_start={}, hook_last={}, now=50.0)
    assert kind == "needs_input"
    assert "Claude asked:" in msg
    assert "skip it" in msg and "run it anyway" in msg


def test_notification_falls_back_to_pending_tool_context(tmp_path):
    # Permission prompts leave no trailing TEXT in the transcript (the last
    # message is a tool call), so the ring must at least say WHICH tool and
    # WHAT it wants to run; that is the decision the user is being asked for.
    t = tmp_path / "t.jsonl"
    t.write_text("")  # no assistant text to read
    pre = {"s10": {"tool_name": "Bash",
                   "input_summary": "{'command': 'make deploy'}"}}
    msg, kind = route_hook(
        {"hook_event_name": "Notification", "session_id": "s10", "cwd": "/p/ti",
         "message": "Claude needs your permission to use Bash",
         "transcript_path": str(t)},
        turn_start={}, hook_last={}, now=60.0, pre_tool=pre)
    assert kind == "needs_input"
    assert "It wants to run Bash" in msg and "make deploy" in msg
