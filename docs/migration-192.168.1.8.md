# Migration plan: move Recalq to 192.168.1.8 under a dedicated `recalq` user

**From:** this dev machine — `~/memlayer`, run as `sumedh` by the
`recalq.service` user unit (`start.sh`) plus a hand-started Telegram bot.
**To:** `octopus` (192.168.1.8) — Ubuntu 24.04, 4 CPU, 7 GB RAM, 84 GB
free, no GPU, timezone UTC. Checked 2026-09-26 (read-only): podman 4.9.3,
podman-compose, Python 3.12, git present; `python3-venv` and `gh` missing;
lingering off; ports 6379/8081 free; `sudo` needs a password.

Expected downtime: ~10 minutes (Telegram bot + data copy). Rollback at any
point: see the end.

## Target layout — one service user, everything separated

| What | Where | Owner / access |
|---|---|---|
| Service account | user `recalq` (no password, reachable only via `sudo`) | runs every service |
| Code | `/opt/recalq` (installed from git; never edited in place) | `recalq`; group `recalq` read-only |
| Secrets + config | `/opt/recalq/.env` (0640), `client.yaml` | `recalq`; group read |
| Data (Redis) | `/opt/recalq/data/redis` | `recalq`'s containers only |
| Embedding model | `/opt/recalq/models` | group read |
| Projects the bot's agent works in | `/srv/projects` (setgid, 2770) | group `recalq`; never anyone's home |
| Services | `systemctl --user` units of `recalq`, lingering on | `recalq` |
| GitHub access for `/pr` from the bot | `gh` login / token of `recalq` (a bot account or fine-grained token) | separate from your personal login |
| Humans (you, teammates) | own accounts, members of group `recalq` | run `recalq` CLI as themselves; logs/undo in their own `~/.recalq` |

## Phase 1 — prepare the server (you, with sudo; ~5 min)

```bash
ssh 192.168.1.8
sudo apt update && sudo apt install -y python3-venv uidmap slirp4netns   # venv for install.sh; rootless podman helpers
# optional, for /pr to open PRs:  https://github.com/cli/cli/blob/trunk/docs/install_linux.md

sudo useradd -m -s /bin/bash recalq          # password stays locked
grep recalq /etc/subuid /etc/subgid         # must print a range for rootless podman (useradd adds it on 24.04)
sudo loginctl enable-linger recalq          # services start at boot and survive logout
sudo usermod -aG recalq sumedh              # + each teammate; log out/in once

sudo mkdir -p /opt/recalq /srv/projects
sudo chown recalq:recalq /opt/recalq /srv/projects
sudo chmod 2770 /srv/projects
sudo ln -sf /opt/recalq/recalq /usr/local/bin/recalq
```

## Phase 2 — install as `recalq` (no services yet; ~10 min, downloads ~1 GB)

```bash
sudo -iu recalq
export XDG_RUNTIME_DIR=/run/user/$(id -u)   # needed for systemctl --user / podman after sudo -iu
git clone https://github.com/sumedhchatse/recalq ~/src && cd ~/src
./install.sh --prefix /opt/recalq --projects /srv/projects --no-services
git config --global user.name "Recalq bot"; git config --global user.email "recalq@octopus"
```

## Phase 3 — configuration

From this machine, copy the secrets and the live config (the repo's
`client.yaml` may lag — `/bench` edits it locally):

```bash
scp ~/memlayer/.env      192.168.1.8:/tmp/recalq.env.old
scp ~/memlayer/client.yaml 192.168.1.8:/tmp/client.yaml.old
```

On the server, as `recalq`:

```bash
cd /opt/recalq
cp /tmp/client.yaml.old client.yaml
# .env: take the provider keys, TELEGRAM_*, RECALQ_ADMINS, AGENT_SANDBOX,
# LLM_REQUEST_TIMEOUT from the old file; KEEP the new file's HF_HOME line.
grep -vE '^(HF_HOME|REDIS_PASSWORD)=' /tmp/recalq.env.old > /tmp/keep.env
grep -E  '^(HF_HOME|REDIS_PASSWORD)=' .env >> /tmp/keep.env && mv /tmp/keep.env .env
echo 'TZ=Asia/Kolkata' >> .env               # server is UTC; schedules are "local time"
chmod 640 .env && rm /tmp/recalq.env.old /tmp/client.yaml.old
```

Edit `client.yaml`:
- `schedules:` → `project: /srv/projects/recalq` (was `/home/sumedh/memlayer`).
- `knowledge_sources:` → `path: docs` already works (relative to the install).

Projects and GitHub for the bot:

```bash
git clone https://github.com/sumedhchatse/recalq /srv/projects/recalq
gh auth login        # as recalq — use a bot account or fine-grained token, not your personal one
```

## Phase 4 — move the data (downtime starts)

On **this machine**:

```bash
kill $(cat ~/memlayer/.telegram.pid)                 # only ONE bot may poll the token
~/memlayer/backup_cache.sh                           # -> ~/memlayer-backups/redis/dump_<date>.rdb
scp ~/memlayer-backups/redis/dump_<date>.rdb 192.168.1.8:/tmp/
```

On the **server**, as `recalq`:

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
cd ~/src && ./install.sh --prefix /opt/recalq --projects /srv/projects   # now WITH services
cd /opt/recalq
mkdir -p ~/memlayer-backups/redis && mv /tmp/dump_<date>.rdb ~/memlayer-backups/redis/
RECALQ_RESTORE_YES=1 ./restore_cache.sh dump_<date>.rdb      # loads it, rebuilds AOF, restarts Redis
systemctl --user restart recalq-telegram
```

Re-running `install.sh` refreshes code and installs the services; `.env`,
`client.yaml`, `data/` and `.venv` are kept.

## Phase 5 — verify (downtime ends)

```bash
systemctl --user status recalq-redis recalq-embed recalq-telegram
/opt/recalq/recalq test                               # all suites pass
cd /srv/projects/recalq && recalq                     # then: /status, /usage (old history should be there), /quit
```

In Telegram: `/help`, `/usage` (same numbers as before), `/jobs`,
`/sync` (knowledge base from docs/), then ask "how do I restore a backup?".

As a teammate: `cd /srv/projects/recalq && recalq` → `signed in as '<them>'`.

## Phase 6 — retire the old setup (after a few days of the new one working)

On this machine:

```bash
systemctl --user disable --now recalq.service         # old start.sh-based stack
```

Keep `~/memlayer` and its Redis data for ~2 weeks as the rollback copy.

## Rollback

1. On the server: `systemctl --user stop recalq-telegram` (as `recalq`).
2. Here: `systemctl --user start recalq.service` and start the bot again
   (`nohup .venv/bin/python3 telegram_bot.py >> telegram_bot.log 2>&1 &`).

Anything written on the new server after cutover isn't in the old copy;
take a `backup_cache.sh` there first if it matters.

## Access from anywhere

- **Telegram** works from anywhere with no changes — the bot polls
  outward; nothing on the server is exposed.
- **CLI**: `ssh 192.168.1.8` then `recalq`. From outside the LAN use a VPN
  (WireGuard/Tailscale) — never open 6379 (Redis) or 8081 (embeddings) to
  the network; they're bound to 127.0.0.1 by design.

## Capacity

Each CLI session loads the embedding model in-process (roughly 0.5 GB RAM) in
addition to the shared embedding service. 7 GB comfortably holds the
services plus ~8 concurrent CLI users; beyond that, add RAM.
