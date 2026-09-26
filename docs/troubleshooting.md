# Troubleshooting

Every entry below is something that actually happened on this project.
First moves for anything: `/status` (live provider check), the log
(`~/.recalq/memlayer.log`, or `journalctl --user -u recalq-telegram`), and
`./recalq test`.

## Model / provider errors

| Symptom (in `/status` or the log) | Cause | Fix |
|---|---|---|
| `AuthenticationError ... API key is invalid` | key revoked/wrong | regenerate at the provider, update `.env`, restart |
| `RateLimitError ... suspended due to insufficient balance` | prepaid account empty (seen: kimi-k3) | recharge, or `enabled: false` so it stops using a fallback slot |
| `RateLimitError` / 429 `exceeded your current quota` | free-tier daily quota (seen: gemini_flash after heavy testing) | wait for reset; routing already skips it for `PROVIDER_COOLDOWN` |
| `410 Gone` | model decommissioned (seen: gpt-oss-120b, llama-3.3-70b, deepseek-v4-flash on NVIDIA) | pick a current model id from the provider's catalog |
| NVIDIA `404 Function not found` for a model that is in the catalog | your account isn't entitled to that model | request access on build.nvidia.com — not a config fix |
| `provider did not respond within 30s` | slow/overloaded model, or a reasoning model thinking long | raise `LLM_REQUEST_TIMEOUT` (90 works for nemotron on agent tasks) |
| Groq `Failed to parse tool call arguments as JSON` | the model emitted a malformed tool call | transient; routing moves to the next provider. Frequent → use another `agent_provider` |
| Every provider failing at once | quotas exhausted, network down | `/status`; add a local Ollama provider as the last fallback |

A failing provider is skipped for 5 minutes by every Recalq process (shared
via Redis), so one bad provider costs at most one timeout per 5 minutes.

## Hangs

- **Calls hang for minutes with no error** — a host whose IPv6 route is a
  black hole. `providers.py` forces IPv4 resolution process-wide for this
  reason; if you import litellm somewhere else directly, it won't have that.
- **Startup hangs importing litellm** — it fetches a pricing table from
  GitHub on import; `LITELLM_LOCAL_MODEL_COST_MAP=True` (set in
  `providers.py`) uses the bundled copy.

## Recalq won't start

| Symptom | Cause | Fix |
|---|---|---|
| `int() argument ... 'NoneType'` at `CACHE_TTL_SECS` | old code + no `CACHE_TTL` in `.env` | upgrade (default now 1 year) or set `CACHE_TTL=31536000` |
| `FileNotFoundError ... memlayer.log` | old code logged to `~/memlayer/` | upgrade (logs go to `~/.recalq/`) |
| Hugging Face "offline mode" / model not found | Recalq runs HF offline; model not downloaded for this user | `HF_HOME` in `.env` must point at the install's `models/` (install.sh does this) |
| `redis.exceptions.ConnectionError` / `AuthenticationError` | Redis not running / wrong `REDIS_PASSWORD` | `systemctl --user status recalq-redis`; password in `.env` must match the one Redis started with |
| install: `python3 can't create virtualenvs` | Ubuntu without `python3-venv` | `sudo apt install python3-venv` |
| install: `port 6379 is already in use` | another Recalq (e.g. the `~/memlayer` dev setup) is running | stop it; one Recalq per machine |

## Telegram

| Symptom | Fix |
|---|---|
| Bot replies "Not authorized … your id (N)" | add N to `TELEGRAM_ALLOWED_USERS`, restart the bot |
| Bot replies "no allowlist configured" | set `TELEGRAM_ALLOWED_USERS` |
| Bot silent; log shows `409 Conflict` | two processes polling the same token — stop the old one |
| Scheduled job at the wrong hour | server is UTC: set `TZ=Asia/Kolkata` (or yours) in `.env`, restart |
| `/pr` from the bot commits but opens no PR | `gh` not installed / not logged in *as the service user*, or not on its PATH |

## Answers

| Symptom | Cause / fix |
|---|---|
| Got someone else's / an unrelated answer | a follow-up ("what about that?") matched a cached answer from another conversation. `is_context_dependent()` catches most; report the phrasing so it can be added. `/reject` removes that entry |
| Answer about code is outdated | answers built from project files are dropped when those files change; answers from general knowledge aren't — `/reject` it |
| "Not found in company knowledge" | nothing in the knowledge base matched; add a doc / source and `/sync` |
| A knowledge doc isn't used | check `/sync` output for errors (unsupported type, empty text); category `policy` is admins-only |

## Agent

| Symptom | Cause / fix |
|---|---|
| Model writes out its plan/reasoning but changes nothing | model quality (seen with nemotron and fallbacks on a README edit). `/bench` and set a better `agent_provider`; a paid model helps most |
| Model says it fixed something in plain Q&A | the read-only path can't write; it's told to say so. Ask as an action or use `/agent` |
| Also "fixed" things it wasn't asked to | weaker models (seen: gemini flash-lite). The prompt forbids it; switch model |
| `crun: controller 'cpu' is not available` | old sandbox code used `--cpus`; rootless podman only has memory+pids delegated. Upgrade |
| Sandbox: `pip install` / downloads fail | by design — no network. Use an `AGENT_SANDBOX_IMAGE` that already has your deps |
| Sandbox: command not found | image lacks the toolchain (default is python:3.12-slim) — set `AGENT_SANDBOX_IMAGE` |
| `/pr`: "You have uncommitted changes" | commit or stash first, so the PR holds only the agent's work |
| `/undo`: nothing to undo | no agent edits recorded for this project (shell-made changes aren't tracked — use git) |
| Agent refuses to edit `.git/...` | intentional (sandbox-escape guard) |

## Data

| Symptom | Fix |
|---|---|
| `restore_cache.sh` "worked" but data unchanged | old script; Redis ignores `dump.rdb` while AOF files exist. Upgrade — it now rebuilds AOF from the snapshot |
| `Permission denied` under `data/redis` | files belong to the container user — use `podman unshare cp/ls/rm` |
| `/usage` lists `unknown` | requests from before per-person identity existed |
