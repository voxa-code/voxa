"""`voxa` launcher: start the server + a public tunnel, then print a scannable QR.

Run it as `python -m server.cli` (or, once installed, just `voxa`). It:
  1. starts the FastAPI server (uvicorn),
  2. opens a Cloudflare quick tunnel and captures its public URL,
  3. prints a scannable QR + the pairing URL right in the terminal,
  4. cleans both up on Ctrl-C.
"""

from __future__ import annotations

import atexit
import os
import re
import signal
import subprocess
import sys
import threading

_CF_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

# The hosted Voxa Cloud (relay + metered Gemini + push). Non-secret defaults so
# `voxa` / `npx voxa-code` runs with ZERO configuration: no key, no token, no .env.
# The user's keys never touch the laptop; auth is by pairing with the phone app.
VOXA_DEFAULT_RELAY = "https://api.voxa.space"
VOXA_DEFAULT_LIVE_PROXY = "wss://api.voxa.space/live"


def _voxa_dir() -> str:
    d = os.path.expanduser("~/.voxa")
    os.makedirs(d, exist_ok=True)
    return d


def _stable_secret(name: str, nbytes: int = 16) -> str:
    """A value that's STABLE across `voxa` runs (persisted in ~/.voxa/<name>).

    The pairing code and auth token must not change between launches, otherwise a
    phone that paired once would be orphaned every time the laptop restarts."""
    path = os.path.join(_voxa_dir(), name)
    try:
        with open(path) as f:
            val = f.read().strip()
        if val:
            return val
    except OSError:
        pass
    import secrets
    val = secrets.token_hex(nbytes)
    try:
        with open(path, "w") as f:
            f.write(val)
        os.chmod(path, 0o600)
    except OSError:
        pass
    return val


def _apply_zero_config_defaults() -> None:
    """Fill env so the laptop client needs nothing typed. Metered mode means the
    cloud holds the Gemini key; a random per-machine auth token gates only the
    laptop's own loopback /ws (the phone reaches us through the relay by code).

    The auth token and relay code are PERSISTED so a previously-paired phone keeps
    working across laptop restarts (the saved QR stays valid)."""
    env = os.environ
    # Prefer a DIRECT Tailscale link (lowest latency: no relay hop) when it's
    # available and the user hasn't pinned their own relay. Only fall back to the
    # hosted relay default when Tailscale isn't an option (or VOXA_FORCE_RELAY is set).
    # Note: switching transports changes the QR, so a phone paired on the relay must
    # re-scan the Tailscale QR (and vice-versa).
    force_relay = env.get("VOXA_FORCE_RELAY", "").strip().lower() not in ("", "0", "false", "no")
    user_set_relay = bool(env.get("VOXA_RELAY_URL", "").strip())
    if user_set_relay or force_relay or not _tailscale_available():
        env.setdefault("VOXA_RELAY_URL", VOXA_DEFAULT_RELAY)
    # else: leave VOXA_RELAY_URL unset so main() takes the Tailscale direct path.
    env.setdefault("VOXA_LIVE_PROXY", VOXA_DEFAULT_LIVE_PROXY)
    if not env.get("VOXA_AUTH_TOKEN", "").strip():
        env["VOXA_AUTH_TOKEN"] = _stable_secret("auth_token")
    if not env.get("VOXA_RELAY_CODE", "").strip():
        # 16 bytes (128-bit) so the pairing code, which bridges a phone to this
        # laptop, is not brute-forceable. (Persisted, so already-paired phones keep
        # their existing code; only fresh installs get the longer one.)
        env["VOXA_RELAY_CODE"] = _stable_secret("relay_code", nbytes=16)


def _print_qr(url: str) -> None:
    import qrcode
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


def _banner(pairing_url: str) -> None:
    line = "─" * 56
    print(f"\n{line}")
    print("  Voxa is live. Scan this with the Voxa app (or your phone camera):\n")
    _print_qr(pairing_url)
    print(f"\n  {pairing_url}")
    print("\n  Phone browser works too, just open that URL.")
    print(f"  Press Ctrl-C to stop.\n{line}\n", flush=True)


def main() -> int:
    from dotenv import load_dotenv
    from server.config import load_config
    from server.observability import init_sentry

    load_dotenv()  # a .env in the current directory
    home_env = os.path.expanduser("~/.voxa/.env")
    if os.path.exists(home_env):
        load_dotenv(home_env)  # optional overrides for self-hosters
    init_sentry("voxa-laptop")  # opt-in: only fires when SENTRY_DSN is set
    # Zero-config: hosted relay + metered cloud Gemini + a random local token.
    _apply_zero_config_defaults()
    try:
        cfg = load_config()
    except ValueError as e:
        print(
            f"\nVoxa needs configuration: {e}\n"
            "To point at your own server instead, set VOXA_RELAY_URL / VOXA_LIVE_PROXY\n"
            "in ~/.voxa/.env. The default talks to the hosted Voxa Cloud.\n",
            file=sys.stderr,
        )
        return 1
    port = cfg.port
    relay_url = os.environ.get("VOXA_RELAY_URL", "").strip()

    if not relay_url and not _which("cloudflared"):
        print(
            "cloudflared is not installed. Install it with:\n"
            "  brew install cloudflared\n"
            "(or set VOXA_RELAY_URL to your hosted relay).",
            file=sys.stderr,
        )
        return 1

    if _port_in_use(port):
        print(
            f"\nPort {port} is already in use, another Voxa may be running.\n"
            f"Stop it first (e.g. `pkill -f 'server.cli'`) or set VOXA_PORT to a free port.\n",
            file=sys.stderr,
        )
        return 1

    env = {**os.environ, "VOXA_MODE": os.environ.get("VOXA_MODE", "attach")}
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server.app:create_app",
         "--factory", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
    )

    active = {"tunnel": None}

    def cleanup(*_):
        for p in (active["tunnel"], server):
            if p:
                try:
                    p.terminate()
                except Exception:
                    pass

    atexit.register(cleanup)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *a: (cleanup(), sys.exit(0)))

    if not _wait_healthy(port):
        print("server did not come up; not showing a QR.", file=sys.stderr)
        cleanup()
        return 1

    # Install the GLOBAL Claude Code hook so EVERY Claude session on this machine
    # rings the phone when it finishes or needs input (reliable + terminal-agnostic,
    # unlike screen-scraping). Idempotent; set VOXA_INSTALL_HOOK=0 to skip.
    if os.environ.get("VOXA_INSTALL_HOOK", "1").strip() not in ("0", "false", ""):
        try:
            from server.hooks import install_claude_hook, default_settings_path, hook_url
            install_claude_hook(default_settings_path(),
                                hook_url("127.0.0.1", port, cfg.auth_token))
            print("  ✓ Claude hook installed, calls you when any Claude session finishes.")
        except Exception as e:
            print(f"  (could not install Claude hook: {e})", file=sys.stderr)

    # Self-hosted relay mode: dial OUT to your cloud relay (no tunnel/Tailscale).
    if relay_url:
        import asyncio
        import uuid
        from server.relay_client import run_bridge
        https = relay_url.rstrip("/")
        host = https.split("://", 1)[-1]
        ws = ("wss://" if https.startswith("https") else "ws://") + host
        # Stable across restarts (persisted in ~/.voxa/relay_code) so a paired phone
        # keeps working; falls back to a random code only if persistence failed.
        code = os.environ.get("VOXA_RELAY_CODE", "").strip() or uuid.uuid4().hex[:10]
        local_ws = f"ws://127.0.0.1:{port}/ws?token={cfg.auth_token}"
        _banner(f"{https}/?code={code}&token={cfg.auth_token}")
        try:
            asyncio.run(run_bridge(ws, code, local_ws,
                                   os.environ.get("VOXA_RELAY_TOKEN", "").strip()))
        except KeyboardInterrupt:
            pass
        finally:
            cleanup()
        return 0

    # Prefer Tailscale (stable, private, permanent URL, no flaky quick tunnels).
    public_url = _tailscale_url(port)
    if public_url:
        print("Using your Tailscale network (stable, private link).", flush=True)
        atexit.register(lambda: subprocess.run(
            ["tailscale", "serve", "--https=443", "off"], capture_output=True))
        if not _wait_public_healthy(public_url, tries=20):
            print("Tailscale URL not reachable; falling back to a tunnel…", file=sys.stderr)
            public_url = None

    # Otherwise a cloudflare quick tunnel. These are per-instance flaky, so retry
    # until one is actually reachable (and re-run if all fail).
    if not public_url:
        print("Opening secure tunnel…", flush=True)
    for attempt in range(1, 4) if not public_url else []:
        tunnel = subprocess.Popen(
            # 127.0.0.1 (not localhost): localhost may resolve to IPv6 ::1 while
            # uvicorn binds IPv4, which makes the tunnel 502.
            ["cloudflared", "tunnel", "--url", f"http://127.0.0.1:{port}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        active["tunnel"] = tunnel
        url = _read_tunnel_url(tunnel, timeout=20)
        if url:
            threading.Thread(
                target=lambda t=tunnel: [None for _ in t.stdout], daemon=True
            ).start()
            if _wait_public_healthy(url, tries=40):
                public_url = url
                break
        try:
            tunnel.terminate()
        except Exception:
            pass
        if attempt < 3:
            print(f"  tunnel attempt {attempt} didn't connect, retrying…", flush=True)

    if not public_url:
        print(
            "\nCouldn't get a working tunnel after 3 tries (cloudflare quick tunnels\n"
            "can be flaky). Re-run `voxa`, or set up Tailscale for a permanent,\n"
            "reliable link.\n",
            file=sys.stderr,
        )
        cleanup()
        return 1

    pairing_url = f"{public_url}/?token={cfg.auth_token}"
    _banner(pairing_url)

    try:
        server.wait()
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()
    return 0


def _which(name: str) -> bool:
    from shutil import which
    return which(name) is not None


def _tailscale_available() -> bool:
    """True if Tailscale is installed and logged in (has a tailnet DNS name). A cheap
    status-only check (no `serve`), used to decide whether to skip the relay default."""
    if not _which("tailscale"):
        return False
    import json
    try:
        st = subprocess.run(["tailscale", "status", "--json"],
                            capture_output=True, text=True, timeout=5)
        if st.returncode != 0:
            return False
        dns = (json.loads(st.stdout).get("Self") or {}).get("DNSName", "").rstrip(".")
        return bool(dns)
    except Exception:
        return False


def _tailscale_url(port: int):
    """If Tailscale is installed and logged in, serve the port over HTTPS on the
    tailnet and return its stable https URL. Returns None to fall back to a tunnel."""
    if not _which("tailscale"):
        return None
    import json
    try:
        st = subprocess.run(["tailscale", "status", "--json"],
                            capture_output=True, text=True, timeout=5)
        if st.returncode != 0:
            return None
        dns = (json.loads(st.stdout).get("Self") or {}).get("DNSName", "").rstrip(".")
        if not dns:
            return None
        r = subprocess.run(
            ["tailscale", "serve", "--bg", "--https=443", f"http://127.0.0.1:{port}"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return None
        return f"https://{dns}"
    except Exception:
        return None


def _read_tunnel_url(proc, timeout: int = 20):
    """Read cloudflared output (in a thread) until its public URL appears, or give up."""
    result = {"url": None}

    def reader():
        for line in proc.stdout:
            m = _CF_URL.search(line)
            if m:
                result["url"] = m.group(0)
                return

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    t.join(timeout)
    return result["url"]


def _port_in_use(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _wait_healthy(port: int, tries: int = 25) -> bool:
    import time
    import urllib.request
    for _ in range(tries):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def _wait_public_healthy(public_url: str, tries: int = 70) -> bool:
    """Poll the PUBLIC tunnel URL until it actually responds (the tunnel can take
    several seconds to connect to Cloudflare's edge after printing its URL)."""
    import time
    import urllib.request
    for _ in range(tries):
        try:
            with urllib.request.urlopen(f"{public_url}/healthz", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


if __name__ == "__main__":
    raise SystemExit(main())
