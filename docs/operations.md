# Operations

## Install / upgrade

```
git clone https://github.com/sumedhchatse/recalq && cd recalq
./install.sh                     # into /opt/recalq; --prefix DIR, --no-services, --venv PATH
```

Installs committed code (never `.env`, data or logs), a virtualenv
(CPU-only torch when there's no GPU), the embedding model into
`<install>/models`, `.env` with a random Redis password, and three
`systemctl --user` services. Re-run from an updated checkout to upgrade;
`.env`, `client.yaml`, `data/` and `.venv` are kept. Steps that need root
are printed at the end, never run.

Prerequisites: Python ≥ 3.9 with venv (`sudo apt install python3-venv` on
Ubuntu), git, podman + podman-compose (or docker compose). Optional: `gh`
(for `/pr` to open PRs).

## Services

| Unit (`systemctl --user …`) | What |
|---|---|
| `recalq-redis` | Redis container via podman-compose (project `recalq`) |
| `recalq-embed` | embedding service on 127.0.0.1:8081 |
| `recalq-telegram` | Telegram bot + scheduled jobs (only if `TELEGRAM_BOT_TOKEN` is set) |

```
systemctl --user status recalq-redis recalq-embed recalq-telegram
systemctl --user restart recalq-telegram        # after editing .env / client.yaml
journalctl --user -u recalq-telegram -f         # bot + scheduler output
sudo loginctl enable-linger <service user>      # keep them running after logout / on boot
```

Only **one** process may poll a Telegram bot token at a time. Stop the old
bot before starting a new one anywhere else (Telegram returns 409 Conflict
otherwise).

The original dev setup (`~/memlayer`, `start.sh` / `stop.sh`, the
`recalq.service` user unit that runs `start.sh`) still works on its own;
don't run it and an `install.sh` install on the same machine — they use the
same ports.

## Logs

| Log | Where |
|---|---|
| engine (every surface) | `~/.recalq/memlayer.log` of whoever ran it (`RECALQ_LOG`) |
| services | `journalctl --user -u recalq-…` |
| dev setup | `telegram_bot.log`, `embedding_service.log` in the repo dir |

## Tests

```
./recalq test          # all suites, ~2 min, needs Redis; non-zero exit on failure
```

## Backups

```
./backup_cache.sh                      # snapshot Redis → ~/memlayer-backups/redis/dump_<date>.rdb (keeps 30)
./restore_cache.sh                     # list backups
./restore_cache.sh dump_<date>.rdb     # replace ALL Redis data with that snapshot
```

Redis holds the cache, knowledge base, conversation history, audit log and
spend counters — back it up. A nightly cron line:
`0 2 * * * /opt/recalq/backup_cache.sh >> ~/.recalq/backup.log 2>&1`.
Env overrides: `RECALQ_BACKUP_DIR`, `REDIS_CONTAINER` (default: the running
container with "redis" in its name).

Restore stops Redis, keeps the previous AOF as
`appendonlydir.before-restore-<date>`, loads the snapshot with AOF off,
rebuilds the AOF from it, and starts Redis again. (Before 2026-09-26 the
script only copied `dump.rdb`, which Redis ignores while AOF files exist —
restores silently did nothing.)

Files in `data/redis` are owned by the container's user; to copy or inspect
them from the host use `podman unshare cp …` / `podman unshare ls …`.

## Scheduled jobs

Defined under `schedules:` in `client.yaml`, run by the Telegram bot, posted
to a chat; `/jobs` lists them. Set `TZ` in `.env` if the server's timezone
isn't yours. Last-run times are in Redis, so restarts don't re-run a job.

## Model health

- `/status` — live check of every provider.
- `/bench` — pick the best provider for agent work (weekly job does this).
- Disable a dead provider with `enabled: false` in `client.yaml` rather than
  letting it eat fallback attempts.

## Adding a teammate

1. `sudo usermod -aG recalq <user>` (they log out/in once).
2. CLI: they `cd` into a project and run `recalq`.
3. Telegram: add their numeric id (they can get it by messaging the bot) to
   `TELEGRAM_ALLOWED_USERS`, restart `recalq-telegram`.
4. Optional: `RECALQ_ADMINS`, `RECALQ_BUDGET_USD`.
