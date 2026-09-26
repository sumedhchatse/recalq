"""
scheduler.py — recurring jobs from client.yaml's `schedules:` list, run by
the Telegram bot (the one long-running Recalq process) and posted to a
chat. Example:

  schedules:
    - name: morning-digest
      when: "daily 09:00"            # daily HH:MM | weekly mon HH:MM | every 30m / every 6h
      kind: digest                   # digest | usage | bench | ask | pr
      project: /home/me/myproject    # digest / ask / pr
      chat: 795445523                # Telegram chat id to post to
      prompt: "..."                  # ask / pr only

Unattended runs can't answer confirmation prompts: file edits are allowed
only for `pr` (they land on a branch), and shell commands only run when
AGENT_SANDBOX is on.
"""
import os
import time
import threading
import subprocess
from datetime import datetime, timedelta

import yaml

_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_LAST_KEY = "recalq:schedule:last:"


def load_jobs(path):
    with open(path) as f:
        return (yaml.safe_load(f) or {}).get("schedules") or []


def last_slot(when, now):
    """The most recent moment <= now that `when` names, as a datetime."""
    parts = when.lower().split()
    if parts[0] == "every":
        n, unit = int(parts[1][:-1]), parts[1][-1]
        step = n * {"m": 60, "h": 3600}[unit]
        # Aligned to local midnight, not the epoch: with a non-whole-hour UTC
        # offset (e.g. +05:30) epoch alignment puts "every 6h" at :30.
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight + timedelta(seconds=(now - midnight).total_seconds() // step * step)
    hh, mm = map(int, parts[-1].split(":"))
    slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if parts[0] == "daily":
        return slot if slot <= now else slot - timedelta(days=1)
    if parts[0] == "weekly":
        slot -= timedelta(days=(now.weekday() - _DAYS.index(parts[1][:3])) % 7)
        return slot if slot <= now else slot - timedelta(days=7)
    raise ValueError(f"bad schedule '{when}' — use 'daily HH:MM', 'weekly mon HH:MM' or 'every 30m'")


def due(job, r, now=None):
    """True (and records the run) if `job` has a slot it hasn't run for yet.
    A job seen for the first time is only marked, not run, so adding one at
    15:00 with 'daily 09:00' waits for tomorrow instead of firing at once."""
    now = now or datetime.now()
    key = _LAST_KEY + job["name"]
    last = r.get(key)
    if last is None:
        r.set(key, now.timestamp())
        return False
    if float(last) < last_slot(job["when"], now).timestamp():
        r.set(key, now.timestamp())
        return True
    return False


def _no_shell_unless_sandboxed(desc):
    return "run:" not in desc  # edits yes (pr is on a branch); host shell never


def run_job(job, memlayer):
    kind, project = job["kind"], job.get("project") or os.getcwd()
    ns = memlayer.project_namespace(project)
    model = memlayer.default_model()
    if kind == "usage":
        return memlayer.usage_report(str(job.get("chat", "")))
    if kind == "bench":
        import bench
        return bench.run(embedder=memlayer.embedder)[0]
    if kind == "ask":
        return memlayer.ask(job["prompt"], model, namespace=ns, root=project,
                            user="scheduler")["answer"]
    if kind == "pr":
        return memlayer.run_agent_pr(job["prompt"], memlayer.agent_provider() or model,
                                     user="scheduler", namespace=ns, root=project,
                                     confirm=_no_shell_unless_sandboxed,
                                     embedder=memlayer.embedder)
    if kind == "digest":
        log = subprocess.run(["git", "log", "--since=24 hours ago", "--no-merges",
                              "--pretty=format:%h %an: %s", "--shortstat"],
                             cwd=project, capture_output=True, text=True).stdout.strip()
        if not log:
            return f"No commits in {os.path.basename(project)} in the last 24 hours."
        resp = memlayer.chat_completion(model, [{"role": "user", "content":
            "Summarize these git commits from the last 24 hours for the team in a few short "
            "plain-text bullet points: what changed and anything risky. No markdown "
            f"headers.\n\n{log[:20000]}"}], max_tokens=600)
        return f"Last 24h in {os.path.basename(project)}:\n{resp.choices[0].message.content}"
    raise ValueError(f"unknown job kind '{kind}'")


def tick(jobs, memlayer, send, log):
    """Start every due job in its own thread; `send(chat, text)` posts the
    result. Call this often (the bot's poll loop does, ~every 30s)."""
    for job in jobs:
        try:
            if not due(job, memlayer.r):
                continue
        except Exception as e:
            log.error(f"schedule '{job.get('name')}' is invalid: {e}")
            continue

        def _run(job=job):
            log.info(f"running scheduled job '{job['name']}'")
            try:
                text = run_job(job, memlayer)
            except Exception as e:
                text = f"failed: {e}"
            send(job["chat"], f"⏰ {job['name']}\n\n{text}")
        threading.Thread(target=_run, daemon=True).start()
