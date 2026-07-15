"""Recap what a terminal was working on by reading Claude Code's own transcript.

Claude Code logs each session to ``~/.claude/projects/<encoded-cwd>/<session>.jsonl``
where the cwd has ``/`` and ``.`` replaced by ``-``. Discovery already knows each
open terminal's cwd, so on attach we read the newest transcript for that cwd and
build a short recap of the recent conversation, instead of scraping the screen.
"""

from __future__ import annotations

import glob
import json
import os
import re

PROJECTS_DIR = os.path.expanduser("~/.claude/projects")


def _encode(cwd: str) -> str:
    return re.sub(r"[/.]", "-", cwd)


def latest_transcript(cwd: str, projects_dir: str = PROJECTS_DIR) -> str | None:
    if not cwd:
        return None
    d = os.path.join(projects_dir, _encode(cwd))
    files = glob.glob(os.path.join(d, "*.jsonl")) if os.path.isdir(d) else []
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text" and b.get("text"):
                    parts.append(b["text"])
                elif b.get("type") == "tool_use" and b.get("name"):
                    parts.append(f"[used {b['name']}]")
        return " ".join(parts)
    return ""


def recap(cwd: str, max_msgs: int = 25, max_chars: int = 500,
          projects_dir: str = PROJECTS_DIR, last_answer_chars: int = 3000) -> str:
    """Return a short text recap of the most recent conversation in ``cwd``'s
    Claude session, or "" if there is no transcript.

    Older messages are capped at ``max_chars`` each, but the FINAL assistant
    message gets the much larger ``last_answer_chars`` budget: the recap rides
    into the call that Claude's finish just triggered, and "what did it
    actually say?" is answered by that last message, so it must arrive whole
    (or nearly), not cut to a teaser.

    Reads only the TAIL of the transcript (the last ~512KB): a long-running
    session's JSONL grows to many megabytes, and json-parsing the whole file
    for the last 25 messages held up every attach/answer for seconds. The
    'This session started with' opener therefore comes from the earliest
    message in the tail, not the literal first message of a huge session."""
    path = latest_transcript(cwd, projects_dir)
    if not path:
        return ""
    msgs: list[tuple[str, str]] = []
    try:
        with open(path, "rb") as fb:
            fb.seek(0, os.SEEK_END)
            size = fb.tell()
            tail = 512 * 1024
            if size > tail:
                fb.seek(size - tail)
                fb.readline()   # drop the first (almost surely partial) line
            else:
                fb.seek(0)
            raw = fb.read().decode("utf-8", errors="ignore")
        for line in raw.splitlines():
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("type") not in ("user", "assistant"):
                continue
            m = o.get("message") or {}
            role = m.get("role") or o.get("type")
            text = _text_of(m.get("content")).strip()
            if text:
                msgs.append((role, text))
    except OSError:
        return ""
    if not msgs:
        return ""
    recent = msgs[-max_msgs:]
    last_assistant_i = next((i for i in range(len(recent) - 1, -1, -1)
                             if recent[i][0] != "user"), None)
    lines = []
    for i, (role, text) in enumerate(recent):
        budget = last_answer_chars if i == last_assistant_i else max_chars
        if len(text) > budget:
            text = text[:budget] + "…"
        who = "You" if role == "user" else "Claude"
        lines.append(f"{who}: {text}")
    opener = next((t for r, t in msgs if r == "user"), "")
    if opener:
        if len(opener) > max_chars:
            opener = opener[:max_chars] + "…"
        lines.insert(0, f"This session started with: {opener}")
    return "\n".join(lines)


def _collect_messages(path: str) -> list[dict]:
    msgs: list[dict] = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("type") not in ("user", "assistant"):
                    continue
                m = o.get("message") or {}
                text = _text_of(m.get("content")).strip()
                if text:
                    msgs.append({"role": m.get("role") or o.get("type"),
                                 "text": text})
    except OSError:
        return []
    return msgs


def read_session(cwd: str, last: int | None = None, search: str | None = None,
                 projects_dir: str = PROJECTS_DIR,
                 max_bytes: int = 6000) -> dict:
    """On-demand deep read of the newest transcript for ``cwd``: the voice
    agent's read_session tool. Returns {"messages": [{"role","text"}...]}."""
    path = latest_transcript(cwd, projects_dir)
    if not path:
        return {"error": f"no Claude transcript found for {cwd or '(no cwd)'}"}
    msgs = _collect_messages(path)
    if search:
        picked: list[int] = []
        hits = 0
        for i, m in enumerate(msgs):
            if search.lower() in m["text"].lower():
                hits += 1
                for j in (i - 1, i, i + 1):          # hit plus one neighbour each side
                    if 0 <= j < len(msgs) and j not in picked:
                        picked.append(j)
                if hits >= 10:
                    break
        out = [msgs[i] for i in sorted(picked)]
    else:
        n = min(int(last or 10), 40)
        out = msgs[-n:]
    # Cap the payload: trim message texts evenly until the JSON fits, accounting
    # for the per-entry JSON overhead (keys, quotes, commas), not just the text.
    if not out:
        return {"messages": []}
    overhead = len(json.dumps({"role": "assistant", "text": ""}).encode()) + 2
    per = max(50, (max_bytes - overhead * len(out)) // len(out))
    result = [{"role": m["role"], "text": m["text"][:per]} for m in out]
    # Safety net for any remaining overshoot (e.g. multi-byte unicode text).
    while result and len(json.dumps({"messages": result}).encode()) > max_bytes:
        result.pop()
    return {"messages": result}
