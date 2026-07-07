"""git_ops tests run against REAL scratch repos in tmp_path: the module's whole
job is shelling out to git correctly, so faking subprocess would test nothing.
git is a hard dependency of the dev machines this suite runs on (verified
git 2.50 at authoring time)."""
import subprocess

import pytest

from server import git_ops


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "a.txt").write_text("one\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo


def test_status_clean_repo_names_branch(repo):
    res = git_ops.git_status_summary(str(repo))
    assert res["branch"] == "main"
    assert "clean" in res["summary"].lower()
    assert "main" in res["summary"]


def test_status_counts_changed_and_untracked(repo):
    (repo / "a.txt").write_text("two\n")
    (repo / "new.txt").write_text("hi\n")
    res = git_ops.git_status_summary(str(repo))
    assert "1 changed" in res["summary"]
    assert "1 untracked" in res["summary"]
    assert "a.txt" in res["summary"]


def test_diff_summary_has_stat_and_condensed_diff(repo):
    (repo / "a.txt").write_text("two\n")
    res = git_ops.git_diff_summary(str(repo))
    assert "a.txt" in res["summary"]        # the --stat line
    assert "+two" in res["diff"]            # the condensed body
    assert "index" not in res["diff"]       # noise lines stripped


def test_diff_summary_clean_repo_says_no_changes(repo):
    res = git_ops.git_diff_summary(str(repo))
    assert "no changes" in res["summary"].lower()
    assert res["diff"] == ""


def test_diff_mentions_untracked_files(repo):
    (repo / "new.txt").write_text("hi\n")
    res = git_ops.git_diff_summary(str(repo))
    assert "untracked" in res["summary"].lower()
    assert "new.txt" in res["summary"]


def test_diff_is_truncated_at_max_chars(repo):
    (repo / "a.txt").write_text("\n".join(f"line {i}" for i in range(500)) + "\n")
    res = git_ops.git_diff_summary(str(repo), max_chars=200)
    assert len(res["diff"]) < 300
    assert "truncated" in res["diff"]


def test_not_a_repo_is_a_spoken_error(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    res = git_ops.git_status_summary(str(plain))
    assert "not a git repository" in res["error"]


def test_missing_or_empty_cwd_is_a_spoken_error(tmp_path):
    assert "error" in git_ops.git_status_summary("")
    assert "error" in git_ops.git_status_summary(str(tmp_path / "ghost"))


def test_detached_head_is_refused(repo):
    _git(repo, "checkout", "--detach")
    res = git_ops.git_status_summary(str(repo))
    assert "detached" in res["error"].lower()


def test_rebase_in_progress_is_refused(repo):
    (repo / ".git" / "rebase-merge").mkdir()
    res = git_ops.git_status_summary(str(repo))
    assert "rebase" in res["error"].lower()


def test_subfolder_of_a_larger_repo_is_refused(repo):
    sub = repo / "sub"
    sub.mkdir()
    res = git_ops.git_status_summary(str(sub))
    assert "root" in res["error"].lower()


def test_commit_preflight_counts_changes(repo):
    (repo / "a.txt").write_text("two\n")
    assert git_ops.commit_preflight(str(repo)) == {"branch": "main", "changes": 1}


def test_commit_preflight_clean_tree_refuses(repo):
    res = git_ops.commit_preflight(str(repo))
    assert "nothing to commit" in res["error"].lower()


def test_git_commit_commits_everything(repo):
    (repo / "a.txt").write_text("two\n")
    (repo / "new.txt").write_text("hi\n")
    res = git_ops.git_commit(str(repo), "voice: update files")
    assert res["branch"] == "main"
    assert "voice: update files" in res["summary"]
    out = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo),
                         capture_output=True, text=True)
    assert out.stdout.strip() == ""         # everything staged and committed
    log = subprocess.run(["git", "log", "-1", "--pretty=%s"], cwd=str(repo),
                         capture_output=True, text=True)
    assert log.stdout.strip() == "voice: update files"


def test_git_commit_requires_a_message(repo):
    (repo / "a.txt").write_text("two\n")
    assert "error" in git_ops.git_commit(str(repo), "  ")


def test_git_commit_clean_tree_is_a_spoken_error(repo):
    res = git_ops.git_commit(str(repo), "nothing here")
    assert "nothing to commit" in res["error"].lower()


def test_push_preflight_no_upstream_names_branch_and_suggests(repo):
    res = git_ops.push_preflight(str(repo))
    assert "upstream" in res["error"]
    assert "main" in res["error"]           # names the branch it refused to push


def test_git_push_to_a_bare_remote(repo, tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                   capture_output=True, text=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "--set-upstream", "origin", "main")
    (repo / "a.txt").write_text("two\n")
    git_ops.git_commit(str(repo), "update")
    res = git_ops.git_push(str(repo))
    assert "error" not in res
    assert "main" in res["summary"]
    log = subprocess.run(["git", "log", "-1", "--pretty=%s", "main"],
                         cwd=str(remote), capture_output=True, text=True)
    assert log.stdout.strip() == "update"


def test_git_push_never_passes_force(repo, monkeypatch):
    calls = []
    real = git_ops._run

    def spy(cwd, *args, **kw):
        calls.append(args)
        return real(cwd, *args, **kw)

    monkeypatch.setattr(git_ops, "_run", spy)
    git_ops.git_push(str(repo))     # fails on no-upstream; calls still recorded
    assert calls, "git_push made no git calls at all"
    assert all("--force" not in a and "-f" not in a for a in calls)


def test_timeout_becomes_a_spoken_error(repo, monkeypatch):
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="git", timeout=10)

    monkeypatch.setattr(git_ops.subprocess, "run", boom)
    res = git_ops.git_status_summary(str(repo))
    assert "timed out" in res["error"]
