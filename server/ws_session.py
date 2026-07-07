# server/ws_session.py
"""One phone WebSocket connection: free setup gate, metered operator lifecycle,
and the recv/idle loops. The Claude session itself lives in the SessionRegistry
and persists across connections; this module only serves one connection at a
time against it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import WebSocket, WebSocketDisconnect

from server.claude_controller import ClaudeController
from server.greetings import compose_opening, suppress_greeting_if_supported
from server.orchestrator import Orchestrator
from server.session import Session
from server.session_hub import SessionHub
from server.task_queue import TaskQueue
from server.tmux_controller import PRESS_KEY_NAMES, TmuxController, pick_session_name


async def serve_ws(websocket: WebSocket, *, config, mode: str, sessions, notifier,
                   operator_factory, session_state=None) -> None:
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

    async def speak(text): await operator.speak(text)
    async def notify(msg):
        if isinstance(msg, dict) and msg.get("type") == "transcript":
            touch()
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
    # phone (via the cloud) when a terminal finishes while they're away.
    if _account:
        notifier.last_account = _account

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
            # Folder/terminal selection is free, handle it before metering.
            await handle_client_control(msg["text"], orchestrator, websocket, None,
                                        notifier=notifier)

    async with _as_cm(operator_factory(config, orchestrator.handle_tool_call, **_kwargs)) as operator:
        async def audio_out(pcm):
            touch()  # Gemini is speaking -> the line is active
            _counts["out"] += 1
            if _counts["out"] % 50 == 1:
                _log.info("ws: sent %d audio chunks -> phone", _counts["out"])
            await websocket.send_bytes(pcm)
        operator.set_audio_out(audio_out)
        operator.set_text_out(notify)

        # Attach this operator's voice to the line and speak anything that
        # queued up while no phone was connected.
        pending_updates = hub.attach(lambda t: operator.speak(t))

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
            await operator.speak(f"{label} needs input. {text}")
        notifier.on_approval_speak = _speak_foreign_approval
        # Everything from here runs with the line attached; detach() MUST run even on
        # cancellation (uvicorn reload/shutdown) or an exception, otherwise line_open
        # stays True and every later background finish is silently dropped.
        try:
            # If this call was triggered by a specific Claude terminal, attach to THAT
            # terminal so Voxa continues that session instead of an empty default one,
            # and OPEN with that context so it knows where you are.
            source = sessions.pop_pending()
            attached_folder = None
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
            still_active = (notifier.approvals.active_for(attached_cwd) if attached_cwd
                            else notifier.approvals.latest())
            await _push_approval(still_active)

            # Push the current queue (and any undrained digest) to the phone so the
            # queue panel renders on connect. Fail-open by contract.
            try:
                await orchestrator.push_queue_snapshot()
            except Exception:
                logging.exception("queue snapshot push failed")

            # Voxa's opening is ALWAYS driven from here. Suppress the operator's own
            # auto-greet: on the metered path the cloud brain would otherwise race
            # ahead and speak a generic "what would you like to do?" the instant the
            # /live socket opens, before this contextual opening arrives.
            suppress_greeting_if_supported(operator)
            if attached_folder:
                opening = compose_opening(attached_folder, pending_updates,
                                          approval=still_active)
            elif pending_updates or still_active:
                opening = compose_opening("", pending_updates, approval=still_active)
            else:
                opening = "Hi! What would you like to work on?"
            # Restart announcement: pending queued tasks from a previous session are
            # NEVER auto-run; the opening mentions them so the user can say "run them".
            opening += _queue_restart_note(orchestrator)
            await operator.speak(opening, immediate=True)

            # If the session began because the user started talking, don't drop
            # that first frame.
            if first_audio is not None:
                await operator.send_audio(first_audio)

            paused = False

            async def recv_loop():
                nonlocal paused
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg["type"] == "websocket.disconnect":
                            break
                        if msg.get("bytes") is not None:
                            _counts["in"] += 1
                            # Read the CURRENTLY-driven controller (it swaps when the
                            # user attaches to another terminal mid-call); the loop's
                            # local `controller` would be stale after a swap.
                            cur = orchestrator.controller
                            if _counts["in"] % 50 == 1:
                                _log.info(
                                    "ws: recv %d mic frames (status=%s)",
                                    _counts["in"], getattr(cur, "status", "?"),
                                )
                            # Cost saver: while Claude is working, stop forwarding mic
                            # audio to Gemini. Gemini bills per audio token, so no
                            # audio in == no charge during the wait. Resume when idle.
                            # EXCEPTION: while a queue is engaged (the driven cwd has a
                            # non-empty queue), keep the mic OPEN during work so the user
                            # can stack another instruction by voice; queue_task's own
                            # verbatim + loop-guard rules still apply. A non-queue user's
                            # cost profile is unchanged (queue_engaged is False here).
                            working = getattr(cur, "status", "idle") == "working"
                            if working and not orchestrator.queue_engaged:
                                paused = True
                            else:
                                if paused:
                                    paused = False
                                    await websocket.send_json(
                                        {"type": "status", "status": "listening"}
                                    )
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
        # Cancel the running Claude task (works even while the mic stream is paused).
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
    elif mtype == "attach_terminal" and data.get("id"):
        res = await orchestrator.handle_tool_call("attach_terminal", {"id": data["id"]})
        if "error" in res:
            await websocket.send_json({"type": "status", "status": res["error"]})
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
        if approval is None or approval.get("cwd", "").rstrip("/") != driven_cwd:
            # Already resolved elsewhere, expired, OR the driven terminal swapped
            # since this prompt appeared (attach_terminal mid-call): pressing now
            # would type into a DIFFERENT live pane than the one that asked.
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
