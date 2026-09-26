# Architecture

## Components

| Piece | File(s) | Runs as |
|---|---|---|
| Engine | `cache_layer/memlayer.py` | imported by every surface below |
| CLI (REPL) | `cache_layer/cli.py`, launched by `./recalq` | the user who runs it |
| Telegram bot + scheduler | `telegram_bot.py`, `cache_layer/scheduler.py` | a long-running service |
| MCP server | `recalq_mcp.py` (stdio) | spawned by the MCP client |
| Coding agent | `cache_layer/agent.py` | inside whichever surface called it |
| Git flow (`/pr`, `/review`) | `cache_layer/gitflow.py` | same |
| Model routing | `cache_layer/providers.py` + `client.yaml` | same |
| Knowledge sync | `cache_layer/knowledge.py` | same |
| Model benchmark | `cache_layer/bench.py` | same |
| Redis | container (`podman-compose.yml`) | service, 127.0.0.1:6379 |
| Embedding service | `embedding_service.py` (FastAPI, all-MiniLM-L6-v2) | service, 127.0.0.1:8081 |

Nothing listens on a public interface. The Telegram bot polls Telegram
outward; no inbound port is needed anywhere.

## How a question is answered (`ask()`)

1. **Guardrails** — PII is redacted (or the query blocked) before anything
   else sees it.
2. **Exact cache** — O(1) lookup of the normalised question in this
   project's namespace, then the shared `commons` pool.
3. **Semantic cache** — embedding similarity ≥ `SIMILARITY_CUTOFF`, plus
   concept-overlap, polarity ("enable" vs "disable"), number, version,
   entity and intent checks so a similar-but-different question is not
   served a wrong answer. A cached answer that was based on project files
   is dropped if any of those files changed since (content hashes).
4. **Org knowledge base** (`sop` namespace) — documents synced from
   `knowledge_sources:` or uploaded; answered with citations. Category
   `policy` is admin-only.
5. **Attached documents** — a PDF/DOCX/TXT/MD attached in this chat.
6. **LLM** — with read-only tools (`search_code`, `grep_code`, `read_file`,
   `list_dir`) so it can look at the project itself. The answer is cached.

Action-shaped asks ("fix the bug in x.py") go to the **agent** instead,
which can also edit files and run commands (confirmed, or sandboxed).

## Model routing

`chat_completion()` tries, in order: the requested provider, its
`fallback_to` chain, then every other ready provider cheapest-first — at
most `PROVIDER_MAX_ATTEMPTS`. A provider that fails is skipped for
`PROVIDER_COOLDOWN` seconds, shared by all processes through Redis. Over a
person's monthly budget, only free (cost 0) providers are used.

## Where data lives

Redis keys (all in one Redis, password-protected, localhost only):

| Key prefix | What |
|---|---|
| `ml:v1:<namespace>:<id>` | cached answers (namespace = project path hash, `commons`, or a Telegram chat) |
| `ml:v1:<ns>:exact:*` | exact-match index |
| `ml:v1:<ns>:__history__` | conversation history (CLI: per project *and* person) |
| `ml:v1:<ns>:__agent_memory__` | past agent runs that changed files |
| `ml:v1:<ns>:__meta__` | per-namespace counters for `/stats` |
| `recalq:doc:<ns>:*`, `recalq:docs:<ns>` | document chunks + index (`sop` = org knowledge base) |
| `recalq:ksync:<source>` | knowledge-sync file hashes |
| `recalq:audit:log` | last 5000 requests (who, model, tokens, cost source) |
| `recalq:spend:<user>:<YYYY-MM>` | monthly $ per person (budgets) |
| `recalq:cooldown:<provider>` | providers being skipped |
| `recalq:schedule:last:<job>` | last run of each scheduled job |

On disk, per person: `~/.recalq/memlayer.log`, `~/.recalq/checkpoints/`
(agent undo history, 0700), `~/.recalq/sources/` (cloned git knowledge
sources). In the install: `.env`, `client.yaml`, `models/` (embedding
model), `data/redis/` (Redis RDB + AOF).
