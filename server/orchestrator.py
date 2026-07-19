from __future__ import annotations

import asyncio
import inspect
import logging
import os
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

Speak = Callable[[str], Awaitable[None]]
NotifyUI = Callable[[dict], Awaitable[None]]


def suggest_dirs(path: str, limit: int = 12) -> tuple[str, list[str]]:
    """Find the deepest existing ancestor of ``path`` and list its subdirectories.

    Used to help the user when a spoken folder path is wrong: the operator can
    read these back as suggestions.
    """
    p = os.path.abspath(os.path.expanduser(path or "~"))
    while p and not os.path.isdir(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    try:
        names = sorted(
            n for n in os.listdir(p)
            if os.path.isdir(os.path.join(p, n)) and not n.startswith(".")
        )
    except OSError:
        names = []
    return p, names[:limit]


class Orchestrator:
    def __init__(self, controller, speak: Speak, notify_ui: NotifyUI):
        self._c = controller
        self._speak = speak
        self._notify = notify_ui
        self._bg: set[asyncio.Task] = set()
        self._last_terminals: list[dict] = []
        # The final path is a two-layer thing: the controller always fires the
        # runner WRAPPER (_final_cb, stored once so its identity is stable across
        # swaps); the wrapper delegates to the INNER callback (_final_inner) for a
        # non-burst final, so a single un-queued task rings exactly as it does
        # today. set_final swaps the inner callback; the wrapper stays registered.
        self._final_inner = self._on_final
        self._final_cb = self._dispatch_final
        controller.on_final(self._final_cb)
        self._wire_output(controller)
        # Task 2: the per-session task queue and its runner state. `queue` is wired
        # by serve_ws (None in bare unit orchestrators -> the runner no-ops and the
        # final path stays byte-identical to today).
        self.queue = None
        self._running_item_id = None   # queue item currently executing, if any
        self._burst_cwd = None         # cwd of an active burst (>=1 item queued)
        self._queue_paused = False     # paused on needs_input, awaiting resolve
        self._on_between_items = None  # serve_ws sets this to touch() (watchdog)
        # Set by serve_ws after construction (mirrors notifier.approvals) so the
        # voice tool below can resolve a pending prompt it never saw arrive.
        self.approvals = None
        # Set by serve_ws after construction too, so resolve_approval can reach
        # notifier.on_approval_resolved (wired only once a line is attached) and
        # tell the phone to clear the card after a voice decision.
        self.notifier = None
        # Set by serve_ws after construction as well: the fleet registry, so the
        # list/switch/new session tools can address every driven session (not
        # just the one this orchestrator was built around).
        self.sessions = None

    async def _on_final(self, text: str) -> None:
        await self._speak(text)
        await self._notify({"type": "status", "status": "finished"})

    def _wire_output(self, controller) -> None:
        """Stream Claude's live screen to the phone UI (text), if the controller
        supports it. Not spoken, purely a visual 'what Claude is doing' feed. The
        colour variant is optional (tmux only); the getattr guard no-ops elsewhere."""
        register = getattr(controller, "on_output", None)
        if register:
            register(self._on_output)
        register_color = getattr(controller, "on_output_color", None)
        if register_color:
            register_color(self._on_output_color)

    async def _on_output(self, text: str) -> None:
        await self._notify({"type": "claude_output", "text": text})

    async def _on_output_color(self, text: str) -> None:
        await self._notify({"type": "claude_output_color", "text": text})

    async def send_scrollback(self) -> None:
        """Push Claude's full scrollback (coloured) to the phone's full-screen view.
        On-demand (the view requests it); tmux-only, a no-op for other controllers."""
        cap = getattr(self._c, "capture_scrollback", None)
        if not cap:
            return
        text = await asyncio.to_thread(cap)
        if text:
            await self._notify({"type": "claude_scrollback", "text": text})

    @property
    def controller(self):
        """The controller currently being driven (swaps when attaching to a terminal)."""
        return self._c

    def set_final(self, cb) -> None:
        """Set the final-output callback (e.g. the session hub's handler) so it is
        preserved across controller swaps when attaching to a different terminal.
        The runner WRAPPER stays the registered callback; this swaps the inner one
        it delegates to for a non-burst final."""
        self._final_inner = cb
        self._c.on_final(self._final_cb)

    async def _dispatch_final(self, text: str) -> None:
        """Controller-final entry point. The queue runner chains here: on an active
        burst it records the finished item, dispatches the next queued item (or emits
        ONE drain digest), and folds the per-item ring into that digest; otherwise it
        delegates to the inner callback so a single un-queued task rings exactly as
        today. Fail-open: any runner error delivers the final normally, so a queue
        bug never drops a real final."""
        suppress = False
        try:
            suppress = await self._runner_on_final(text)
        except Exception:
            logger.exception("queue runner on_final failed; delivering final normally")
            suppress = False
        if not suppress:
            await self._final_inner(text)

    # --- queue runner -------------------------------------------------------

    def _driven_cwd(self) -> str:
        """The rstrip-normalized cwd of the controller currently being driven; the
        key everything queue-side (add/items/finish/active-set) agrees on."""
        return (getattr(self._c, "working_dir", "") or "").rstrip("/")

    async def _send_and_report(self, text: str) -> None:
        """Await the send (TmuxController.send now returns whether it CONFIRMED the
        submit) and tell the phone the outcome via a `command_sent` control, so a
        command that got typed but not submitted surfaces instead of sitting silently
        in Claude's input box. A controller whose send() returns None (the non-tmux
        iTerm/Terminal/AX/Claude backends, which have no verification) is treated as an
        optimistic success so they still report ok=True. Fail-open: a failing _notify
        must never break this background task."""
        ok = await self._c.send(text or "")
        ok_bool = True if ok is None else bool(ok)
        try:
            await self._notify({"type": "command_sent", "text": text, "ok": ok_bool})
        except Exception:
            logger.warning("command_sent notify failed", exc_info=True)

    def _dispatch(self, text: str) -> None:
        """Fire-and-forget a send to the live Claude session (shared by
        send_to_claude and the queue runner). Non-blocking; a failed send is logged,
        never raised, so it cannot break the caller. Routes through _send_and_report
        so a `command_sent` outcome lands once the send resolves."""
        task = asyncio.create_task(self._send_and_report(text or ""))
        self._bg.add(task)

        def _done(t: asyncio.Task) -> None:
            self._bg.discard(t)
            exc = t.exception()
            if exc:
                logger.warning("send failed: %s", exc)
        task.add_done_callback(_done)

    async def _still_working(self) -> bool:
        """The busy guard's ground truth. Controllers set status='working'
        optimistically on send and only their monitor resets it, so a send
        that never produced pane activity wedges the flag and every later
        dispatch is refused as busy forever. Trust 'working' only after the
        controller re-verifies it against the live pane (verify_working heals
        a stale flag to idle as a side effect). Controllers without
        verify_working keep today's cached-flag behavior; fail-safe True on
        any error (never risk a double dispatch)."""
        if getattr(self._c, "status", "idle") != "working":
            return False
        verify = getattr(self._c, "verify_working", None)
        if verify is None:
            return True
        try:
            return bool(await verify())
        except Exception:
            logger.exception("verify_working failed; trusting cached busy state")
            return True

    def _queue_idle(self, cwd: str) -> bool:
        """A queue_task dispatches immediately only when nothing is running or
        queued for the driven cwd. A running item stays in items() until it
        finishes, so a non-empty queue always routes new instructions to enqueue."""
        if self.queue.items(cwd):
            return False
        return getattr(self._c, "status", "idle") != "working"

    async def _push_queue(self) -> None:
        """Push the current queue to the phone (id/text/state per item). Fail-open:
        a push error must never break a live call or drop a hook."""
        if self.queue is None:
            return
        try:
            cwd = self._driven_cwd()
            items = [{"id": i["id"], "text": i["text"], "state": i["state"]}
                     for i in self.queue.items(cwd)]
            await self._notify({"type": "task_queue", "items": items})
        except Exception:
            logger.exception("task_queue push failed")

    def _end_burst(self, cwd: str) -> None:
        """Clear the burst so the next un-queued task rings normally and later
        per-item finishes for this cwd stop being suppressed."""
        self._burst_cwd = None
        self._running_item_id = None
        self._queue_paused = False
        if self.notifier is not None:
            try:
                self.notifier.queue_active_cwds.discard(cwd)
            except Exception:
                pass

    async def _runner_on_final(self, text: str) -> bool:
        """Return True to SUPPRESS the inner final (folded into the burst digest),
        False to let it ring/speak as today. Records the finished item, dispatches
        the next queued one (calling on_between_items so the idle watchdog counts
        queue progress), and emits the digest once the queue drains."""
        if self.queue is None:
            return False
        cwd = self._driven_cwd()
        burst = self._burst_cwd is not None and self._burst_cwd == cwd
        running_id = self._running_item_id
        if not burst:
            # A lone immediate task (or a plain send_to_claude): clear any tracked
            # running item WITHOUT a digest so today's ring stands, and keep the
            # history clean (a single task must not leave a leftover outcome).
            if running_id is not None:
                self._running_item_id = None
                try:
                    self.queue.finish(running_id, "done")
                    self.queue.drain_outcomes(cwd)   # discard: single task keeps today's ring
                except Exception:
                    logger.exception("queue cleanup failed")
                await self._push_queue()
            return False
        # Burst in flight for this cwd.
        if running_id is not None:
            self._running_item_id = None
            try:
                self.queue.finish(running_id, "done")
            except Exception:
                logger.exception("queue finish failed")
        await self._push_queue()
        nxt = self.queue.pop_next(cwd)   # exactly once per finish
        if nxt is not None:
            self._running_item_id = nxt["id"]
            self._dispatch(nxt["text"])
            await self._push_queue()
            if self._on_between_items:
                try:
                    self._on_between_items()
                except Exception:
                    logger.exception("on_between_items callback failed")
            return True
        await self._emit_digest(cwd)
        return True

    async def _emit_digest(self, cwd: str) -> None:
        """Compose and route the ONE burst digest. Line open -> speak it on the live
        call; otherwise -> notifier.report so app-open queues it and closed rings it
        ONCE. Always ends the burst (even on an empty drain). Fail-open throughout."""
        outcomes = []
        try:
            outcomes = self.queue.drain_outcomes(cwd)
        except Exception:
            logger.exception("drain_outcomes failed")
        self._end_burst(cwd)
        if not outcomes:
            return
        from server.greetings import compose_digest
        project = os.path.basename(cwd) or cwd
        digest = compose_digest(project, outcomes)
        if not digest:
            return
        line_open = bool(getattr(getattr(self.notifier, "call_manager", None),
                                 "line_open", False)) if self.notifier else False
        try:
            if line_open or self.notifier is None:
                await self._speak(digest)
            else:
                await self.notifier.report(digest, kind="finish", cwd=cwd)
        except Exception:
            logger.exception("digest emit failed")

    async def _queue_note_needs_input(self, cwd: str) -> None:
        """The running queued item hit a permission/needs-input prompt: record it as
        the burst's 'needs you' outcome and PAUSE (do not dispatch the next item).
        The Phase 1 approval flow rings it immediately; the queue resumes once the
        approval is resolved. No-op unless a burst is running for this cwd."""
        if self.queue is None:
            return
        cwd = (cwd or "").rstrip("/")
        if self._burst_cwd is None or self._burst_cwd != cwd or self._queue_paused:
            return
        if self._running_item_id is not None:
            rid = self._running_item_id
            self._running_item_id = None
            try:
                self.queue.finish(rid, "needs_input")
            except Exception:
                logger.exception("queue finish needs_input failed")
        self._queue_paused = True
        await self._push_queue()

    async def _queue_resume(self, cwd: str) -> None:
        """Resume a burst paused on needs_input once its approval resolves: dispatch
        the next queued item, or emit the drain digest if nothing remains. No-op when
        no burst is paused for this cwd (an ordinary, non-queue approval resolve)."""
        if self.queue is None:
            return
        cwd = (cwd or "").rstrip("/")
        if not self._queue_paused or self._burst_cwd != cwd:
            return
        self._queue_paused = False
        nxt = self.queue.pop_next(cwd)   # exactly once per resume
        if nxt is not None:
            self._running_item_id = nxt["id"]
            self._dispatch(nxt["text"])
            await self._push_queue()
            if self._on_between_items:
                try:
                    self._on_between_items()
                except Exception:
                    logger.exception("on_between_items callback failed")
            return
        await self._emit_digest(cwd)

    async def queue_remove(self, item_id: str) -> None:
        """Phone control: drop a QUEUED item, then re-push the queue. Fail-open."""
        if self.queue is None:
            return
        try:
            self.queue.remove(item_id)
        except Exception:
            logger.exception("queue remove failed")
        await self._push_queue()

    async def queue_move(self, item_id: str, index: int) -> None:
        """Phone control: reorder a QUEUED item, then re-push the queue. Fail-open."""
        if self.queue is None:
            return
        try:
            self.queue.move(item_id, index)
        except Exception:
            logger.exception("queue move failed")
        await self._push_queue()

    async def push_queue_snapshot(self) -> None:
        """On connect: push the current queue (a pure read, no disk write), and if
        this cwd has pending items AND undrained outcomes, deliver them as a digest
        control once. Guarded by pending_counts so a queue-less session performs no
        queue writes on connect."""
        if self.queue is None:
            return
        await self._push_queue()
        cwd = self._driven_cwd()
        try:
            if not self.queue.pending_counts().get(cwd):
                return   # nothing pending -> do not touch outcomes (no disk churn)
            outcomes = self.queue.drain_outcomes(cwd)
        except Exception:
            logger.exception("queue snapshot outcomes failed")
            return
        if outcomes:
            done = [(o.get("summary") or o.get("text") or "") for o in outcomes
                    if o.get("outcome") == "done"]
            needs = [(o.get("summary") or o.get("text") or "") for o in outcomes
                     if o.get("outcome") == "needs_input"]
            try:
                await self._notify({"type": "digest", "done": done, "needs_input": needs})
            except Exception:
                logger.exception("digest push failed")

    # --- attaching to an already-open terminal -----------------------------

    def _build_controller(self, sess: dict):
        if sess.get("backend") == "iterm":
            from server.terminals import ItermController
            return ItermController(sess["raw_id"])
        if sess.get("backend") == "terminal_app":
            from server.terminals import TerminalAppController
            return TerminalAppController(sess["raw_id"])
        if sess.get("backend") == "tmux":
            from server.tmux_controller import TmuxController
            return TmuxController(
                session_name=sess["raw_id"], socket=sess.get("socket"),
                launch_terminal=False,
            )
        if sess.get("backend") == "ax" and sess.get("app_pid"):
            from server.ax_controller import AXController
            return AXController(sess["app_pid"], sess.get("cwd", ""))
        return None

    async def _swap_controller(self, new) -> None:
        # A queue burst is scoped to the session we are driving; only the active
        # session's queue runs. Switching away (fleet switch, new_session, attach
        # to another terminal) abandons the runner for that cwd, so end the burst
        # here. Otherwise its cwd stays in the notifier's suppression set and every
        # later finish for it (arriving via the Stop hook once it is backgrounded)
        # is silently dropped, and stale burst state could resurrect a queued item
        # after switching back. Remaining queued items stay PENDING on disk
        # (announced on the next connect), never auto-run headless.
        if self._burst_cwd is not None:
            self._end_burst(self._burst_cwd)
        try:
            # detach_only: stop the old monitor but do NOT send Escape, otherwise
            # attaching to terminal B would cancel the in-flight generation in the
            # session we're leaving. (Escape is reserved for the explicit stop_claude.)
            await self._c.stop(detach_only=True)
        except Exception:
            pass
        self._c = new
        new.on_final(self._final_cb)
        self._wire_output(new)

    def _resolve_terminal(self, args: dict):
        items = self._last_terminals
        if not items:
            return None
        tid = args.get("id")
        if tid:
            for s in items:
                if s["id"] == tid:
                    return s
        idx = args.get("index")
        if isinstance(idx, int) and 1 <= idx <= len(items):
            return items[idx - 1]
        match = (args.get("match") or args.get("target") or "").lower().strip()
        if match:
            for s in items:
                if match in s["label"].lower() or match in s.get("cwd", "").lower():
                    return s
        return None

    async def _attach(self, sess: dict, include_recap: bool = True) -> dict:
        new = self._build_controller(sess)
        if new is None:
            return {"error": f"cannot control a {sess.get('app','')} terminal directly"}
        await self._swap_controller(new)
        try:
            await new.start(sess.get("cwd") or None)
        except PermissionError as e:
            return {"error": str(e)}
        await self._notify({"type": "status", "working_dir": new.working_dir or sess.get("cwd", "")})
        if getattr(new, "mirrors_screen", True) is False:
            await self._notify({
                "type": "claude_output",
                "text": "Live view isn't available for this terminal.",
            })
        # Recap what this terminal was working on, read from Claude's own
        # transcript. Skipped on the phone's TAP path (include_recap=False):
        # the recap exists for Gemini's context, the phone never renders it,
        # and building it here held up the scrollback request queued right
        # behind this control, so the just-opened terminal view sat blank.
        recap = ""
        if include_recap:
            try:
                from server.transcripts import recap as build_recap
                recap = await asyncio.to_thread(build_recap, sess.get("cwd", ""))
            except Exception:
                pass
        result = {"attached": sess["label"], "working_dir": sess.get("cwd", "")}
        if recap:
            result["recap"] = recap
        return result

    async def attach_source(self, cwd: str) -> dict:
        """Attach to the open Claude terminal running in ``cwd`` (the session that
        triggered a call), so answering continues THAT session with its context.
        Returns the attach result (with recap) or an error dict."""
        cwd = (cwd or "").rstrip("/")
        if not cwd:
            return {"error": "no cwd"}
        from server.terminals import discover_claude_sessions
        sessions = await asyncio.to_thread(discover_claude_sessions)
        self._last_terminals = list(sessions)
        match = next((s for s in sessions if (s.get("cwd") or "").rstrip("/") == cwd), None)
        if match is None:
            return {"error": "source terminal not open or not discoverable"}
        if not match.get("controllable"):
            return {"error": f"{match.get('app', '')} terminal can't be driven (use tmux/iTerm2)"}
        return await self._attach(match)

    # --- fleet: multiple registered sessions --------------------------------

    @staticmethod
    def _session_label(session) -> str:
        """Speakable name for a fleet member: the folder it works in. This is
        what the user says to switch ("the adcli one"), so it must match what
        the sessions card shows."""
        cwd = (getattr(session.controller, "working_dir", "") or "").rstrip("/")
        return os.path.basename(cwd) or cwd

    def _fleet_payload(self) -> list[dict]:
        """The fleet as the phone renders it. `active` marks the registry's
        explicit selection (active_id), i.e. the session voice is driving."""
        return [{
            "id": s.id,
            "label": self._session_label(s),
            "cwd": getattr(s.controller, "working_dir", "") or "",
            "status": getattr(s.controller, "status", "idle"),
            "active": s.id == self.sessions.active_id,
        } for s in self.sessions.all()]

    async def push_sessions(self) -> None:
        """Push the fleet list to the phone unsolicited (on connect and after
        any switch/new), so the sessions card stays current without polling.
        No-op when no registry is wired (bare unit-test orchestrators)."""
        if self.sessions is None:
            return
        await self._notify({"type": "sessions", "sessions": self._fleet_payload()})

    def _resolve_fleet_member(self, target: str):
        """Resolve a spoken/tapped switch target: exact id first, then exact
        (rstrip-normalized) cwd, then case-insensitive label substring; the
        same laddering _resolve_terminal applies to open terminals."""
        members = self.sessions.all()
        for s in members:
            if s.id == target:
                return s
        want = target.rstrip("/")
        for s in members:
            cwd = (getattr(s.controller, "working_dir", "") or "").rstrip("/")
            if cwd and cwd == want:
                return s
        needle = target.lower().strip()
        if needle:
            for s in members:
                if needle in self._session_label(s).lower():
                    return s
        return None

    async def _reattach(self, controller) -> None:
        """Re-arm a swapped-in controller so its still-running Claude can be driven
        again. _swap_controller detaches the OUTGOING session; the INCOMING one may
        itself have been detached on an earlier switch-away (its _started cleared,
        monitor dead), so send_to_claude/press_key would return no_session. Calling
        the controller's reattach (when it has one) restarts the monitor without
        start()'s kill+relaunch. Fail-open: a controller without reattach, or a
        session that is already gone, just stays as it was."""
        fn = getattr(controller, "reattach", None)
        if fn is None:
            return
        try:
            await fn()
        except Exception:
            logger.exception("reattach failed")

    async def _switch_to(self, session) -> None:
        """Make ``session`` the active fleet member and drive its controller.
        The generalized _swap_controller: only the LOCAL monitor of the session
        being left is stopped (detach_only), its Claude process keeps running
        and its finishes keep arriving via the cwd-keyed hooks."""
        # Switching to the session we are ALREADY driving would detach its own
        # controller (stop(detach_only=True) on self._c) and leave it undrivable;
        # a spoken "switch to X" that fuzzy-matches the current project hits this.
        # Just refresh the card and return.
        if session.controller is self._c:
            self.sessions.set_active(session.id)
            await self.push_sessions()
            return
        self.sessions.set_active(session.id)
        await self._swap_controller(session.controller)
        # The target was detached the last time we switched away from it; re-arm
        # its monitor so its live output is spoken and it accepts input again.
        await self._reattach(session.controller)
        await self._notify({
            "type": "status",
            "working_dir": getattr(session.controller, "working_dir", "") or ""})
        await self.push_sessions()

    async def cancel_all(self) -> int:
        """Cancel any in-flight Claude send task and stop the controller. Also
        flushes the driven session's task queue (a stop drops the whole burst) and
        returns how many queued+running items were dropped, so the caller can speak
        the count. Fail-open on the queue side."""
        pending = list(self._bg)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await self._c.stop()
        return await self._flush_queue()

    async def interrupt_task(self) -> int:
        """Stop the CURRENT run without tearing the session down: cancel any
        in-flight send task, interrupt the controller's generation (tmux Escape /
        SDK interrupt; controllers without interrupt fall back to a full stop),
        and flush the queue (a stop drops the whole burst). Unlike cancel_all,
        the session stays attached and driveable, so the user can immediately
        say what to do instead. Returns how many queued+running items dropped."""
        pending = list(self._bg)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        fn = getattr(self._c, "interrupt", None)
        if fn is not None:
            await fn()
        else:
            await self._c.stop()
        return await self._flush_queue()

    async def _flush_queue(self) -> int:
        """Drop the driven cwd's queued items after a stop/interrupt; returns the
        dropped count so the caller can speak it. Fail-open on the queue side."""
        dropped = 0
        if self.queue is not None:
            cwd = self._driven_cwd()
            try:
                dropped = self.queue.flush(cwd)
            except Exception:
                logger.exception("queue flush failed")
            self._end_burst(cwd)
            await self._push_queue()
        return dropped

    def set_terminal_app(self, app: str) -> None:
        fn = getattr(self._c, "set_terminal_app", None)
        if fn:
            fn(app)

    def remember_terminals(self, sessions: list[dict]) -> None:
        """Cache the terminals the phone is shown (its tappable list) so a later
        attach_terminal by id resolves against the same set the user sees."""
        self._last_terminals = list(sessions or [])

    async def send_direct(self, text: str) -> None:
        """Type text straight into the live Claude session (raw terminal chat from the
        phone's full-screen view), bypassing the voice operator. Non-blocking like
        send_to_claude; the result streams back through the live-output feed."""
        text = (text or "").strip()
        if not text:
            return
        if not getattr(self._c, "_started", True):
            await self._notify({"type": "status",
                                "status": "Open a folder first to chat with Claude."})
            return
        task = asyncio.create_task(self._send_and_report(text))
        self._bg.add(task)

        def _done(t: asyncio.Task) -> None:
            self._bg.discard(t)
            if t.exception():
                logger.warning("claude_input send failed: %s", t.exception())
        task.add_done_callback(_done)
        await self._notify({"type": "status", "status": "Claude working"})

    async def press_key(self, key: str) -> dict:
        """Actuate a decided approval, or a named special key from the terminal
        view's `claude_key` control, by pressing a single key in the live
        session (no text, no Enter): the ONLY way Voxa acts on a prompt, since the
        hook never blocks a tool. Guards `_started` like `send_direct` so an
        approval that outlives its session degrades to an error, not a crash.
        `press()` raises `ValueError` for a key it doesn't recognise (not a
        named special key, not a single printable character); that's surfaced
        here as an error reply instead of propagating as a crash."""
        if not getattr(self._c, "_started", True):
            return {"error": "no_session"}
        press = getattr(self._c, "press", None)
        if press is None:
            return {"error": "press not supported"}
        try:
            await press(key)
        except ValueError as e:
            return {"error": str(e)}
        return {"pressed": key}

    async def _notify_approval_resolved(self, approval_id: str) -> None:
        """Mirror the tap path's approval_resolved push so the phone's card
        clears even though a voice decision never touched the websocket.
        Fail-open: no wired notifier (or no line attached) must never break
        the voice tool itself."""
        cb = getattr(getattr(self, "notifier", None), "on_approval_resolved", None)
        if cb:
            try:
                await cb(approval_id)
            except Exception:
                logger.warning("on_approval_resolved callback failed", exc_info=True)

    async def _offer_approval(self, approval: dict) -> dict:
        """Register a synthetic approval and push its card to the attached
        phone. The tool result tells Gemini nothing ran yet, so it reads the
        summary aloud and waits for the user's decision instead of narrating
        a commit that never happened."""
        if self.approvals is None:
            return {"error": "approvals are not available on this connection"}
        self.approvals.put(approval)
        # Push straight through on_approval (not notifier.report): a git tool
        # only fires during a live call, and report()'s queue/ring branches are
        # for background updates, not a card the user just asked for.
        cb = getattr(getattr(self, "notifier", None), "on_approval", None)
        if cb:
            try:
                await cb(approval)
            except Exception:
                logger.warning("on_approval push failed", exc_info=True)
        return {"pending_approval": approval["approval_id"],
                "summary": approval["summary"],
                "hint": "Nothing has run yet. Read the summary to the user and "
                        "ask them to confirm; when they answer, call "
                        "resolve_approval with their decision."}

    async def _maybe_gate_dangerous(self, text: str, via: str) -> dict | None:
        """Classify ``text`` for a destructive/irreversible request; if flagged,
        build a synthetic approval (same SYNTHETIC pattern as git_commit/git_push)
        instead of letting send_to_claude/queue_task dispatch it straight away.
        Returns the approval-offer dict to return from handle_tool_call, or None
        when the text is safe to dispatch immediately."""
        from server.danger import classify
        reason = classify(text)
        if not reason:
            return None
        from server.approvals import build_action_approval
        cwd = self._c.working_dir or ""
        capped = (text or "")[:120]
        summary = f"Careful: this {reason}. Run it? {capped}"
        action = {"kind": "dangerous_send", "cwd": cwd, "text": text, "via": via}
        approval = build_action_approval(
            cwd, summary, tool="dangerous_command", action=action,
            options=[{"key": "y", "label": "Run it"},
                     {"key": "n", "label": "Cancel"}])
        return await self._offer_approval(approval)

    async def execute_approved_action(self, approval: dict) -> dict:
        """Run the pending action a synthetic approval carries, AFTER the user
        approved it (either path). Kept on the orchestrator so the tap path
        (ws_session) and the voice path share one executor. git calls run in a
        thread because they are blocking subprocesses."""
        from server import git_ops
        action = (approval or {}).get("action") or {}
        kind = action.get("kind", "")
        cwd = action.get("cwd", "")
        if kind == "dangerous_send":
            # Re-dispatch the ORIGINAL text through the same path it came from,
            # now that the user has confirmed it. `confirmed=True` skips the
            # danger gate on re-entry so this can't loop back into another
            # approval.
            text = action.get("text", "")
            via = action.get("via", "send_to_claude")
            if via == "send_to_claude":
                self._dispatch(text)
                await self._notify({"type": "status", "status": "Claude working"})
                return {"summary": "Okay, running it."}
            return await self.handle_tool_call(
                "queue_task", {"text": text, "confirmed": True})
        if kind in ("git_commit", "git_push"):
            # Run the confirmed git action IN the visible Claude session (not a
            # hidden subprocess) so the user watches it happen in their terminal.
            # The preflight already validated the repo/branch at offer time and
            # the user just approved; we hand Claude a tightly-scoped git-only
            # instruction and let the monitor relay the result. _dispatch bypasses
            # the danger gate (that gate lives in the send_to_claude/queue_task
            # tools), so a confirmed commit/push can't loop back into an approval.
            if kind == "git_commit":
                msg = (action.get("message") or "").replace('"', "'")
                instr = ("Using git only, stage all current changes and commit them "
                         f'with exactly this message: "{msg}". Do not modify any files.')
                if action.get("push"):
                    instr += " Then push the current branch to its remote."
            else:
                instr = ("Using git only, push the current branch to its remote. "
                         "Do not modify any files or make a commit.")
            self._dispatch(instr)
            await self._notify({"type": "status", "status": "Claude working"})
            verb = "Committing" if kind == "git_commit" else "Pushing"
            if kind == "git_commit" and action.get("push"):
                verb = "Committing and pushing"
            return {"summary": f"{verb} in the terminal now; I'll tell you when it's done."}
        return {"error": f"unknown pending action: {kind or 'none'}"}

    def _decide_key(self, decision: str, options: list[dict]) -> str:
        """Map a spoken decision to one of the approval's option keys. Exact key
        match wins first (Gemini may just pass through the key it read aloud);
        otherwise "yes"/"no" map onto the option set. Deterministic: an
        unrecognised decision falls back to the same safe path as "no" (decline)
        rather than guessing an acceptance."""
        d = (decision or "").strip().lower()
        for o in options:
            if d == o["key"].lower():
                return o["key"]
        if d == "yes":
            return options[0]["key"]
        for o in options:
            if o["key"].lower() == "n":
                return o["key"]
        for o in options:
            if o["key"].lower() == "esc" or "esc" in o.get("label", "").lower():
                return o["key"]
        return options[-1]["key"]

    async def _start(self, working_dir: str, resume: str | None = None) -> dict:
        try:
            await self._launch(working_dir, resume)
        except RuntimeError as e:
            # A missing tool (tmux not installed) is not a bad-path problem, so
            # folder suggestions would be misleading; give the model something it
            # can read back verbatim instead, and tell it not to improvise a
            # workaround (this is what used to produce the "I'm running into an
            # issue with tmux, but I can still work in the Desktop folder" confabulation).
            msg = str(e)
            if msg.startswith("tmux_not_installed"):
                return {"error": "tmux_not_installed",
                        "say": "tmux isn't installed on this Mac, so I can't start "
                               "sessions yet. On the laptop, run: brew install tmux, "
                               "then ask me again."}
            return {"error": msg}
        except ValueError as e:
            # A bad spoken folder path: help the user recover with suggestions.
            base, options = suggest_dirs(working_dir)
            return {"error": str(e), "searched_in": base, "suggestions": options}
        except Exception as e:
            return {"error": str(e)}
        await self._notify({"type": "status", "working_dir": self._c.working_dir})
        # If the visible terminal window couldn't be opened, tell the user how to
        # attach manually instead of leaving them with an invisible session.
        hint = getattr(self._c, "window_hint", "")
        if hint:
            await self._notify({"type": "status", "status": hint})
        return {"status": self._c.status, "working_dir": self._c.working_dir}

    async def _launch(self, working_dir: str, resume: str | None) -> None:
        """Start the driven controller. Pass ``resume`` ONLY to a controller whose
        start() accepts it (TmuxController); any other backend (iTerm, Terminal.app,
        AX, the default ClaudeController) degrades gracefully to a normal launch so
        resume can never break a non-tmux session."""
        if resume and "resume" in inspect.signature(self._c.start).parameters:
            await self._c.start(working_dir, resume=resume)
        else:
            await self._c.start(working_dir)

    async def resume_session(self, cwd: str, session_id: str) -> dict:
        """Reopen a past Claude conversation in ``cwd`` by launching the driven
        controller with ``claude --resume <stem>``. ``session_id`` may be a bare
        stem or a history id shaped "enc_dir/stem"; only the last "/"-segment is the
        resume stem. Same path as set_working_dir/_start (dir validation with folder
        suggestions on failure, working_dir status on success), just passing resume."""
        stem = (session_id or "").rsplit("/", 1)[-1].strip()
        return await self._start(cwd, resume=stem or None)

    async def handle_tool_call(self, name: str, args: dict) -> dict:
        # Timed: a tool round-trip sits INSIDE the user's turn (Gemini answers
        # only after it returns), so slow tools read as "Voxa takes seconds to
        # respond". The log makes that share of turn latency visible.
        import time as _time
        _t0 = _time.monotonic()
        try:
            return await self._handle_tool_call(name, args)
        finally:
            logging.getLogger("voxa").info(
                "tool %s took %.2fs", name, _time.monotonic() - _t0)

    async def _handle_tool_call(self, name: str, args: dict) -> dict:
        if name in ("start_claude_session", "set_working_dir"):
            key = "working_dir" if name == "start_claude_session" else "path"
            return await self._start(args.get(key, ""))
        if name == "list_dirs":
            base, options = suggest_dirs(args.get("parent", "~"))
            return {"path": base, "dirs": options}
        if name == "make_dir":
            target = os.path.abspath(os.path.expanduser(args.get("path", "")))
            if not target:
                return {"error": "no path given"}
            try:
                os.makedirs(target, exist_ok=True)
            except OSError as e:
                return {"error": str(e)}
            return await self._start(target)
        if name == "send_to_claude":
            # Guard: if no Claude session is running yet, tell Gemini so it asks the
            # user for a folder (the system prompt handles this) instead of firing a
            # background send() that crashes with "call start() before send()".
            if not getattr(self._c, "_started", True):
                return {"error": "no_session",
                        "hint": "No Claude session is running. Ask the user which "
                                "folder to work in (or to create one) before sending."}
            # Busy guard (server-side, authoritative): one task at a time. The
            # mic stays open while Claude works, so Gemini WILL hear the user
            # mid-task; whatever it decides, a second dispatch is refused here
            # and steered to the right tool instead. Verified against the LIVE
            # pane, never the cached flag alone (a wedged 'working' heals here).
            if await self._still_working():
                return {"error": "busy",
                        "hint": "Claude is still working on the previous task; do "
                                "NOT resend it. If this is a NEW instruction from "
                                "the user, call queue_task with their exact words. "
                                "If they want the current task stopped, call "
                                "stop_claude. Otherwise just tell them it's still "
                                "in progress."}
            text = args.get("text", "")
            if not args.get("confirmed"):
                gate = await self._maybe_gate_dangerous(text, "send_to_claude")
                if gate is not None:
                    return gate
            self._dispatch(text)
            await self._notify({"type": "status", "status": "Claude working"})
            return {"accepted": True, "status": "working",
                    "note": "Claude is now working; its result will be relayed to "
                            "you when it finishes. Until then, never call "
                            "send_to_claude again or answer for Claude. If the "
                            "user speaks meanwhile: queue_task for a new "
                            "instruction, stop_claude to cancel, get_claude_status "
                            "for progress, otherwise say it's still in progress."}
        if name == "queue_task":
            # Relay an ADDITIONAL instruction while a task runs: dispatch it now if
            # the driven session is idle with an empty queue (behaves like
            # send_to_claude), else enqueue it to run after the current one. Mirrors
            # send_to_claude's no_session guard so the loop-guard integrity holds.
            text = args.get("text", "")
            confirmed = bool(args.get("confirmed"))
            if self.queue is None:
                # No queue wired (bare orchestrator): fall back to a plain send.
                return await self.handle_tool_call(
                    "send_to_claude", {"text": text, "confirmed": confirmed})
            if not getattr(self._c, "_started", True):
                return {"error": "no_session",
                        "hint": "No Claude session is running. Ask the user which "
                                "folder to work in (or to create one) before queueing."}
            if not confirmed:
                gate = await self._maybe_gate_dangerous(text, "queue_task")
                if gate is not None:
                    return gate
            cwd = self._driven_cwd()
            # Heal a wedged 'working' flag first: _queue_idle reads the cached
            # status, and a stale one would wrongly enqueue instead of dispatch.
            await self._still_working()
            if self._queue_idle(cwd):
                item = self.queue.add(cwd, text)
                self.queue.pop_next(cwd)   # mark it running (exactly once)
                self._running_item_id = item["id"]
                self._dispatch(text)
                await self._notify({"type": "status",
                                    "status": "Claude working"})
                await self._push_queue()
                return {"accepted": True, "queued": False}
            self.queue.add(cwd, text)
            self._burst_cwd = cwd   # a burst is now in flight for this cwd
            if self.notifier is not None:
                try:
                    self.notifier.queue_active_cwds.add(cwd)
                except Exception:
                    pass
            await self._push_queue()
            position = sum(1 for i in self.queue.items(cwd) if i["state"] == "queued")
            return {"queued": True, "position": position}
        if name == "list_terminals":
            from server.terminals import discover_claude_sessions
            self._last_terminals = await asyncio.to_thread(discover_claude_sessions)
            items = [
                {"id": s["id"], "label": s["label"], "app": s["app"],
                 "cwd": s.get("cwd", ""), "controllable": s["controllable"],
                 "backend": s.get("backend", ""),
                 # Activity preview: lets the phone list and the voice operator
                 # tell apart several sessions in the SAME folder by what each
                 # one is doing ("working on X" vs an idle prompt) and for how
                 # long it has been open.
                 "status": s.get("status", ""), "hint": s.get("hint", ""),
                 "age": s.get("age", "")}
                for s in self._last_terminals
            ]
            # Send both keys: the phone reads `terminals`, the web client reads `items`.
            await self._notify({"type": "terminals", "terminals": items, "items": items})
            return {"terminals": items}
        if name == "take_screenshot":
            from server.screenshot import capture_screenshot
            result = await capture_screenshot()
            if "error" in result:
                await self._notify({"type": "screenshot", "error": result["error"]})
                return {"error": result["error"]}
            await self._notify({"type": "screenshot", "image": result["image"]})
            return {"status": "sent"}
        if name == "attach_terminal":
            sess = self._resolve_terminal(args)
            if sess is None:
                # The phone's tappable list is pushed by a direct discovery, which
                # doesn't populate _last_terminals (and it resets on every reconnect,
                # since the orchestrator is rebuilt). Re-discover and retry so tapping
                # an open terminal works without a prior list_terminals tool call.
                from server.terminals import discover_claude_sessions
                self._last_terminals = await asyncio.to_thread(discover_claude_sessions)
                sess = self._resolve_terminal(args)
            if sess is None:
                return {"error": "terminal not found; call list_terminals first"}
            if not sess.get("controllable"):
                return {"error": f"that {sess.get('app','')} terminal can't be driven; "
                                 "run Claude inside tmux and I can attach"}
            return await self._attach(sess,
                                      include_recap=args.get("recap") is not False)
        if name == "list_sessions":
            if self.sessions is None:
                return {"error": "no session registry wired"}
            payload = self._fleet_payload()
            # Push the same payload to the phone so a voice-initiated listing
            # also refreshes the sessions card.
            await self._notify({"type": "sessions", "sessions": payload})
            return {"sessions": payload}
        if name == "switch_session":
            if self.sessions is None:
                return {"error": "no session registry wired"}
            target = str(args.get("target") or args.get("session_id") or "").strip()
            session = self._resolve_fleet_member(target) if target else None
            if session is None:
                labels = ", ".join(self._session_label(s)
                                   for s in self.sessions.all())
                return {"error": f"no session matching {target!r}; "
                                 f"available: {labels or 'none'}"}
            await self._switch_to(session)
            # Recap what the target session was working on, read from Claude's
            # own transcript: the same mechanism attach_source uses.
            cwd = getattr(session.controller, "working_dir", "") or ""
            recap = ""
            try:
                from server.transcripts import recap as build_recap
                recap = await asyncio.to_thread(build_recap, cwd)
            except Exception:
                pass
            result = {"switched": self._session_label(session)}
            if recap:
                result["recap"] = recap
            return result
        if name == "new_session":
            if self.sessions is None:
                return {"error": "no session registry wired"}
            path = os.path.abspath(os.path.expanduser(str(args.get("path") or "")))
            if not os.path.isdir(path):
                # Same recovery _start offers for a wrong spoken folder path.
                base, options = suggest_dirs(path)
                return {"error": f"{path} is not a folder",
                        "searched_in": base, "suggestions": options}
            import uuid
            from server.session import Session
            from server.session_hub import SessionHub
            from server.tmux_controller import TmuxController, pick_session_name
            sid = uuid.uuid4().hex[:8]
            controller = TmuxController(
                session_name=pick_session_name(sid, cwd=path),
                launch_terminal=True,
                terminal_app=os.environ.get("VOXA_TERMINAL_APP", "auto"),
            )
            call_manager = getattr(self.notifier, "call_manager", None)
            hub = SessionHub(controller, call_manager)
            if getattr(self.notifier, "hooks_live", False):
                hub.set_offline_ring(False)   # hooks already drive offline rings
            # Remember where we were so a failed start can put everything back:
            # a half-created member must never strand the fleet on a dead
            # controller or leave a ghost entry in the registry.
            prev_active = self.sessions.active_id
            prev_controller = self._c
            session = self.sessions.add(Session(sid, controller, hub, call_manager))
            self.sessions.set_active(sid)
            # The previous session keeps running untouched: only its local
            # monitor detaches (the generalized swap, same as switch_session).
            await self._swap_controller(controller)
            try:
                await controller.start(path)
            except Exception as e:
                self.sessions.remove(sid)
                if prev_active:
                    self.sessions.set_active(prev_active)
                await self._swap_controller(prev_controller)
                # _swap_controller detached the previous session (stop(detach_only=
                # True) cleared its _started); re-arm it so the restored session is
                # drivable again instead of degrading to no_session on the next send.
                await self._reattach(prev_controller)
                # Differentiate failure kinds the same way _start does: a missing
                # tool is not a bad-path problem (folder suggestions would mislead
                # the model into confabulating a workaround), so give it a plain,
                # model-legible error instead.
                msg = str(e)
                if isinstance(e, RuntimeError) and msg.startswith("tmux_not_installed"):
                    return {"error": "tmux_not_installed",
                            "say": "tmux isn't installed on this Mac, so I can't "
                                   "start sessions yet. On the laptop, run: "
                                   "brew install tmux, then ask me again."}
                if isinstance(e, ValueError):
                    base, options = suggest_dirs(path)
                    return {"error": msg, "searched_in": base, "suggestions": options}
                return {"error": msg}
            await self._notify({"type": "status",
                                "working_dir": controller.working_dir or path})
            await self.push_sessions()
            return {"created": self._session_label(session)}
        if name == "get_claude_status":
            # Answer from the live pane, not the cached flag: verification heals
            # a wedged 'working' to idle before we report it.
            await self._still_working()
            return {"status": self._c.status, "working_dir": self._c.working_dir}
        if name == "stop_claude":
            dropped = await self.interrupt_task()
            res = {"status": "interrupted",
                   "note": "The task was stopped but the session is still open "
                           "with its context intact. Confirm it stopped and ask "
                           "the user what they'd like to do next."}
            if dropped:
                res["dropped"] = dropped
            return res
        if name == "resolve_approval":
            # Prefer the pane actually being driven; fall back to the latest
            # approval from ANY session (the user may be answering a menu that
            # rang from a fleet member Voxa isn't attached to). A spoken
            # decision still only ever presses into the terminal that raised
            # the STILL-CURRENT prompt: for a foreign approval we attach to its
            # terminal first, and refuse if that attach fails.
            driven_cwd = (getattr(self._c, "working_dir", "") or "").rstrip("/")
            approval = (self.approvals.active_for(driven_cwd)
                        if (self.approvals and driven_cwd) else None)
            if approval is None and self.approvals is not None:
                approval = self.approvals.latest()
            if not approval or not approval.get("options"):
                return {"error": "no active approval"}
            target_cwd = (approval.get("cwd") or "").rstrip("/")
            if approval.get("action") and target_cwd and target_cwd != driven_cwd:
                # Synthetic (git) approvals have no pane to attach to; keep the
                # original strict guard: only the driven cwd's action may run.
                return {"error": "no active approval"}
            if (not approval.get("action") and target_cwd
                    and target_cwd != driven_cwd):
                res = await self.attach_source(target_cwd)
                if isinstance(res, dict) and "error" in res:
                    return {"error": f"the prompt is in {target_cwd} but I "
                                     f"couldn't attach to it: {res['error']}"}
                driven_cwd = target_cwd
            key = self._decide_key(args.get("decision", ""), approval["options"])
            if approval.get("action"):
                # Synthetic (git) approval: there is no on-screen prompt to
                # press into, so dispatch the stored action instead. Resolve
                # and clear the card FIRST so it disappears at decision time,
                # not after a slow network push finishes.
                self.approvals.resolve(approval["approval_id"])
                await self._notify_approval_resolved(approval["approval_id"])
                if key != "y":
                    return {"declined": True,
                            "summary": "Okay, cancelled; nothing was run."}
                return await self.execute_approved_action(approval)
            press_result = await self.press_key(key)
            if isinstance(press_result, dict) and "error" in press_result:
                # The press itself failed (no session, unsupported key, ...): leave
                # the approval active so the user can retry, instead of resolving
                # it and telling them a selection happened when nothing reached
                # the live pane (the phantom-selection bug this guards against).
                return {"error": f"couldn't press {key}: {press_result['error']}. "
                                 "The prompt is still waiting; nothing was selected."}
            self.approvals.resolve(approval["approval_id"])
            # Resume a queue burst that paused on this needs_input, so the next
            # queued item dispatches once the user has decided. No-op otherwise.
            try:
                await self._queue_resume(driven_cwd)
            except Exception:
                logger.exception("queue resume after resolve failed")
            # Mirror the tap path's approval_resolved push so the phone's card
            # clears even though this decision never touched the websocket.
            await self._notify_approval_resolved(approval["approval_id"])
            return {"resolved": key}
        if name == "read_session":
            import server.transcripts as transcripts
            cwd = self._c.working_dir or ""
            kwargs = {}
            if args.get("last") is not None:
                kwargs["last"] = args["last"]
            if args.get("search"):
                kwargs["search"] = args["search"]
            return await asyncio.to_thread(
                lambda: transcripts.read_session(cwd, **kwargs))
        if name == "get_cost":
            cwd = self._c.working_dir or ""
            if not cwd:
                return {"error": "No session folder is open; open or attach one first."}
            from server import cost as cost_mod
            return await asyncio.to_thread(cost_mod.session_cost, cwd)
        if name in ("git_status", "git_diff"):
            from server import git_ops
            cwd = self._c.working_dir or ""
            if not cwd:
                return {"error": "No session folder is open; open or attach one first."}
            fn = (git_ops.git_status_summary if name == "git_status"
                  else git_ops.git_diff_summary)
            return await asyncio.to_thread(fn, cwd)
        if name == "git_commit":
            from server import git_ops
            from server.approvals import build_action_approval
            cwd = self._c.working_dir or ""
            if not cwd:
                return {"error": "No session folder is open; open or attach one first."}
            message = (args.get("message") or "").strip()
            if not message:
                return {"error": "I need a commit message; ask the user what "
                                 "the commit should say."}
            pre = await asyncio.to_thread(git_ops.commit_preflight, cwd)
            if "error" in pre:
                return pre
            project = os.path.basename(cwd.rstrip("/")) or cwd
            summary = (f"Commit {pre['changes']} change(s) in {project} on "
                       f"branch {pre['branch']}: {message}")
            action = {"kind": "git_commit", "cwd": cwd, "message": message}
            label = "Commit"
            if args.get("push"):
                up = await asyncio.to_thread(git_ops.push_preflight, cwd)
                if "error" in up:
                    return up
                summary += f", then push branch {pre['branch']} to {up['upstream']}"
                action["push"] = True
                label = "Commit and push"
            approval = build_action_approval(
                cwd, summary, tool="git_commit", action=action,
                options=[{"key": "y", "label": label},
                         {"key": "n", "label": "Cancel"}])
            return await self._offer_approval(approval)
        if name == "git_push":
            from server import git_ops
            from server.approvals import build_action_approval
            cwd = self._c.working_dir or ""
            if not cwd:
                return {"error": "No session folder is open; open or attach one first."}
            up = await asyncio.to_thread(git_ops.push_preflight, cwd)
            if "error" in up:
                return up
            project = os.path.basename(cwd.rstrip("/")) or cwd
            # Hard requirement: a push approval's summary must state the branch
            # explicitly, so the user hears and sees exactly what publishes.
            summary = f"Push branch {up['branch']} to {up['upstream']} in {project}"
            approval = build_action_approval(
                cwd, summary, tool="git_push",
                action={"kind": "git_push", "cwd": cwd},
                options=[{"key": "y", "label": "Push"},
                         {"key": "n", "label": "Cancel"}])
            return await self._offer_approval(approval)
        return {"error": f"unknown tool: {name}"}
