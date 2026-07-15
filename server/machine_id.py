# server/machine_id.py
from __future__ import annotations

import os
import socket
import uuid

_DEFAULT_PATH = os.path.expanduser("~/.voxa/machine-id")


def machine_id(path: str | None = None) -> str:
    """A stable per-install id, generated once and persisted. Falls back to an
    ephemeral id (not written) if the path is unwritable."""
    path = path or _DEFAULT_PATH
    try:
        if os.path.exists(path):
            existing = open(path).read().strip()
            if existing:
                return existing
    except OSError:
        pass
    new_id = uuid.uuid4().hex
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(new_id)
    except OSError:
        pass
    return new_id


def machine_name() -> str:
    return os.environ.get("VOXA_MACHINE_NAME", "").strip() or socket.gethostname()
