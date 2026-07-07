"""Disk-backed per-project queue of voice-dictated follow-up instructions.

While Claude works on one task the operator can dictate more ("after this, bump
the deps and run tests"); those enqueue here instead of dispatching, so the
QueueRunner (Task 2) can run them sequentially and fold their completions into
ONE digest call. Persistence mirrors session_state.py / notify_rules.py: a
nested dict keyed by the driven cwd, atomic tmp+os.replace writes, fail-open (a
write error is swallowed so persistence never breaks a live session), and
corrupt/missing files degrade to an empty store rather than raising.

Unlike notify_rules, the in-memory store is authoritative and is loaded ONCE at
construction; reads do not re-read the file. A running queue is a mutable,
ordered structure whose "running" marker is set by pop_next and persisted, so
re-reading from disk on every call would race the live state. Loading once also
gives the restart contract its natural home: a "running" item on disk has no
process driving it after a restart, so on load it is demoted back to "queued"
(a restart survivor is PENDING and is never auto-run: Phase 4 boots announce
"you have N queued tasks", it does not resume execution).
"""
from __future__ import annotations

import json
import os
import time
import uuid

_OUTCOMES = ("done", "needs_input", "failed")
_MAX_HISTORY = 20


def _default_path() -> str:
    # Live under ~/.voxa like the other persistent state (relay_code, .env), so
    # the queue survives no matter which directory voxa is launched from.
    return os.path.expanduser("~/.voxa/task_queue.json")


def _normalize(cwd: str) -> str:
    # Match the repo convention (approvals.py, notify_rules.py): a trailing slash
    # must not fork a project into two queues.
    return (cwd or "").rstrip("/")


class TaskQueue:
    def __init__(self, path: str | None = None):
        self._path = path or os.environ.get("VOXA_TASK_QUEUE_FILE") or _default_path()
        self._store = self._load()
        # Restart survivors are PENDING, not resumed: nothing is driving a
        # "running" item once the server has gone away, so treat it as queued.
        for section in self._store.values():
            for item in section.get("items", []):
                if item.get("state") == "running":
                    item["state"] = "queued"

    def add(self, cwd: str, text: str) -> dict:
        item = {
            "id": uuid.uuid4().hex[:12],
            "text": text,
            "state": "queued",
            "created_at": time.time(),
        }
        self._section(cwd)["items"].append(item)
        self._persist()
        return dict(item)

    def items(self, cwd: str) -> list[dict]:
        # queued + running, insertion order (finished items have moved to history).
        return [dict(i) for i in self._section(cwd)["items"]]

    def pop_next(self, cwd: str) -> dict | None:
        # The oldest queued item becomes the running one; None if nothing queued.
        for item in self._section(cwd)["items"]:
            if item["state"] == "queued":
                item["state"] = "running"
                self._persist()
                return dict(item)
        return None

    def finish(self, id: str, outcome: str, summary: str = "") -> None:
        # Record the completed item as an outcome for later digest composition and
        # drop it from the live queue. Validate BEFORE mutating so a bad outcome
        # leaves the item untouched.
        if outcome not in _OUTCOMES:
            raise ValueError(f"invalid outcome: {outcome!r}")
        for section in self._store.values():
            for i, item in enumerate(section["items"]):
                if item["id"] == id:
                    del section["items"][i]
                    section["history"].append({
                        "id": item["id"],
                        "text": item["text"],
                        "outcome": outcome,
                        "summary": summary,
                        "finished_at": time.time(),
                    })
                    # Bound the per-cwd history so a long-lived queue file cannot
                    # grow without limit; the digest only needs recent outcomes.
                    del section["history"][:-_MAX_HISTORY]
                    self._persist()
                    return

    def remove(self, id: str) -> bool:
        # Queued items only: a running item is in flight and a finished one has
        # already left the queue, so neither can be removed here.
        for section in self._store.values():
            for i, item in enumerate(section["items"]):
                if item["id"] == id:
                    if item["state"] != "queued":
                        return False
                    del section["items"][i]
                    self._persist()
                    return True
        return False

    def move(self, id: str, index: int) -> bool:
        # Reorder within the queued items only; running items keep their place at
        # the front. Index clamps into the queued range.
        for section in self._store.values():
            target = next((i for i in section["items"] if i["id"] == id), None)
            if target is None:
                continue
            if target["state"] != "queued":
                return False
            running = [i for i in section["items"] if i["state"] == "running"]
            queued = [i for i in section["items"] if i["state"] == "queued"]
            queued.remove(target)
            index = max(0, min(index, len(queued)))
            queued.insert(index, target)
            section["items"] = running + queued
            self._persist()
            return True
        return False

    def flush(self, cwd: str) -> int:
        # Drop every queued + running item for the cwd; outcomes/history remain
        # so a pending digest is not lost. Returns how many were dropped.
        section = self._section(cwd)
        dropped = len(section["items"])
        section["items"] = []
        self._persist()
        return dropped

    def drain_outcomes(self, cwd: str) -> list[dict]:
        # Return AND clear the finished-outcome records for digest composition;
        # the live queue is untouched.
        section = self._section(cwd)
        outcomes = section["history"]
        section["history"] = []
        self._persist()
        return outcomes

    def pending_counts(self) -> dict:
        # queued + running counts per cwd for the restart announcement; zero-count
        # cwds are omitted.
        return {
            cwd: len(section["items"])
            for cwd, section in self._store.items()
            if section["items"]
        }

    def _section(self, cwd: str) -> dict:
        return self._store.setdefault(_normalize(cwd), {"items": [], "history": []})

    def _load(self) -> dict:
        try:
            with open(self._path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        # Coerce each section into the expected shape; a malformed section on disk
        # must not crash a live read.
        store: dict = {}
        for cwd, section in data.items():
            if not isinstance(section, dict):
                continue
            items = section.get("items")
            history = section.get("history")
            store[cwd] = {
                "items": items if isinstance(items, list) else [],
                "history": history if isinstance(history, list) else [],
            }
        return store

    def _persist(self) -> None:
        # Fail-open: persistence must never break a live session.
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._store, f)
            os.replace(tmp, self._path)
        except OSError:
            pass
