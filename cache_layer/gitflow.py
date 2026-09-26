"""
gitflow.py — git-aware agent work: /pr runs an agent task on its own
branch, commits it and (if there's a GitHub remote + `gh`) opens a PR;
/review reviews a branch, PR or uncommitted diff with a read-only agent.

The user's own checkout is left exactly as it was: /pr refuses to start on
a dirty tree (so the commit holds only the agent's work) and switches back
to the original branch at the end.
"""
import os
import re
import time
import shutil
import subprocess

import agent

PR_PUSH = os.getenv("RECALQ_PR_PUSH", "1") == "1"
REVIEW_MAX_DIFF_CHARS = 60_000


class GitError(RuntimeError):
    pass


# Never run repo-controlled code: hooks and core.fsmonitor in .git would
# execute whatever the agent (or anything it ran) put there. Also means the
# team's own git hooks don't run on /pr commits.
_SAFE_GIT = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]


def _git(root, *args):
    proc = subprocess.run([*_SAFE_GIT, *args], cwd=root, capture_output=True, text=True)
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {(proc.stderr or proc.stdout).strip()[:300]}")
    return proc.stdout.strip()


def _slug(text, n=40):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:n].rstrip("-") or "task"


def _has_github_remote(root):
    try:
        return "github.com" in _git(root, "remote", "get-url", "origin") and bool(shutil.which("gh"))
    except GitError:
        return False


def run_as_pr(task, run_fn, root):
    """Run `run_fn(task) -> summary` on a fresh branch, commit what it
    changed, switch back, and push + open a PR when possible. Returns a
    plain-text report."""
    try:
        _git(root, "rev-parse", "--is-inside-work-tree")
    except GitError:
        return "Not a git repository — /pr needs git. Use /agent instead."
    if _git(root, "status", "--porcelain"):
        return ("You have uncommitted changes — commit or stash them first, so the PR "
                "contains only the agent's work.")
    base = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if base == "HEAD":
        return "Detached HEAD — check out a branch first."
    branch = f"recalq/{_slug(task)}-{int(time.time()) % 100000:05d}"
    _git(root, "switch", "-c", branch)

    summary, error = "", None
    try:
        summary = run_fn(task)
    except Exception as e:  # still commit partial work below, so nothing leaks onto `base`
        error = e
    try:
        changed = _git(root, "status", "--porcelain")
        if changed:
            _git(root, "add", "-A")
            _git(root, "commit", "-q", "-m", f"recalq: {task[:60]}",
                 "-m", (summary or f"(agent failed: {error})")[:3000])
        # The edits now live on `branch`; an /undo replaying them onto
        # `base` after we switch back would be wrong.
        agent.forget_last(root)
    finally:
        _git(root, "switch", "-q", base)

    if not changed:
        _git(root, "branch", "-D", branch)  # nothing on it — don't leave it behind
    if error:
        return (f"Agent failed: {error}" + (f"\nPartial work committed on branch {branch}."
                                            if changed else ""))
    if not changed:
        return f"{summary}\n\n(No files changed — no branch or PR created.)"

    report = f"{summary}\n\nCommitted on branch {branch} (you're back on {base})."
    if not (PR_PUSH and _has_github_remote(root)):
        return report + f"\nReview with: git diff {base}...{branch}"
    try:
        _git(root, "push", "-q", "-u", "origin", branch)
        proc = subprocess.run(
            ["gh", "pr", "create", "--head", branch, "--base", base,
             "--title", f"recalq: {task[:70]}", "--body", f"Task: {task}\n\n{summary[:5000]}"],
            cwd=root, capture_output=True, text=True)
        if proc.returncode != 0:
            return report + f"\nPushed, but opening the PR failed: {proc.stderr.strip()[:200]}"
        return report + f"\nPR: {proc.stdout.strip().splitlines()[-1]}"
    except GitError as e:
        return report + f"\nNot pushed: {e}"


def _default_base(root):
    try:
        return _git(root, "rev-parse", "--abbrev-ref", "origin/HEAD").split("/", 1)[1]
    except (GitError, IndexError):
        for b in ("main", "master"):
            try:
                _git(root, "rev-parse", "--verify", "-q", b)
                return b
            except GitError:
                pass
    return None


def review_diff(root, target=""):
    """(label, diff) to review: a PR number via gh, a base branch, or —
    with no target on the base branch itself — uncommitted changes."""
    target = target.strip().lstrip("#")
    if target.isdigit():
        proc = subprocess.run(["gh", "pr", "diff", target], cwd=root, capture_output=True, text=True)
        if proc.returncode != 0:
            raise GitError(f"gh pr diff {target}: {proc.stderr.strip()[:300]}")
        return f"PR #{target}", proc.stdout
    current = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    base = target or _default_base(root)
    if target:
        # Must name a real commit — otherwise "/review --output=/some/path"
        # is a git option, not a ref, and writes files wherever it points.
        if target.startswith("-"):
            raise GitError(f"'{target}' isn't a branch or commit")
        _git(root, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{target}^{{commit}}")
    if not base or base == current:
        # `git diff HEAD` skips untracked files — often the most important
        # part of a change (a whole new module) — so add them as new files.
        diff = _git(root, "diff", "HEAD")
        for path in _git(root, "ls-files", "--others", "--exclude-standard").splitlines():
            proc = subprocess.run([*_SAFE_GIT, "diff", "--no-index", "--", os.devnull, path],
                                  cwd=root, capture_output=True, text=True)
            diff += "\n" + proc.stdout  # exit code 1 just means "files differ"
        return "uncommitted changes", diff.strip()
    return f"{current} vs {base}", _git(root, "diff", f"{base}...HEAD")


REVIEW_PROMPT = (
    "You are reviewing a code change. Find real problems: bugs, security issues, broken "
    "edge cases, missing error handling, missing or wrong tests, breaking changes for "
    "callers. Use the tools to read the surrounding code and confirm a problem before you "
    "report it — no guesses. Skip style nitpicks. Reply with a short list, most severe "
    "first, each as 'file:line — what's wrong — why it matters'. If you find nothing real, "
    "say 'No issues found.'")


def review(root, model, target="", embedder=None, on_step=None, on_usage=None):
    try:
        label, diff = review_diff(root, target)
    except GitError as e:
        return f"Can't get the diff: {e}"
    if not diff.strip():
        return f"Nothing to review ({label} is empty)."
    cut = "" if len(diff) <= REVIEW_MAX_DIFF_CHARS else \
        f"\n... (diff truncated, {len(diff) - REVIEW_MAX_DIFF_CHARS} more chars)"
    msgs = [{"role": "system", "content": REVIEW_PROMPT},
            {"role": "user", "content": f"Review {label}:\n```diff\n"
                                         f"{diff[:REVIEW_MAX_DIFF_CHARS]}{cut}\n```"}]
    found = agent.answer(msgs, model, root=root, max_steps=12, max_tokens=1500,
                         embedder=embedder, on_step=on_step)
    if on_usage:
        on_usage(found["model"], found["tokens_used"])
    return f"Review of {label}:\n\n{found['answer']}"
