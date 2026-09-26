# Knowledge sources

Recalq answers from your team's documents before it asks an LLM: the org
knowledge base (`sop` namespace) is checked on every question, for every
user, and answers cite the document. Knowledge sources keep that knowledge
base in sync with folders and git repos.

## Configure

In `client.yaml`:

```yaml
knowledge_sources:
  - name: recalq-docs              # unique; documents are named "<name>:<path>"
    path: /opt/recalq/docs         # a folder on the server …
  - name: team-wiki
    git: https://github.com/acme/wiki.git   # … or a git repo (cloned/pulled each sync)
    branch: main                   # optional
    include: ["*.md", "*.pdf"]     # optional; default: md, markdown, rst, txt, pdf, docx
    category: sop                  # optional; info (default) | sop | solution | policy
```

`category: policy` documents are only used for admins' questions.

Private git repos: the service user needs access (an SSH deploy key or a
credential helper) — Recalq just runs `git clone` / `git pull`.

## Sync

- `/sync` (CLI, or Telegram for admins) — all sources; `/sync team-wiki` — one.
- Scheduled: add a job — `kind: sync` (optionally `source: <name>`), e.g.
  `when: "every 6h"`.

Output: `team-wiki: +3 new, 1 updated, 0 removed, 42 unchanged`.

Sync is incremental: files are hashed and only new/changed ones are
re-embedded; documents whose file was deleted are removed from the
knowledge base. Hidden directories, `.git`, `node_modules`, `venv`, `dist`
and `build` are skipped. Files over 25 MB, scanned PDFs without a text
layer, and other formats are reported as failed and skipped.

## Check what it knows

Ask something only the docs would know — the reply is marked with the
source document, e.g. `(recalq-docs:operations.md)`.
