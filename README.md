<div align="center">

<img src="assets/voxa-icon.svg" width="110" alt="Voxa" />

<h1>Voxa</h1>

<p><strong>Talk to <a href="https://claude.com/claude-code">Claude Code</a> from anywhere.<br/>When it finishes, your phone rings.</strong></p>

[![Website](https://img.shields.io/badge/voxa.space-black?style=flat-square&logo=safari&logoColor=white)](https://voxa.space)
[![App Store](https://img.shields.io/badge/iPhone-App_Store-0D96F6?style=flat-square&logo=apple&logoColor=white)](https://voxa.space)
[![npm](https://img.shields.io/npm/v/voxa-code?style=flat-square&label=npm&color=CB3837)](https://www.npmjs.com/package/voxa-code)
[![PyPI](https://img.shields.io/pypi/v/voxa-code?style=flat-square&label=PyPI&color=3775A9)](https://pypi.org/project/voxa-code/)
[![License](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](./LICENSE)

</div>

---

<img src="assets/hero.jpg" width="100%" alt="Voxa on iPhone: your laptop calls you back, a live voice session with Claude, and the agent's terminal streamed to your phone" />

---

## Install

Pick your way, same as [voxa.space/setup](https://voxa.space/setup):

**macOS / Linux**

```bash
curl -fsSL https://voxa.space/install.sh | sh
```

**Windows (PowerShell)**

```powershell
irm https://voxa.space/install.ps1 | iex
```

**npm**

```bash
npm install -g voxa-code
```

**Python**

```bash
uv tool install voxa-code
```

Then run `voxa` on your laptop, scan the QR code with the **[Voxa iOS app](https://voxa.space)** (any phone browser works too), and start talking. Zero config, no API keys: voice runs through the hosted relay, terminal control never leaves your laptop.

**Requires:** [Claude Code](https://claude.com/claude-code) installed and logged in.

## How it works

- **Speak, don't type.** Pick a project, give instructions, hear results read back, fully hands-free.
- **Attach any terminal.** iTerm2, Terminal.app, tmux, Ghostty, Warp, VS Code: say "use my open terminal."
- **It calls you back.** Leave a task running and pocket your phone. When Claude finishes (or needs input), you get a real incoming call. Answer to land in the live session.

```
phone ──── voice ────► laptop ────► Claude Code
  ▲                                     │
  └───────── rings when done ◄──────────┘
```

## From source

```bash
git clone https://github.com/Ti-03/voxa.git && cd voxa
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m server.cli
```

Full guide (including fully self-hosted mode, your own keys, no relay): [docs/RUNNING.md](docs/RUNNING.md)

## FAQ

**Is it open source?**

Everything in this repo is [MIT](LICENSE): the laptop server and the phone web client, everything you need to self-host with your own API key, no account required ([guide](docs/RUNNING.md)). The native iOS app and the hosted relay/push/billing service behind the zero-config install live in separate, proprietary repos, they power the hosted experience but aren't required to run Voxa yourself.

**Is it free?**

Self-hosting is free forever: run the server with your own API key, no relay needed ([guide](docs/RUNNING.md)). The hosted zero-config relay is free to get started, with paid plans for more agent minutes ([pricing](https://voxa.space/pricing)).

**What platforms does it support?**

The laptop side runs on macOS, Linux, and Windows. The phone side is the Voxa iPhone app, with any mobile browser as a fallback. Attaching to already-open GUI terminals (iTerm2, Terminal.app, Ghostty, Warp, VS Code) is macOS only; tmux attach works everywhere.

## Security

Security model and vulnerability reporting: [SECURITY.md](SECURITY.md)

---

<div align="center">
Built with ❤️ by <a href="https://ti0.me/">Ti</a> &nbsp;·&nbsp; <a href="https://voxa.space">voxa.space</a> &nbsp;·&nbsp; <a href="LICENSE">MIT</a>
</div>
