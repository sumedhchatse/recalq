# Recalq documentation

Recalq is a self-hosted, multi-model team assistant: a shared semantic
cache + memory + knowledge base over any LLM (or a local model), with a
coding agent, reachable from a terminal CLI, Telegram, and MCP clients
(Claude Code, Cursor, VS Code).

| Doc | Read it when |
|---|---|
| [architecture.md](architecture.md) | you want to know how a question flows through the system and where data lives |
| [configuration.md](configuration.md) | you're editing `client.yaml` or `.env` |
| [commands.md](commands.md) | you want every CLI / Telegram command in one place |
| [operations.md](operations.md) | install, start/stop, logs, backups, upgrades, scheduled jobs |
| [knowledge-sources.md](knowledge-sources.md) | you want Recalq to answer from your team's docs |
| [troubleshooting.md](troubleshooting.md) | something's wrong — symptoms, causes and fixes we've actually hit |
| [security.md](security.md) | you want the trust model, what's protected, and what isn't |
| [migration-192.168.1.8.md](migration-192.168.1.8.md) | moving Recalq to the team server under a dedicated `recalq` user |

These docs are also a knowledge source for Recalq itself (see
`knowledge_sources:` in `client.yaml`): after `/sync`, you can ask the bot
"how do I restore a backup?" and it answers from here.
