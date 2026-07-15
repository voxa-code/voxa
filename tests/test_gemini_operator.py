import time

from google.genai import types

from server.config import Config
from server.gemini_operator import TOOL_DECLARATIONS, GeminiOperator, barge_in_enabled


def _cfg():
    return Config("k", "m", "t", "127.0.0.1", 8787)


async def _noop(name, args):
    return {}


def test_voice_stored():
    op = GeminiOperator(_cfg(), _noop, voice="Kore")
    assert op._voice == "Kore"


def test_voice_defaults_empty():
    op = GeminiOperator(_cfg(), _noop)
    assert op._voice == ""


def test_suppress_greeting_blocks_opening():
    op = GeminiOperator(_cfg(), _noop)
    assert op._greeted is False
    op.suppress_greeting()
    assert op._greeted is True


class _FakeSession:
    def __init__(self):
        self.spoken = []

    async def send_client_content(self, turns=None, turn_complete=True):
        self.spoken.append(turns.parts[0].text)


async def test_speak_coalesces_a_burst_into_one():
    # One action -> a burst of finished-updates as Claude's screen settles. They must
    # collapse to ONE spoken message, not one confirmation each (the repetition bug).
    op = GeminiOperator(_cfg(), _noop)
    op._session = _FakeSession()
    op._ready.set()
    op._speak_debounce = 0.01
    await op.speak("I've created an empty file named my_test.txt.")
    await op.speak("I've created a file named my_test.txt in Documents.")
    await op.speak("Created it.")
    await op._speak_task
    assert len(op._session.spoken) == 1


async def test_speak_immediate_bypasses_debounce():
    # The on-answer opening must not wait the debounce window: with a long debounce,
    # immediate=True still speaks right away (so Voxa beats the phone's fallback voice).
    op = GeminiOperator(_cfg(), _noop)
    op._session = _FakeSession()
    op._ready.set()
    op._speak_debounce = 10.0
    await op.speak("A task finished: created index.html.", immediate=True)
    await op._speak_task
    assert len(op._session.spoken) == 1


async def test_speak_skips_cross_action_duplicate():
    op = GeminiOperator(_cfg(), _noop)
    op._session = _FakeSession()
    op._ready.set()
    op._speak_debounce = 0.01
    await op.speak("Done: created index.html.")
    await op._speak_task
    before = op._speak_task
    await op.speak("Done: created index.html.")   # identical, after it was spoken
    assert op._speak_task is before                # deduped: no new flush scheduled
    assert len(op._session.spoken) == 1


async def test_relay_consumes_pending_user_turn():
    # A relay (greeting/recap/result) must not leave a user turn available for a
    # dispatch, so the agent can't echo Claude's own output back as a command.
    op = GeminiOperator(_cfg(), _noop)
    op._session = _FakeSession()
    op._ready.set()
    op._user_spoke = True
    op._speak_debounce = 0.01
    await op.speak("Ready when you are. Last session you created notes.txt.")
    await op._speak_task
    assert op._user_spoke is False
    assert op._allow_tool("send_to_claude") is False


async def test_greet_speaks_an_opening_message():
    op = GeminiOperator(_cfg(), _noop)
    op._session = _FakeSession()
    await op.greet()
    assert len(op._session.spoken) == 1
    assert "greet" in op._session.spoken[0].lower()


async def test_speak_collapses_paraphrased_renarration_in_a_burst():
    # One Claude turn settles several times; each relay is a REWORDED narration of the
    # SAME result. They must collapse to one statement, not be read out 3 times (the
    # "repeats the same things" bug). A genuinely different line (the copies) survives.
    op = GeminiOperator(_cfg(), _noop)
    op._session = _FakeSession()
    op._ready.set()
    op._speak_debounce = 0.01
    await op.speak("I've opened the voxa-ui folder in Finder. It contains two screenshots, "
                   "voxa-done.png and voxa-working.png. Do you want me to open the images "
                   "themselves, or something else?")
    await op.speak("I've opened the folder. It contains two screenshots: voxa-done.png and "
                   "voxa-working.png. Do you want me to open the images or something else?")
    await op.speak("I've made copies of both images: voxa-done copy.png and voxa-working copy.png.")
    await op._speak_task
    assert len(op._session.spoken) == 1
    spoken = op._session.spoken[0]
    assert spoken.count("two screenshots") == 1   # the re-narration is not repeated
    assert "copies" in spoken                      # the distinct action survives


def test_send_to_claude_requires_a_fresh_user_turn():
    op = GeminiOperator(_cfg(), _noop)
    # No user has spoken yet -> a self-initiated dispatch is blocked.
    assert op._allow_tool("send_to_claude") is False
    # Other tools are never gated.
    assert op._allow_tool("get_claude_status") is True
    assert op._allow_tool("list_terminals") is True
    # A real user turn licenses exactly one dispatch, then it's consumed.
    op._user_spoke = True
    assert op._allow_tool("send_to_claude") is True
    assert op._allow_tool("send_to_claude") is False   # no second auto-dispatch (no loop)


def test_fleet_tool_declarations():
    # The fleet tools must match orchestrator.handle_tool_call's names and param
    # keys EXACTLY (list_sessions takes no params, switch_session takes 'target',
    # new_session takes 'path'), or Gemini's calls silently miss their handlers.
    decls = {d["name"]: d for d in TOOL_DECLARATIONS}
    assert "list_sessions" in decls
    assert decls["list_sessions"]["parameters"]["properties"] == {}
    switch = decls["switch_session"]["parameters"]
    assert set(switch["properties"]) == {"target"}
    assert switch["properties"]["target"]["type"] == "string"
    assert switch["required"] == ["target"]
    new = decls["new_session"]["parameters"]
    assert set(new["properties"]) == {"path"}
    assert new["properties"]["path"]["type"] == "string"
    assert new["required"] == ["path"]


def test_take_screenshot_declaration_present():
    # take_screenshot must match orchestrator.handle_tool_call's name exactly and
    # take no parameters, or Gemini's call would silently miss the handler.
    decls = {d["name"]: d for d in TOOL_DECLARATIONS}
    assert "take_screenshot" in decls
    assert decls["take_screenshot"]["parameters"]["properties"] == {}


def test_queue_task_declaration_present():
    # queue_task must expose the same shape send_to_claude does (a single required
    # string `text`), so Gemini can relay an ADDITIONAL instruction verbatim while a
    # task runs and it reaches orchestrator.handle_tool_call's queue_task case.
    decls = {d["name"]: d for d in TOOL_DECLARATIONS}
    assert "queue_task" in decls
    params = decls["queue_task"]["parameters"]
    assert set(params["properties"]) == {"text"}
    assert params["properties"]["text"]["type"] == "string"
    assert params["required"] == ["text"]


def test_queue_task_gated_like_send_to_claude():
    # queue_task is a real user request too, never the operator's own words: it
    # requires a fresh user turn and CONSUMES it, exactly like send_to_claude, so the
    # agent can't queue work on its own or split a request into steps.
    op = GeminiOperator(_cfg(), _noop)
    assert op._allow_tool("queue_task") is False       # no user turn yet -> blocked
    op._user_spoke = True
    assert op._allow_tool("queue_task") is True        # a real user turn licenses one
    assert op._allow_tool("queue_task") is False       # consumed: no second auto-queue
    # And a consumed queue turn does not license a send_to_claude either (shared guard).
    op._user_spoke = True
    assert op._allow_tool("queue_task") is True
    assert op._allow_tool("send_to_claude") is False


def test_git_tool_declarations_present_and_typed():
    from server.gemini_operator import TOOL_DECLARATIONS
    by_name = {d["name"]: d for d in TOOL_DECLARATIONS}
    assert {"git_status", "git_diff", "git_commit", "git_push"} <= set(by_name)
    commit = by_name["git_commit"]
    assert commit["parameters"]["required"] == ["message"]
    assert commit["parameters"]["properties"]["push"]["type"] == "boolean"
    assert by_name["git_push"]["parameters"]["properties"] == {}


def test_git_tools_are_not_loop_guarded():
    op = GeminiOperator(_cfg(), _noop)
    for name in ("git_status", "git_diff", "git_commit", "git_push"):
        assert op._allow_tool(name) is True


def test_system_instruction_has_operator_principles_preamble():
    # The "HOW YOU WORK" preamble encodes the five researched voice-operator
    # patterns (spoken-output constraint, operator-not-coder allow-list,
    # no-impersonation, clarity gate, examples-are-not-tasks) and must appear
    # BEFORE the folder/task detail sections so it frames everything after it.
    from server.gemini_operator import SYSTEM_INSTRUCTION as s
    assert "HOW YOU WORK" in s
    assert s.index("HOW YOU WORK") < s.index("CHOOSING THE FOLDER")
    low = s.lower()
    assert "spoken aloud" in low and "no screen" in low          # output channel
    assert "operator, not the coder" in low                       # allow-list framing
    assert "defer everything else to" in low
    assert "never speak or act as if you are claude" in low       # no impersonation
    assert "clear request you actually heard" in low              # clarity gate
    assert "—" not in s


def test_system_instruction_covers_git_flow():
    from server.gemini_operator import SYSTEM_INSTRUCTION
    s = SYSTEM_INSTRUCTION
    assert "git_diff" in s and "git_status" in s
    assert "git_commit" in s and "git_push" in s
    # The prompt must route confirmation through resolve_approval and forbid
    # claiming a commit or push happened before the tool confirms it.
    git_section = s[s.index("GIT BY VOICE"):]
    assert "resolve_approval" in git_section
    assert "branch" in git_section


def test_system_instruction_covers_multiple_sessions():
    # Fix 5: the operator must be told several sessions can run at once, how to
    # recognize which project a relayed update belongs to, never attribute one
    # session's output to another, and to switch_session (or ask) instead of
    # guessing when a request targets a session other than the attached one.
    from server.gemini_operator import SYSTEM_INSTRUCTION as s
    assert "MULTIPLE SESSIONS" in s
    # Placed near the existing SESSIONS (fleet) paragraph.
    assert s.index("SESSIONS (a fleet)") < s.index("MULTIPLE SESSIONS")
    section = s[s.index("MULTIPLE SESSIONS"):]
    low = section.lower()
    assert "[project]" in low
    assert "never attribute" in low or "never" in low and "attribute" in low
    assert "switch_session" in section
    assert "ask" in low
    assert "—" not in s


def test_system_instruction_routes_start_new_session_to_new_session():
    # "start a new session in <folder>" must open a NEW terminal (new_session),
    # never repurpose the current one (set_working_dir). The prompt must make the
    # distinction explicit: new_session for starting new work in a folder, and
    # set_working_dir clearly described as REUSING/relaunching the current terminal.
    from server.gemini_operator import SYSTEM_INSTRUCTION
    s = SYSTEM_INSTRUCTION
    low = s.lower()
    assert "start a new session" in low
    assert "new_session" in s
    # The instruction ties the "start a new session in a folder" phrasing to
    # new_session, and warns that set_working_dir reuses/relaunches the current one.
    assert "reuse" in low or "relaunch" in low or "restart" in low
    # No em dash anywhere in the prompt.
    assert "—" not in s


class _FakeUsage:
    def __init__(self, prompt_token_count, response_token_count):
        self.prompt_token_count = prompt_token_count
        self.response_token_count = response_token_count


class _FakeLiveResponse:
    def __init__(self, usage_metadata=None):
        self.data = None
        self.server_content = None
        self.tool_call = None
        self.session_resumption_update = None
        self.go_away = None
        self.usage_metadata = usage_metadata


class _FakeReceiveSession:
    """Mimics session.receive(): the first call yields the given responses,
    every call after that raises so run()'s outer `while True` terminates
    instead of looping forever (there's no resume handle in this test, so
    run() re-raises and returns)."""

    def __init__(self, responses):
        self._responses = responses
        self._calls = 0

    async def receive(self):
        self._calls += 1
        if self._calls > 1:
            raise RuntimeError("connection closed")
        for r in self._responses:
            yield r


async def test_usage_out_called_with_token_counts():
    import pytest
    op = GeminiOperator(_cfg(), _noop)
    usage_events = []
    op.set_usage_out(usage_events.append)
    op.suppress_greeting()   # skip greet(), which needs send_client_content
    op._session = _FakeReceiveSession([_FakeLiveResponse(_FakeUsage(120, 45))])
    with pytest.raises(RuntimeError):
        await op.run()
    assert usage_events == [{"tokens_in": 120, "tokens_out": 45}]


def test_lang_stored_and_defaults_empty():
    assert GeminiOperator(_cfg(), _noop, lang="ar")._lang == "ar"
    assert GeminiOperator(_cfg(), _noop)._lang == ""


def test_build_config_arabic_sets_language_code_and_prompt():
    # lang=ar must reach Gemini twice: SpeechConfig.language_code steers the TTS,
    # and a LANGUAGE block in the system prompt steers the words (including
    # rendering English-injected 'Tell the user:' relays in Arabic).
    op = GeminiOperator(_cfg(), _noop, voice="Kore", lang="ar")
    cfg = op._build_config()
    assert cfg.speech_config.language_code == "ar-XA"
    assert cfg.speech_config.voice_config.prebuilt_voice_config.voice_name == "Kore"
    assert "LANGUAGE:" in cfg.system_instruction
    assert "Arabic" in cfg.system_instruction
    assert "Tell the user" in cfg.system_instruction


def test_build_config_english_is_unchanged():
    # en (or empty) must be a byte-for-byte no-op against today's behavior.
    op = GeminiOperator(_cfg(), _noop, voice="Kore", lang="en")
    cfg = op._build_config()
    assert getattr(cfg.speech_config, "language_code", None) in (None, "")
    assert "LANGUAGE:" not in cfg.system_instruction


def test_build_config_lang_without_voice_still_sets_language_code():
    op = GeminiOperator(_cfg(), _noop, lang="ar")
    cfg = op._build_config()
    assert cfg.speech_config.language_code == "ar-XA"
    assert cfg.speech_config.voice_config is None


def test_get_cost_tool_declaration_present_and_no_params():
    decls = {d["name"]: d for d in TOOL_DECLARATIONS}
    assert "get_cost" in decls
    assert decls["get_cost"]["parameters"]["properties"] == {}
    assert "token" in decls["get_cost"]["description"].lower()


def test_get_cost_tool_is_not_loop_guarded():
    op = GeminiOperator(_cfg(), _noop)
    assert op._allow_tool("get_cost") is True


def test_system_instruction_covers_cost_by_voice():
    from server.gemini_operator import SYSTEM_INSTRUCTION as s
    low = s.lower()
    assert "get_cost" in s
    assert "cost" in low and "token" in low
    # No em dash anywhere in the prompt (still true after this addition).
    assert "—" not in s


class _FakeContentSession:
    """Captures send_client_content payloads (text + turn_complete)."""

    def __init__(self):
        self.turns = []

    async def send_client_content(self, turns=None, turn_complete=None):
        self.turns.append((turns.parts[0].text, turn_complete))


async def test_open_with_context_sends_background_and_opening_in_one_turn():
    op = GeminiOperator(_cfg(), _noop)
    fake = _FakeContentSession()
    op._session = fake
    op._ready.set()
    await op.open_with_context("Hi. You're back in loop.",
                               "You: fix the tests\nClaude: done, 42 passing")
    assert len(fake.turns) == 1
    text, complete = fake.turns[0]
    assert complete is True
    assert "for your context only" in text
    assert "42 passing" in text
    assert text.rstrip().endswith("Tell the user: Hi. You're back in loop.")
    # The background must come BEFORE the spoken directive.
    assert text.index("42 passing") < text.index("Tell the user:")


async def test_open_with_context_without_context_falls_back_to_speak():
    op = GeminiOperator(_cfg(), _noop)
    fake = _FakeContentSession()
    op._session = fake
    op._ready.set()
    await op.open_with_context("Hi there.", "")
    # speak() debounces through a task; let it flush.
    import asyncio as _a
    await _a.sleep(0.05)
    if op._speak_task:
        await op._speak_task
    assert len(fake.turns) == 1
    text, _ = fake.turns[0]
    assert text == "Tell the user: Hi there."


# --- Task D1: barge-in mode with tuned VAD ----------------------------------

def test_barge_in_enabled_by_default(monkeypatch):
    monkeypatch.delenv("VOXA_BARGE_IN", raising=False)
    assert barge_in_enabled() is True


def test_barge_in_disabled_by_env_zero(monkeypatch):
    monkeypatch.setenv("VOXA_BARGE_IN", "0")
    assert barge_in_enabled() is False


def test_barge_in_disabled_by_env_false(monkeypatch):
    monkeypatch.setenv("VOXA_BARGE_IN", "false")
    assert barge_in_enabled() is False


def test_barge_in_disabled_by_empty_env(monkeypatch):
    monkeypatch.setenv("VOXA_BARGE_IN", "")
    assert barge_in_enabled() is False


def test_build_config_barge_in_on_uses_automatic_activity_detection(monkeypatch):
    # Default (unset) env -> barge-in ON: Gemini's own VAD handles turn taking,
    # tuned for a quick, confident interrupt, and NO_INTERRUPTION must be gone.
    monkeypatch.delenv("VOXA_BARGE_IN", raising=False)
    monkeypatch.delenv("VOXA_VAD_SILENCE_MS", raising=False)
    op = GeminiOperator(_cfg(), _noop)
    cfg = op._build_config()
    ric = cfg.realtime_input_config
    assert ric.activity_handling != types.ActivityHandling.NO_INTERRUPTION
    aad = ric.automatic_activity_detection
    assert aad is not None
    assert aad.start_of_speech_sensitivity == types.StartSensitivity.START_SENSITIVITY_HIGH
    assert aad.end_of_speech_sensitivity == types.EndSensitivity.END_SENSITIVITY_HIGH
    assert aad.prefix_padding_ms == 40
    assert aad.silence_duration_ms == 300


def test_build_config_barge_in_on_respects_vad_silence_env(monkeypatch):
    monkeypatch.delenv("VOXA_BARGE_IN", raising=False)
    monkeypatch.setenv("VOXA_VAD_SILENCE_MS", "500")
    op = GeminiOperator(_cfg(), _noop)
    cfg = op._build_config()
    assert cfg.realtime_input_config.automatic_activity_detection.silence_duration_ms == 500


def test_build_config_barge_in_off_matches_today_no_interruption(monkeypatch):
    # VOXA_BARGE_IN=0 must reproduce today's config verbatim: NO_INTERRUPTION,
    # no automatic activity detection tuning.
    monkeypatch.setenv("VOXA_BARGE_IN", "0")
    op = GeminiOperator(_cfg(), _noop)
    cfg = op._build_config()
    ric = cfg.realtime_input_config
    assert ric.activity_handling == types.ActivityHandling.NO_INTERRUPTION
    assert ric.automatic_activity_detection is None


class _FakeAudioSession:
    """Captures send_realtime_input calls for send_audio half-duplex tests."""

    def __init__(self):
        self.sent = []

    async def send_realtime_input(self, audio=None):
        self.sent.append(audio)


async def test_send_audio_forwards_during_playback_when_barge_in_on(monkeypatch):
    monkeypatch.delenv("VOXA_BARGE_IN", raising=False)
    op = GeminiOperator(_cfg(), _noop)
    op._session = _FakeAudioSession()
    op._ready.set()
    op._play_until = time.monotonic() + 10.0   # reply still "playing"
    await op.send_audio(b"\x01\x02")
    assert len(op._session.sent) == 1


async def test_send_audio_drops_during_playback_when_barge_in_off(monkeypatch):
    monkeypatch.setenv("VOXA_BARGE_IN", "0")
    op = GeminiOperator(_cfg(), _noop)
    op._session = _FakeAudioSession()
    op._ready.set()
    op._play_until = time.monotonic() + 10.0   # reply still "playing"
    await op.send_audio(b"\x01\x02")
    assert len(op._session.sent) == 0


class _FakeServerContent:
    def __init__(self, interrupted=False):
        self.interrupted = interrupted
        self.output_transcription = None
        self.input_transcription = None


async def test_speak_dedupe_key_scopes_cross_call_check():
    # Fix 3: two DIFFERENT sessions' updates must never collapse into one another
    # just because they happen to arrive in the same dedupe window. Different keys
    # each get their own "last spoken" slot.
    op = GeminiOperator(_cfg(), _noop)
    op._session = _FakeSession()
    op._ready.set()
    op._speak_debounce = 0.01
    await op.speak("veil finished: tests pass", dedupe_key="/a")
    await op._speak_task
    await op.speak("loop finished: tests pass", dedupe_key="/b")
    await op._speak_task
    assert len(op._session.spoken) == 2   # neither dropped


async def test_speak_dedupe_key_same_key_same_text_is_dropped():
    op = GeminiOperator(_cfg(), _noop)
    op._session = _FakeSession()
    op._ready.set()
    op._speak_debounce = 0.01
    await op.speak("veil finished: tests pass", dedupe_key="/a")
    await op._speak_task
    before = op._speak_task
    await op.speak("veil finished: tests pass", dedupe_key="/a")   # identical, same key
    assert op._speak_task is before   # deduped: no new flush scheduled
    assert len(op._session.spoken) == 1


async def test_speak_unkeyed_default_matches_todays_behavior():
    # Default dedupe_key="" for callers that don't pass one (e.g. the hub attach
    # relay's `lambda t: operator.speak(t)`) keeps today's single-slot dedupe.
    op = GeminiOperator(_cfg(), _noop)
    op._session = _FakeSession()
    op._ready.set()
    op._speak_debounce = 0.01
    await op.speak("Done: created index.html.")
    await op._speak_task
    before = op._speak_task
    await op.speak("Done: created index.html.")
    assert op._speak_task is before
    assert len(op._session.spoken) == 1


async def test_flush_speak_keeps_two_burst_lines_with_different_tags():
    # Fix 3: a burst that happens to contain two DIFFERENT sessions' bracket-tagged
    # lines must keep both even if they're highly similar; only the tag-matching (or
    # untagged) collapse logic applies.
    op = GeminiOperator(_cfg(), _noop)
    op._session = _FakeSession()
    op._ready.set()
    op._speak_debounce = 0.01
    await op.speak("[veil] finished: tests pass, 3 skipped.")
    await op.speak("[loop] finished: tests pass, 3 skipped.")
    await op._speak_task
    assert len(op._session.spoken) == 1
    spoken = op._session.spoken[0]
    assert "[veil]" in spoken and "[loop]" in spoken


async def test_flush_speak_untagged_lines_still_collapse_as_today():
    op = GeminiOperator(_cfg(), _noop)
    op._session = _FakeSession()
    op._ready.set()
    op._speak_debounce = 0.01
    await op.speak("Done: created index.html.")
    await op.speak("Done, created index.html.")   # near-identical, no tags
    await op._speak_task
    spoken = op._session.spoken[0]
    assert spoken.count("index.html") == 1


def test_line_tag_helper():
    from server.gemini_operator import _line_tag
    assert _line_tag("[veil] finished: ok") == "veil"
    assert _line_tag("plain text, no tag") == ""
    assert _line_tag("[loop] ") == "loop"


async def test_run_resets_stale_play_until_on_interrupted():
    # A stale playback window must never gate the mic after an interruption in
    # half-duplex mode: sc.interrupted must reset _play_until to 0.0.
    import pytest
    op = GeminiOperator(_cfg(), _noop)
    flushed = []

    async def _text_out(msg):
        flushed.append(msg)

    op.set_text_out(_text_out)
    op.suppress_greeting()
    op._play_until = time.monotonic() + 50.0   # stale window from a prior reply
    resp = _FakeLiveResponse()
    resp.server_content = _FakeServerContent(interrupted=True)
    op._session = _FakeReceiveSession([resp])
    with pytest.raises(RuntimeError):
        await op.run()
    assert op._play_until == 0.0
    assert {"type": "flush_audio"} in flushed
