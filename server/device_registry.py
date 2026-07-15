from __future__ import annotations

import json
import os

_KINDS = ("voip", "alert")


class DeviceRegistry:
    """Push tokens, keyed by account so the cloud can ring (or alert) the right
    phone for the right customer. Two kinds are tracked per account:
      - "voip": PushKit VoIP tokens (rings via CallKit). Default kind, and the
        only kind that existed before free-tier alert fallback was added.
      - "alert": plain APNs tokens used for the visual "Claude finished..."
        push sent instead of a ring once a free account is past its monthly
        call quota.

    Persisted shape: {account: {"voip": [tokens...], "alert": [tokens...]}}.
    Two legacy on-disk shapes are silently migrated on load, both treated as
    voip tokens (the only kind that existed when they were written):
      - a flat list of tokens (very old): becomes account "".
      - {account: [tokens...]}: becomes {account: {"voip": [tokens...], "alert": []}}.
    """

    def __init__(self, path: str):
        self._path = path
        self._by_account: dict[str, dict[str, set[str]]] = {}
        if os.path.exists(path):
            try:
                data = json.load(open(path))
                if isinstance(data, dict):
                    for acct, val in data.items():
                        self._by_account[acct] = self._migrate_entry(val)
                elif isinstance(data, list):           # legacy flat list
                    self._by_account = {"": {"voip": set(data), "alert": set()}}
            except (ValueError, OSError):
                self._by_account = {}

    @staticmethod
    def _migrate_entry(val) -> dict[str, set[str]]:
        if isinstance(val, list):                      # legacy {account: [tokens]}
            return {"voip": set(val), "alert": set()}
        if isinstance(val, dict):
            return {"voip": set(val.get("voip", [])), "alert": set(val.get("alert", []))}
        return {"voip": set(), "alert": set()}

    def register(self, token: str, account: str = "", kind: str = "voip") -> None:
        if not token:
            return
        kind = kind if kind in _KINDS else "voip"
        # A device token belongs to exactly ONE (account, kind) slot: the most
        # recent registration wins. When the phone signs in it re-registers the
        # same token under its new (Apple) account; drop the stale entry under
        # the old anonymous account. Otherwise ring(None)/alert(None) fans out
        # across every account and pushes the same physical phone twice, which
        # the user sees as a second, very short "ghost" call right after the
        # real one.
        changed = False
        for acct, kinds in self._by_account.items():
            for k, s in kinds.items():
                if (acct, k) != (account, kind) and token in s:
                    s.discard(token)
                    changed = True
        entry = self._by_account.setdefault(account, {"voip": set(), "alert": set()})
        if token not in entry[kind]:
            entry[kind].add(token)
            changed = True
        if changed:
            self._flush()

    def remove(self, token: str) -> None:
        """Remove a token from wherever it is registered (any account, either kind)."""
        changed = False
        for kinds in self._by_account.values():
            for s in kinds.values():
                if token in s:
                    s.discard(token)
                    changed = True
        if changed:
            self._flush()

    def tokens(self, account: str | None = None) -> list[str]:
        """VoIP tokens for one account, or all unique voip tokens if account is
        None. The account=None list is de-duplicated so a token that (defensively)
        still appears under more than one account is only rung once."""
        if account is None:
            return list({t for kinds in self._by_account.values() for t in kinds["voip"]})
        return list(self._by_account.get(account, {}).get("voip", set()))

    def alert_tokens(self, account: str | None = None) -> list[str]:
        """Plain-alert tokens for one account, or all unique alert tokens if
        account is None. Mirrors tokens() but for the "alert" kind."""
        if account is None:
            return list({t for kinds in self._by_account.values() for t in kinds["alert"]})
        return list(self._by_account.get(account, {}).get("alert", set()))

    def _flush(self) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(
                {a: {"voip": sorted(k["voip"]), "alert": sorted(k["alert"])}
                 for a, k in self._by_account.items()},
                f,
            )
        os.replace(tmp, self._path)
