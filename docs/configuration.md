# Configuration

Two files, both in the install directory:

- **`.env`** — secrets and per-server settings. Never committed. Created
  from `.env.example` by `install.sh`. Mode 0600 (0640 with a team group).
- **`client.yaml`** — providers, models, schedules, knowledge sources.
  Committed; `install.sh` keeps your copy on upgrade.

## `client.yaml`

### `providers:`

```yaml
providers:
  groq:
    enabled: true
    provider_type: groq            # any litellm provider: openai, anthropic, gemini, groq,
                                   # mistral, cohere, azure, bedrock, ollama, openrouter, ...
    model: openai/gpt-oss-120b
    api_key_env: GROQ_API_KEY      # the .env variable holding the key (omit for ollama)
    api_base: https://...          # only for OpenAI-compatible endpoints / local servers
    fallback_to: gemini            # tried first if this one fails
    display_name: "Groq (fast)"
    cost_per_1k_tokens: 0.0        # drives routing (cheapest first), budgets and /usage cost
```

Disable a provider with `enabled: false` (keep a comment saying why — see
`kimi-k3`). Add one interactively with `/add` in the CLI.

### Model choice

| Key | Meaning |
|---|---|
| `auto_provider` | default chat model |
| `agent_provider` | model for `/agent`, `/pr`, `/review` (`/bench` sets this) |
| `architect_provider` | optional: a model that plans before `/agent` edits (one extra call) |

`/model <alias>` overrides for your session. On Telegram, non-admins can
only pick aliases from `client.yaml`.

### `schedules:`

```yaml
schedules:
  - name: morning-digest
    when: "daily 09:00"      # daily HH:MM | weekly mon HH:MM | every 30m | every 6h (server local time)
    kind: digest             # digest | usage | bench | sync | ask | pr
    project: /srv/projects/recalq   # digest / ask / pr
    chat: 795445523          # Telegram chat to post to
    prompt: "..."            # ask / pr
    source: recalq-docs      # sync: one source (omit = all)
```

Run by the Telegram bot. A new job waits for its next slot instead of
firing immediately. `pr` jobs edit only on a branch; their shell commands
run only if `AGENT_SANDBOX` is on.

### `knowledge_sources:`

See [knowledge-sources.md](knowledge-sources.md).

## `.env`

| Variable | Default | Meaning |
|---|---|---|
| provider keys (`GROQ_API_KEY`, ...) | — | named by `api_key_env` in `client.yaml` |
| `REDIS_PASSWORD` | — (install generates) | Redis auth |
| `REDIS_HOST` / `REDIS_PORT` | localhost / 6379 | |
| `TELEGRAM_BOT_TOKEN` | — | bot token from @BotFather |
| `TELEGRAM_ALLOWED_USERS` | — | comma-separated Telegram user ids; nobody else gets answers |
| `RECALQ_ADMINS` | — | OS logins / Telegram ids who see all `/usage`, `/approve`, `/bench`, `/sync` |
| `RECALQ_BUDGET_USD` | none | monthly $ per person; over it → free models only |
| `RECALQ_USER` | OS login | override the CLI identity |
| `RECALQ_REFERENCE_COST_PER_1K` | priciest provider | price used for the `/usage` savings line |
| `AGENT_SANDBOX` | off | `podman` or `docker`: agent shell commands in a no-network container, no prompt |
| `AGENT_SANDBOX_IMAGE` | python:3.12-slim | must contain your project's toolchain |
| `AGENT_AUTO_ALLOW` | — | globs of shell commands the agent may run unasked without sandbox (`pytest*,npm test`) |
| `AGENT_WEB_FETCH` | 0 | 1 lets the agent fetch public URLs (SSRF-guarded) |
| `AGENT_MAX_CONTEXT_CHARS` | 100000 | older tool output is trimmed past this |
| `AGENT_CHECKPOINT_DIR` | ~/.recalq/checkpoints | `/undo` history |
| `RECALQ_PR_PUSH` | 1 | 0 = `/pr` commits but never pushes |
| `RECALQ_SOURCES_DIR` | ~/.recalq/sources | where git knowledge sources are cloned |
| `LLM_REQUEST_TIMEOUT` | 30 | seconds per model call; 90 suits reasoning models on agent tasks |
| `PROVIDER_COOLDOWN` | 300 | seconds a failed provider is skipped |
| `PROVIDER_MAX_ATTEMPTS` | 3 | providers tried per call |
| `CACHE_TTL` | 31536000 | cache entry lifetime (s); approved answers never expire |
| `SIMILARITY_CUTOFF` | 0.78 | semantic cache threshold |
| `VISION_MODEL` | gemini | model for image questions |
| `TZ` | system | timezone for scheduled jobs (servers are often UTC) |
| `HF_HOME` | install sets `<install>/models` | shared embedding model location |
| `RECALQ_LOG` | ~/.recalq/memlayer.log | per-person log file |
| `RECALQ_BACKUP_DIR` | ~/memlayer-backups/redis | used by backup/restore scripts |

`.env` is loaded with override on, and litellm also loads it on import —
so a value in `.env` wins over the same variable exported in your shell.
