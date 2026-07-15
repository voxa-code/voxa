# Contributing to Voxa

Voxa is early and the workflow is still being validated, so contributions that help shape it are welcome.

## Scope of this repo

This repo is the laptop server, CLI, and phone web client, the parts of Voxa you can run entirely yourself. The native iOS app and the hosted relay/billing service live in separate, private repos and aren't accepted here.

## Bugs

Open an issue with:
- What you ran (`voxa`, `npx voxa-code`, `uv tool run voxa-code`, ...) and your OS.
- What you expected vs. what happened.
- Logs or terminal output, if there's an error.

Check [SECURITY.md](SECURITY.md) first, security-relevant bugs (auth, relay trust, arbitrary command execution) go to a private channel, not a public issue.

## Feature requests / questions

Use GitHub Discussions. Issues are for confirmed bugs.

## Sending a PR

1. Fork the repo, branch off `main`.
2. Keep the change scoped, small PRs land faster than large ones.
3. Add or update a test in `tests/` for behavior changes.
4. Run the test suite locally before opening the PR:
   ```bash
   python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
   .venv/bin/pytest
   ```
5. Describe what changed and why in the PR body, not just what.

## Code style

Match the existing style in the file you're touching. No unrelated reformatting in the same PR as a functional change.

## No SLA

This is a solo-maintained alpha. Response times are best-effort.
