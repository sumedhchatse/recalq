# Commands

Anything that isn't a command is a question. Phrasing it as an action
("fix the bug in x.py", "add a feature that…") runs the agent.

## CLI (`./recalq` inside a project directory)

| Command | Does |
|---|---|
| `/agent <task>` | agent reads, edits (diff + confirm) and runs commands in this project |
| `/pr <task>` | agent on a new `recalq/…` branch → commit → push + PR (needs a clean tree) |
| `/review [pr#\|base]` | review uncommitted changes, this branch vs `base`, or PR #n |
| `/diff` | what the last agent run changed |
| `/undo` | revert the last agent run; repeat to step further back (20 runs) |
| `/model <alias>` | switch model for this session (also used by `/agent`) |
| `/providers` | configured providers, key present?, currently skipped? |
| `/status` | live check: one tiny real call per provider |
| `/add` | add a provider interactively (writes `client.yaml` / `.env`) |
| `/project` | index this project's files for Q&A |
| `/doc <path>` | attach a PDF/DOCX/TXT/MD for questions |
| `/image <path> [question]` | ask about an image |
| `/stats` | cache hits / LLM calls for this project |
| `/usage` | per-person usage + cost + savings (admins see everyone) |
| `/cache` | recent cached questions |
| `/approve` / `/reject` | admins: keep the last answer as verified forever / delete it |
| `/bench` | benchmark providers on 4 agent tasks, set `agent_provider` |
| `/sync [source]` | sync knowledge sources into the knowledge base |
| `/plugins` | loaded plugins and their commands |
| `/reset` | forget this conversation |
| `/quit` | exit |

`./recalq test` runs the whole test suite (needs Redis up).

## Telegram

Same engine; the allowlist (`TELEGRAM_ALLOWED_USERS`) decides who gets
answers. Each chat has its own conversation, cache namespace and model.

| Command | Does |
|---|---|
| `/help` | command list |
| `/cd <path>` | point this chat's agent at a project directory on the server |
| `/agent <task>`, `/pr <task>`, `/review [...]`, `/diff`, `/undo` | as in the CLI; confirmations come as yes/no messages |
| `/model <alias>` | non-admins: only aliases from `client.yaml` |
| `/providers`, `/stats`, `/usage`, `/cache`, `/reset` | as in the CLI |
| `/approve`, `/reject`, `/bench`, `/sync [source]` | admins only |
| `/jobs` | scheduled jobs |

Send a document to attach it; send a photo (optional caption) to ask about it.

## MCP

Tools: `recalq_ask`, `recalq_ingest_document`, `recalq_scan_project`,
`recalq_ask_image` — each takes a `path` naming the project. Setup for
Claude Code, Cursor and VS Code is in the top-level README.
