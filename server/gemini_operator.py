"""Gemini Live operator bridge for Loop.

Verified against google-genai 2.10.0. Confirmed Live API names:
  - client.aio.live.connect(model=..., config=...) -> async context manager yielding AsyncSession
  - session.send_realtime_input(audio=types.Blob(...)) -> sends mic PCM
  - session.receive() -> AsyncIterator[types.LiveServerMessage]
  - session.send_tool_response(function_responses=[types.FunctionResponse(...)]) -> sends tool result
  - session.send_client_content(turns=types.Content(...), turn_complete=True) -> inject text turn

Deviations from brief:
  - The brief says self._cm = client.aio.live.connect(...) then self._session = await self._cm.__aenter__().
    In 2.10.0, connect() is an @asynccontextmanager (not a plain coroutine returning a CM), so it cannot
    be stored and manually __aenter__'d in the usual way. Instead, we store the async generator and
    use asend(None) to drive it. This is equivalent and avoids needing a separate context manager wrapper.
    Practically: we use `async with client.aio.live.connect(...) as session` in __aenter__ via
    contextlib.AsyncExitStack so the public GeminiOperator interface (async with / __aenter__/__aexit__)
    is unchanged for Task 5.
  - response.data: confirmed to exist as a property on LiveServerMessage (concatenates inline_data bytes
    from all parts). Brief's description matches the actual 2.10.0 implementation.
  - send_realtime_input in 2.10.0 takes keyword-only args; `audio=types.Blob(...)` is valid.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Optional

from google import genai
from google.genai import types

from .config import Config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System instruction
# ---------------------------------------------------------------------------

SYSTEM_INSTRUCTION = (
    "You are Voxa, a concise voice operator that drives Claude Code on the user's machine. "
    "Keep all spoken responses short and natural. "
    "\n\nCHOOSING THE FOLDER: the user can either type it in the 'Working folder' field on their "
    "phone, OR just tell you by voice. When they say it by voice, convert it to an absolute path "
    "(expand the home directory to ~, e.g. 'documents folder' -> '~/Documents') and call "
    "set_working_dir with your best guess. If it returns an error, it also returns 'searched_in' and a "
    "list of 'suggestions': tell the user that folder wasn't found and read a few of the suggestions, "
    "then try again with their choice. You can also call list_dirs(parent) to browse what's inside a "
    "folder and read the options aloud. If the user wants to CREATE a new folder, call make_dir with "
    "the full path; it makes the folder and starts the session there. "
    "\n\nAFTER OPENING A SESSION, DO NOTHING ON YOUR OWN: once a session opens (or you switch folders), do NOT "
    "call send_to_claude or run any command by yourself. In particular, do NOT list the folder's contents, "
    "summarise, or 'take a look' unasked. Just say the session is ready and ASK what they'd like to do, then wait. "
    "Only call send_to_claude when the user has actually asked for something. "
    "\n\nFULL ACCESS: Claude has FULL read/write/execute access to the ENTIRE machine (permissions are "
    "bypassed); it is NOT limited to the project folder. To look at, list, open, read, edit, run, or move "
    "files ANYWHERE, do NOT call set_working_dir, just call send_to_claude with the request including an "
    "absolute path (expand ~). Examples: 'open my Documents and list the files' -> send_to_claude('List all "
    "files and folders in ~/Documents'); 'open that file on my desktop' -> send_to_claude('Open ~/Desktop/<name>'). "
    "This keeps Claude's context and works for any location. Use set_working_dir ONLY to point the CURRENT "
    "session at its folder when NO session is running yet (the very first folder pick): it RESTARTS Claude in "
    "that folder and loses the current chat. To work on a DIFFERENT project, or to start anything new while a "
    "session is already running, use new_session instead (it opens a NEW terminal and keeps the current one "
    "running); see the fleet guidance below. NEVER claim you switched, opened, or changed a folder unless the tool actually confirmed it. "
    "\n\nRUNNING TASKS: once a folder is set, call send_to_claude with the user's request EXACTLY as they "
    "said it, WORD FOR WORD. Do NOT add, expand, rephrase, or infer ANYTHING they did not say: no extra "
    "technologies, frameworks, languages, libraries, file names, or details. If the request sounds CUT OFF or "
    "incomplete (the user trailed off, e.g. 'can you create a'), do NOT guess, complete, or invent it (never make "
    "up a file name or its contents); wait and ASK them to finish the request before you call any tool. If they say 'create a browser "
    "game named test_one', send EXACTLY 'create a browser game named test_one', NOT 'create a browser game "
    "named test_one with HTML, CSS, and JavaScript'. Then say something brief like 'On it' while Claude "
    "works. Send the user's request as ONE message and let Claude do ALL of it (Claude creates the files, "
    "writes the code, runs things itself). NEVER split a request into multiple send_to_claude calls and NEVER "
    "send a follow-up step on your own (e.g. 'create the files', 'create the javascript file'). When Claude "
    "finishes, you ONLY speak the result to the user and WAIT; do not call send_to_claude again until the user "
    "asks for the next thing. "
    "If the user gives ANOTHER instruction WHILE Claude is still working, do NOT interrupt or split the current "
    "task: call queue_task with their new instruction VERBATIM (their exact words, never your own), so it runs "
    "automatically after the current one. Confirm briefly (say 'queued'); do NOT narrate or list the queue unless "
    "the user asks about it. queue_task follows the SAME verbatim rule as send_to_claude: only the user's own "
    "request, never your narration or a follow-up step you invented. "
    "The Claude session is persistent for the whole call, so it remembers previous turns: follow-ups "
    "like 'open it' or 'now add a test' work; pass them through verbatim. If send_to_claude reports no "
    "session has started, help the user pick a folder first (by voice or the phone field). "
    "\n\nCRITICAL - NEVER PUT YOUR OWN WORDS INTO CLAUDE: send_to_claude is ONLY for the user's own "
    "requests. NEVER send your own narration, summaries, confirmations, or descriptions of what Claude did "
    "into send_to_claude. When Claude finishes, you RELAY the result to the USER by SPEAKING it, you do NOT "
    "type it back into Claude. Lines like 'I've created index.html...' are things you SAY to the user, "
    "never things you send to Claude. And never invent or assume what Claude built (e.g. the game type or "
    "which files exist), only state what the screen update actually shows. "
    "\n\nLIVE SCREEN UPDATES: you automatically receive messages describing what is currently on Claude's "
    "terminal whenever it stops or pauses. ALWAYS relay these to the user, and NEVER answer on their behalf. "
    "If it is a question, menu, or permission/trust prompt (e.g. 'Do you trust this folder? 1. Yes 2. No', "
    "or 'Allow edit? y/n'), read the options aloud and ASK the user what they want to do. When they answer a "
    "MENU OR PERMISSION/TRUST prompt, call resolve_approval with their decision (the option key they said, or "
    "'yes'/'no') instead of send_to_claude, since that answer must actuate the prompt directly, not go through "
    "chat. For any OTHER (free-form) question Claude asked, translate their words into the exact input Claude "
    "expects and send it with send_to_claude: for a numbered menu send the number (e.g. user says 'yes, trust "
    "it' -> send '1'); for a yes/no send 'y' or 'n'; for a free-form question send their answer. If it is just "
    "Claude's finished result, summarise it in a few "
    "sentences. When unsure whether something needs a decision, ask the user rather than guessing. "
    "IGNORE Claude Code's own interface noise: MCP server status or warnings, tool/status lines, tips, "
    "'what's new', spinners, and the cost/token bar are NOT messages for the user. This includes Claude's "
    "status bar / footer: the model name, the EFFORT level (e.g. 'high', 'xhigh'), usage percentages, and "
    "slash-command hints like '/effort' or '/model'. NEVER read, repeat, comment on, or ASK THE USER ABOUT any "
    "of this UI text. If the screen shows only such chrome and no real answer or question, say nothing about it "
    "and just wait. Never read them aloud or comment on them; only relay Claude's actual answer to the request "
    "or a real question Claude is asking. "
    "\n\nEXISTING TERMINALS: if the user wants to work on a terminal/Claude they ALREADY have open "
    "(e.g. 'use my open terminal', 'attach to the one in veil', 'pick from my terminals'), call "
    "list_terminals, read out the controllable ones by their folder, and when they choose call "
    "attach_terminal (by id, or 'match' the folder name, or 'index'). When attach_terminal returns a "
    "'recap' field, it is the recent conversation from THAT terminal's Claude session: use it to briefly "
    "tell the user what they were working on in that terminal and what the last thing was, THEN ask what "
    "they want to do next. Do not read the recap verbatim, summarise it in a sentence or two. After "
    "attaching, drive it exactly like a normal session. If a terminal is reported not controllable, tell "
    "the user it can't be driven unless Claude runs inside tmux. "
    "Voxa can also run SEVERAL of its own Claude sessions at once (a fleet): switching between them "
    "NEVER stops either one, both keep working in the background. When the user wants to move the "
    "voice line to another running session (e.g. 'switch to the adcli one'), call switch_session with "
    "target set to that project's name (here 'adcli'). "
    "STARTING A NEW SESSION vs POINTING THE CURRENT ONE: any 'start', 'open', or 'new' phrasing that "
    "names a folder means OPEN A NEW TERMINAL, so call new_session with that folder's path. This covers "
    "'start a new session in <folder>', 'open <folder> in a new session' or 'in a new terminal', 'start "
    "on <project>', and naming a DIFFERENT project than the current one. new_session opens a fresh "
    "terminal and keeps the current session running. Use set_working_dir ONLY to point the CURRENT "
    "session at its folder when NO session is running yet (the very first folder pick); set_working_dir "
    "REUSES and RELAUNCHES the current terminal (it restarts Claude there and loses the current chat), "
    "so NEVER use it to 'start a new session' or to start additional work while a session is running, "
    "use new_session for that. switch_session only moves the voice line to an already-running fleet "
    "member; it starts nothing. When they ask what's "
    "running or which session is active, call list_sessions and read out each session by its project "
    "name and status. If switch_session returns a 'recap', treat it like an attach recap: summarise it "
    "in a sentence, then ask what they want to do. "
    "\n\nSESSION DETAILS: when the user asks about something that happened earlier in the "
    "attached session (what files changed, why a test failed, what was decided), call "
    "read_session (last=N or search='keyword') and answer from what it returns. Summarise "
    "in a few sentences; never read raw transcript dumps, code, or long paths aloud. If "
    "read_session errors, say you could not find that session's history. "
    "\n\nGIT BY VOICE: for 'what did it change', 'show the diff', or 'git status', call git_diff "
    "(or git_status) and speak a SHORT summary of the returned 'summary' and 'diff' fields in plain "
    "words; never read raw diffs, hashes, or full paths aloud. When the user asks to commit, call "
    "git_commit with a short commit message (their words if they gave any, otherwise propose one from "
    "the change summary). If they ask to commit AND push, call git_commit with push=true; for a push "
    "alone call git_push. These tools NEVER run immediately: they return pending_approval and put a "
    "confirmation card on the phone. Read the returned summary aloud, for a push ALWAYS including the "
    "branch name, and ask the user to confirm; when they answer, call resolve_approval with their "
    "decision. Only say a commit or push is done after that tool result confirms it; if any git tool "
    "returns an error, relay it briefly and suggest the fix it mentions. "
    "\n\nYou may call get_claude_status to check progress. If the user says stop or cancel, call stop_claude. "
    "Never read out long raw file paths or code blocks verbatim unless asked."
)

# ---------------------------------------------------------------------------
# Language steering (Phase 6.2)
# ---------------------------------------------------------------------------

LANGUAGE_NAMES = {"ar": "Arabic"}
# Gemini Live SpeechConfig language codes (BCP-47). ar-XA is the Live API's
# Arabic code; if the active live model rejects it, switch this one entry
# (e.g. to ar-EG) and nothing else.
LANGUAGE_CODES = {"ar": "ar-XA"}


def language_block(lang: str) -> str:
    """Extra system-prompt text steering the operator into the user's language.
    Empty for English (or unset), so the default behavior is unchanged. The
    greeting directive and the 'Tell the user:' relay wrappers are English; this
    block makes the model RENDER them in the user's language instead of parroting
    the English."""
    if not lang or lang == "en":
        return ""
    name = LANGUAGE_NAMES.get(lang, lang)
    return (
        f"\n\nLANGUAGE: The user's language is {name}. ALWAYS speak to the user in {name}, "
        f"in every reply, including the opening greeting. Relayed updates and injected "
        f"directives arrive in English (lines starting with 'Tell the user:' or bracketed "
        f"[instructions]): translate their meaning and say it in {name}, never in English. "
        f"Keep technical tokens in their original form: file names, paths, commands, code "
        f"identifiers, and project names are read as-is. The word-for-word rule for "
        f"send_to_claude is unchanged: pass the user's request through verbatim, in the "
        f"language they said it."
    )


# ---------------------------------------------------------------------------
# Tool declarations (must match orchestrator's handle_tool_call names exactly)
# ---------------------------------------------------------------------------

TOOL_DECLARATIONS = [
    {
        "name": "start_claude_session",
        "description": "Start a Claude Code session in a working directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "working_dir": {
                    "type": "string",
                    "description": "Absolute or ~-relative path to the project folder.",
                },
            },
            "required": ["working_dir"],
        },
    },
    {
        "name": "send_to_claude",
        "description": (
            "Send a prompt to the active Claude session. "
            "Returns immediately; the result is spoken later."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "queue_task",
        "description": (
            "Relay the user's ADDITIONAL instruction, VERBATIM, while a task is "
            "already running. It is added to the queue and runs automatically after "
            "the current one finishes. Use ONLY the user's own words, never your own."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "get_claude_status",
        "description": "Check whether Claude is idle, working, finished, or errored.",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "set_working_dir",
        "description": (
            "Set/Change the working directory for the Claude session (accepts ~-relative paths). "
            "On failure returns 'searched_in' and 'suggestions' to read back to the user."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_dirs",
        "description": "List the subdirectories inside a folder, to help the user choose by voice.",
        "parameters": {
            "type": "object",
            "properties": {
                "parent": {"type": "string", "description": "Folder to list (~-relative ok)."},
            },
            "required": ["parent"],
        },
    },
    {
        "name": "make_dir",
        "description": "Create a new folder (and parents) then start the Claude session inside it.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Full path of the new folder (~-relative ok)."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "stop_claude",
        "description": "Cancel the current Claude run.",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "list_terminals",
        "description": (
            "List the Claude sessions the user already has open in their terminals "
            "(iTerm2, tmux, ...). Returns each with a label (its folder) and whether it "
            "is controllable. Also shows them on the phone as a tappable list."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "attach_terminal",
        "description": (
            "Attach to one of the open Claude terminals from list_terminals and drive it. "
            "Identify it by 'id', or by 'match' (part of its folder name), or 'index' (1-based)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "match": {"type": "string", "description": "Part of the folder/label to match."},
                "index": {"type": "integer", "description": "1-based position in the last list."},
            },
        },
    },
    {
        "name": "list_sessions",
        "description": (
            "List the Claude sessions Voxa is running, with each one's status "
            "and which is active. Use when the user asks what is running."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "switch_session",
        "description": (
            "Switch the voice line to another running session by its project "
            "name; both sessions keep working."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "The project/folder name (or part of it) of the session to switch to.",
                },
            },
            "required": ["target"],
        },
    },
    {
        "name": "new_session",
        "description": (
            "Start an additional Claude session in a folder, keeping the "
            "current one running."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Folder for the new session (~-relative ok).",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "resolve_approval",
        "description": (
            "Resolve the pending permission prompt when the user answers it verbally "
            "(e.g. 'yes, allow it', 'option 2', 'deny that')."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "description": "The option key the user chose, or 'yes'/'no'."},
            },
            "required": ["decision"],
        },
    },
    {
        "name": "read_session",
        "description": (
            "Read the attached Claude session's full transcript on demand. "
            "Use when the user asks about details of past work in this session "
            "(what changed, why something failed, what was decided). "
            "Pass last=N for the most recent N messages, or search='text' to "
            "find messages mentioning something."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "last": {"type": "integer", "description": "How many recent messages (max 40)."},
                "search": {"type": "string", "description": "Find messages containing this text."},
            },
        },
    },
    {
        "name": "git_status",
        "description": (
            "Summarise git status in the session folder: the current branch and "
            "how many files changed. Read-only and safe to call any time."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "git_diff",
        "description": (
            "Summarise what changed in the session folder since the last commit: "
            "a diff stat plus a condensed diff. Use for questions like "
            "'what did it change?'. Read-only."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "git_commit",
        "description": (
            "Ask to commit all current changes in the session folder. Does NOT "
            "run immediately: it returns pending_approval and shows a "
            "confirmation card the user must approve first. Set push=true only "
            "when the user asked to commit AND push."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string",
                            "description": "Short commit message."},
                "push": {"type": "boolean",
                         "description": "Also push after committing."},
            },
            "required": ["message"],
        },
    },
    {
        "name": "git_push",
        "description": (
            "Ask to push the current branch to its upstream. Does NOT run "
            "immediately: it returns pending_approval and shows a confirmation "
            "card naming the branch. Never force-pushes."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
]

# ---------------------------------------------------------------------------
# GeminiOperator
# ---------------------------------------------------------------------------


class GeminiOperator:
    """Bridges a phone call to a Gemini Live realtime voice session.

    Usage::

        async with GeminiOperator(config, handle_tool_call) as op:
            op.set_audio_out(send_to_phone)
            await asyncio.gather(op.run(), mic_pump(op))
    """

    def __init__(
        self,
        config: Config,
        handle_tool_call: Callable[[str, dict], Awaitable[dict]],
        voice: str = "",
        lang: str = "",
    ) -> None:
        self._config = config
        self._handle = handle_tool_call
        self._voice = voice
        self._lang = lang
        self._audio_out: Optional[Callable[[bytes], Awaitable[None]]] = None
        self._text_out: Optional[Callable[[dict], Awaitable[None]]] = None
        self._usage_out: Optional[Callable[[dict], None]] = None
        self._session: Optional[genai.live.AsyncSession] = None  # type: ignore[name-defined]
        self._client: Optional[genai.Client] = None  # type: ignore[name-defined]
        # The active session lives in its own stack so it can be torn down and
        # reopened (resume) independently of the operator's lifetime.
        self._session_stack: Optional[contextlib.AsyncExitStack] = None
        # Set while a session is open and usable; cleared during a (re)connect so
        # senders drop/await instead of writing to a half-open socket.
        self._ready = asyncio.Event()
        self._closing = False           # True once __aexit__ starts (suppress resume)
        # Server-side half-duplex: while Voxa is speaking we model the phone's
        # realtime playback timeline and DROP mic audio until it finishes (+margin),
        # so Voxa's own voice off the speaker is never fed back to Gemini as "user
        # input". Robust regardless of the app build.
        self._play_until = 0.0          # monotonic time the current reply finishes playing
        self._echo_margin = 0.7         # extra guard after playback ends (s)
        # Latest session-resumption handle from the server (see run()). Passed back on
        # (re)connect so a dropped Live connection can resume mid-call. None until the
        # server first marks a checkpoint resumable.
        self._resume_handle: Optional[str] = None
        # Dedupe relayed updates (see speak): the same finished-task confirmation can
        # be pushed several times in a row (a self-interruption/echo loop re-triggers
        # the task), which reads aloud as a stutter. Skip near-identical repeats.
        self._last_spoken = ""
        self._last_spoken_at = 0.0
        self._speak_dedupe_window = 90.0
        # Debounce relays: one user action makes Claude's screen settle in stages, so
        # the finished-update fires several times in a burst. Without coalescing,
        # Gemini speaks a confirmation for EACH (the "again and again" repetition). We
        # accumulate a burst and speak ONE summary after a brief quiet window.
        self._pending_speak = ""
        self._speak_task: Optional[asyncio.Task] = None
        self._speak_debounce = 2.5   # coalesce a settling burst (monitor idles ~3.6s apart)
        # Loop guard: send_to_claude may only fire after a genuine NEW user turn
        # (spoken or typed). A finished-task relay is injected as a user turn but is
        # NOT a real utterance, so it can't license another dispatch. This stops the
        # agent from auto-continuing / decomposing a task into repeated send_to_claude.
        self._user_spoke = False
        self._greeted = False   # speak an opening greeting once, so Voxa talks first

    # ------------------------------------------------------------------
    # Async context manager: opens the Live session
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "GeminiOperator":
        # Vertex AI mode (set on hosts whose IP the Developer API geo-blocks): auth
        # by service account, no IP-location check. Falls back to the Developer API
        # (api key) everywhere else. Env: GOOGLE_GENAI_USE_VERTEXAI + project/location.
        import os
        if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").strip().lower() in ("1", "true", "yes"):
            client = genai.Client(
                vertexai=True,
                project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
                location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
            )
        else:
            client = genai.Client(api_key=self._config.gemini_api_key)
        self._client = client
        await self._open()
        return self

    def _build_config(self) -> types.LiveConnectConfig:
        """The Live session config. Rebuilt on every (re)connect so it carries the
        latest session-resumption handle."""
        cfg = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=SYSTEM_INSTRUCTION + language_block(self._lang),
            tools=[{"function_declarations": TOOL_DECLARATIONS}],
            # Live captions: transcribe both the user's speech and Gemini's spoken output.
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            # Never let incoming audio (mic bleed, noise, our own tail) cut off a
            # reply mid-sentence. Each reply finishes fully -> no overlap, no
            # accidental interruptions. The phone's "interrupt" button is the only
            # way to stop playback.
            realtime_input_config=types.RealtimeInputConfig(
                activity_handling=types.ActivityHandling.NO_INTERRUPTION,
            ),
            # Survive Gemini Live's caps on long calls. Context-window compression
            # (sliding window) prunes the oldest turns instead of ending the audio
            # session at its ~15-min limit, so a long conversation keeps going.
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(),
            ),
            # Enable session resumption so the server emits resume handles (captured in
            # run()). Gemini refreshes the underlying connection roughly every ~10 min;
            # the handle lets reconnect() pick the session back up. `transparent` is
            # left off (Vertex-only); `handle` is None on the first connect.
            session_resumption=types.SessionResumptionConfig(handle=self._resume_handle),
        )
        lang_code = LANGUAGE_CODES.get(self._lang, "")
        if self._voice or lang_code:
            speech_kwargs = {}
            if self._voice:
                speech_kwargs["voice_config"] = types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self._voice)
                )
            if lang_code:
                speech_kwargs["language_code"] = lang_code
            cfg.speech_config = types.SpeechConfig(**speech_kwargs)
        return cfg

    async def _open(self) -> None:
        """Open a fresh Live connection (resuming via the stored handle if present)
        and mark the session ready."""
        stack = contextlib.AsyncExitStack()
        self._session = await stack.enter_async_context(
            self._client.aio.live.connect(
                model=self._config.gemini_live_model,
                config=self._build_config(),
            )
        )
        self._session_stack = stack
        self._ready.set()

    async def _close_session(self) -> None:
        """Tear down the current Live connection (used before a resume and on exit)."""
        self._ready.clear()
        stack, self._session_stack = self._session_stack, None
        self._session = None
        if stack is not None:
            with contextlib.suppress(Exception):
                await stack.aclose()

    async def _reconnect(self) -> None:
        """Resume the session on a fresh connection using the latest handle. Retries
        with backoff; raises if it can't reconnect (the call then ends as before)."""
        await self._close_session()
        delay = 0.5
        for attempt in range(5):
            try:
                await self._open()
                logger.info("Gemini session resumed (handle=%s…)", (self._resume_handle or "")[:8])
                return
            except Exception as exc:
                logger.warning("Gemini resume attempt %d failed: %s", attempt + 1, exc)
                await asyncio.sleep(delay)
                delay = min(8.0, delay * 2)
        raise RuntimeError("Gemini session resume failed after retries")

    async def _await_ready(self, timeout: float = 10.0) -> bool:
        """Wait for an open session (e.g. through a brief resume). False if we're
        closing or it didn't come back in time."""
        if self._closing:
            return False
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return self._session is not None

    async def __aexit__(self, *exc) -> bool:
        self._closing = True
        if self._speak_task and not self._speak_task.done():
            self._speak_task.cancel()
        await self._close_session()
        return False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def set_audio_out(self, cb: Callable[[bytes], Awaitable[None]]) -> None:
        """Register the callback that receives 24 kHz PCM audio from Gemini."""
        self._audio_out = cb

    def set_text_out(self, cb: Callable[[dict], Awaitable[None]]) -> None:
        """Register the callback that receives JSON control/caption messages."""
        self._text_out = cb

    def set_usage_out(self, fn: Callable[[dict], None]) -> None:
        """Register a callback invoked whenever Gemini reports token usage
        for this session, with the latest cumulative counts. Live's
        usage_metadata is cumulative for the connection, so the last call
        before the session ends holds the session total."""
        self._usage_out = fn

    async def send_audio(self, pcm16k: bytes) -> None:
        """Forward a mic audio frame (16 kHz mono PCM) to Gemini, EXCEPT while Voxa
        is still speaking (so the speaker's output captured by the mic isn't fed
        back and mistaken for the user)."""
        if self._session is None or not self._ready.is_set():
            return  # dropped during a (re)connect; mic frames are continuous, safe to drop
        if time.monotonic() < self._play_until + self._echo_margin:
            return  # half-duplex: drop mic while the reply is still playing
        try:
            await self._session.send_realtime_input(
                audio=types.Blob(data=pcm16k, mime_type="audio/pcm;rate=16000")
            )
        except Exception:
            return  # connection dropping; run()'s receive loop handles the resume

    async def speak(self, text: str, immediate: bool = False) -> None:
        """Relay text for Gemini to read aloud, DEBOUNCED and DEDUPED.

        ``immediate`` skips the debounce window (used for the on-answer opening, so
        Voxa speaks in its own voice right away instead of the phone's fallback voice).

        One user action makes Claude's screen settle in stages, firing the
        finished-update several times in a burst; speaking each one is the "again and
        again" repetition. So we accumulate the burst and speak ONE summary after a
        brief quiet window, and skip a relay near-identical to the last thing we spoke
        (a cross-action duplicate)."""
        norm = " ".join((text or "").split())
        if not norm:
            return
        now = time.monotonic()
        if self._last_spoken and now - self._last_spoken_at < self._speak_dedupe_window:
            if difflib.SequenceMatcher(None, norm.lower(), self._last_spoken.lower()).ratio() >= 0.7:
                logger.info("speak: skipped near-duplicate update")
                return
        # Accumulate this update and (re)arm the debounce timer; the burst becomes one.
        self._pending_speak = f"{self._pending_speak}\n{text}".strip() if self._pending_speak else text
        # A relay (greeting/recap/result) is NOT a user request: consume any pending
        # user turn NOW, at queue time. Doing it later in _flush_speak could clear a
        # genuine user turn that arrives during the debounce window (blocking the loop
        # guard from dispatching the user's real request).
        self._user_spoke = False
        if self._speak_task and not self._speak_task.done():
            self._speak_task.cancel()
        delay = 0.0 if immediate else self._speak_debounce
        self._speak_task = asyncio.create_task(self._flush_speak(delay))

    async def _flush_speak(self, delay: float | None = None) -> None:
        """After the relays go quiet, speak the accumulated burst as one message."""
        if delay is None:
            delay = self._speak_debounce
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if not self._pending_speak:
            return
        # Check readiness BEFORE consuming _pending_speak, so a reconnect mid-debounce
        # doesn't lose the message, it stays queued and the next relay re-arms the flush.
        if not await self._await_ready():
            logger.warning("speak deferred; Gemini session not ready (kept pending)")
            return
        text = self._pending_speak
        self._pending_speak = ""
        # Collapse duplicate AND near-duplicate (reworded) lines across the burst: one
        # Claude turn settles in stages, each a paraphrased re-narration of the same
        # result, which reads aloud as the "repeats the same thing" stutter. Drop a line
        # that is >=0.7 similar to one already kept; genuinely new lines survive.
        out: list[str] = []
        seen: list[str] = []
        for ln in text.split("\n"):
            norm_ln = " ".join(ln.split()).lower()
            if not norm_ln:
                continue
            if any(difflib.SequenceMatcher(None, norm_ln, s).ratio() >= 0.7 for s in seen):
                continue
            out.append(ln)
            seen.append(norm_ln)
        text = "\n".join(out)
        self._last_spoken = " ".join(text.split())
        self._last_spoken_at = time.monotonic()
        with contextlib.suppress(Exception):
            await self._session.send_client_content(
                turns=types.Content(
                    role="user",
                    parts=[types.Part(text=f"Tell the user: {text}")],
                ),
                turn_complete=True,
            )

    def suppress_greeting(self) -> None:
        """Skip the automatic opening greeting. Used when there is a queued update to
        relay on answer, so Voxa speaks ONE contextual opening instead of greeting and
        then re-reading the update."""
        self._greeted = True

    async def greet(self) -> None:
        """Speak a short opening greeting so Voxa talks first, without waiting for the
        user. Injected as a one-off directive at session start."""
        if self._session is None:
            return
        with contextlib.suppress(Exception):
            await self._session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text=(
                    "[The call just connected. Greet the user warmly in ONE short "
                    "sentence and ask what they'd like to work on. Speak now; do not "
                    "call any tool.]"))]),
                turn_complete=True,
            )

    def _allow_tool(self, name: str) -> bool:
        """Loop guard. send_to_claude AND queue_task require a fresh user turn and
        consume it, so the agent can't dispatch/queue work to Claude on its own (e.g.
        after a finished-task relay) or split one request into multiple steps. A
        queued instruction is a real user turn too, never the operator's own words.
        All other tools are free."""
        if name not in ("send_to_claude", "queue_task"):
            return True
        if not self._user_spoke:
            return False
        self._user_spoke = False   # consume this user turn
        return True

    async def send_text(self, text: str) -> None:
        """Send the user's typed message as a normal user turn (like speaking it)."""
        self._user_spoke = True    # a typed command is a real user request
        if not await self._await_ready():
            logger.warning("send_text dropped; Gemini session not ready")
            return
        with contextlib.suppress(Exception):
            await self._session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text=text)]),
                turn_complete=True,
            )

    async def run(self) -> None:
        """Receive loop: dispatch audio, stream captions, route tool calls.

        ``session.receive()`` yields the messages for a single model turn and then
        ends (it breaks on ``turn_complete``). The outer ``while True`` re-enters it
        to keep listening across turns; it blocks on the socket each call, so this
        does not busy-loop. If the connection drops (GoAway / ~10-min cap), it is
        resumed transparently via the stored handle and the loop continues.
        """
        if self._session is None:
            raise RuntimeError("GeminiOperator is not open; use 'async with'.")

        if not self._greeted:        # Voxa speaks first, once, at session start
            self._greeted = True
            await self.greet()

        while True:
            try:
                async for response in self._session.receive():
                    # Session resumption: remember the latest resumable checkpoint so a
                    # dropped connection can be reopened mid-call (handle fed back in
                    # __aenter__). Only update when the server marks it resumable.
                    sru = getattr(response, "session_resumption_update", None)
                    if sru is not None and getattr(sru, "resumable", False) and sru.new_handle:
                        self._resume_handle = sru.new_handle
                    # GoAway: the server will close this connection shortly (it caps the
                    # connection lifetime at ~10 min). Log it; the stored handle is what
                    # _reconnect() uses to continue without losing the session.
                    ga = getattr(response, "go_away", None)
                    if ga is not None:
                        logger.info("Gemini go_away: time_left=%s", getattr(ga, "time_left", "?"))

                    um = response.usage_metadata
                    if um is not None and self._usage_out is not None:
                        self._usage_out({
                            "tokens_in": um.prompt_token_count or 0,
                            "tokens_out": um.response_token_count or 0,
                        })

                    # Audio data from Gemini (24 kHz mono PCM)
                    if response.data is not None:
                        # Advance the playback timeline by this chunk's real duration
                        # (24kHz, 16-bit mono = 48000 bytes/sec) so the mic stays gated
                        # for as long as the phone will actually be playing it.
                        dur = len(response.data) / 48000.0
                        self._play_until = max(self._play_until, time.monotonic()) + dur
                        if self._audio_out is not None:
                            await self._audio_out(response.data)

                    # Live captions: transcripts of the user's speech and Gemini's output
                    sc = response.server_content
                    if sc is not None and self._text_out is not None:
                        # Barge-in: Gemini stopped its current reply to start a new one.
                        # Tell the phone to drop any buffered audio so the old and new
                        # replies don't play over each other ("multiple things at once").
                        if getattr(sc, "interrupted", False):
                            await self._text_out({"type": "flush_audio"})
                        if sc.output_transcription and sc.output_transcription.text:
                            await self._text_out({
                                "type": "transcript",
                                "role": "agent",
                                "text": sc.output_transcription.text,
                            })
                        if sc.input_transcription and sc.input_transcription.text:
                            self._user_spoke = True   # a real spoken request just came in
                            await self._text_out({
                                "type": "transcript",
                                "role": "user",
                                "text": sc.input_transcription.text,
                            })

                    # Tool/function calls from Gemini
                    if response.tool_call is not None:
                        for fc in response.tool_call.function_calls:
                            if not self._allow_tool(fc.name):
                                # Self-initiated dispatch with no new user request: refuse
                                # and tell the model to relay + wait instead of looping.
                                logger.info("suppressed self-initiated %s (no new user turn)", fc.name)
                                result = {
                                    "ignored": True,
                                    "reason": "No new request from the user since the last "
                                    "one. Do NOT send another instruction to Claude or "
                                    "split the task into steps yourself; Claude does the "
                                    "whole job. Relay Claude's result to the user and ASK "
                                    "what they want next; only call send_to_claude after "
                                    "the user actually asks for something.",
                                }
                            else:
                                try:
                                    result = await self._handle(fc.name, dict(fc.args or {}))
                                except Exception as exc:
                                    logger.exception("handle_tool_call(%s) raised: %s", fc.name, exc)
                                    result = {"error": str(exc)}
                            await self._session.send_tool_response(
                                function_responses=[
                                    types.FunctionResponse(
                                        id=fc.id,
                                        name=fc.name,
                                        response=result,
                                    )
                                ]
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The Live connection dropped (GoAway / ~10-min cap / network). Resume
                # on a fresh connection using the stored handle and keep going, so the
                # call survives transparently. With no handle (or while closing) there's
                # nothing to resume, propagate as before and let the call end.
                if self._closing or not self._resume_handle:
                    raise
                logger.info("Gemini connection lost (%s); resuming", exc)
                await self._reconnect()
