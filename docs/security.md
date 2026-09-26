# Security

## Trust model

- **People on the allowlist are trusted colleagues**, not adversaries: an
  allowed Telegram user can `/cd` to any directory the bot's user can read
  and run the agent there. Keep `TELEGRAM_ALLOWED_USERS` tight, and run the
  bot as a dedicated user that can only reach project directories (see
  [migration-192.168.1.8.md](migration-192.168.1.8.md)).
- **Model output is untrusted.** A model can be wrong, confused, or steered
  by content it reads (prompt injection from a file or web page). Every
  guard below assumes the model might try something harmful.
- **Everyone in the `recalq` group can read the API keys** in `.env` —
  their own `recalq` processes call the LLMs. Give out provider keys with
  spend limits on the provider side too.

## What's protected, and how

| Risk | Guard |
|---|---|
| Agent edits outside the project | every file path resolved and confined to the project root (symlinks included) |
| Agent runs a harmful command | without sandbox: each command shown and confirmed (or a narrow `AGENT_AUTO_ALLOW` glob with no shell metacharacters or `..`); with `AGENT_SANDBOX`: runs in a container with no network, only the project dir mounted, memory/process limits |
| Sandbox escape via git | `.git` is mounted read-only in the sandbox, the agent's edit tools refuse `.git/…`, and Recalq's own git calls run with hooks and fsmonitor disabled — otherwise sandboxed code could plant a hook the host runs on the next commit (this was reproduced, then fixed) |
| Command/option injection | git and gh are always called with argument lists, never a shell; `/review` targets must resolve to a commit (`/review --output=…` used to write files) |
| SSRF via `web_fetch` | off by default; public http(s) only, every redirect hop re-checked. DNS rebinding is not covered |
| PII in questions / documents | redacted (or blocked) by guardrails before caching, logging or the LLM; critical ids masked in ingested docs |
| One project's answers leaking into another | cache, docs and history are namespaced per project path (and per Telegram chat); project-grounded answers never enter the shared `commons` pool; "first asked by" is hidden for commons hits |
| Stale answers after code changes | file-grounded answers carry file hashes and are dropped when a file changes |
| Runaway spend | per-person monthly budget → free models only; non-admins can't pick unpriced raw models on Telegram; `/usage` for visibility |
| Other local users reading your data | checkpoints 0700/0600 under your home; `.env` 0600 (0640 for the team group); Redis and embeddings on 127.0.0.1 with a Redis password |
| Unknown Telegram users | default-deny allowlist; unknown users only get told their id |
| Admin-only actions | `/approve`, `/reject`, `/bench`, `/sync`, raw `/model` need `RECALQ_ADMINS` |

Regression tests for the fixed holes: `cache_layer/test_security.py`.

## Known limits

- Without `AGENT_SANDBOX`, an approved command runs as you, with your
  access. Read what you approve.
- In the sandbox the project directory is writable; `/undo` covers edits
  made through the agent's edit tools, not files changed by commands. Use
  git as the real safety net (`/pr` does this for you).
- `AGENT_AUTO_ALLOW="python3 test_*"` lets the agent run a test file it
  just wrote — only use auto-allow together with the sandbox.
- The audit log keeps the last 5000 requests, redacted; it's not a
  compliance archive.
