"""Persist the minimal descriptor of the driven session (its cwd) so a restarted
server can seed pending_source and re-attach through the normal answer path. The
live Session object cannot survive a restart, but the terminal it was driving
usually does; conversation context comes back via transcripts.recap().
"""
from __future__ import annotations

import json
import os


def _default_path() -> str:
    # Live under ~/.voxa like the other persistent state (relay_code, .env), so
    # restart re-attach works no matter which directory voxa is launched from.
    return os.path.expanduser("~/.voxa/last_session.json")


class SessionStateFile:
    def __init__(self, path: str | None = None):
        self._path = path or os.environ.get("VOXA_SESSION_STATE_FILE") or _default_path()

    def save(self, cwd: str) -> None:
        # Fail-open: persistence must never break a live session.
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"cwd": cwd}, f)
            os.replace(tmp, self._path)
        except OSError:
            pass

    def load(self) -> dict | None:
        try:
            with open(self._path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        if isinstance(data, dict) and data.get("cwd"):
            return {"cwd": data["cwd"]}
        return None

    def clear(self) -> None:
        try:
            os.remove(self._path)
        except OSError:
            pass
