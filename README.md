# Recalq

A terminal-first memory/cache layer over any LLM (or a local Ollama model).
No web UI, no license check, no proxy server. Talk to it from the CLI, from
Telegram, or as an MCP tool inside Claude Code.

## 1. API keys — where they go

Two files, two different jobs:

- **`.env`** (repo root, gitignored) — the actual secrets. One line per key:
  ```
  GEMINI_API_KEY=...
  ANTHROPIC_API_KEY=...
  GROQ_API_KEY=...
  NVIDIA_MODEL_API_KEY=...
  KIMI_K3_API_KEY=...
  REDIS_PASSWORD=...
  TELEGRAM_BOT_TOKEN=...
  TELEGRAM_ALLOWED_USERS=...
  ```
- **`client.yaml`** (repo root) — the provider *registry*. Says which
  providers exist, which model each uses, which env var holds its key, and
  which provider to fall back to if it fails. Add a new provider by adding
  a block here:
  ```yaml
  providers:
    openai:
      enabled: true
      provider_type: openai
      model: gpt-4o
      api_key_env: OPENAI_API_KEY
      fallback_to: gemini
  ```
  Then put `OPENAI_API_KEY=...` in `.env`. No restart of any proxy needed —
  `client.yaml` is read directly, live, on the next call.

You are not limited to what's in `client.yaml` — the CLI and MCP server also
accept any raw litellm model string on the fly (e.g. `ollama/llama3.1` for a
local model, no API key needed at all). Type `providers` in the CLI (or
`/providers` in Telegram) to see what's currently configured and whether
each one's key is actually set.

## 2. Attaching documents and images

**Documents** (PDF / DOCX / TXT, up to 25MB CLI / 20MB Telegram) get
chunked, embedded, and stored in Redis so later questions can be answered
from them (RAG) instead of guessing.

- **CLI**: `doc /path/to/file.pdf` — ingests it and remembers it as "the
  current doc" for the rest of the session. Just ask questions normally
  after.
- **Telegram**: send the file as a normal Telegram document attachment.
  The bot ingests it and replies with the chunk count. Questions you send
  afterward in that chat use it automatically.

**Images** go straight to a vision-capable model (default: Gemini) — no
OCR, no text extraction, the model actually looks at the picture.

- **CLI**: `image /path/to/photo.jpg what's in this?` (question is
  optional — png/jpg/jpeg/gif/webp).
- **Telegram**: send a photo, with an optional caption as your question.

Documents and images are handled separately — a document teaches the
engine facts to cite later; an image is a one-off "look at this" call and
isn't cached.

## 3. Running it

### Backend (Redis + embedding service)

```
cd ~/memlayer
./start.sh      # starts Redis (podman) + the embedding service
./stop.sh       # stops both
```

### CLI

```
cd ~/memlayer && source .venv/bin/activate
./recalq
```
Commands inside: `quit`, `stats`, `cache`, `providers`, `model <name>`,
`doc <path>`, `image <path> [question]`. Anything else is a question.

### Telegram bot

One-time setup — see the "Getting a bot token" section below, then:

```
cd ~/memlayer && source .venv/bin/activate
nohup python3 telegram_bot.py > telegram_bot.log 2>&1 &
disown
pgrep -f "^python3 telegram_bot.py$" > .telegram.pid
```

Stop it:
```
kill $(cat ~/memlayer/.telegram.pid)
```

### MCP — Claude Code, Cursor, VS Code, any MCP client

The MCP server gives an IDE's own agent Recalq's shared team cache,
project-grounded answers and ingested docs (tools: `recalq_ask`,
`recalq_ingest_document`, `recalq_scan_project`, `recalq_ask_image`).
Use the venv's python so dependencies resolve.

Claude Code:
```
claude mcp add recalq -- ~/memlayer/.venv/bin/python3 ~/memlayer/recalq_mcp.py
```

Cursor — `~/.cursor/mcp.json`:
```json
{"mcpServers": {"recalq": {"command": "/home/you/memlayer/.venv/bin/python3",
                           "args": ["/home/you/memlayer/recalq_mcp.py"]}}}
```

VS Code (Copilot agent mode) — `.vscode/mcp.json` in your project:
```json
{"servers": {"recalq": {"type": "stdio",
                        "command": "/home/you/memlayer/.venv/bin/python3",
                        "args": ["/home/you/memlayer/recalq_mcp.py"]}}}
```

### Slack / Teams — opt-in, not yet built

`slack_bot.py` and `teams_bot.py` are scaffolds: config-gated the same way
Telegram is (unset the required `.env` vars and they refuse to start), but
the actual connector logic isn't implemented yet. Run either one for setup
steps and what's needed to finish it — Slack's docstring explains why it's
a smaller task (Socket Mode, one new dependency) than Teams' (needs a
public HTTPS endpoint + Azure Bot Service registration).

### Installing on a server

```
git clone https://github.com/sumedhchatse/recalq && cd recalq
./install.sh                    # into /opt/recalq (prints the one sudo step if needed)
```

It installs the code, a virtualenv and the embedding model (shared by
everyone, in `models/`), creates `.env` from `.env.example` with a random
Redis password, and starts `recalq-redis`, `recalq-embed` and (if a bot
token is set) `recalq-telegram` as `systemctl --user` services. Re-run it
to upgrade — `.env`, `client.yaml` and data are kept. It ends by printing
the root-only steps for a team: a `recalq` group, the
`/usr/local/bin/recalq` symlink, and `loginctl enable-linger`.

### Team mode (one shared host)

Put Recalq on one server; teammates SSH in, `cd` into a project and run
`./recalq`. Everyone in the same project directory shares its cache and
ingested docs (a question one person already paid for is free for the
next); conversations stay per person. Identity is the OS login
(`RECALQ_USER` overrides it) or the Telegram user id.

- `./recalq test` — runs the whole test suite (needs Redis up; exits
  non-zero on any failure, so it can gate a commit or CI).
- `/usage` — per-person queries, cache hits, tokens used/saved and cost
  (from `cost_per_1k_tokens` in `client.yaml`). Admins see the whole team.
- `RECALQ_ADMINS=alice,795445523` in `.env` — who counts as admin.
- `/undo` — reverts the files the last `/agent` run changed.
- Cached answers that were based on project files remember those files'
  hashes; once any of them changes, the answer is dropped instead of
  served stale.
- Cache hits say who first asked ("first asked by alice 2d ago") and, if
  an admin vouched for it, "verified by …". Admins: `/approve` keeps the
  last answer permanently as verified, `/reject` deletes it.
- `/agent` remembers runs that changed files, per project; a similar new
  task starts from that run's summary instead of re-exploring.
- `/usage` ends with a savings line: what every token would have cost at
  your priciest model (`RECALQ_REFERENCE_COST_PER_1K` overrides) vs. what
  was actually spent.
- `RECALQ_BUDGET_USD=5` — monthly spend cap per person. Over it, they
  still get cache hits and free (cost 0) models, never paid ones.
- A provider that fails is skipped for `PROVIDER_COOLDOWN` seconds (300)
  and the next cheapest healthy one answers instead. Shared through Redis,
  so the CLI, Telegram bot and MCP server all skip it at once.
- `/agent` reads `AGENTS.md` (or `RECALQ.md` / `CLAUDE.md`) from the
  project root — the same file Codex/Claude Code use for project
  conventions, test commands, and what not to touch.
- `/diff` shows what the last agent run changed; `/undo` reverts it, and
  repeating `/undo` steps further back (up to 20 runs).
- `AGENT_SANDBOX=podman` (or `docker`) — agent shell commands run in a
  throwaway container: no network, only the project dir visible, memory
  and process caps — and so without asking. `AGENT_SANDBOX_IMAGE` picks
  the image (default `python:3.12-slim`; it needs your project's
  toolchain). The project dir itself is writable from inside.
- Long agent runs shrink old tool output past `AGENT_MAX_CONTEXT_CHARS`
  (100000) so small models don't overflow.
- `agent_provider:` in `client.yaml` — model for `/agent` (default
  `gemini_flash`), separate from the chat model; `/model` overrides it.
- `/agent` can hand read-only investigations to `explore` sub-agents,
  each with its own fresh context; several run in parallel, and
  read-only tool calls in the same turn run concurrently too.
- Checkpoints are saved under `~/.recalq/checkpoints/`
  (`AGENT_CHECKPOINT_DIR`), so `/undo` works after a restart and across
  the CLI and Telegram.
- `/pr <task>` — runs the agent on a new `recalq/…` branch, commits, and
  (with a GitHub remote + `gh`) pushes and opens a PR; you stay on your
  branch. Needs a clean working tree. `RECALQ_PR_PUSH=0` = commit only.
- `/review` — reviews uncommitted changes (incl. new files), `/review main`
  the current branch vs main, `/review 42` PR #42 (via `gh`).
- `/bench` — runs two small agent tasks on every ready provider (no
  fallback, model-written code is never executed on the host) and sets
  `agent_provider` to the best one that passes both.
- `schedules:` in `client.yaml` — recurring digest / usage / bench / ask /
  pr jobs run by the Telegram bot and posted to a chat (`/jobs` lists them).
- `AGENT_AUTO_ALLOW="pytest*,python3 test_*,npm test"` — shell commands
  `/agent` may run without asking (anything with `; & | $ > <` or `..`
  still asks).

## 4. Status checks

| What | Command |
|---|---|
| Redis container | `podman ps --filter name=memlayer_redis_1` |
| Redis actually answering | `podman exec memlayer_redis_1 redis-cli -a "$REDIS_PASSWORD" ping` |
| Embedding service | `curl -s http://localhost:8081/health` |
| Telegram bot running | `pgrep -af "python3 telegram_bot.py"` |
| Telegram bot log (live) | `tail -f ~/memlayer/telegram_bot.log` |
| Engine log (live) | `tail -f ~/memlayer/memlayer.log` |
| Which providers have a key set | `providers` in the CLI, or `/providers` in Telegram |
| Cache/usage stats | `stats` in the CLI, or `/stats` in Telegram |

## 5. Getting a Telegram bot token

1. In Telegram, message **@BotFather** → `/newbot` → give it a name, then a
   username ending in `bot`.
2. It replies with a token (`123456:AA...`). Put it in `.env` as
   `TELEGRAM_BOT_TOKEN=`.
3. Message your new bot once (anything), then run:
   ```
   python3 telegram_bot.py --whoami
   ```
   This prints your numeric Telegram user ID.
4. Put that ID in `.env` as `TELEGRAM_ALLOWED_USERS=<id>` (comma-separate
   more IDs to allow more people). **Without this set the bot answers no
   one** — every message costs LLM tokens, so it default-denies until you
   explicitly allow someone.
5. Start the bot (see section 3).

## 6. Troubleshooting

**CLI/bot hangs for a long time then eventually answers or errors.**
A provider request is slow or the network route to it is bad. Each call is
bounded to `LLM_REQUEST_TIMEOUT` seconds (default 30, set it in `.env` to
change) before falling back to the next provider in `client.yaml`'s
`fallback_to` chain, or giving up. It will never hang forever — if it does,
that's a bug, not expected behavior.

**"provider 'X' needs $Y_API_KEY set".**
That provider's key is missing from `.env`. Either add it, or switch to a
provider that has one (`providers` / `/providers` shows which are ready).

**A specific provider keeps failing with an auth error.**
The key in `.env` for that provider is wrong/expired — check it against the
provider's dashboard. (At last check, `ANTHROPIC_API_KEY` in this repo's
`.env` was rejecting as invalid — regenerate it if you plan to use Claude.)

**Everything is slow / times out on every provider.**
Check basic connectivity: `curl -sv https://api.groq.com` (or whichever
provider). If TCP connects fine but requests still hang, check for a broken
IPv6 route — `cache_layer/providers.py` already forces IPv4-only resolution
for exactly this reason, but if you see the same symptom outside Python
(e.g. `curl -6` also hangs while plain `curl` doesn't), it's your network,
not Recalq.

**Telegram bot exits immediately.**
`TELEGRAM_BOT_TOKEN` isn't set in `.env`, or two copies are running at once
(Telegram allows only one long-poll connection per token — you'll see
`HTTP Error 409: Conflict` in `telegram_bot.log`). Fix:
`pkill -f "python3 telegram_bot.py"` then start it again.

**Bot replies "Not authorized" / "no allowlist configured".**
`TELEGRAM_ALLOWED_USERS` in `.env` doesn't include your numeric ID, or is
empty. See section 5, step 4.

**Document upload says "Unsupported file type".**
Only PDF, DOCX, and TXT are supported — no OCR, no images-as-documents (use
the image/photo path for those instead).

**Redis connection errors.**
`./start.sh` didn't run, or the container died. Check
`podman ps -a --filter name=memlayer_redis_1` and `podman logs
memlayer_redis_1`; restart with `./stop.sh && ./start.sh`.

**Embedding service down.**
`memlayer.py` falls back to loading the embedding model in-process
automatically (slower per-request, but it keeps working). Check
`curl -s http://localhost:8081/health`; restart it standalone with
`uvicorn embedding_service:app --port 8081` from the repo root if needed.
