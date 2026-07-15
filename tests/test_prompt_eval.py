"""Evaluation harness for the Voxa operator system prompt.

Two layers:

1. DETERMINISTIC (always runs in CI): a behavioural SPEC plus structural
   consistency checks. It cannot prove Gemini behaves, but it guards the two
   failure classes that actually bit us: a load-bearing RULE silently dropped
   from the prompt (so a later "consolidation" can't quietly regress a hard-won
   fix), and a tool that is declared/handled but undocumented or vice versa.
   It also caps prompt length so the instruction block can't creep back up.

2. LIVE (opt-in, skipped by default): the SCENARIOS table drives a real
   OpenAI-compatible model through the prompt and asserts the tool it picks.
   Enable with VOXA_LIVE_EVAL=1 and VOXA_EVAL_BASE_URL/MODEL (e.g. a local
   MLX server). Off in CI because it needs a model and is non-deterministic.
"""
from __future__ import annotations

import os

import pytest

from server.gemini_operator import SYSTEM_INSTRUCTION, TOOL_DECLARATIONS


# ---------------------------------------------------------------------------
# The behavioural spec: each RULE is one thing the prompt must always express.
# `any_of` are interchangeable phrasings; the rule passes if ANY appears
# (case-insensitive). A consolidation pass may reword freely, but if it drops
# a rule entirely this test fails. Keep this list as the canonical record of
# WHY each instruction exists (every entry traces to a real bug or decision).
# ---------------------------------------------------------------------------

RULES: list[tuple[str, list[str], str]] = [
    ("spoken-output",
     ["spoken aloud", "read aloud", "no screen"],
     "output is TTS: short, plain, no markdown (bug: read status bar/markdown aloud)"),
    ("short-replies",
     ["one or two short sentences", "short and natural", "keep replies", "very short"],
     "voice replies must be brief"),
    ("no-markdown",
     ["no markdown", "plain speech", "plain text"],
     "TTS-hostile formatting banned"),
    ("operator-not-coder",
     ["operator, not the coder", "defer everything else", "let Claude do"],
     "allow-list framing: Voxa defers real work to Claude"),
    ("no-impersonation",
     ["never speak or act as if you are claude", "never put your own words into claude",
      "you do not author"],
     "Voxa relays Claude, never authors as Claude"),
    ("verbatim-dispatch",
     ["word for word", "verbatim", "exactly as they"],
     "send the user's request unchanged (bug: appended a tech stack)"),
    ("no-invented-task",
     ["never invent", "do not guess", "ask the user to repeat", "clearly hear"],
     "unclear/cut-off/noisy input must not become a task (bug: test_one phantom)"),
    ("examples-not-tasks",
     ["examples in these instructions are documentation",
      "illustrative phrases inside these instructions are documentation",
      "documentation for you"],
     "prompt examples must never be run (bug: ran 'create a browser game named test_one')"),
    ("relay-screen-updates",
     ["live screen", "relay these", "relay the", "screen update"],
     "surface Claude's screen updates to the user"),
    ("ignore-chrome",
     ["interface noise", "status bar", "effort level", "ui text"],
     "never read Claude Code's chrome aloud (bug: read '/effort xhigh')"),
    ("approval-actuates",
     ["resolve_approval"],
     "menu/permission answers go through resolve_approval, not chat"),
    ("git-approval",
     ["git_commit", "git_push", "pending_approval"],
     "git actions are gated behind a confirmation card"),
    ("push-names-branch",
     ["branch"],
     "a push confirmation must name the branch"),
    ("danger-gate",
     ["destructive", "irreversible", "dangerous"],
     "destructive requests return an approval, not an immediate run"),
    ("never-claim-unconfirmed",
     ["never claim", "only say", "until resolve_approval", "until the tool"],
     "do not claim an action ran before the tool confirms it"),
    ("while-working",
     ["still working", "while a task is running", "stays busy", "get_claude_status"],
     "during a run: do not answer for Claude; status/queue/stop instead"),
    ("stop-cancels",
     ["stop_claude"],
     "'stop' interrupts the current task"),
    ("queue-additional",
     ["queue_task"],
     "a new instruction mid-task is queued verbatim"),
    ("cost-by-voice",
     ["get_cost"],
     "cost/token questions call get_cost"),
    ("new-vs-current-session",
     ["new_session"],
     "'start/open a new session in X' opens a NEW terminal, never reuses the current"),
    ("set-working-dir-reuses",
     ["reuse", "relaunch", "restart"],
     "set_working_dir restarts the current terminal (must be distinguished from new_session)"),
    ("fleet-switch",
     ["switch_session"],
     "moving the voice line between running sessions keeps both alive"),
]


# Tools that are reachable from the phone UI / lifecycle rather than something
# the operator chooses by voice, so they need not be named in the prose.
_PROMPT_MENTION_EXEMPT = {"start_claude_session"}


def _handled_tool_names() -> set[str]:
    """Tool names the orchestrator actually handles, read statically from its
    source (no side effects), so a declared-but-unhandled tool is caught."""
    import server.orchestrator as orch
    src = open(orch.__file__, encoding="utf-8").read()
    return {d["name"] for d in TOOL_DECLARATIONS if f'"{d["name"]}"' in src}


# ---------------------------------------------------------------------------
# Deterministic checks
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rule_id,any_of,why", RULES, ids=[r[0] for r in RULES])
def test_prompt_expresses_rule(rule_id, any_of, why):
    low = SYSTEM_INSTRUCTION.lower()
    assert any(p.lower() in low for p in any_of), (
        f"prompt no longer expresses rule '{rule_id}' ({why}); "
        f"none of {any_of} found")


def test_every_declared_tool_is_handled():
    declared = {d["name"] for d in TOOL_DECLARATIONS}
    handled = _handled_tool_names()
    missing = declared - handled
    assert not missing, f"declared tools with no orchestrator handler: {missing}"


def test_every_actionable_tool_is_documented():
    # Every tool the operator picks by voice must be named in the prose, or the
    # model won't know when to call it.
    for d in TOOL_DECLARATIONS:
        name = d["name"]
        if name in _PROMPT_MENTION_EXEMPT:
            continue
        assert name in SYSTEM_INSTRUCTION, f"tool {name} is declared but never documented in the prompt"


def test_no_em_dash_anywhere():
    assert "—" not in SYSTEM_INSTRUCTION


def test_prompt_within_length_budget():
    # Anti-regrowth guard: the instruction block is sent on every Gemini Live
    # turn, and voice models lose the middle of long prompts. This ceiling locks
    # in the consolidated length; raising it should be a deliberate decision.
    approx_tokens = len(SYSTEM_INSTRUCTION) // 4
    assert approx_tokens <= 2850, (
        f"prompt grew to ~{approx_tokens} tokens (ceiling 2850); "
        f"consolidate instead of appending")


# ---------------------------------------------------------------------------
# Live behavioural scenarios (opt-in). Also serve as documentation of the
# intended tool choice per situation.
# ---------------------------------------------------------------------------

SCENARIOS: list[dict] = [
    {"id": "plain-task", "session": "open",
     "user": "add a dark mode toggle to the settings page",
     "expect_tool": "send_to_claude"},
    {"id": "danger", "session": "open",
     "user": "delete the whole project folder and everything in it",
     "expect_tool": "send_to_claude",   # dispatched, then the server gates it
     "note": "server danger gate turns this into an approval; the model still routes to send_to_claude"},
    {"id": "stop", "session": "working",
     "user": "stop, cancel that",
     "expect_tool": "stop_claude"},
    {"id": "status", "session": "working",
     "user": "how is it going?",
     "expect_tool": "get_claude_status"},
    {"id": "queue", "session": "working",
     "user": "when that's done, also add tests",
     "expect_tool": "queue_task"},
    {"id": "cost", "session": "open",
     "user": "how much has this session cost so far?",
     "expect_tool": "get_cost"},
    {"id": "commit", "session": "open",
     "user": "commit everything with the message initial layout",
     "expect_tool": "git_commit"},
    {"id": "diff", "session": "open",
     "user": "what did it change?",
     "expect_tool": "git_diff"},
    {"id": "new-session", "session": "open",
     "user": "start a new session in the veil folder",
     "expect_tool": "new_session"},
    {"id": "list-terminals", "session": "none",
     "user": "use one of my open terminals",
     "expect_tool": "list_terminals"},
]


def test_scenarios_reference_only_real_tools():
    # Keep the spec honest even without a live model: every expected tool must
    # actually exist.
    names = {d["name"] for d in TOOL_DECLARATIONS}
    for s in SCENARIOS:
        assert s["expect_tool"] in names, f"scenario {s['id']} expects unknown tool {s['expect_tool']}"


@pytest.mark.skipif(os.environ.get("VOXA_LIVE_EVAL") != "1",
                    reason="live eval is opt-in (set VOXA_LIVE_EVAL=1 and VOXA_EVAL_BASE_URL)")
def test_live_scenarios_pick_expected_tool():
    """Drive a real OpenAI-compatible model through the prompt and check the
    tool it chooses for each scenario. Non-deterministic, so it reports a pass
    RATE rather than demanding perfection; fails only if the model is clearly
    ignoring the prompt (under 70% correct)."""
    import json
    import urllib.request

    base = os.environ["VOXA_EVAL_BASE_URL"].rstrip("/")
    model = os.environ.get("VOXA_EVAL_MODEL", "local")
    tools = [{"type": "function", "function": {
        "name": d["name"], "description": d.get("description", ""),
        "parameters": d.get("parameters", {"type": "object", "properties": {}})}}
        for d in TOOL_DECLARATIONS]

    correct = 0
    misses = []
    for s in SCENARIOS:
        ctx = {"none": "No Claude session is running yet.",
               "open": "A Claude session is open and idle.",
               "working": "A Claude task is currently running."}[s["session"]]
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_INSTRUCTION + "\n\n[Context: " + ctx + "]"},
                {"role": "user", "content": s["user"]},
            ],
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0,
        }
        req = urllib.request.Request(
            f"{base}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {os.environ.get('VOXA_EVAL_KEY', 'x')}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
        calls = data["choices"][0]["message"].get("tool_calls") or []
        got = calls[0]["function"]["name"] if calls else "(none)"
        if got == s["expect_tool"]:
            correct += 1
        else:
            misses.append(f"{s['id']}: got {got}, expected {s['expect_tool']}")

    rate = correct / len(SCENARIOS)
    assert rate >= 0.7, f"tool-choice pass rate {rate:.0%} too low; misses: {misses}"
