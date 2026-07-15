# Running Voxa from source

Voxa's laptop side is a Python server. Your phone (the Voxa app, or a phone
browser) calls into it, and the voice operator drives Claude Code sessions in
your terminals. If you just want to *use* Voxa, prefer the published package
(`npx voxa-code` / `uvx voxa-code` or the installers at https://voxa.space);
this guide is for running a git checkout.

## One-time setup

```bash
git clone https://github.com/voxa-code/voxa.git && cd voxa
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"     # installs the server + pyobjc (macOS) deps
```

## Start it (recommended: zero-config, hosted voice)

```bash
.venv/bin/python -m server.cli
```

This is the `voxa` launcher. It:
1. starts the FastAPI server on `127.0.0.1:8787` (override with `VOXA_PORT`),
2. opens a Cloudflare quick tunnel (`cloudflared`) to get a public HTTPS URL
   (the phone needs HTTPS for microphone access),
3. prints a QR code + pairing URL right in the terminal,
4. cleans both up on Ctrl-C.

Scan the QR with the Voxa app to pair. Voice minutes run through the hosted
cloud (the metered path); no API keys touch the laptop. Terminal control runs
locally.

Leave this terminal running for the whole session. Ctrl-C to stop.

## Alternative: fully self-hosted (your own Gemini key, over Tailscale)

Use this if you want to run Gemini with your own key instead of the hosted
proxy. Requires Tailscale installed and logged in on BOTH the laptop and the
phone, and a `.env` (copy `.env.example`) with `GEMINI_API_KEY` and
`VOXA_AUTH_TOKEN` set.

```bash
bash scripts/serve.sh
```

It starts the server and runs `tailscale serve` to expose it over HTTPS on
your tailnet, then prints a QR for the tailnet URL.

## Using the any-terminal feature

Once the server is running and the phone is paired:

- Open a `claude` session in any terminal (iTerm2, Terminal.app, Ghostty, Warp,
  VS Code, tmux, ...).
- On the phone, say something like "use my open terminal" or "attach to the one
  in <folder>". Voxa lists the sessions it found and attaches to your choice.
- The first time you attach to a non-scriptable terminal (Ghostty, Warp, VS
  Code, etc.), Voxa asks for macOS Accessibility permission and opens System
  Settings. Grant it to the app you launched the server from (the terminal
  running the `voxa` command), then attach again. iTerm2, Terminal.app, and
  tmux do not need this; they use scripting.

## Run the tests

```bash
.venv/bin/python -m pytest -q
```

## Troubleshooting

- Two terminals with the same folder name: attaching by folder matches the
  first one, so a task may land in the other window with that name.
- Port already in use: set a different port, e.g.
  `VOXA_PORT=8788 .venv/bin/python -m server.cli`.
- No call when a task finishes: make sure the phone is paired and the app has
  notification permission; the laptop rings through the hosted relay unless you
  configured your own APNs key.
