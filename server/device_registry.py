from __future__ import annotations

import json
import os


class DeviceRegistry:
    """VoIP push tokens, keyed by account so the cloud can ring the right phone
    for the right customer. Persisted as {account: [tokens]} (a legacy flat list
    is migrated under account "")."""

    def __init__(self, path: str):
        self._path = path
        self._by_account: dict[str, set[str]] = {}
        if os.path.exists(path):
            try:
                data = json.load(open(path))
                if isinstance(data, dict):
                    self._by_account = {a: set(t) for a, t in data.items()}
                elif isinstance(data, list):           # legacy flat list
                    self._by_account = {"": set(data)}
            except (ValueError, OSError):
                self._by_account = {}

    def register(self, token: str, account: str = "") -> None:
        if not token:
            return
        # A device token belongs to exactly ONE account: the most recent to
        # register it. When the phone signs in it re-registers the same token
        # under its new (Apple) account; drop the stale entry under the old
        # anonymous account. Otherwise ring(None) fans out across every account
        # and pushes the same physical phone twice, which the user sees as a
        # second, very short "ghost" call right after the real one.
        changed = False
        for acct, s in self._by_account.items():
            if acct != account and token in s:
                s.discard(token)
                changed = True
        s = self._by_account.setdefault(account, set())
        if token not in s:
            s.add(token)
            changed = True
        if changed:
            self._flush()

    def remove(self, token: str) -> None:
        changed = False
        for s in self._by_account.values():
            if token in s:
                s.discard(token)
                changed = True
        if changed:
            self._flush()

    def tokens(self, account: str | None = None) -> list[str]:
        """Tokens for one account, or all unique tokens if account is None.
        The account=None list is de-duplicated so a token that (defensively)
        still appears under more than one account is only rung once."""
        if account is None:
            return list({t for s in self._by_account.values() for t in s})
        return list(self._by_account.get(account, set()))

    def _flush(self) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({a: sorted(t) for a, t in self._by_account.items()}, f)
        os.replace(tmp, self._path)
