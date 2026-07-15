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


def test_drop_for_removes_matching_cwd_and_returns_dropped_ids():
    st = ApprovalStore()
    a1 = build_approval("/p/loop", "s", CLAUDE_MENU)
    st.put(a1)
    dropped = st.drop_for("/p/loop")
    assert dropped == [a1["approval_id"]]
    assert st.get(a1["approval_id"]) is None
    assert st.active_for("/p/loop") is None


def test_drop_for_tolerates_trailing_slash_either_side():
    st = ApprovalStore()
    a1 = build_approval("/p/loop/", "s", CLAUDE_MENU)
    st.put(a1)
    assert st.drop_for("/p/loop") == [a1["approval_id"]]
    st2 = ApprovalStore()
    a2 = build_approval("/p/loop", "s", CLAUDE_MENU)
    st2.put(a2)
    assert st2.drop_for("/p/loop/") == [a2["approval_id"]]


def test_drop_for_leaves_other_cwds_untouched():
    st = ApprovalStore()
    mine = build_approval("/p/loop", "s", CLAUDE_MENU)
    other = build_approval("/p/unrelated", "s", CLAUDE_MENU)
    st.put(mine)
    st.put(other)
    assert st.drop_for("/p/loop") == [mine["approval_id"]]
    assert st.get(other["approval_id"]) == other


def test_drop_for_no_match_returns_empty_list():
    st = ApprovalStore()
    assert st.drop_for("/p/nothing-here") == []


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


def test_parse_options_from_raw_ansi_pane():
    # Real capture shape (tmux capture-pane -e keeps colour): SGR escapes sit in
    # front of the arrow, the digits, and the labels. This is EXACTLY what the
    # hook path feeds build_approval; before the ANSI strip it parsed to [] and
    # the phone call said "needs your permission" with no options. The SELECTED
    # row ("❯ 1. Yes") must survive too (clean_pane strips it as chrome).
    pane = (
        " Do you want to proceed?\n"
        " \x1b[38;5;153m❯\x1b[39m \x1b[38;5;246m1. \x1b[38;5;153mYes\x1b[39m\n"
        "   \x1b[38;5;246m2. Yes, and always allow access to scratchpad/\x1b[39m\n"
        "   \x1b[38;5;246m3. No\x1b[39m\n"
    )
    opts = parse_options(pane)
    assert [o["key"] for o in opts] == ["1", "2", "3"]
    assert opts[0]["label"] == "Yes"
    assert opts[2]["label"] == "No"


def test_pane_shows_live_prompt_footers():
    from server.approvals import pane_shows_live_prompt
    assert pane_shows_live_prompt(
        "Do you want to proceed?\n> 1. Yes\n  2. No\n Esc to cancel") is True
    assert pane_shows_live_prompt(
        "Pick a style:\n> 1. Bold\n  2. POV\nEnter to select · Tab/Arrow keys to navigate") is True
    # A numbered list in ordinary OUTPUT must not count as a prompt.
    assert pane_shows_live_prompt(
        "Here is my plan:\n1. build\n2. test\n3. ship\n") is False
    assert pane_shows_live_prompt("") is False


def test_pane_quoting_footer_text_mid_scrollback_is_not_a_prompt():
    # A session whose OUTPUT quotes prompt chrome (someone developing these
    # prompts, a pasted doc) has content BELOW the quote; only a footer at the
    # pane's bottom counts. This exact false positive built a phantom
    # 'stop, blue, green' menu out of a dev session's scrollback.
    from server.approvals import pane_shows_live_prompt
    quoted = (
        'menu = "Pick one:\\n> 1. Red\\n 2. Blue\\n 3. Green\\nEsc to cancel"\n'
        "assert pane_shows_live_prompt(menu)\n"
        "more code below the quote\n"
        "and more output\n"
        "then the composer footer\n"
        "bypass permissions on (shift+tab to cycle)\n"
    )
    assert pane_shows_live_prompt(quoted) is False


def test_parse_options_windows_to_block_above_footer():
    # Numbered lines high in the scrollback (an EARLIER answered menu, quoted
    # text like '1. stop') must not leak into the current menu's options.
    pane = (
        "1. stop\n"
        "2. old leftover line\n"
        + "unrelated output\n" * 30
        + "Which color do you prefer?\n"
        "❯ 1. Blue\n"
        "  2. Green\n"
        "Enter to select · Esc to cancel\n"
    )
    opts = parse_options(pane)
    assert [o["label"] for o in opts] == ["Blue", "Green"]


def test_find_live_prompt_pane_scans_and_filters():
    from server.terminals import find_live_prompt_pane
    menu = "Pick one:\n> 1. A\n  2. B\nEnter to select · Esc to cancel"
    plain = "just output\n1. not a menu\n2. also not\n"
    sessions = [
        {"backend": "tmux", "raw_id": "idle", "cwd": "/p/idle"},
        {"backend": "tmux", "raw_id": "menu", "cwd": "/p/menu"},
    ]
    panes = {"idle": plain, "menu": menu}

    def fake_run(args):
        return panes[args[-1]]           # capture-pane -t <raw_id>

    # cwd=None scans everything and returns the first LIVE prompt.
    found = find_live_prompt_pane(None, discover=lambda: sessions, run=fake_run)
    assert found == ("/p/menu", menu)
    # cwd narrows the sweep; a non-prompt pane in that cwd yields None.
    assert find_live_prompt_pane("/p/idle", discover=lambda: sessions,
                                 run=fake_run) is None
    assert find_live_prompt_pane("/p/menu", discover=lambda: sessions,
                                 run=fake_run) == ("/p/menu", menu)
