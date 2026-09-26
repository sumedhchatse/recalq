"""assert-based regression checks for security fixes (sandbox escape via
.git, git option injection, redirect SSRF, checkpoint permissions, budget
bypass via raw /model) — no network, no LLM, needs Redis (imports the
Telegram bot). Run: python3 cache_layer/test_security.py"""
import os
import sys
import stat
import tempfile
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agent
import gitflow

agent.CHECKPOINT_DIR = os.path.join(tempfile.mkdtemp(), "ckpt")


def _repo():
    d = tempfile.mkdtemp()
    for cmd in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *cmd], cwd=d, check=True)
    open(os.path.join(d, "a.txt"), "w").write("x\n")
    subprocess.run(["git", "add", "-A"], cwd=d, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=d, check=True)
    return d


def test_sandbox_mounts_git_read_only():
    d = _repo()
    argv = agent._sandbox_argv("true", d)
    g = os.path.join(os.path.realpath(d), ".git")
    assert f"{g}:{g}:ro" in argv, argv


def test_agent_cannot_edit_git_internals():
    d = _repo()
    for path in (".git/hooks/pre-commit", ".git/config", "sub/../.git/HEAD"):
        out = None
        try:
            out = agent._run_tool("write_file", {"path": path, "content": "x"}, d, lambda desc: True)
        except ValueError as e:
            out = str(e)
        assert "inside .git" in out, (path, out)
    assert not os.path.exists(os.path.join(d, ".git", "hooks", "pre-commit"))


def test_pr_never_runs_repo_hooks():
    d = _repo()
    marker = os.path.join(d, "..", os.path.basename(d) + "_HOOK_RAN")
    hook = os.path.join(d, ".git", "hooks", "pre-commit")
    with open(hook, "w") as f:
        f.write(f"#!/bin/sh\ntouch {marker}\n")
    os.chmod(hook, 0o755)

    def run_fn(task):
        open(os.path.join(d, "a.txt"), "w").write("y\n")
        return "changed"
    assert "Committed on branch" in gitflow.run_as_pr("t", run_fn, d)
    assert not os.path.exists(marker), "a repo hook ran during /pr"


def test_review_target_cannot_be_a_git_option():
    d = _repo()
    out = os.path.join(tempfile.mkdtemp(), "INJ")
    for target in (f"--output={out}", "-p", "nosuchref"):
        try:
            gitflow.review_diff(d, target)
            assert False, target
        except gitflow.GitError:
            pass
    assert not any(n.startswith("INJ") for n in os.listdir(os.path.dirname(out)))


def test_web_fetch_checks_every_redirect_hop():
    class Resp:
        def __init__(self, loc=None):
            self.is_redirect, self.headers, self.text = bool(loc), {"location": loc or ""}, "secret"
        def raise_for_status(self):
            pass
    seen = []
    real_get, real_safe = agent._requests.get, agent._is_safe_url
    agent._requests.get = lambda url, **kw: seen.append(url) or Resp("http://169.254.169.254/meta")
    agent._is_safe_url = lambda url: "169.254" not in url
    try:
        assert agent.web_fetch("http://public.example/").startswith("error: refusing")
        assert seen == ["http://public.example/"], seen  # never requested the metadata IP
    finally:
        agent._requests.get, agent._is_safe_url = real_get, real_safe


def test_checkpoints_are_private():
    d = tempfile.mkdtemp()
    agent._new_checkpoint(d)
    assert stat.S_IMODE(os.stat(agent.CHECKPOINT_DIR).st_mode) == 0o700
    f = agent._ckpt_file(os.path.realpath(d))
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600


def test_telegram_raw_model_needs_admin():
    import telegram_bot
    sent = []
    telegram_bot.send = lambda chat, text: sent.append(text)
    telegram_bot.memlayer.ADMINS.discard("sec_user")
    telegram_bot.handle_command("sec_chat", "sec_user", "/model", "anthropic/claude-opus-4")
    assert "isn't a configured provider" in sent[-1] and "sec_chat" not in telegram_bot._chat_model
    alias = next(iter(telegram_bot.memlayer.provider_registry()))
    telegram_bot.handle_command("sec_chat", "sec_user", "/model", alias)
    assert telegram_bot._chat_model["sec_chat"] == alias


if __name__ == "__main__":
    test_sandbox_mounts_git_read_only()
    test_agent_cannot_edit_git_internals()
    test_pr_never_runs_repo_hooks()
    test_review_target_cannot_be_a_git_option()
    test_web_fetch_checks_every_redirect_hop()
    test_checkpoints_are_private()
    test_telegram_raw_model_needs_admin()
    print("ok")
