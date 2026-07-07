"""Per-project rules for whether a notification should ring or stay silent. A
user working late in one repo may want completions silent there while another
repo still rings; this store lets that preference persist across restarts the
same way session_state.py persists the driven session's cwd.
"""
from __future__ import annotations

import json
import os

_KINDS = ("finish", "needs_input")
_MODES = ("ring", "silent")
_DEFAULT_MODE = "ring"
# Reserved top-level key holding the GLOBAL per-kind default, so the phone can set
# one switch instead of a per-project matrix. It can NEVER collide with a real cwd:
# _normalize only rstrips trailing "/", and every real cwd is an absolute path (so
# it keeps a leading "/", or normalizes to "" for the root "/"), whereas this key is
# a bare token with no slash. Chosen over "" precisely because _normalize CAN
# produce "" (from cwd "/"), which would then be ambiguous with the defaults.
_DEFAULTS_KEY = "__default__"


def _default_path() -> str:
    # Live under ~/.voxa like the other persistent state (relay_code, .env), so
    # rules apply no matter which directory voxa is launched from.
    return os.path.expanduser("~/.voxa/notify_rules.json")


def _normalize(cwd: str) -> str:
    return cwd.rstrip("/")


class NotifyRules:
    def __init__(self, path: str | None = None):
        self._path = path or os.environ.get("VOXA_NOTIFY_RULES_FILE") or _default_path()

    def mode(self, cwd: str, kind: str) -> str:
        """Resolve the effective mode: a per-cwd override wins, else the global
        default for this kind, else "ring". A per-cwd "silent" IS the mute list."""
        rules = self._load()
        per_cwd = rules.get(_normalize(cwd), {})
        if isinstance(per_cwd, dict) and kind in per_cwd:
            return per_cwd[kind]
        defaults = rules.get(_DEFAULTS_KEY, {})
        if isinstance(defaults, dict) and kind in defaults:
            return defaults[kind]
        return _DEFAULT_MODE

    def set_mode(self, cwd: str, kind: str, mode: str) -> None:
        if kind not in _KINDS:
            raise ValueError(f"invalid kind: {kind!r}")
        if mode not in _MODES:
            raise ValueError(f"invalid mode: {mode!r}")

        rules = self._load()
        rules.setdefault(_normalize(cwd), {})[kind] = mode
        self._persist(rules)

    def set_default(self, kind: str, mode: str) -> None:
        """Set the GLOBAL default for a kind (finish/needs_input). Per-cwd rules
        still override it; this is the fleet-wide fallback the phone toggles instead
        of a per-project matrix. Stored under the reserved _DEFAULTS_KEY section."""
        if kind not in _KINDS:
            raise ValueError(f"invalid kind: {kind!r}")
        if mode not in _MODES:
            raise ValueError(f"invalid mode: {mode!r}")

        rules = self._load()
        section = rules.get(_DEFAULTS_KEY)
        if not isinstance(section, dict):
            section = {}
        section[kind] = mode
        rules[_DEFAULTS_KEY] = section
        self._persist(rules)

    def defaults(self) -> dict:
        """The resolved global default per kind (each falling back to "ring"), the
        "default" map the phone renders alongside the per-cwd mute list."""
        section = self._load().get(_DEFAULTS_KEY, {})
        if not isinstance(section, dict):
            section = {}
        return {k: section.get(k, _DEFAULT_MODE) for k in _KINDS}

    def all(self) -> dict:
        # Raw store: per-cwd rules plus the reserved _DEFAULTS_KEY section when set,
        # so the defaults are exposed here too (the phone reads defaults() for the
        # clean map, but all() carries the whole persisted picture).
        return self._load()

    def overrides(self) -> dict:
        """Per-cwd rules ONLY (the mute list), with the reserved _DEFAULTS_KEY
        section stripped. This is what the phone renders as muted projects; the
        global default travels separately via defaults(), so the reserved key must
        never leak in here (or it shows up as a phantom project named __default__)."""
        return {k: v for k, v in self._load().items() if k != _DEFAULTS_KEY}

    def _persist(self, rules: dict) -> None:
        # Fail-open: persistence must never break a live session.
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(rules, f)
            os.replace(tmp, self._path)
        except OSError:
            pass

    def _load(self) -> dict:
        try:
            with open(self._path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}
