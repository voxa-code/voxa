"""Laptop-side relay bridge.

Dials OUT to your cloud relay's /agent socket and pipes it to the laptop's own
local /ws, so the phone reaches the laptop through your relay with no inbound
port, tunnel, or Tailscale. The local server (server.app) runs unchanged.

The laptop only opens the local /ws (and thus a paid V2V session) when the relay
signals a phone is actually connected (`__peer up`), and tears it down on `down`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

import websockets

logger = logging.getLogger(__name__)


def _with_params(url: str, **params: str) -> str:
    """Append only the non-empty query params (account/voice/lang) to the local
    /ws URL, matching the phone's own direct-connect URL shape."""
    for key, val in params.items():
        if val:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{key}={val}"
    return url


async def run_bridge(relay_ws_url: str, code: str, local_ws_url: str,
                     relay_token: str = "") -> None:
    agent_url = f"{relay_ws_url}/agent?code={code}"
    if relay_token:
        agent_url += f"&token={relay_token}"
    # Backoff + deduped logging: a network outage otherwise logs "relay bridge
    # dropped" every 2s forever. Log the FIRST drop (and each time the error
    # changes), stay quiet on identical repeats, and back off 2s -> 30s so we
    # don't spin. Reconnecting resets both.
    backoff = 2.0
    last_err: str | None = None
    while True:
        try:
            async with websockets.connect(agent_url, max_size=None, ping_interval=20) as agent:
                if last_err is not None:
                    logger.info("relay bridge reconnected (code=%s)", code)
                else:
                    logger.info("relay bridge connected (code=%s)", code)
                backoff, last_err = 2.0, None
                await _serve(agent, local_ws_url)
        except Exception as e:
            msg = str(e)
            if msg != last_err:
                logger.warning("relay bridge dropped: %s (retrying, quiet until it changes)", msg)
                last_err = msg
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
            continue
        await asyncio.sleep(2)  # clean close (peer down): reconnect promptly


async def _serve(agent, local_ws_url: str) -> None:
    local = None
    pump: asyncio.Task | None = None

    async def open_local(account: str = "", voice: str = "", lang: str = ""):
        nonlocal local, pump
        if local is not None:
            return
        # Pass the phone's account (metering), voice, and language through to the
        # laptop's local /ws so they reach the metered /live session.
        url = _with_params(local_ws_url, account=account, voice=voice, lang=lang)
        local = await websockets.connect(url, max_size=None, ping_interval=20)
        pump = asyncio.create_task(_pump(local, agent))

    async def close_local():
        nonlocal local, pump
        if pump:
            pump.cancel()
            pump = None
        if local:
            with contextlib.suppress(Exception):
                await local.close()
            local = None

    try:
        async for msg in agent:                 # phone -> laptop
            if isinstance(msg, str) and '"__peer"' in msg:
                try:
                    data = json.loads(msg)
                except ValueError:
                    data = {}
                if data.get("type") == "__peer":
                    if data.get("state") == "up":
                        await open_local(data.get("account", ""), data.get("voice", ""),
                                         data.get("lang", ""))
                    elif data.get("state") == "down":
                        await close_local()
                    continue
            if local is not None:
                await local.send(msg)
    finally:
        await close_local()


async def _pump(local, agent) -> None:
    """Laptop -> phone: forward everything from the local /ws to the relay agent."""
    with contextlib.suppress(Exception):
        async for msg in local:
            await agent.send(msg)
