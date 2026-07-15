# server/greetings.py
"""Voxa's spoken opening when a call is answered, and greeting suppression.

The opening leads with the project and what its last task actually did, then
asks what's next, instead of a bare greeting followed by the raw update.
"""
from __future__ import annotations

import os


def should_suppress_greeting(pending_updates: list) -> bool:
    """Suppress Voxa's generic opening when there is a queued update to relay on
    answer, so the answer opening is the contextual update spoken once."""
    return bool(pending_updates)


def _strip_finished_prefix(summary: str) -> str:
    """Turn a finish summary ('<project> finished: <result>') into just the result,
    since the opening phrases the 'finished' part itself. '<project> finished' with no
    result becomes ''. Other summaries (e.g. 'needs input: ...') pass through."""
    s = (summary or "").strip()
    low = s.lower()
    i = low.find("finished:")
    if i != -1:
        return s[i + len("finished:"):].strip()
    if low.endswith(" finished") or low == "finished":
        return ""
    return s


def format_approval_for_speech(approval: dict | None, current_project: str = "") -> str:
    """One spoken sentence for a pending approval: the question plus numbered
    options, so the operator reads the choices the moment a call is answered. The
    live pane monitor only re-emits option text that CHANGES mid-call, never a
    prompt that was already on screen when the phone answered, so without this a
    static prompt reaches the phone as a silent card and is never read aloud.
    Returns '' for a falsy or optionless approval (caller then speaks nothing extra).

    When the approval's cwd names a DIFFERENT project than ``current_project`` (the
    one the call is attached to), the lead names it ("There's a prompt waiting in
    veil: ...") so the user knows which fleet member is asking, instead of a bare
    "There's a prompt waiting" that reads as if it belongs to the attached session.
    Unchanged when cwd is missing or its basename matches current_project."""
    if not approval:
        return ""
    options = [o for o in (approval.get("options") or []) if isinstance(o, dict)]
    if not options:
        return ""
    summary = (approval.get("summary") or "").strip()
    opts = ". ".join(
        f"{o.get('key', '')}: {o.get('label', '')}".strip() for o in options
    )
    cwd = (approval.get("cwd") or "").rstrip("/")
    label = os.path.basename(cwd) if cwd else ""
    if label and label != current_project:
        lead = (f"There's a prompt waiting in {label}: {summary} " if summary
                else f"There's a prompt waiting in {label}. ")
    else:
        lead = f"There's a prompt waiting: {summary} " if summary else "There's a prompt waiting. "
    return f"{lead}Options: {opts}. Which should I pick?"


def split_updates_for(project: str, updates: list) -> tuple[list, list]:
    """Partition queued update summaries into those that belong to ``project``
    (hook summaries are labeled '<project> finished ...' / '<project> needs
    input ...') and everything else, so an opening never claims another
    session's finish as the attached project's ("your last task in Ti0
    finished: <loop's result>"). Unlabeled updates (the driven session's own
    hub finals) count as the project's. With no project, nothing is foreign."""
    updates = [u for u in (updates or []) if u and str(u).strip()]
    if not project:
        return updates, []
    low_p = project.lower()
    own, foreign = [], []
    for u in updates:
        low = str(u).strip().lower()
        labeled = (" finished" in low) or (" needs input" in low)
        if not labeled or low.startswith((f"{low_p} finished",
                                          f"{low_p} needs input")):
            own.append(u)
        else:
            foreign.append(u)
    return own, foreign


def compose_opening(project: str, updates: list, approval: dict | None = None) -> str:
    """Voxa's spoken opening when a call is answered: lead with the project and what
    its last task actually did, then ask what's next, instead of a bare greeting
    followed by the raw update. `project` is '' when we couldn't attach to a folder.
    When `approval` is a pending prompt, its question and options are appended so
    the choices are read aloud on answer; with no project and no update to relay,
    the opening leads with the prompt instead of a bare 'You're back.'."""
    detail = " ".join(_strip_finished_prefix(u) for u in (updates or []) if u and u.strip()).strip()
    prompt = format_approval_for_speech(approval, current_project=project)
    if project and detail:
        body = f"Your last task in {project} just finished. Here's what it did: {detail}."
    elif project and any((u or "").strip() for u in (updates or [])):
        # A real finish arrived, it just carried no detail worth reading.
        body = f"You're back in {project}. Your last task there just finished."
    elif project:
        # NOTHING pending: a plain (re)connect, often a brand-new session.
        # Claiming "your last task there just finished" here was a fabrication
        # the user noticed; say where they are and nothing more.
        body = f"You're back in {project}."
    elif detail:
        body = f"Your last task just finished. Here's what it did: {detail}."
    elif prompt:
        # No project and nothing to relay, but a prompt is blocking: lead with it so
        # the first thing spoken is the choice, not a content-free "You're back.".
        return f"Hi. {prompt} What would you like to do next?"
    else:
        body = "You're back."
    if prompt:
        body = f"{body} {prompt}"
    return f"Hi. {body} What would you like to do next?"


def compose_digest(project: str, outcomes: list) -> str:
    """One spoken summary for a burst of queued tasks, so a run of instructions
    ("bump the deps, then run the tests") rings ONCE at the end instead of per
    item: "3 tasks done" when everything finished, or "2 done, 1 needs you: <the
    first blocked instruction>" when a task stopped for input. `outcomes` are the
    finished-item records drained from the TaskQueue (each has an ``outcome`` of
    "done"/"needs_input"/"failed", plus ``text`` and ``summary``). Returns '' for
    an empty burst so the caller reports nothing. Never uses an em dash."""
    outcomes = outcomes or []
    if not outcomes:
        return ""
    done = sum(1 for o in outcomes if o.get("outcome") == "done")
    failed = sum(1 for o in outcomes if o.get("outcome") == "failed")
    needs = [o for o in outcomes if o.get("outcome") == "needs_input"]
    where = f" in {project}" if project else ""
    # All done is the common, clean case: "N tasks done".
    if done and not failed and not needs:
        noun = "task" if done == 1 else "tasks"
        return f"{done} {noun} done{where}."
    segs: list[str] = []
    if done:
        segs.append(f"{done} done")
    if failed:
        segs.append(f"{failed} failed")
    if needs:
        first = (needs[0].get("summary") or needs[0].get("text") or "").strip()
        verb = "needs" if len(needs) == 1 else "need"
        seg = f"{len(needs)} {verb} you"
        if first:
            seg += f": {first}"
        segs.append(seg)
    body = ", ".join(segs) if segs else f"{len(outcomes)} tasks done"
    return f"{body}{where}."


def suppress_greeting_if_supported(operator) -> bool:
    """Suppress the operator's generic opening, but only if it supports it. The metered
    RemoteOperator greets cloud-side and has no suppress_greeting, so this no-ops there
    instead of raising (which would kill the answer flow)."""
    fn = getattr(operator, "suppress_greeting", None)
    if callable(fn):
        fn()
        return True
    return False


def apply_greeting_suppression(operator, pending_updates: list) -> bool:
    """Suppress the operator's generic opening when there is a queued update to relay.
    Safe when the operator has no suppress_greeting (the metered RemoteOperator greets
    cloud-side); never raises on that path."""
    if not should_suppress_greeting(pending_updates):
        return False
    return suppress_greeting_if_supported(operator)
