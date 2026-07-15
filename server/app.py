from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse

from dotenv import load_dotenv

from server.config import Config, load_config

STATIC = Path(__file__).resolve().parent.parent / "static"

# Re-exported for callers and tests that import these from server.app.
from server.greetings import (  # noqa: F401,E402
    _strip_finished_prefix,
    apply_greeting_suppression,
    compose_opening,
    should_suppress_greeting,
    suppress_greeting_if_supported,
)
# handle_client_control is a re-export for tests that import it from server.app.
from server.ws_session import handle_client_control, serve_ws  # noqa: F401,E402


def _default_operator_factory(config, handle_tool_call, voice="", account="", lang=""):
    # Metered mode: route V2V through the cloud /live proxy (your key + minute
    # metering live there). Direct mode: talk to Gemini locally with your own key.
    proxy = os.environ.get("VOXA_LIVE_PROXY", "").strip()
    if proxy:
        from server.remote_operator import RemoteOperator
        # Account precedence: the paired phone's id (per-connection) wins, so each
        # phone meters its own balance; fall back to env/auth_token for solo runs.
        acct = account or os.environ.get("VOXA_ACCOUNT", "") or config.auth_token
        return RemoteOperator(
            config, handle_tool_call, proxy_url=proxy, account=acct,
            token=os.environ.get("VOXA_PROXY_TOKEN", ""), voice=voice, lang=lang)
    from server.gemini_operator import GeminiOperator
    return GeminiOperator(config, handle_tool_call, voice=voice, lang=lang)


def create_app(config: Config | None = None, operator_factory=None) -> FastAPI:
    if config is None:
        load_dotenv()
        config = load_config()
    operator_factory = operator_factory or _default_operator_factory
    # "attach" = visible interactive claude in a tmux/Terminal you can also type in;
    # "drive" = headless SDK session with a read-only watch log.
    mode = os.environ.get("VOXA_MODE", "attach").strip().lower()
    app = FastAPI()

    from server.device_registry import DeviceRegistry
    from server.call_manager import CallManager
    registry = DeviceRegistry(os.environ.get("VOXA_DEVICES_FILE", "devices.json"))
    from server.machine_registry import MachineRegistry
    machines = MachineRegistry(
        os.environ.get("VOXA_MACHINE_REGISTRY_PATH", "machines.json"),
        ttl_days=int(os.environ.get("MACHINE_ROSTER_TTL_DAYS", "30")))
    app.state.machines = machines
    if config.push_enabled:
        from server.apns import ApnsClient
        pusher = ApnsClient(config)
    else:
        class _NoPush:
            async def send_voip(self, *a, **k):
                logging.warning("push disabled; dropping call %r", a)
                return False
        pusher = _NoPush()
    call_manager = CallManager(pusher, registry)
    app.state.registry = registry
    app.state.call_manager = call_manager

    from server.notifier import Notifier
    from server.session import SessionRegistry
    sessions = SessionRegistry()
    notifier = Notifier(call_manager, push_enabled=config.push_enabled)
    app.state.sessions = sessions
    app.state.notifier = notifier

    from server.prewarm import Prewarmer
    # Warms the Gemini Live session (and speaks the greeting) while the phone
    # is still ringing; report() kicks it, serve_ws's claim() adopts it on
    # answer. Purely an optimization: disabled automatically in proxy mode and
    # fail-open on any error (see server/prewarm.py's module docstring).
    prewarmer = Prewarmer(config, operator_factory, notifier, sessions)
    notifier.prewarmer = prewarmer
    app.state.prewarmer = prewarmer

    from server.session_state import SessionStateFile
    session_state = SessionStateFile()
    app.state.session_state = session_state
    saved = session_state.load()
    if saved and saved.get("cwd"):
        # A previous run was driving this folder; the first connection's auto-attach
        # re-attaches to it (or fails gracefully if that terminal is gone).
        sessions.push_pending(saved["cwd"])

    from server.hook_routes import add_hook_routes, add_terminal_watcher
    add_hook_routes(app, config, sessions, notifier)
    add_terminal_watcher(app, config, sessions, notifier)

    def _check(request: Request):
        return request.query_params.get("token") == config.auth_token

    from server.push_routes import add_push_routes
    add_push_routes(app, registry, call_manager, _check, machines=machines)
    from server.machine_routes import add_machine_routes
    add_machine_routes(app, machines, _check)

    @app.on_event("startup")
    async def _machine_heartbeat():
        import asyncio
        interval = float(os.environ.get("VOXA_MACHINE_HEARTBEAT_SECONDS", "60"))

        async def _loop():
            while True:
                await asyncio.sleep(interval)
                n = getattr(app.state, "notifier", None)
                if n is not None:
                    try:
                        await n.register_machine_cloud()
                    except Exception:
                        logging.exception("machine heartbeat failed")

        app.state.machine_heartbeat = asyncio.ensure_future(_loop())

    @app.on_event("shutdown")
    async def _stop_machine_heartbeat():
        t = getattr(app.state, "machine_heartbeat", None)
        if t is not None:
            t.cancel()

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return (STATIC / "index.html").read_text()

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        if websocket.query_params.get("token") != config.auth_token:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        logging.getLogger("voxa").info("ws: phone connected")
        # Count live connections so background/hook events know the app is OPEN
        # (don't call, surface on the line) vs CLOSED (place a call).
        notifier.note_client_connected()
        try:
            await serve_ws(websocket, config=config, mode=mode, sessions=sessions,
                           notifier=notifier, operator_factory=operator_factory,
                           session_state=session_state, prewarmer=prewarmer)
        finally:
            notifier.note_client_disconnected()

    # mount static assets (js/worklet) under /static
    from fastapi.staticfiles import StaticFiles
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
    return app
