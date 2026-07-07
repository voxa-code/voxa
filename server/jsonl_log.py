"""Size-rotated JSONL append log, shared by the metrics/events/sessions
capture points feeding the admin dashboard. Bounded disk use: when the
active file exceeds max_bytes it is rotated to `<path>.1` (a single
previous generation is kept; older rotations are discarded).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path


class RotatingJsonlLog:
    def __init__(self, path: str, max_bytes: int = 5_000_000):
        self._path = Path(path)
        self._max_bytes = max_bytes
        self._lock = threading.Lock()

    def append(self, record: dict) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record) + "\n"
            if self._path.exists() and self._path.stat().st_size + len(line) >= self._max_bytes:
                self._path.replace(self._rotated_path())
            with self._path.open("a") as f:
                f.write(line)

    def _rotated_path(self) -> Path:
        return self._path.with_suffix(self._path.suffix + ".1")

    def read_all(self) -> list[dict]:
        with self._lock:
            out: list[dict] = []
            for p in (self._rotated_path(), self._path):
                if not p.exists():
                    continue
                for line in p.read_text().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            return out

    def read_recent(self, limit: int) -> list[dict]:
        return self.read_all()[-limit:]
