"""Session history for the phone: list past Claude sessions and replay one as an
interleaved timeline. Claude's side comes from Claude Code's own transcripts
(~/.claude/projects); the user's side is recorded here from live captions into
~/.voxa/history/voice.jsonl, matched to a session at read time by cwd and the
session's time window. Read tolerance and fail-open writes throughout: history
must never break a live call or crash on a corrupt file.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import time
from datetime import datetime

from server.jsonl_log import RotatingJsonlLog
from server.transcripts import PROJECTS_DIR, _text_of

logger = logging.getLogger(__name__)

VOICE_WINDOW_SLACK = 120.0   # seconds of slack around the session window
VOICE_COALESCE_GAP = 3.0     # seconds: merge same-role voice deltas closer than this

# path -> (mtime at scan time, row-or-None). Holds the small listing ROW, never
# the full parsed messages, so a large history directory stays cheap to cache.
_scan_cache: dict[str, tuple[float, dict | None]] = {}


def _default_voice_path() -> str:
    return os.environ.get("VOXA_VOICE_HISTORY_FILE",
                          os.path.expanduser("~/.voxa/history/voice.jsonl"))


def _index_path() -> str:
    return os.environ.get("VOXA_HISTORY_INDEX_FILE",
                          os.path.expanduser("~/.voxa/history/index.json"))


def _load_index() -> dict[str, tuple[float, dict | None]]:
    """Read the persisted listing index (path -> (mtime, row)). Fail-open: a
    missing or corrupt file rebuilds from scratch, it never raises, so a bad
    index can only cost a rescan, never break the listing."""
    out: dict[str, tuple[float, dict | None]] = {}
    try:
        with open(_index_path()) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return out
    if not isinstance(data, dict):
        return out
    for path, entry in data.items():
        try:
            mtime, row = entry
            out[path] = (float(mtime), row)
        except (TypeError, ValueError):
            continue
    return out


def _save_index(index: dict[str, tuple[float, dict | None]]) -> None:
    """Persist the listing index so a server restart does not re-parse every
    transcript. Atomic tmp+replace, and fail-open on OSError: history durability
    must never take precedence over serving the request."""
    p = _index_path()
    try:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        tmp = f"{p}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump({path: [mtime, row] for path, (mtime, row) in index.items()}, f)
        os.replace(tmp, p)
    except OSError:
        logger.warning("history index write failed", exc_info=True)


def _ts(entry: dict) -> float | None:
    raw = entry.get("timestamp")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _scan(path: str) -> dict | None:
    """One pass over a transcript: metadata + messages (with timestamps)."""
    first_ts = last_ts = None
    cwd = ""
    preview = ""
    messages: list[dict] = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("type") not in ("user", "assistant"):
                    continue
                if not cwd:
                    cwd = o.get("cwd") or ""
                t = _ts(o)
                if t is not None:
                    first_ts = t if first_ts is None else first_ts
                    last_ts = t
                m = o.get("message") or {}
                text = _text_of(m.get("content")).strip()
                if not text:
                    continue
                role = m.get("role") or o.get("type")
                # Real transcripts often open with harness-injected wrappers
                # (<local-command-caveat>, <system-reminder>, ...); skip them so
                # the preview shows what the human actually asked.
                if not preview and role == "user" and not text.startswith("<"):
                    preview = text[:120]
                messages.append({"role": role, "ts": t, "text": text})
    except (OSError, ValueError):
        # ValueError covers UnicodeDecodeError from invalid bytes mid-file: one
        # corrupt transcript must skip itself, never break the whole listing.
        return None
    if not messages:
        return None
    mtime = os.path.getmtime(path)
    return {"cwd": cwd, "started": first_ts or mtime, "ended": last_ts or mtime,
            "preview": preview, "messages": messages}


def _scan_row(path: str) -> dict | None:
    """Cheap listing row for one transcript, without the full-parse cost.

    A listing row only needs cwd + first-human preview + started/ended + a
    count, so this json-parses only far enough to fill cwd, preview and the
    first timestamp, then STOPS (the preview early-stop is the natural cutoff)
    instead of decoding and materialising every message like ``_scan`` does.
    ``ended`` is the file mtime, which the listing already trusts as its sort
    proxy. ``msg_count`` is a cheap non-blank line count: it does not json-parse
    the tail of a multi-MB transcript just to report an exact figure.
    """
    first_ts: float | None = None
    cwd = ""
    preview = ""
    msg_count = 0
    have_meta = False
    saw_message = False
    try:
        with open(path) as f:
            for line in f:
                if not line.strip():
                    continue
                msg_count += 1
                if have_meta:
                    # Everything the row needs is already collected; keep
                    # counting lines but skip the expensive json/text decode.
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("type") not in ("user", "assistant"):
                    continue
                m = o.get("message") or {}
                text = _text_of(m.get("content")).strip()
                if not text:
                    continue
                saw_message = True
                if not cwd:
                    cwd = o.get("cwd") or ""
                if first_ts is None:
                    first_ts = _ts(o)
                role = m.get("role") or o.get("type")
                # Skip harness-injected wrappers so the preview shows the human's
                # actual first ask (mirrors _scan).
                if not preview and role == "user" and not text.startswith("<"):
                    preview = text[:120]
                have_meta = bool(preview) and bool(cwd) and first_ts is not None
    except (OSError, ValueError):
        # ValueError also covers UnicodeDecodeError from invalid bytes mid-file:
        # one corrupt transcript skips itself, never breaks the whole listing.
        return None
    if not saw_message:
        return None
    enc_dir = os.path.basename(os.path.dirname(path))
    stem = os.path.splitext(os.path.basename(path))[0]
    mtime = os.path.getmtime(path)
    label = os.path.basename(cwd.rstrip("/")) if cwd else enc_dir
    return {"id": f"{enc_dir}/{stem}", "cwd": cwd, "label": label,
            "started": first_ts if first_ts is not None else mtime, "ended": mtime,
            "msg_count": msg_count, "preview": preview}


def list_sessions(limit: int = 50, projects_dir: str | None = None) -> list[dict]:
    """Recent sessions across all projects, newest ended first.

    Scanning every transcript on every call is too slow once a project has a
    few thousand of them, so this walks files newest-mtime-first (mtime is a
    faithful proxy for `ended`), reuses both a module-level cache and an
    on-disk index keyed by mtime (so a server restart does not re-parse), does
    a cheap `_scan_row` (never the full-message `_scan`) on a miss, and stops
    as soon as `limit` usable rows are collected.
    """
    projects_dir = projects_dir or PROJECTS_DIR
    limit = max(1, min(int(limit), 200))

    def _mtime(p: str) -> float | None:
        try:
            return os.path.getmtime(p)
        except OSError:
            return None

    paths = glob.glob(os.path.join(projects_dir, "*", "*.jsonl"))
    ranked = sorted(
        ((p, m) for p in paths if (m := _mtime(p)) is not None),
        key=lambda pm: pm[1], reverse=True,
    )

    index = _load_index()
    rows: list[dict] = []
    dirty = False
    for path, mtime in ranked:
        cached = _scan_cache.get(path)
        if cached is not None and cached[0] == mtime:
            row = cached[1]
        else:
            disk = index.get(path)
            if disk is not None and disk[0] == mtime:
                row = disk[1]
            else:
                row = _scan_row(path)
                index[path] = (mtime, row)
                dirty = True
            _scan_cache[path] = (mtime, row)
        if row is None:
            continue
        rows.append(row)
        if len(rows) >= limit:
            break
    if dirty:
        _save_index(index)
    # mtime order is only approximate for `ended`; re-sort the (small) result.
    rows.sort(key=lambda r: r["ended"], reverse=True)
    return rows[:limit]


def record_voice(cwd: str, role: str, text: str, path: str | None = None) -> None:
    """Append one live caption. Fail-open: never raises into the call path."""
    text = (text or "").strip()
    if not text:
        return
    try:
        p = path or _default_voice_path()
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        RotatingJsonlLog(p).append(
            {"ts": time.time(), "cwd": (cwd or "").rstrip("/"), "role": role, "text": text})
    except Exception:
        logger.warning("voice history append failed", exc_info=True)


def _coalesce_voice(rows: list[dict]) -> list[dict]:
    """Merge consecutive same-role rows less than VOICE_COALESCE_GAP seconds
    apart: Gemini streams voice text as many small deltas, which otherwise show
    up as a flood of fragmented chat bubbles instead of one line."""
    out: list[dict] = []
    last_ts: float | None = None
    for r in rows:
        if (out and out[-1]["role"] == r["role"] and last_ts is not None
                and r["ts"] - last_ts <= VOICE_COALESCE_GAP):
            out[-1]["text"] += r["text"]
        else:
            out.append({"role": r["role"], "ts": r["ts"], "text": r["text"]})
        last_ts = r["ts"]
    return out


def _voice_rows(path: str, cwd: str, start: float, end: float) -> list[dict]:
    try:
        rows = RotatingJsonlLog(path).read_all()
    except Exception:
        return []
    cwd = cwd.rstrip("/")
    out = []
    for r in rows:
        try:
            if (r.get("cwd") or "").rstrip("/") != cwd:
                continue
            t = float(r.get("ts", 0))
            if start - VOICE_WINDOW_SLACK <= t <= end + VOICE_WINDOW_SLACK and r.get("text"):
                role = "voice_agent" if r.get("role") == "agent" else "voice_user"
                out.append({"role": role, "ts": t, "text": r["text"]})
        except (TypeError, ValueError):
            continue
    return _coalesce_voice(out)


def session_detail(session_id: str, limit: int = 200,
                   projects_dir: str | None = None,
                   voice_path: str | None = None) -> dict:
    """One session as an interleaved timeline, capped to the LAST `limit` messages."""
    projects_dir = projects_dir or PROJECTS_DIR
    parts = (session_id or "").split("/")
    if len(parts) != 2 or ".." in session_id or session_id.startswith("/"):
        return {"error": "bad session id"}
    path = os.path.join(projects_dir, parts[0], parts[1] + ".jsonl")
    if not os.path.isfile(path):
        return {"error": "session not found"}
    info = _scan(path)
    if info is None:
        return {"error": "session unreadable or empty"}
    merged = list(info["messages"])
    vp = voice_path or _default_voice_path()
    if os.path.exists(vp) and info["cwd"]:
        merged += _voice_rows(vp, info["cwd"], info["started"], info["ended"])
    # Stable sort: untimed Claude messages keep their file order at the front.
    merged.sort(key=lambda m: m["ts"] if m["ts"] is not None else 0.0)
    truncated = len(merged) > limit
    return {"id": session_id, "messages": merged[-limit:], "truncated": truncated}
