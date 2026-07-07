#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1
# Load .env so VOXA_AUTH_TOKEN / VOXA_PORT are available to this script.
if [ -f .env ]; then set -a; . ./.env; set +a; fi
# Start the Voxa server, then expose it over your tailnet with HTTPS.
# Requires: tailscale installed and logged in on this laptop AND the phone.
PORT="${VOXA_PORT:-8787}"
.venv/bin/uvicorn server.app:create_app --factory --host 127.0.0.1 --port "$PORT" &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true; tailscale serve --https=443 off || true' EXIT
# Serve HTTPS on the tailnet -> local server (gives the phone a secure context for mic access)
tailscale serve --bg --https=443 "http://127.0.0.1:${PORT}"
DNS=$(tailscale status --json | .venv/bin/python -c 'import sys,json;print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
URL="https://$DNS/?token=$VOXA_AUTH_TOKEN"
echo "Voxa is live. Scan this in the Voxa app, or open on your phone:"
.venv/bin/python scripts/print_qr.py "$URL"
wait $SERVER_PID
