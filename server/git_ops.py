"""Read-mostly git awareness for the voice operator ("what did it change?").

Everything here is subprocess-based and scoped HARD to the driven session's
folder: every call takes an explicit cwd (the orchestrator passes
controller.working_dir), runs git with that cwd, and refuses folders that are
not the repo ROOT, so a voice command can never reach an enclosing repo above
the session folder. It also refuses detached HEAD and an in-progress rebase,
because acting there by voice is how you lose work.

Write operations (commit, push) exist here as plain executors; the orchestrator
NEVER runs them from a tool call directly. It wraps them in a structured
approval and only calls them after the user confirms on the card or by voice.

Fail-open by contract: no function raises into the call path; every failure
comes back as {"error": "<spoken sentence>"} that Gemini can read aloud.
"""
from __future__ import annotations

import os
import subprocess

DEFAULT_TIMEOUT = 10.0   # reads are local and fast
PUSH_TIMEOUT = 60.0      # push crosses the network


def _run(cwd: str, *args: str, timeout: float = DEFAULT_TIMEOUT) -> tuple[int, str, str]:
    """Run one git command. Returns (returncode, stdout, stderr) and maps every
    process-level failure (git missing, timeout, OS error) onto a nonzero code
    with a SPOKEN sentence in stderr, so callers have exactly one error shape."""
    try:
        p = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return 127, "", "git is not installed on this machine."
    except subprocess.TimeoutExpired:
        return 124, "", f"git {args[0]} timed out after {int(timeout)} seconds."
    except OSError as e:
        return 126, "", str(e)
    return p.returncode, p.stdout or "", p.stderr or ""


def _fail(op: str, err: str) -> str:
    """One-line spoken failure: the LAST stderr line is git's actual reason."""
    detail = (err or "").strip().splitlines()[-1] if (err or "").strip() else "unknown git error"
    return f"git {op} failed: {detail}"


def repo_state(cwd: str) -> dict:
    """Gate for every operation: the folder must exist, be the ROOT of a git
    work tree, be on a real branch (not detached), and not be mid-rebase.
    Returns {"branch": name} or {"error": spoken sentence}."""
    cwd = (cwd or "").strip()
    if not cwd or not os.path.isdir(cwd):
        return {"error": "No session folder is open, so there is no repo to look at."}
    rc, out, err = _run(cwd, "rev-parse", "--is-inside-work-tree")
    if rc != 0 or out.strip() != "true":
        if "timed out" in err or "not installed" in err:
            return {"error": err}
        name = os.path.basename(cwd.rstrip("/")) or cwd
        return {"error": f"{name} is not a git repository."}
    rc, top, err = _run(cwd, "rev-parse", "--show-toplevel")
    if rc != 0:
        return {"error": _fail("rev-parse", err)}
    if os.path.realpath(top.strip()) != os.path.realpath(cwd):
        return {"error": "This folder is inside a larger repo; open the repo "
                         "root to use git by voice."}
    rc, branch, err = _run(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0:
        return {"error": _fail("rev-parse", err)}
    branch = branch.strip()
    if branch == "HEAD":
        return {"error": "This repo is on a detached HEAD; check out a branch "
                         "first, then try again."}
    for name in ("rebase-merge", "rebase-apply"):
        rc, path, _ = _run(cwd, "rev-parse", "--git-path", name)
        p = path.strip()
        if rc == 0 and p:
            if not os.path.isabs(p):
                p = os.path.join(cwd, p)
            if os.path.isdir(p):
                return {"error": "A rebase is in progress in this repo; finish "
                                 "or abort it first, then try again."}
    return {"branch": branch}


def git_status_summary(cwd: str) -> dict:
    """A human summary of git status for reading aloud: branch, change counts,
    and a few file names. Never raw porcelain output."""
    state = repo_state(cwd)
    if "error" in state:
        return state
    rc, out, err = _run(cwd, "status", "--porcelain")
    if rc != 0:
        return {"error": _fail("status", err)}
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if not lines:
        return {"summary": f"On branch {state['branch']}, the working tree is clean.",
                "branch": state["branch"]}
    untracked = [ln for ln in lines if ln.startswith("??")]
    changed = len(lines) - len(untracked)
    names = ", ".join(ln[3:].strip() for ln in lines[:8])
    summary = (f"On branch {state['branch']}: {changed} changed and "
               f"{len(untracked)} untracked file(s), including {names}.")
    return {"summary": summary, "branch": state["branch"]}


def git_diff_summary(cwd: str, max_chars: int = 4000) -> dict:
    """What changed since the last commit: a --stat summary (spoken-friendly)
    plus a condensed diff (file headers, hunks, +/- lines only; index and
    +++/--- noise stripped) capped at max_chars so it fits an LLM turn.
    Diffs against HEAD so staged and unstaged changes both show; falls back to
    a plain diff in a repo whose HEAD does not exist yet (no commits)."""
    state = repo_state(cwd)
    if "error" in state:
        return state
    rc, stat, err = _run(cwd, "diff", "HEAD", "--stat", "--no-color")
    if rc != 0:
        rc, stat, err = _run(cwd, "diff", "--stat", "--no-color")
        if rc != 0:
            return {"error": _fail("diff", err)}
    rc2, raw, _ = _run(cwd, "diff", "HEAD", "--unified=0", "--no-color")
    if rc2 != 0:
        _, raw, _ = _run(cwd, "diff", "--unified=0", "--no-color")
    kept = [ln for ln in (raw or "").splitlines()
            if ln.startswith(("diff --git", "@@", "+", "-"))
            and not ln.startswith(("+++", "---"))]
    condensed = "\n".join(kept)
    if len(condensed) > max_chars:
        condensed = condensed[:max_chars] + "\n... (truncated)"
    rc3, porc, _ = _run(cwd, "status", "--porcelain")
    untracked = ([ln[3:].strip() for ln in porc.splitlines() if ln.startswith("??")]
                 if rc3 == 0 else [])
    parts = []
    if stat.strip():
        parts.append(f"On branch {state['branch']}: {stat.strip()}")
    if untracked:
        parts.append(f"{len(untracked)} untracked file(s): " + ", ".join(untracked[:8]))
    summary = " ".join(parts) if parts else (
        f"No changes on branch {state['branch']} since the last commit.")
    return {"summary": summary, "diff": condensed, "branch": state["branch"]}


def commit_preflight(cwd: str) -> dict:
    """Everything the approval card needs BEFORE asking the user: the branch
    name the card must show, and that there is actually something to commit
    (confirming an empty commit would be noise)."""
    state = repo_state(cwd)
    if "error" in state:
        return state
    rc, out, err = _run(cwd, "status", "--porcelain")
    if rc != 0:
        return {"error": _fail("status", err)}
    changes = len([ln for ln in out.splitlines() if ln.strip()])
    if changes == 0:
        return {"error": "Nothing to commit; the working tree is clean."}
    return {"branch": state["branch"], "changes": changes}


def git_commit(cwd: str, message: str) -> dict:
    """Stage everything and commit. Only ever called AFTER an approval was
    confirmed (the orchestrator enforces that); still re-checks the preflight
    because the tree may have changed between card and confirmation."""
    message = (message or "").strip()
    if not message:
        return {"error": "I need a commit message before committing."}
    pre = commit_preflight(cwd)
    if "error" in pre:
        return pre
    rc, _, err = _run(cwd, "add", "-A")
    if rc != 0:
        return {"error": _fail("add", err)}
    rc, out, err = _run(cwd, "commit", "-m", message)
    if rc != 0:
        if "nothing to commit" in f"{out}\n{err}".lower():
            return {"error": "Nothing to commit; the working tree is clean."}
        return {"error": _fail("commit", err or out)}
    return {"summary": f"Committed on {pre['branch']}: {message}.",
            "branch": pre["branch"]}


def push_preflight(cwd: str) -> dict:
    """The push approval card must name the exact branch, and a branch with no
    upstream is refused outright (publishing a branch is a deliberate act the
    user should do once, by hand, with --set-upstream)."""
    state = repo_state(cwd)
    if "error" in state:
        return state
    rc, out, _ = _run(cwd, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if rc != 0:
        return {"error": f"Branch {state['branch']} has no upstream configured, "
                         "so I won't push it. Publish it once from the terminal "
                         "with git push --set-upstream, then ask me again."}
    return {"branch": state["branch"], "upstream": out.strip()}


def git_push(cwd: str) -> dict:
    """Plain git push to the configured upstream. Never --force, by policy."""
    pre = push_preflight(cwd)
    if "error" in pre:
        return pre
    rc, _, err = _run(cwd, "push", timeout=PUSH_TIMEOUT)
    if rc != 0:
        return {"error": f"Push failed: {(err or '').strip().splitlines()[-1] if (err or '').strip() else 'unknown git error'}"}
    return {"summary": f"Pushed {pre['branch']} to {pre['upstream']}.",
            "branch": pre["branch"]}
