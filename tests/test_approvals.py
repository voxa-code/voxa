from server.approvals import ApprovalStore, build_approval, parse_options

CLAUDE_MENU = """\
Claude needs your permission to use Bash

  Bash(rm -rf build/)

Do you want to proceed?
> 1. Yes
  2. Yes, and don't ask again for rm commands
  3. No, and tell Claude what to do differently (esc)
"""

YESNO = "Overwrite existing file config.json? (y/n)"

PLAIN = "some ordinary output\nnothing actionable here"


def test_parse_numbered_menu_extracts_keys_and_labels():
    opts = parse_options(CLAUDE_MENU)
    assert [o["key"] for o in opts] == ["1", "2", "3"]
    assert opts[0]["label"] == "Yes"
    assert "don't ask again" in opts[1]["label"]


def test_parse_yes_no_prompt():
    opts = parse_options(YESNO)
    assert [o["key"] for o in opts] == ["y", "n"]
    assert opts[0]["label"].lower() == "yes"


def test_parse_plain_text_yields_nothing():
    assert parse_options(PLAIN) == []


def test_build_approval_object_shape():
    a = build_approval("/p/loop", "loop needs input: permission", CLAUDE_MENU, tool="Bash")
    assert a["project"] == "loop" and a["cwd"] == "/p/loop" and a["tool"] == "Bash"
    assert len(a["approval_id"]) == 12
    assert [o["key"] for o in a["options"]] == ["1", "2", "3"]
    assert a["created_at"] > 0


def test_build_approval_returns_none_without_options():
    assert build_approval("/p/loop", "s", PLAIN) is None


def test_store_put_get_resolve_and_replace():
    st = ApprovalStore()
    a1 = build_approval("/p/loop", "s", CLAUDE_MENU)
    st.put(a1)
    assert st.get(a1["approval_id"]) == a1
    assert st.active_for("/p/loop") == a1
    a2 = build_approval("/p/loop", "s2", YESNO)
    st.put(a2)                                   # same cwd replaces
    assert st.get(a1["approval_id"]) is None
    assert st.active_for("/p/loop") == a2
    assert st.latest() == a2
    assert st.resolve(a2["approval_id"]) == a2
    assert st.active_for("/p/loop") is None
    assert st.resolve("nope") is None


def test_build_action_approval_shape_and_store_roundtrip():
    from server.approvals import build_action_approval
    a = build_action_approval(
        "/p/loop", "Commit 2 change(s) in loop on branch main: fix bug",
        tool="git_commit",
        action={"kind": "git_commit", "cwd": "/p/loop", "message": "fix bug"})
    assert len(a["approval_id"]) == 12
    assert a["project"] == "loop" and a["cwd"] == "/p/loop"
    assert a["tool"] == "git_commit"
    assert [o["key"] for o in a["options"]] == ["y", "n"]
    assert a["action"] == {"kind": "git_commit", "cwd": "/p/loop", "message": "fix bug"}
    assert a["created_at"] > 0
    st = ApprovalStore()
    st.put(a)
    assert st.active_for("/p/loop") == a
    assert st.resolve(a["approval_id"]) == a


def test_build_action_approval_custom_option_labels():
    from server.approvals import build_action_approval
    a = build_action_approval("/p/loop", "Push branch main to origin/main",
                              tool="git_push",
                              action={"kind": "git_push", "cwd": "/p/loop"},
                              options=[{"key": "y", "label": "Push"},
                                       {"key": "n", "label": "Cancel"}])
    assert a["options"][0]["label"] == "Push"
