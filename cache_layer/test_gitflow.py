"""assert-based self-check for gitflow (/pr, /review diff selection) on a
throwaway git repo — no network, no LLM. Run: python3 cache_layer/test_gitflow.py"""
import os
import tempfile
import subprocess
import agent
import gitflow

agent.CHECKPOINT_DIR = tempfile.mkdtemp()


def _repo():
    d = tempfile.mkdtemp()
    for cmd in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *cmd], cwd=d, check=True)
    open(os.path.join(d, "calc.py"), "w").write("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=d, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=d, check=True)
    return d


def _edit(d):
    def run_fn(task):
        agent._new_checkpoint(d)
        agent._run_tool("edit_file", {"path": "calc.py", "old_string": "x = 1", "new_string": "x = 2"},
                        d, lambda desc: True)
        return "changed x"
    return run_fn


def test_pr_commits_on_branch_and_returns_to_base():
    d = _repo()
    out = gitflow.run_as_pr("set x to 2", _edit(d), d)
    assert "Committed on branch recalq/set-x-to-2-" in out and "back on main" in out, out
    assert gitflow._git(d, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert open(os.path.join(d, "calc.py")).read() == "x = 1\n"      # base untouched
    assert gitflow._git(d, "status", "--porcelain") == ""
    branch = [b for b in gitflow._git(d, "branch", "--format=%(refname:short)").split() if b != "main"][0]
    assert gitflow._git(d, "show", f"{branch}:calc.py") == "x = 2"
    assert agent.undo(d) == []  # /undo must not replay the branch's edits onto main


def test_pr_refuses_dirty_tree_and_cleans_up_when_nothing_changed():
    d = _repo()
    open(os.path.join(d, "calc.py"), "w").write("dirty\n")
    assert "uncommitted changes" in gitflow.run_as_pr("x", _edit(d), d)
    subprocess.run(["git", "checkout", "-q", "calc.py"], cwd=d, check=True)
    out = gitflow.run_as_pr("nothing", lambda t: "nothing to do", d)
    assert "No files changed" in out, out
    assert gitflow._git(d, "branch", "--format=%(refname:short)") == "main"


def test_pr_agent_crash_keeps_partial_work_off_base():
    d = _repo()
    def boom(task):
        _edit(d)(task)
        raise RuntimeError("model died")
    out = gitflow.run_as_pr("crash", boom, d)
    assert "Agent failed: model died" in out and "Partial work committed" in out, out
    assert gitflow._git(d, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert open(os.path.join(d, "calc.py")).read() == "x = 1\n"


def test_review_diff_picks_uncommitted_or_branch():
    d = _repo()
    open(os.path.join(d, "calc.py"), "w").write("x = 3\n")
    open(os.path.join(d, "new_module.py"), "w").write("def brand_new(): pass\n")
    label, diff = gitflow.review_diff(d)
    assert label == "uncommitted changes" and "+x = 3" in diff
    assert "+def brand_new(): pass" in diff, diff  # untracked files are reviewed too
    os.remove(os.path.join(d, "new_module.py"))
    subprocess.run(["git", "switch", "-qc", "feat"], cwd=d, check=True)
    subprocess.run(["git", "commit", "-qam", "x3"], cwd=d, check=True)
    label, diff = gitflow.review_diff(d)
    assert label == "feat vs main" and "+x = 3" in diff, label
    open(os.path.join(d, "calc.py"), "w").write("x = 3\n")


if __name__ == "__main__":
    test_pr_commits_on_branch_and_returns_to_base()
    test_pr_refuses_dirty_tree_and_cleans_up_when_nothing_changed()
    test_pr_agent_crash_keeps_partial_work_off_base()
    test_review_diff_picks_uncommitted_or_branch()
    print("ok")
