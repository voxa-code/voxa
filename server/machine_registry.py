# server/machine_registry.py
from __future__ import annotations

import json
import os
import time


class MachineRegistry:
    """Per-account roster of the user's Macs, keyed by a stable machine id.

    On-disk: {account: {machine_id: {"name", "last_seen", "can_ring"}}}.
    can_ring defaults True (fleet-wide default). Machines whose last_seen is
    older than ttl_days are pruned on read/write. `online` is never stored; it
    is derived at list() time from `now - last_seen < online_window`.
    """

    def __init__(self, path: str, ttl_days: int = 30,
                 online_window: float = 120.0, now_fn=time.time):
        self._path = path
        self._ttl = ttl_days * 86400
        self._online_window = online_window
        self._now = now_fn
        self._by_account: dict[str, dict[str, dict]] = {}
        if os.path.exists(path):
            try:
                data = json.load(open(path))
                if isinstance(data, dict):
                    self._by_account = {
                        a: {m: dict(v) for m, v in machines.items()}
                        for a, machines in data.items()
                        if isinstance(machines, dict)
                    }
            except (ValueError, OSError):
                self._by_account = {}

    def _prune(self) -> bool:
        cutoff = self._now() - self._ttl
        changed = False
        for account in list(self._by_account):
            machines = self._by_account[account]
            for mid in list(machines):
                if float(machines[mid].get("last_seen", 0)) < cutoff:
                    del machines[mid]
                    changed = True
            if not machines:
                del self._by_account[account]
        return changed

    def upsert(self, account: str, machine_id: str, name: str) -> None:
        if not account or not machine_id:
            return
        acct = self._by_account.setdefault(account, {})
        row = acct.get(machine_id)
        if row is None:
            acct[machine_id] = {"name": name or machine_id,
                                "last_seen": self._now(), "can_ring": True}
        else:
            row["last_seen"] = self._now()
            if name:
                row["name"] = name
        self._prune()
        self._flush()

    def update(self, account: str, machine_id: str,
               name: str | None = None, can_ring: bool | None = None) -> None:
        row = self._by_account.get(account, {}).get(machine_id)
        if row is None:
            return
        if name is not None:
            row["name"] = name
        if can_ring is not None:
            row["can_ring"] = bool(can_ring)
        self._flush()

    def remove(self, account: str, machine_id: str) -> None:
        acct = self._by_account.get(account)
        if acct and machine_id in acct:
            del acct[machine_id]
            if not acct:
                del self._by_account[account]
            self._flush()

    def list(self, account: str) -> list[dict]:
        if self._prune():
            self._flush()
        now = self._now()
        out = []
        for mid, row in self._by_account.get(account, {}).items():
            last_seen = float(row.get("last_seen", 0))
            out.append({
                "machine_id": mid,
                "name": row.get("name", mid),
                "last_seen": last_seen,
                "online": (now - last_seen) < self._online_window,
                "can_ring": bool(row.get("can_ring", True)),
            })
        out.sort(key=lambda r: r["name"].lower())
        return out

    def can_ring(self, account: str, machine_id: str) -> bool:
        row = self._by_account.get(account, {}).get(machine_id)
        return True if row is None else bool(row.get("can_ring", True))

    def _flush(self) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._by_account, f)
        os.replace(tmp, self._path)
