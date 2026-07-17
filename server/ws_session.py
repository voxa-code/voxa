# server/ws_session.py
"""One phone WebSocket connection: free setup gate, metered operator lifecycle,
and the recv/idle loops. The Claude session itself lives in the SessionRegistry
and persists across connections; this module only serves one connection at a
time against it.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import WebSocket, WebSocketDisconnect

from server.claude_controller import ClaudeController
from server.greetings import (compose_opening, split_updates_for,
                              suppress_greeting_if_supported)
from server.orchestrator import Orchestrator
from server.session import Session
from server.session_hub import SessionHub
from server.task_queue import TaskQueue
from server.tmux_controller import PRESS_KEY_NAMES, TmuxController, pick_session_name


def _fleet_status_line(sessions) -> str:
    """'Open sessions right now: loop (attached, idle); veil (working).' built from
    every fleet member's project (its controller's working_dir basename) and status,
    with an '(attached)' marker for whichever one the phone is currently driving.
    Returns '' when zero or one session is open, so the common single-session
    opening is unaffected (fix 5: tell Gemini about the fleet)."""
    members = sessions.all()
    if len(members) <= 1:
        return ""
    active = sessions.active()
    parts = []
    for member in members:
        cwd = getattr(member.controller, "working_dir", "") or ""
        label = os.path.basename(cwd.rstrip("/")) or cwd or "session"
        status = getattr(member.controller, "status", "?")
        tag = "attached, " if member is active else ""
        parts.append(f"{label} ({tag}{status})")
    return "Open sessions right now: " + "; ".join(parts) + "."


async def _approval_from_driven_pane(orchestrator, notifier, cwd: str):
    """Build (and store) an approval from the CURRENTLY-driven pane when it is
    sitting on a live interactive prompt. Used at answer time for prompts whose
    Notification predates the attach (the hook's scrape had no pane it was
    allowed to read then). Gated on the prompt footer so a numbered list inside
    ordinary output can never become a phantom approval. Fail-open."""
    cap = getattr(orchestrator.controller, "capture_text", None)
    if cap is None:
        return None
    try:
        pane = await asyncio.to_thread(cap)
        from server.approvals import build_approval, pane_shows_live_prompt
        if not pane or not pane_shows_live_prompt(pane):
            return None
        approval = build_approval(cwd, "", pane)
        if approval is not None:
            notifier.approvals.put(approval)
        return approval
    except Exception:
        logging.exception("answer-time approval scrape failed")
        return None


async def _approval_from_any_terminal(notifier):
    """Sweep every open Claude terminal for a live interactive prompt and build
    (and store) an approval from the first one found. Answer-time fallback for
    prompts in sessions Voxa is NOT attached to (or whose Notification predates
    a server restart). Fail-open."""
    try:
        from server.approvals import build_approval
        from server.terminals import find_live_prompt_pane
        found = await asyncio.to_thread(find_live_prompt_pane, None)
        if not found:
            return None
        cwd, pane = found
        approval = build_approval(cwd, "", pane)
        if approval is not None:
            notifier.approvals.put(approval)
        return approval
    except Exception:
        logging.exception("any-terminal approval sweep failed")
        return None


async def serve_ws(websocket: WebSocket, *, config, mode: str, sessions, notifier,
                   operator_factory, session_state=None, prewarmer=None) -> None:
    # The controller (and the Claude session it owns) persists across
    # connections via the registry. Build it once, then reuse it so Claude
    # keeps running when the phone hangs up.
    session = sessions.active()
    if session is None:
        session_id = uuid.uuid4().hex[:8]
        if mode == "drive":
            watch_path = os.path.join(
                tempfile.gettempdir(), f"loop-watch-{os.getpid()}.log"
            )
            controller = ClaudeController(
                watch_log_path=watch_path, launch_terminal=True
            )
        else:
            controller = TmuxController(
                session_name=pick_session_name(session_id),
                launch_terminal=True,
                terminal_app=os.environ.get("VOXA_TERMINAL_APP", "auto"),
            )
        hub = SessionHub(controller, notifier.call_manager)
        if notifier.hooks_live:
            hub.set_offline_ring(False)   # hooks already drive offline rings
        session = sessions.add(Session(session_id, controller, hub,
                                       notifier.call_manager))
        sessions.set_active(session.id)
    controller = session.controller
    hub = session.hub
    # Fleet awareness (fix 1): label the driven session's narration when more than
    # one session/terminal is live, so a foreign monitor speaking through this same
    # hub is never mistaken for the session the user thinks they're driving. Both
    # closures read the CURRENT active session's controller (not a captured stale
    # one), so they keep tracking correctly across mid-call attach/switch swaps.
    hub.label_fn = lambda: os.path.basename(
        (getattr((sessions.active() or session).controller, "working_dir", "") or "")
        .rstrip("/"))
    hub.multi_fn = lambda: len(sessions.all()) > 1
    # A fleet member that was switched away from carries _started=False (its
    # monitor was detached). If the phone reconnects onto it as the active
    # session, re-arm its still-running tmux session so it is drivable again,
    # without start()'s kill+relaunch. No-op for a fresh session with no tmux
    # yet (reattach returns False) and for an already-armed one.
    if not getattr(controller, "_started", False):
        _reattach = getattr(controller, "reattach", None)
        if _reattach is not None:
            try:
                await _reattach()
            except Exception:
                logging.exception("resume reattach failed")

    # Conversational-activity clock for the idle auto-disconnect (below). Raw mic
    # frames don't count (the phone streams continuously); only real speech /
    # spoken replies / a working task do.
    activity = {"t": time.monotonic()}
    def touch(): activity["t"] = time.monotonic()

    # Turn-latency probe: monotonic time of the LAST user-speech transcript
    # chunk; the first audio bytes after it close the turn and log the gap
    # (generation + tools + transport). Diagnostic only.
    _turn = {"t0": 0.0}

    async def speak(text): await operator.speak(text)
    async def notify(msg):
        if isinstance(msg, dict) and msg.get("type") == "transcript":
            touch()
            if msg.get("role") == "user":
                _turn["t0"] = time.monotonic()
            # Voice history: the user's half of the session, recorded so past
            # sessions can be replayed on the phone. Fail-open by contract.
            try:
                from server.history import record_voice
                record_voice(getattr(orchestrator.controller, "working_dir", "") or "",
                             msg.get("role", ""), msg.get("text", ""))
            except Exception:
                logging.exception("voice history record failed")
        await websocket.send_json(msg)
    orchestrator = Orchestrator(controller, speak, notify)
    # So the resolve_approval voice tool can act on a prompt it never saw arrive
    # (pushed live or queued before this connection attached).
    orchestrator.approvals = notifier.approvals
    # So resolve_approval can also reach notifier.on_approval_resolved (set below,
    # once a line is attached) to clear the phone's approval card after a voice
    # decision, without capturing a stale None from before the line attached.
    orchestrator.notifier = notifier
    # So the fleet tools (list/switch/new session) and the matching WS controls
    # can address every driven session, not just this connection's.
    orchestrator.sessions = sessions
    # Route finals through the hub (spoken when a line is attached, ring via the
    # call manager otherwise). set_final keeps this wired across controller swaps
    # when the user attaches to a different open terminal.
    orchestrator.set_final(hub.on_final)
    # Task 2: the disk-backed task queue and its runner. The queue survives server
    # restarts (announced, not auto-run); the runner dispatches queued items on the
    # controller-final boundary and folds their rings into ONE digest. touch() as
    # on_between_items keeps the idle watchdog from hanging up mid-queue.
    orchestrator.queue = TaskQueue()
    orchestrator._on_between_items = touch
    # A needs_input for a burst cwd pauses the queue (Phase 1 still rings it).
    notifier.on_queue_needs_input = orchestrator._queue_note_needs_input

    _log = logging.getLogger("voxa")
    _counts = {"in": 0, "out": 0}
    voice = websocket.query_params.get("voice", "")
    # The bridge appends ?account=<paired phone's id> so the metered session
    # bills that balance. Only pass it to factories that accept it.
    import inspect
    _kwargs = {"voice": voice}
    _account = websocket.query_params.get("account", "")
    if "account" in inspect.signature(operator_factory).parameters:
        _kwargs["account"] = _account
    # The phone's language rides the same path as voice; only pass it to
    # factories that accept it (keeps older 2-3 arg test factories working).
    _lang = websocket.query_params.get("lang", "")
    if "lang" in inspect.signature(operator_factory).parameters:
        _kwargs["lang"] = _lang
    # Remember who's paired so the background watcher can ring this account's
    # phone (via the cloud) when a terminal finishes while they're away, and so
    # a background prewarm (kicked from notifier.report while THIS connection
    # may not even exist yet) opens its operator with the same account/voice/
    # lang the phone will ask for again on the next answer. Persisted, so a
    # fresh `voxa` run prewarms with the right identity before any reconnect.
    notifier.remember_phone(_account, voice, _lang)
    if _account:
        asyncio.ensure_future(notifier.register_machine_cloud())

    # Pre-session gate: do NOT open the (metered) /live voice session until the
    # user taps Start ("begin") or starts talking. Until then we only do FREE
    # setup, listing terminals and setting the folder/terminal, so idle pairing
    # and choosing a project never cost the user a minute.
    await websocket.send_json(
        {"type": "status", "status": "ready",
         "working_dir": getattr(controller, "working_dir", "") or ""})
    # Fleet snapshot right after `ready`, so the phone's sessions card renders
    # without having to ask (and before the async terminals push below).
    await orchestrator.push_sessions()

    async def _push_terminals():
        try:
            from server.terminals import discover_claude_sessions
            terminals = await asyncio.to_thread(discover_claude_sessions)
            # Seed the orchestrator with the SAME list the phone is shown, so
            # tapping a terminal (attach_terminal by id) resolves correctly.
            orchestrator.remember_terminals(terminals)
            await websocket.send_json({"type": "terminals", "terminals": terminals})
        except Exception:
            pass
    asyncio.ensure_future(_push_terminals())

    first_audio = None
    # True once the USER explicitly picked where to work during this
    # connection's free gate (attached a terminal, chose a folder, resumed a
    # session). The ring-answer conveniences further down (pending-source
    # auto-attach and the prewarmed ring greeting) exist for "answered the
    # call that rang"; they must stand down for a deliberate choice, or
    # starting voice inside terminal A gets yanked to whatever session B rang
    # last (the Ti0-yanked-to-loop misattribution bug).
    user_pinned = False
    _PIN_CONTROLS = ("attach_terminal", "attach_source", "set_dir",
                     "resume_session", "new_session", "switch_session")
    while True:
        msg = await websocket.receive()
        if msg["type"] == "websocket.disconnect":
            # Hung up during the free setup gate (before "begin"/metering); still
            # persist the driven cwd if the folder was already picked.
            if session_state is not None:
                wd = getattr(orchestrator.controller, "working_dir", None)
                if wd:
                    session_state.save(wd)
            return  # nothing was attached or metered
        if msg.get("bytes") is not None:
            first_audio = msg["bytes"]   # talking implicitly begins the session
            break
        if msg.get("text"):
            try:
                data = json.loads(msg["text"])
            except ValueError:
                continue
            if data.get("type") == "begin":
                break
            if data.get("type") in _PIN_CONTROLS:
                user_pinned = True
            # Folder/terminal selection is free, handle it before metering.
            await handle_client_control(msg["text"], orchestrator, websocket, None,
                                        notifier=notifier)

    # A prewarmed operator (opened and greeted while the phone was still
    # ringing, see server/prewarm.py) beats building a fresh one from scratch:
    # the greeting is already synthesized and buffered. claim() is fail-open
    # by contract (never raises, returns None on any mismatch/staleness/error),
    # so a miss here is exactly today's cold path. NOT claimed when the user
    # pinned a terminal themselves: the warm greeting was composed for the
    # session that RANG, exactly the wrong thing to say here; discard it so it
    # stops burning its (metered, in proxy mode) clock.
    warm = None
    if prewarmer is not None:
        if user_pinned:
            _discard = getattr(prewarmer, "discard", None)
            if _discard is not None:
                asyncio.ensure_future(_discard())
        else:
            warm = prewarmer.claim(voice, _lang, _account)
    if warm is not None:
        operator_cm = _warm_cm(warm)
    else:
        operator_cm = _as_cm(operator_factory(config, orchestrator.handle_tool_call, **_kwargs))
    async with operator_cm as operator:
        async def audio_out(pcm):
            touch()  # Gemini is speaking -> the line is active
            if _turn["t0"]:
                _log.info("turn latency: %.2fs (last user words -> first reply audio)",
                          time.monotonic() - _turn["t0"])
                _turn["t0"] = 0.0
            _counts["out"] += 1
            if _counts["out"] % 50 == 1:
                _log.info("ws: sent %d audio chunks -> phone", _counts["out"])
            await websocket.send_bytes(pcm)

        if warm is not None:
            # Bind the REAL orchestrator now that one exists for this connection
            # (the operator was built during the ring with a late-bound stub that
            # just said "still connecting").
            warm.bind_tools(orchestrator.handle_tool_call)
            operator.set_audio_out(audio_out)
            operator.set_text_out(notify)
            # Swap the callbacks atomically and flush whatever was buffered
            # during the ring, BEFORE the run/recv loops start below, so the
            # greeting plays instantly instead of the phone hearing dead air
            # while a cold connect would have been happening.
            buffered_audio, buffered_controls = warm.stop_buffering(audio_out, notify)
            for chunk in buffered_audio:
                try:
                    await websocket.send_bytes(chunk)
                except Exception:
                    logging.exception("warm audio flush to phone failed")
            for control_msg in buffered_controls:
                try:
                    await notify(control_msg)
                except Exception:
                    logging.exception("warm control flush to phone failed")
            # The prewarm run_task is what produced the buffered audio above;
            # cancel it before this connection starts its OWN run() task below,
            # so there is never more than one concurrent receive() loop on the
            # adopted operator. GeminiOperator.run() re-enters session.receive()
            # in a `while True`, so restarting it after cancelling the first
            # invocation is safe, and `_greeted` stays True so it won't greet
            # again.
            warm.run_task.cancel()
            with contextlib.suppress(BaseException):
                await warm.run_task
        else:
            operator.set_audio_out(audio_out)
            operator.set_text_out(notify)

        # Attach this operator's voice to the line and speak anything that
        # queued up while no phone was connected.
        pending_updates = hub.attach(lambda t: operator.speak(t))
        # Also send the phone a control message with the same missed updates, so
        # the UI can render them (not just have them spoken). Fail-open: a send
        # failure here must never break the call.
        if pending_updates:
            try:
                await websocket.send_json(
                    {"type": "missed_updates",
                     "items": [{"text": t} for t in pending_updates]})
            except Exception:
                logging.exception("missed_updates push to phone failed")

        # Structured approvals reach the phone through TWO paths, both funnelled
        # through here: (1) queued alongside a summary while no line was open
        # (drained via attach_approvals, mirroring pending_updates above), and (2)
        # still ACTIVE from a prior connection that saw it live (via on_approval,
        # below) but hung up before deciding. `_pushed` dedupes across both.
        _pushed_approval_ids: set[str] = set()

        async def _push_approval(approval):
            if not approval or approval.get("approval_id") in _pushed_approval_ids:
                return
            _pushed_approval_ids.add(approval["approval_id"])
            try:
                await websocket.send_json({"type": "approval", **approval})
            except Exception:
                logging.exception("approval push to phone failed")

        for queued_approval in notifier.call_manager.attach_approvals():
            await _push_approval(queued_approval)
        # While this line is attached, a FRESH approval built from a hook that
        # fires mid-session must reach the phone live, not wait for the next
        # attach. Cleared in the finally below no matter how this connection ends.
        notifier.on_approval = _push_approval

        async def _push_approval_resolved(approval_id):
            try:
                await websocket.send_json(
                    {"type": "approval_resolved", "approval_id": approval_id, "outcome": "sent"})
            except Exception:
                logging.exception("approval_resolved push to phone failed")
        # Mirrors on_approval above: lets the voice path (Orchestrator.resolve_approval,
        # which has no websocket of its own) clear the phone's approval card too.
        notifier.on_approval_resolved = _push_approval_resolved

        async def _speak_foreign_approval(approval):
            """Read a FRESH approval's question and options aloud when it belongs to a
            session OTHER than the one we're driving. The driven pane's own monitor
            re-narrates its prompts, so speaking those here too would double up; a
            foreign session has no such narration on this line, so its card would
            otherwise arrive silent. Fail-open (notifier.report also guards this)."""
            driven = (getattr(orchestrator.controller, "working_dir", "") or "").rstrip("/")
            cwd = (approval.get("cwd") or "").rstrip("/")
            if cwd and cwd == driven:
                return   # the driven pane's monitor narrates its own prompts
            from server.greetings import format_approval_for_speech
            text = format_approval_for_speech(approval)
            if not text:
                return
            label = os.path.basename(cwd) or "another session"
            # immediate=True: a blocked prompt is time-critical, unlike the 2.5s
            # debounce that exists to coalesce a finished-task settling burst.
            await operator.speak(f"{label} needs input. {text}", immediate=True)
        notifier.on_approval_speak = _speak_foreign_approval

        async def _speak_foreign_update(summary: str, cwd: str) -> None:
            """Speak a background report (typically a finish) for a session OTHER
            than the one we're driving, so a foreign session finishing mid-call is
            never silently dropped just because THIS line happens to be open. The
            driven pane's own monitor narrates its own finish already (through
            hub.on_final on this same line), so skip it here to avoid a double
            narration. Hook summaries already start with the project label, so
            speak as-is; dedupe_key=cwd keeps this session's near-duplicate check
            scoped to its own project (fix 3), never colliding with another
            session's. Fail-open: also push missed_updates so the phone UI shows it
            even if speaking somehow fails."""
            driven = (getattr(orchestrator.controller, "working_dir", "") or "").rstrip("/")
            if (cwd or "").rstrip("/") == driven:
                return   # the driven pane's monitor narrates its own updates
            await operator.speak(summary, immediate=True, dedupe_key=cwd)
            try:
                await websocket.send_json(
                    {"type": "missed_updates", "items": [{"text": summary}]})
            except Exception:
                logging.exception("missed_updates push (foreign live update) failed")
        notifier.on_update_speak = _speak_foreign_update
        # Everything from here runs with the line attached; detach() MUST run even on
        # cancellation (uvicorn reload/shutdown) or an exception, otherwise line_open
        # stays True and every later background finish is silently dropped.
        try:
            # If this call was triggered by a specific Claude terminal, attach to THAT
            # terminal so Voxa continues that session instead of an empty default one,
            # and OPEN with that context so it knows where you are. NEVER when the
            # user pinned a terminal during the free gate: their deliberate choice
            # outranks whatever session rang last (the pending source is left in
            # place for a future real answer).
            source = None if user_pinned else sessions.pop_pending()
            attached_folder = None
            if user_pinned:
                wd = (getattr(orchestrator.controller, "working_dir", "") or "").rstrip("/")
                attached_folder = os.path.basename(wd) or None
            if source and source.get("cwd"):
                try:
                    # The ringing cwd may belong to a registered fleet member: that
                    # member becomes the ACTIVE one so the fleet card (and later
                    # reconnects) follow the answer. An external terminal with no
                    # member keeps today's behavior: the swap persists onto the
                    # session this connection started with.
                    member = sessions.find_by_cwd(source["cwd"])
                    res = await orchestrator.attach_source(source["cwd"])
                    if "attached" in res:
                        controller = orchestrator.controller   # follow the swap
                        if member is not None:
                            sessions.set_active(member.id)
                            member.controller = controller     # persist for reconnects
                        else:
                            session.controller = controller    # persist for reconnects
                        attached_folder = (os.path.basename(source["cwd"].rstrip("/"))
                                           or source["cwd"])
                        await websocket.send_json(
                            {"type": "status",
                             "working_dir": res.get("working_dir", source["cwd"])})
                        await orchestrator.push_sessions()   # active flag moved
                    else:
                        _log.info("auto-attach to %s skipped: %s",
                                  source["cwd"], res.get("error"))
                except Exception:
                    logging.exception("auto-attach on answer failed")

            # An approval still active for wherever we just attached (e.g. it was
            # pushed live on a previous connection that hung up without deciding)
            # must reach THIS connection too, not only fresh/queued ones above.
            attached_cwd = getattr(orchestrator.controller, "working_dir", "") or ""

            async def _resolve_still_active_approval():
                # Prefer the attached session's prompt, but a prompt waiting in ANY
                # session must still reach the opening: answering a finish call for
                # session A while session B sits on a menu used to say nothing about
                # B's choices at all.
                still = ((notifier.approvals.active_for(attached_cwd)
                         if attached_cwd else None)
                        or notifier.approvals.latest())
                if still is None:
                    # No stored approval (e.g. its Notification predates a server
                    # restart, or the hook's scrape had no pane it was allowed to
                    # read). Rebuild it NOW: first from the just-attached pane, then
                    # by sweeping every open Claude terminal for a live prompt.
                    if attached_cwd:
                        still = await _approval_from_driven_pane(
                            orchestrator, notifier, attached_cwd)
                    if still is None:
                        still = await _approval_from_any_terminal(notifier)
                return still

            async def _push_queue():
                # Push the current queue (and any undrained digest) to the phone so
                # the queue panel renders on connect. Fail-open by contract.
                try:
                    await orchestrator.push_queue_snapshot()
                except Exception:
                    logging.exception("queue snapshot push failed")

            async def _build_recap():
                # Prime the call with the attached session's recent transcript as
                # silent background, so Voxa can answer "what did it do / why?"
                # from the first word instead of knowing only the ring's one-line
                # summary. Best-effort; a fake/legacy operator just speaks.
                recap = ""
                if attached_cwd:
                    try:
                        from server.transcripts import recap as build_recap
                        recap = await asyncio.to_thread(build_recap, attached_cwd)
                    except Exception:
                        logging.exception("session recap for opening failed")
                        recap = ""
                # Fleet awareness (fix 5): tell Gemini what ELSE is open right now, so
                # it never attributes a foreign session's update or question to the
                # one it's driving. '' (no-op) when zero or one session is open, so
                # the single-session opening stays byte-identical to today.
                fleet_line = _fleet_status_line(sessions)
                if not fleet_line:
                    return recap
                return f"{fleet_line}\n\n{recap}" if recap else fleet_line

            # None of these three depend on each other: the approval resolution
            # (which may scrape a live tmux pane), the queue snapshot push, and
            # the recap file read used to run one after another, so the answer
            # paid for the SUM of their latencies. Run them concurrently instead,
            # so the answer only waits on the SLOWEST of the three.
            # return_exceptions=True: each branch already fails open internally
            # (logged above), but a stray exception here must still degrade
            # gracefully rather than cancel the other two or this connection.
            still_active, _queue_result, session_context = await asyncio.gather(
                _resolve_still_active_approval(), _push_queue(), _build_recap(),
                return_exceptions=True,
            )
            if isinstance(still_active, BaseException):
                logging.exception("approval resolution failed", exc_info=still_active)
                still_active = None
            if isinstance(session_context, BaseException):
                session_context = ""
            await _push_approval(still_active)

            if warm is None:
                # Voxa's opening is ALWAYS driven from here. Suppress the operator's
                # own auto-greet: on the metered path the cloud brain would otherwise
                # race ahead and speak a generic "what would you like to do?" the
                # instant the /live socket opens, before this contextual opening
                # arrives. The warm path skips all of this: `warm.opening` (with its
                # own recap) was already spoken during the ring, and the buffered
                # audio/controls were flushed above, before this point.
                suppress_greeting_if_supported(operator)
                # Attribution guard: only the attached project's own updates may
                # be phrased as "your last task in <project> finished"; another
                # session's queued finish is reported separately, by name.
                own_updates, foreign_updates = split_updates_for(
                    attached_folder or "", pending_updates)
                if attached_folder:
                    opening = compose_opening(attached_folder, own_updates,
                                              approval=still_active)
                elif pending_updates or still_active:
                    opening = compose_opening("", pending_updates, approval=still_active)
                else:
                    opening = "Hi! What would you like to work on?"
                if attached_folder and foreign_updates:
                    joined = "; ".join(str(u).strip() for u in foreign_updates)
                    opening += f" Meanwhile: {joined[:400]}."
                # Restart announcement: pending queued tasks from a previous session are
                # NEVER auto-run; the opening mentions them so the user can say "run them".
                opening += _queue_restart_note(orchestrator)
                open_fn = getattr(operator, "open_with_context", None)
                if open_fn is not None:
                    await open_fn(opening, session_context)
                else:
                    await operator.speak(opening, immediate=True)

            # If the session began because the user started talking, don't drop
            # that first frame.
            if first_audio is not None:
                await operator.send_audio(first_audio)

            async def recv_loop():
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg["type"] == "websocket.disconnect":
                            break
                        if msg.get("bytes") is not None:
                            _counts["in"] += 1
                            if _counts["in"] % 50 == 1:
                                # Read the CURRENTLY-driven controller (it swaps when
                                # the user attaches to another terminal mid-call).
                                _log.info(
                                    "ws: recv %d mic frames (status=%s)",
                                    _counts["in"],
                                    getattr(orchestrator.controller, "status", "?"),
                                )
                            # Busy mode: the mic stays OPEN while Claude works, so the
                            # user can say "stop", ask for progress, or queue another
                            # instruction by voice. (This replaces the old cost saver
                            # that paused the mic during work; a voice interrupt
                            # requires listening.) Gemini is steered by the busy
                            # guidance in the tool results/system prompt, and the
                            # orchestrator refuses duplicate dispatches server-side,
                            # so open-mic chatter can't double-send a task.
                            await operator.send_audio(msg["bytes"])
                        elif msg.get("text"):
                            await handle_client_control(
                                msg["text"], orchestrator, websocket, operator,
                                notifier=notifier,
                            )
                except (WebSocketDisconnect, RuntimeError):
                    pass

            # Idle auto-disconnect: hang up after a quiet stretch (no speech, not
            # working) so an idle line stops burning V2V minutes. Off if 0.
            idle_timeout = float(os.environ.get("VOXA_IDLE_TIMEOUT", "180"))

            async def idle_watchdog():
                if idle_timeout <= 0:
                    return await asyncio.Event().wait()  # disabled
                while True:
                    await asyncio.sleep(5)
                    # Follow controller swaps: a mid-call attach must not leave the
                    # watchdog reading the old (idle) controller and hang up mid-task.
                    if getattr(orchestrator.controller, "status", "idle") == "working":
                        touch()  # an active task is not idle
                        continue
                    if time.monotonic() - activity["t"] > idle_timeout:
                        try:
                            await websocket.send_json(
                                {"type": "status",
                                 "status": "idle, disconnecting to save minutes"})
                        except Exception:
                            pass
                        return

            run_task = asyncio.ensure_future(operator.run())
            recv_task = asyncio.ensure_future(recv_loop())
            idle_task = asyncio.ensure_future(idle_watchdog())
            done, pending = await asyncio.wait(
                [run_task, recv_task, idle_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logging.exception("loop task raised during cancellation")
            for task in done:
                exc = task.exception()
                if exc is not None:
                    logging.error("loop task raised: %r", exc)
        finally:
            # Detach the line but keep Claude running. Only an explicit stop_claude
            # (via the orchestrator) tears the session down. Persist the
            # currently-driven controller so the next connection reuses it.
            notifier.on_approval = None   # mirror hub.detach: no line, no live push
            notifier.on_approval_resolved = None
            notifier.on_approval_speak = None
            notifier.on_update_speak = None
            notifier.on_queue_needs_input = None   # runner is per-connection
            # Persist the driven controller onto the ACTIVE fleet member (which
            # may have changed mid-call via switch/new/answer-attach), not the
            # member this connection started with: writing there would clone one
            # member's controller onto another and corrupt cwd-keyed routing.
            (sessions.active() or session).controller = orchestrator.controller
            if session_state is not None:
                wd = getattr(orchestrator.controller, "working_dir", None)
                if wd:
                    session_state.save(wd)
            _log.info("ws: phone disconnected (mic_in=%d audio_out=%d)",
                      _counts["in"], _counts["out"])
            hub.detach()


def _queue_restart_note(orchestrator) -> str:
    """Spoken suffix announcing pending queued tasks for the driven project on a
    fresh session, so a restart survivor is surfaced without auto-running it.
    Returns '' when nothing is pending. Fail-open (never breaks the opening)."""
    queue = getattr(orchestrator, "queue", None)
    if queue is None:
        return ""
    try:
        cwd = (getattr(orchestrator.controller, "working_dir", "") or "").rstrip("/")
        n = queue.pending_counts().get(cwd, 0)
    except Exception:
        return ""
    if not n:
        return ""
    proj = os.path.basename(cwd) or cwd
    plural = "s" if n != 1 else ""
    return (f" You have {n} queued task{plural} for {proj}; "
            f"say run them to continue.")


async def _handle_close_terminal(id_: str, orchestrator, websocket, notifier) -> None:
    """Close (tmux/iTerm2/Terminal.app) or, for an ax-backed session, end the
    Claude process behind an open terminal, on the phone's explicit request
    (works pre-voice, ``operator`` is never needed here).

    Resolves the terminal's cwd FIRST via a fresh discovery pass, so the
    driven controller and any registered fleet member pointed at that cwd can
    be cleanly detached BEFORE the close itself runs; closing out from under a
    live monitor/capture is exactly what that ordering avoids. Only after that
    cleanup does the actual close/end happen, followed by dropping that cwd's
    now-stale pending approvals. Fail-open throughout: any step failing still
    leaves the phone with a status line, never a broken connection."""
    from server.terminals import close_terminal, discover_claude_sessions

    try:
        sessions = await asyncio.to_thread(discover_claude_sessions)
    except Exception:
        logging.exception("close_terminal: discovery failed")
        sessions = []
    sess = next((s for s in sessions if s.get("id") == id_), None)
    cwd = (sess.get("cwd") or "").rstrip("/") if sess else ""

    if cwd:
        try:
            driven_cwd = (getattr(orchestrator.controller, "working_dir", "") or "").rstrip("/")
            if driven_cwd == cwd:
                await orchestrator.controller.stop(detach_only=True)
        except Exception:
            logging.exception("close_terminal: detaching driven controller failed")

        sessions_reg = getattr(orchestrator, "sessions", None)
        if sessions_reg is not None:
            try:
                member = sessions_reg.find_by_cwd(cwd)
                if member is not None:
                    try:
                        await member.controller.stop(detach_only=True)
                    except Exception:
                        logging.exception("close_terminal: fleet member detach failed")
                    sessions_reg.remove(member.id)
                    await orchestrator.push_sessions()
            except Exception:
                logging.exception("close_terminal: fleet cleanup failed")

    try:
        # Reuse the discovery snapshot from above so close_terminal doesn't run a
        # second full (slow) discovery sweep; fall back to its own discovery only
        # if the first pass failed, so a hiccup can't turn a closable terminal
        # into a spurious "no longer open".
        result = await asyncio.to_thread(
            close_terminal, id_,
            discover=(lambda: sessions) if sessions else None)
    except Exception:
        logging.exception("close_terminal: close failed")
        result = {"error": "internal error while closing the terminal"}

    if "error" in result:
        await websocket.send_json(
            {"type": "status", "status": f"close error: {result['error']}"})
    else:
        close_cwd = (result.get("cwd") or cwd or "").rstrip("/")
        if close_cwd and notifier is not None:
            try:
                for approval_id in notifier.approvals.drop_for(close_cwd):
                    await websocket.send_json(
                        {"type": "approval_resolved", "approval_id": approval_id,
                         "outcome": "stale"})
            except Exception:
                logging.exception("close_terminal: approval cleanup failed")

        reply = {"type": "terminal_closed", "id": id_}
        if result.get("note"):
            reply["note"] = result["note"]
        try:
            await websocket.send_json(reply)
        except Exception:
            logging.exception("close_terminal: terminal_closed push failed")

    try:
        await orchestrator.handle_tool_call("list_terminals", {})
    except Exception:
        logging.exception("close_terminal: terminals refresh failed")


async def handle_client_control(raw: str, orchestrator, websocket, operator=None,
                                notifier=None) -> None:
    """Handle a JSON control message sent by the phone (e.g. setting the folder).

    ``notifier`` is optional (defaults to ``None``) so every pre-existing call
    site (and every older test) that doesn't pass it keeps working unchanged;
    the approval/rules branches below are simply unreachable without one.
    """
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return
    mtype = data.get("type")
    if mtype == "say" and data.get("text") and operator is not None:
        await operator.send_text(data["text"])
    elif mtype == "claude_input" and data.get("text"):
        # Raw terminal chat from the phone's full-screen view: type straight into the
        # live Claude session, bypassing the voice operator.
        await orchestrator.send_direct(data["text"])
    elif mtype == "claude_key" and data.get("key"):
        # The terminal view's special-key row (arrows, Tab, Ctrl-C, ...): a named
        # keypress with no text and no Enter, injected directly. This is NOT an
        # approval decision, so it skips approval_decision's option validation
        # entirely; the allowlist here is the tmux name map itself, so an unknown
        # name is dropped before it ever reaches the session.
        key = data["key"]
        if key not in PRESS_KEY_NAMES:
            await websocket.send_json(
                {"type": "status",
                 "status": f"claude_key ignored: {key!r} is not a supported key"})
        else:
            await orchestrator.press_key(key)
    elif mtype == "claude_scrollback":
        # Full-screen terminal view wants the whole scrollback (not just the pane).
        await orchestrator.send_scrollback()
    elif mtype == "set_terminal" and data.get("app"):
        orchestrator.set_terminal_app(data["app"])
    elif mtype == "set_dir" and data.get("path"):
        result = await orchestrator.handle_tool_call(
            "set_working_dir", {"path": data["path"]}
        )
        if "error" in result:
            await websocket.send_json(
                {"type": "status", "status": f"folder error: {result['error']}"}
            )
    elif mtype == "attach_source" and data.get("cwd"):
        # Attach to a live open Claude terminal running in this folder (Phase 1/3
        # semantics: swaps the driven controller). Emit the working_dir status the
        # same way the pending-source answer path does; surface an attach failure.
        res = await orchestrator.attach_source(data["cwd"])
        if isinstance(res, dict) and "attached" in res:
            await websocket.send_json(
                {"type": "status", "working_dir": res.get("working_dir", data["cwd"])})
        elif isinstance(res, dict) and "error" in res:
            await websocket.send_json({"type": "status", "status": res["error"]})
    elif mtype == "resume_session" and data.get("cwd"):
        # Reopen a past conversation: launch the active session's controller in cwd
        # with `claude --resume <stem>` (stem = last "/"-segment of the history id).
        # Same drivable-after outcome as set_dir; only the error surfaces a status.
        result = await orchestrator.resume_session(
            data["cwd"], str(data.get("session_id") or ""))
        if isinstance(result, dict) and "error" in result:
            await websocket.send_json(
                {"type": "status", "status": f"resume error: {result['error']}"})
    elif mtype == "stop":
        # Interrupt the running Claude task; the session itself stays alive.
        # A stop also flushes the task queue; confirm how many items were dropped.
        res = await orchestrator.handle_tool_call("stop_claude", {})
        status = "stopped"
        if isinstance(res, dict) and res.get("dropped"):
            n = res["dropped"]
            status = f"stopped, dropped {n} queued task{'s' if n != 1 else ''}"
        await websocket.send_json({"type": "status", "status": status})
    elif mtype == "queue_task" and data.get("text"):
        # Relay an additional instruction to the queue (runs after the current task).
        await orchestrator.handle_tool_call("queue_task", {"text": data["text"]})
    elif mtype == "queue_remove" and data.get("id"):
        # Phone dropped a queued item; the orchestrator re-pushes the queue.
        await orchestrator.queue_remove(data["id"])
    elif mtype == "queue_move" and data.get("id") and data.get("index") is not None:
        # Phone reordered a queued item; the orchestrator re-pushes the queue.
        try:
            index = int(data["index"])
        except (TypeError, ValueError):
            return
        await orchestrator.queue_move(data["id"], index)
    elif mtype == "list_dirs":
        # The phone's folder browser asks for the subdirectories of a path. Resolve the
        # deepest existing ancestor (so a half-typed/nonexistent path still lists
        # something sensible) and send its subfolders back for navigation.
        from server.orchestrator import suggest_dirs
        base, options = suggest_dirs(data.get("path") or "~", limit=500)
        await websocket.send_json({"type": "dirs", "path": base, "dirs": options})
    elif mtype == "list_terminals":
        # The tool pushes a {"type":"terminals",...} message to the phone itself.
        await orchestrator.handle_tool_call("list_terminals", {})
    elif mtype == "screenshot":
        # The tool pushes a {"type":"screenshot",...} message to the phone itself.
        await orchestrator.handle_tool_call("take_screenshot", {})
    elif mtype == "attach_terminal" and data.get("id"):
        # recap=False: the phone never renders the transcript recap (it exists
        # for Gemini's context on the voice path), and building it here queued
        # multi-second transcript reads ahead of the scrollback request the
        # just-opened terminal view sends next.
        res = await orchestrator.handle_tool_call(
            "attach_terminal", {"id": data["id"], "recap": False})
        if "error" in res:
            await websocket.send_json({"type": "status", "status": res["error"]})
    elif mtype == "close_terminal" and data.get("id"):
        await _handle_close_terminal(data["id"], orchestrator, websocket, notifier)
    elif mtype == "list_sessions":
        # The tool pushes an unsolicited {"type":"sessions",...} on the voice
        # path; here the phone asked directly, so reply on this socket too (a
        # fake orchestrator in tests has no notify channel of its own).
        result = await orchestrator.handle_tool_call("list_sessions", {})
        await websocket.send_json(
            {"type": "sessions", "sessions": result.get("sessions", [])})
    elif mtype == "switch_session" and data.get("session_id"):
        result = await orchestrator.handle_tool_call(
            "switch_session", {"target": data["session_id"]})
        if "error" in result:
            await websocket.send_json({"type": "status", "status": result["error"]})
    elif mtype == "new_session" and data.get("path"):
        result = await orchestrator.handle_tool_call(
            "new_session", {"path": data["path"]})
        if "error" in result:
            await websocket.send_json(
                {"type": "status", "status": f"new session error: {result['error']}"})
    elif mtype == "history_list":
        from server import history
        sessions = await asyncio.to_thread(history.list_sessions,
                                           int(data.get("limit") or 50))
        await websocket.send_json({"type": "history_sessions", "sessions": sessions})
    elif mtype == "history_get" and data.get("id"):
        from server import history
        detail = await asyncio.to_thread(history.session_detail, data["id"])
        if "error" in detail:
            await websocket.send_json(
                {"type": "status", "status": f"history error: {detail['error']}"})
        else:
            await websocket.send_json({"type": "history_session", **detail})
    elif mtype == "approval_decision" and data.get("approval_id") and notifier is not None:
        approval_id = data["approval_id"]
        approval = notifier.approvals.get(approval_id)
        driven = getattr(orchestrator, "controller", None)
        driven_cwd = (getattr(driven, "working_dir", "") or "").rstrip("/")
        approval_cwd = (approval or {}).get("cwd", "").rstrip("/")
        if (approval is not None and not approval.get("action")
                and approval_cwd and approval_cwd != driven_cwd):
            # The prompt lives in a session we're not driving (a fleet member,
            # or the driven terminal swapped since it appeared). Attach to ITS
            # terminal first so the key lands in the pane that asked; only if
            # that fails does the decision degrade to stale below.
            attach = getattr(orchestrator, "attach_source", None)
            res = await attach(approval_cwd) if attach is not None else {"error": "n/a"}
            if isinstance(res, dict) and "error" not in res:
                driven_cwd = approval_cwd
        if approval is None or approval_cwd != driven_cwd:
            # Already resolved elsewhere, expired, or its terminal is gone:
            # pressing now would type into a DIFFERENT live pane than the one
            # that asked.
            await websocket.send_json(
                {"type": "approval_resolved", "approval_id": approval_id, "outcome": "stale"})
        elif data.get("key") not in [o["key"] for o in approval.get("options", [])]:
            await websocket.send_json(
                {"type": "status", "status": f"approval key error: {data.get('key')!r} is not an option"})
        elif approval.get("action"):
            # Synthetic (git) approval: there is no on-screen prompt behind it,
            # so dispatch the stored action instead of pressing a key into a
            # pane. Resolve and clear the card first (the decision itself was
            # delivered); the action's outcome, including a failure, is then
            # reported as a status line rather than leaving the card up for a
            # retry that would fail the same way.
            notifier.approvals.resolve(approval_id)
            await websocket.send_json(
                {"type": "approval_resolved", "approval_id": approval_id, "outcome": "sent"})
            if data["key"] == "y":
                res = await orchestrator.execute_approved_action(approval)
            else:
                res = {"summary": "Cancelled; nothing was run."}
            status = res.get("error") or res.get("summary") or "Done."
            await websocket.send_json({"type": "status", "status": status})
            if operator is not None:
                await operator.speak(status)
        else:
            res = await orchestrator.press_key(data["key"])
            if isinstance(res, dict) and "error" in res:
                # The press itself failed (session gone, unsupported key): leave the
                # approval active so the user can retry, instead of resolving it as
                # "sent" when nothing actually reached the live pane.
                await websocket.send_json(
                    {"type": "status", "status": f"approval press error: {res['error']}"})
            else:
                notifier.approvals.resolve(approval_id)
                await websocket.send_json(
                    {"type": "approval_resolved", "approval_id": approval_id, "outcome": "sent"})
                # Resume a queue burst that paused on this needs_input (tap path).
                resume = getattr(orchestrator, "_queue_resume", None)
                if resume is not None:
                    try:
                        await resume(driven_cwd)
                    except Exception:
                        logging.exception("queue resume after tap resolve failed")
    elif mtype == "get_notify_rules" and notifier is not None:
        await _broadcast_notify_rules(websocket, notifier)
    elif mtype == "set_notify_rule" and notifier is not None:
        try:
            notifier.rules.set_mode(data.get("cwd", ""), data.get("kind", ""),
                                    data.get("mode", ""))
        except ValueError as e:
            await websocket.send_json({"type": "status", "status": f"rule error: {e}"})
        else:
            await _broadcast_notify_rules(websocket, notifier)
    elif mtype == "set_notify_default" and notifier is not None:
        # Global default (finish/needs_input): the phone toggles ONE switch instead
        # of a per-project matrix. Per-cwd rules still override it; a per-cwd "silent"
        # remains the mute list. Re-broadcast the full payload on success.
        try:
            notifier.rules.set_default(data.get("kind", ""), data.get("mode", ""))
        except ValueError as e:
            await websocket.send_json({"type": "status", "status": f"rule error: {e}"})
        else:
            await _broadcast_notify_rules(websocket, notifier)


async def _broadcast_notify_rules(websocket, notifier) -> None:
    """Send the notify-rules payload: the per-cwd "rules" (the mute list) plus the
    additive global "default" map {finish, needs_input}. Old clients read only
    "rules" and ignore "default"."""
    await websocket.send_json({
        "type": "notify_rules",
        "rules": notifier.rules.overrides(),
        "default": notifier.rules.defaults(),
    })


@asynccontextmanager
async def _as_cm(obj):
    """Accept either an async context manager or a plain object."""
    if hasattr(obj, "__aenter__"):
        async with obj as entered:
            yield entered
    else:
        yield obj


@asynccontextmanager
async def _warm_cm(warm):
    """Adopt a prewarmed operator: it was already entered during the ring, so
    only the EXIT is ours here. The greeting it spoke is buffered inside
    `warm` (flushed by the caller right after entering)."""
    try:
        yield warm.operator
    finally:
        # Tolerate an already-cancelled run_task: serve_ws cancels it itself
        # right after flushing the buffers, well before this exit runs.
        warm.run_task.cancel()
        with contextlib.suppress(BaseException):
            await warm.run_task
        with contextlib.suppress(Exception):
            await warm.operator.__aexit__(None, None, None)
