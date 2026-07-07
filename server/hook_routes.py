"""Claude Code hook ingestion and the fallback terminal watcher.

The /hook endpoint is the reliable, terminal-agnostic signal that a session
finished or needs input (Stop / Notification / UserPromptSubmit, installed
globally by the voxa launcher). The watcher is a screen-scraping fallback for
terminals without hooks; it stands down the moment a real hook arrives.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from fastapi import Request
from fastapi.responses import JSONResponse


def stand_down_watcher(app, sessions, notifier, hook_cwd: str = "") -> None:
    # The first real Claude Code hook event proves hooks are live: stop the screen
    # scraper, and make hooks the SOLE offline-ring source (so the driven session's
    # own monitor doesn't also ring); the two would otherwise double-report.
    notifier.hooks_live = True
    t = getattr(app.state, "bg_watcher", None)
    if t is not None and not t.done():
        t.cancel()
    app.state.bg_watcher = None
    # Ring down the SESSION THE HOOK CAME FROM (a fleet can have several); fall
    # back to active() when the hook's cwd doesn't match any registered session.
    s = sessions.find_by_cwd(hook_cwd) or sessions.active()
    if s is not None:
        s.hub.set_offline_ring(False)


def _scrape_driven_pane(sessions, cwd: str) -> str:
    """Best-effort: the driven controller's pane text, ONLY when it is actually
    sitting in the hook's cwd and exposes a capture. Fail-open (empty string) at
    every step -- v1 deliberately does not build a temp controller for other open
    terminals, so a scrape failure never blocks the hook's plain-summary report."""
    session = sessions.find_by_cwd(cwd) or sessions.active()
    ctrl = session.controller if session is not None else None
    if ctrl is None or not cwd or getattr(ctrl, "working_dir", None) != cwd:
        return ""
    cap = getattr(ctrl, "capture_text", None) or getattr(ctrl, "capture", None)
    if cap is None:
        return ""
    try:
        return cap() or ""
    except Exception:
        return ""


async def _build_approval_for_hook(sessions, notifier, *, cwd: str, msg: str,
                                    session_id: str) -> dict | None:
    """Turn a Notification hook into a structured approval the phone can render
    as buttons, scraping the driven pane in a thread (tmux subprocess calls are
    blocking). Fail-open: any error here degrades to today's plain report."""
    try:
        pane = await asyncio.to_thread(_scrape_driven_pane, sessions, cwd)
        if not pane:
            return None
        from server.approvals import build_approval
        tool = notifier.pre_tool.get(session_id, {}).get("tool_name", "")
        approval = build_approval(cwd, msg, pane, tool=tool)
    except Exception:
        return None
    if approval:
        try:
            notifier.approvals.put(approval)
        except Exception:
            pass
    return approval


def add_hook_routes(app, config, sessions, notifier) -> None:
    from server.ring_policy import RingScheduler, pane_is_busy

    # One scheduler per app, closed over by the endpoint; exposed on app.state so
    # tests can inspect/replace it. Reads VOXA_RING_QUIET_SECONDS at construction.
    scheduler = RingScheduler(notifier.report)
    app.state.ring_scheduler = scheduler

    @app.post("/hook")
    async def claude_hook(request: Request):
        if request.query_params.get("token") != config.auth_token:
            return JSONResponse({"ok": False}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return {"ok": True}
        from server.hooks import route_hook
        stand_down_watcher(app, sessions, notifier, hook_cwd=(body or {}).get("cwd", ""))
        # Default 0 = call on EVERY finish. Set VOXA_HOOK_MIN_SECONDS to a positive
        # value to suppress quick interactive turns.
        msg, kind = route_hook(
            body or {},
            turn_start=notifier.turn_start,
            hook_last=notifier.hook_last,
            now=time.monotonic(),
            min_seconds=float(os.environ.get("VOXA_HOOK_MIN_SECONDS", "0")),
            pre_tool=notifier.pre_tool,
        )
        # A bare turn boundary (UserPromptSubmit / PreToolUse) means this session
        # is still working: cancel any pending finish ring so it does not fire
        # mid-task. Do this even when route_hook stayed silent (no msg).
        event = (body or {}).get("hook_event_name") or (body or {}).get("hook_event") or ""
        hook_session = (body or {}).get("session_id", "")
        if event in ("UserPromptSubmit", "PreToolUse"):
            scheduler.note_activity(hook_session)
        if msg:
            # Remember WHICH session triggered this call so answering attaches to it
            # and continues that work (instead of opening an empty default session).
            cwd = (body or {}).get("cwd", "")
            if cwd:
                sessions.push_pending(cwd)
            if kind == "needs_input":
                approval = await _build_approval_for_hook(
                    sessions, notifier, cwd=cwd, msg=msg,
                    session_id=hook_session)
                # A human is blocking: ring now, cancelling any pending finish.
                await scheduler.needs_input(hook_session, msg, cwd, approval=approval)
            else:
                # A finish: suppress outright if the driven pane shows work still
                # running (background tasks), else gate behind the quiet window.
                pane = await asyncio.to_thread(_scrape_driven_pane, sessions, cwd)
                if pane and pane_is_busy(pane):
                    scheduler.note_activity(hook_session)   # clearly still working
                    logging.info("suppressing finish ring: driven pane is busy")
                else:
                    await scheduler.finish(hook_session, msg, cwd)
        return {"ok": True}


def add_terminal_watcher(app, config, sessions, notifier) -> None:
    # Ring the phone when ANY open Claude terminal finishes, not just the one
    # Voxa is attached to. Off by setting VOXA_WATCH_TERMINALS=0.
    if os.environ.get("VOXA_WATCH_TERMINALS", "1").strip() in ("0", "false", ""):
        return
    from server.terminal_watcher import TerminalWatcher

    call_manager = notifier.call_manager
    # For the FIRST finish after startup both sources can see the same event, so
    # the scraper yields: it waits a grace window and only rings if no hook
    # claimed the finish meanwhile.
    scraper_grace = float(os.environ.get("VOXA_SCRAPER_GRACE_SECONDS", "10"))

    async def _on_bg_done(label, cwd, summary):
        await asyncio.sleep(scraper_grace)
        if notifier.hooks_live:
            return   # a real hook reported (or will report) this finish
        msg = f"{label or 'a terminal'} finished" + (f": {summary}" if summary else "")
        await notifier.report(msg)

    async def _on_bg_resumed(label, cwd):
        # The user picked the task back up on the laptop before answering: cancel
        # the ring. NOT when the phone is (or just was) on the line; then the
        # "resume" is Voxa itself driving the terminal after the user ANSWERED.
        if call_manager.line_open or call_manager.recently_open():
            return
        if config.push_enabled:
            await call_manager.cancel(notifier.last_account or None)
            return
        await notifier.cancel_via_cloud()

    def _skip(session_info):
        # The terminal we're actively driving is reported by the main loop;
        # skip it here only while a phone line is open (to avoid double-report).
        # Checked against EVERY registered session (a fleet can drive several
        # terminals at once), not just the single default one.
        if not call_manager.line_open:
            return False
        cwd = session_info.get("cwd")
        return any(getattr(s.controller, "working_dir", None) == cwd
                   for s in sessions.all())

    watcher = TerminalWatcher(_on_bg_done, on_resumed=_on_bg_resumed, should_skip=_skip)

    @app.on_event("startup")
    async def _start_watcher():
        app.state.bg_watcher = asyncio.ensure_future(watcher.run())

    @app.on_event("shutdown")
    async def _stop_watcher():
        t = getattr(app.state, "bg_watcher", None)
        if t:
            t.cancel()
