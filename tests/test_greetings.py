"""compose_opening and format_approval_for_speech: read a pending approval's
question and options aloud the moment a call is answered, so a static prompt that
was already on screen (which the live pane monitor never re-emits) is not lost."""
from __future__ import annotations

from server.greetings import compose_opening, format_approval_for_speech


def test_format_approval_reads_summary_and_all_options():
    approval = {"approval_id": "a1", "cwd": "/p/loop",
                "summary": "Bash command: rm -rf build",
                "options": [{"key": "1", "label": "Yes"},
                            {"key": "2", "label": "Yes, and don't ask again"},
                            {"key": "3", "label": "No, and tell Claude what to do differently"}]}
    s = format_approval_for_speech(approval)
    assert "rm -rf build" in s
    assert "1: Yes" in s and "don't ask again" in s and "3: No" in s


def test_format_approval_empty_for_none_and_optionless():
    assert format_approval_for_speech(None) == ""
    assert format_approval_for_speech({"summary": "x", "options": []}) == ""


# --- FIX 4: name another session's approval in the opening ------------------------

def test_format_approval_names_project_when_it_differs_from_current():
    approval = {"cwd": "/p/veil", "summary": "continue?",
                "options": [{"key": "1", "label": "Yes"}]}
    s = format_approval_for_speech(approval, current_project="loop")
    assert s.startswith("There's a prompt waiting in veil: continue? ")
    assert "1: Yes" in s


def test_format_approval_names_project_with_no_summary():
    approval = {"cwd": "/p/veil", "summary": "",
                "options": [{"key": "1", "label": "Yes"}]}
    s = format_approval_for_speech(approval, current_project="loop")
    assert s.startswith("There's a prompt waiting in veil. ")


def test_format_approval_unchanged_when_project_matches_current():
    approval = {"cwd": "/p/loop", "summary": "continue?",
                "options": [{"key": "1", "label": "Yes"}]}
    s = format_approval_for_speech(approval, current_project="loop")
    assert s.startswith("There's a prompt waiting: continue? ")
    assert "in loop" not in s


def test_format_approval_unchanged_when_cwd_missing():
    approval = {"summary": "continue?", "options": [{"key": "1", "label": "Yes"}]}
    s = format_approval_for_speech(approval, current_project="loop")
    assert s.startswith("There's a prompt waiting: continue? ")


def test_format_approval_default_current_project_keeps_todays_wording():
    # Existing single-arg callers keep working; default current_project="" means
    # any non-empty cwd basename always differs, so the label is named.
    approval = {"cwd": "/p/loop", "summary": "continue?",
                "options": [{"key": "1", "label": "Yes"}]}
    assert format_approval_for_speech(approval).startswith(
        "There's a prompt waiting in loop: continue? ")


def test_compose_opening_appends_the_pending_prompt():
    approval = {"summary": "pick a file", "options": [{"key": "1", "label": "a.py"}]}
    s = compose_opening("loop", ["loop needs input: pick a file"], approval=approval)
    assert "1: a.py" in s


def test_compose_opening_without_context_leads_with_the_prompt():
    approval = {"summary": "continue?", "options": [{"key": "1", "label": "Yes"}]}
    s = compose_opening("", [], approval=approval)
    assert "1: Yes" in s and "You're back." not in s


# --- compose_digest: ONE spoken summary for a burst of queued tasks --------------
from server.greetings import compose_digest


def _done(text="t", summary=""):
    return {"outcome": "done", "text": text, "summary": summary}


def _needs(text="t", summary=""):
    return {"outcome": "needs_input", "text": text, "summary": summary}


def _failed(text="t"):
    return {"outcome": "failed", "text": text, "summary": ""}


def test_compose_digest_all_done_plural():
    assert compose_digest("loop", [_done(), _done(), _done()]) == "3 tasks done in loop."


def test_compose_digest_single_done_is_singular():
    assert compose_digest("loop", [_done()]) == "1 task done in loop."


def test_compose_digest_no_project_omits_where():
    assert compose_digest("", [_done(), _done()]) == "2 tasks done."


def test_compose_digest_mixed_done_and_needs_input_uses_first_summary():
    s = compose_digest("loop", [_done(), _done(), _needs(summary="pick a file")])
    assert "2 done" in s
    assert "needs you: pick a file" in s
    assert "in loop" in s


def test_compose_digest_needs_input_without_summary_falls_back_to_text():
    s = compose_digest("loop", [_done(), _needs(text="bump the deps")])
    assert "1 done" in s
    assert "needs you: bump the deps" in s


def test_compose_digest_includes_failed():
    s = compose_digest("loop", [_done(), _failed()])
    assert "1 done" in s and "1 failed" in s


def test_compose_digest_empty_is_blank():
    assert compose_digest("loop", []) == ""


def test_compose_digest_never_uses_em_dash():
    s = compose_digest("loop", [_done(), _needs(summary="x")])
    assert "—" not in s


# --- split_updates_for: opening attribution across sessions ------------------

from server.greetings import split_updates_for


def test_split_updates_partitions_by_project_label():
    own, foreign = split_updates_for("ti0", [
        "ti0 finished: shipped the feature",
        "loop finished: built the thing",
        "adcli needs input: pick an option",
    ])
    assert own == ["ti0 finished: shipped the feature"]
    assert foreign == ["loop finished: built the thing",
                       "adcli needs input: pick an option"]


def test_split_updates_unlabeled_counts_as_own():
    own, foreign = split_updates_for("ti0", ["All tests green, 42 passing."])
    assert own == ["All tests green, 42 passing."]
    assert foreign == []


def test_split_updates_without_project_keeps_everything():
    own, foreign = split_updates_for("", ["loop finished: x"])
    assert own == ["loop finished: x"] and foreign == []
