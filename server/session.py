"""The registry of Claude sessions Voxa can drive.

Today the registry always holds exactly one session (the same singleton the
server has always had); fleet view (Phase 3) makes it plural. Extracting the
state that used to live loose on app.state gives every later phase one obvious
place to key per-session behavior.
"""
from __future__ import annotations


class Session:
    """One driven Claude Code session: the controller (swapped in place when the
    user attaches to a different terminal), its hub, and its call-line state."""

    def __init__(self, session_id: str, controller, hub, call_manager):
        self.id = session_id
        self.controller = controller
        self.hub = hub
        # Not read yet: Phase 3 gives each session its own CallManager and call
        # sites switch to reading it here.
        self.call_manager = call_manager


class SessionRegistry:
    def __init__(self):
        self._sessions: dict[str, Session] = {}
        # Which session the fleet view currently has selected. None until a
        # caller explicitly picks one (set_active); active() falls back to
        # default() (today's single-session behavior) until then.
        self.active_id: str | None = None
        # Which terminal (cwd) triggered a pending call, so answering attaches
        # to it. Keyed by cwd (not a single slot) so a second hook firing for a
        # DIFFERENT terminal while the first is still unanswered doesn't clobber
        # it; insertion order tracks "most recently pushed" for pop_pending().
        # Registry-level because a hook can fire before any session exists.
        self._pending: dict[str, dict] = {}

    def add(self, session: Session) -> Session:
        self._sessions[session.id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def default(self) -> Session | None:
        """The single active session (first added), or None. Phase 3 replaces
        callers of this with explicit per-session routing."""
        return next(iter(self._sessions.values()), None)

    def all(self) -> list[Session]:
        return list(self._sessions.values())

    def remove(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def set_active(self, session_id: str) -> None:
        """Mark ``session_id`` as the fleet view's current selection."""
        self.active_id = session_id

    def active(self) -> Session | None:
        """The explicitly-selected session (via set_active), falling back to
        default() when nothing has been selected yet, or the selection no
        longer exists (e.g. the session was removed)."""
        if self.active_id is not None:
            session = self._sessions.get(self.active_id)
            if session is not None:
                return session
        return self.default()

    def find_by_cwd(self, cwd: str) -> Session | None:
        """The session whose controller is currently driving ``cwd``, or None.
        Compares rstrip-normalized paths (a trailing slash must not cause a
        miss) and skips sessions whose controller has no working_dir yet."""
        if not cwd:
            return None
        target = cwd.rstrip("/")
        for session in self._sessions.values():
            working_dir = getattr(session.controller, "working_dir", None)
            if working_dir and working_dir.rstrip("/") == target:
                return session
        return None

    def push_pending(self, cwd: str) -> None:
        """Record that ``cwd`` triggered a pending call, so answering attaches
        to it. Re-pushing the same cwd moves it back to "most recent" instead
        of leaving a stale position."""
        self._pending.pop(cwd, None)
        self._pending[cwd] = {"cwd": cwd}

    def pop_pending(self) -> dict | None:
        """Return and clear the MOST RECENTLY pushed pending source. Older
        entries (different cwds still awaiting an answer) survive."""
        if not self._pending:
            return None
        _, value = self._pending.popitem()
        return value

    @property
    def pending_source(self) -> dict | None:
        """Read-only peek at the most recently pushed pending source, without
        clearing it. Back-compat for callers/tests that inspect the pending
        source directly; production code should use push_pending/pop_pending."""
        if not self._pending:
            return None
        return next(reversed(self._pending.values()))
