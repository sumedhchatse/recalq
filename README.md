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

### MCP (use it as a tool from Claude Code)

```
claude mcp add recalq -- python3 ~/memlayer/recalq_mcp.py
```

### Slack / Teams — opt-in, not yet built

`slack_bot.py` and `teams_bot.py` are scaffolds: config-gated the same way
Telegram is (unset the required `.env` vars and they refuse to start), but
the actual connector logic isn't implemented yet. Run either one for setup
steps and what's needed to finish it — Slack's docstring explains why it's
a smaller task (Socket Mode, one new dependency) than Teams' (needs a
public HTTPS endpoint + Azure Bot Service registration).

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
