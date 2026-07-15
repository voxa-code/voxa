"""Two-tier danger classification: a pure, unit-testable check for whether a
piece of text (raw shell, or natural language asking Claude to do something)
requests a destructive/irreversible action. Used by the orchestrator to gate
send_to_claude/queue_task behind a synthetic approval instead of dispatching
straight away.

Deliberately conservative: everyday phrases like "remove the unused import",
"delete this function", "drop me a summary", or "kill the dev server" must
NOT trigger. classify() returns a short human reason (e.g. "recursively
deletes files") when it does, else None.
"""
from __future__ import annotations

import re

# Each entry: (compiled pattern, short human reason). classify() returns the
# reason for the FIRST pattern that matches; order only affects which wording
# comes back when more than one category happens to match the same text.
_PATTERNS: list[tuple[re.Pattern, str]] = [
    # --- recursive / forced deletion ---------------------------------------
    (re.compile(r"\brm\s+-[a-z]*r[a-z]*f[a-z]*\b", re.I), "recursively deletes files"),
    (re.compile(r"\brm\s+-[a-z]*f[a-z]*r[a-z]*\b", re.I), "recursively deletes files"),
    (re.compile(r"\brm\s+--recursive\b", re.I), "recursively deletes files"),
    (re.compile(r"\brm\s+-r(?!\w)", re.I), "recursively deletes files"),
    (re.compile(r"\brmdir\s+\S", re.I), "deletes a directory"),
    (re.compile(
        r"\bdelete\s+(the\s+)?(entire\s+)?(repo|repository|project|folder|directory|everything)\b",
        re.I), "recursively deletes files"),
    (re.compile(r"\bwipe\b", re.I), "wipes data irreversibly"),

    # --- git history rewrites / force pushes -------------------------------
    (re.compile(r"\bpush\b.{0,30}--force\b", re.I | re.S), "force-pushes over remote history"),
    (re.compile(r"--force\b.{0,30}\bpush\b", re.I | re.S), "force-pushes over remote history"),
    (re.compile(r"\bforce[- ]push", re.I), "force-pushes over remote history"),
    (re.compile(r"\bpush\s+-f\b", re.I), "force-pushes over remote history"),
    (re.compile(r"\breset\s+--hard\b", re.I),
     "discards uncommitted work and rewrites history (reset --hard)"),
    (re.compile(r"\bclean\s+-[a-z]*f[a-z]*d[a-z]*\b", re.I),
     "force-deletes untracked files (git clean)"),
    (re.compile(r"\bclean\s+-[a-z]*d[a-z]*f[a-z]*\b", re.I),
     "force-deletes untracked files (git clean)"),
    (re.compile(r"\brebase\b.{0,40}\bforce\b", re.I | re.S),
     "rewrites git history with a forced rebase"),
    (re.compile(r"\bforce\b.{0,40}\brebase\b", re.I | re.S),
     "rewrites git history with a forced rebase"),
    (re.compile(r"--force-rebase\b", re.I), "rewrites git history with a forced rebase"),
    (re.compile(r"\bbranch\s+-D\b"), "force-deletes a git branch"),
    (re.compile(r"\bdelete\s+(the\s+)?branch\b", re.I), "deletes a git branch"),

    # --- database destruction ----------------------------------------------
    (re.compile(r"\bdrop\s+(table|database|db|schema)\b", re.I), "drops a database/table"),
    (re.compile(r"\btruncate\b", re.I), "truncates a database table"),

    # --- production deployment ----------------------------------------------
    (re.compile(r"\bdeploy\w*\b.{0,20}\bto\s+prod(uction)?\b", re.I | re.S),
     "deploys straight to production"),
    (re.compile(r"\bpush\s+(it\s+)?live\b", re.I), "pushes straight to production"),
    (re.compile(r"\brelease\s+.{0,20}\bto\s+the\s+app\s+store\b", re.I | re.S),
     "releases to the App Store"),

    # --- credential / key deletion -------------------------------------------
    (re.compile(
        r"\bdelete\s+(the\s+|my\s+)?(api\s+keys?|credentials?|secrets?|ssh\s+keys?|"
        r"private\s+keys?|tokens?|certificates?)\b", re.I),
     "deletes credentials or keys"),
    (re.compile(
        r"\brevoke\s+(the\s+|my\s+)?(api\s+keys?|credentials?|ssh\s+keys?|certificates?)\b",
        re.I), "revokes credentials or keys"),

    # --- disk operations -----------------------------------------------------
    (re.compile(r"\bdiskutil\s+erase", re.I), "erases a disk"),
    (re.compile(r"\bmkfs\b", re.I), "formats a disk"),
    (re.compile(r"\bdd\s+.*\bof=/dev", re.I | re.S), "writes raw data over a disk device"),
    (re.compile(r"\bformat\s+(the\s+)?(disk|drive|volume)\b", re.I), "formats a disk"),

    # --- killing all processes / shutdown -------------------------------------
    (re.compile(r"\bkillall\b", re.I), "kills all matching processes"),
    (re.compile(r"\bkill\s+-9\s+(-1|\*|all)\b", re.I), "kills every process"),
    (re.compile(r"\bkill\s+all\b", re.I), "kills all processes"),
    (re.compile(r"\bshutdown\b", re.I), "shuts down the machine"),
    (re.compile(r"\breboot\b", re.I), "reboots the machine"),

    # --- wide-open permissions -------------------------------------------------
    (re.compile(r"\bchmod\s+-R\s+777\b", re.I),
     "recursively opens permissions to everyone (chmod -R 777)"),
]


def classify(text: str) -> str | None:
    """Return a short human reason if ``text`` asks for a destructive/irreversible
    action, else None. Case-insensitive; tolerant of both raw shell commands and
    natural-language phrasing."""
    if not text:
        return None
    for pattern, reason in _PATTERNS:
        if pattern.search(text):
            return reason
    return None
