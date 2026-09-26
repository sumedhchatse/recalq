"""
knowledge.py — keep the org knowledge base (the 'sop' namespace every
user's questions check before the LLM) in sync with folders and git repos
listed under client.yaml's `knowledge_sources:`:

  knowledge_sources:
    - name: recalq-docs
      path: docs                                 # a folder (relative = inside the install), or
      # git: https://github.com/org/wiki.git     # a repo, cloned/pulled per sync
      # branch: main
      include: ["*.md", "*.txt", "*.pdf", "*.docx"]   # default
      category: info                             # info | sop | solution | policy (admins only)

Incremental: a file is re-ingested only when its content hash changed, and
documents whose file disappeared are removed. Run with /sync, or on a
schedule (scheduler kind: sync).
"""
import os
import fnmatch
import hashlib
import subprocess

import yaml

import documents
import sop_layer

DEFAULT_INCLUDE = ["*.md", "*.markdown", "*.rst", "*.txt", "*.pdf", "*.docx"]
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
STATE_KEY = "recalq:ksync:"            # + source name -> hash {relpath: sha256}
CLONE_DIR = os.path.expanduser(os.getenv("RECALQ_SOURCES_DIR", "~/.recalq/sources"))
_SAFE_GIT = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]


def load_sources(path):
    with open(path) as f:
        return (yaml.safe_load(f) or {}).get("knowledge_sources") or []


def _checkout(src, base="."):
    """Local directory holding the source's files (cloning/pulling git ones).
    A relative `path` is relative to the install (client.yaml's directory),
    so `path: docs` works wherever Recalq is installed."""
    if src.get("path"):
        return os.path.join(base, os.path.expanduser(src["path"]))
    dest = os.path.join(CLONE_DIR, src["name"])
    if os.path.isdir(os.path.join(dest, ".git")):
        cmd = [*_SAFE_GIT, "-C", dest, "pull", "-q", "--ff-only"]
    else:
        os.makedirs(CLONE_DIR, mode=0o700, exist_ok=True)
        cmd = [*_SAFE_GIT, "clone", "-q", "--depth", "1",
               *(["--branch", src["branch"]] if src.get("branch") else []),
               "--", src["git"], dest]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"git failed: {proc.stderr.strip()[:200]}")
    return dest


def _files(root, include):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if any(fnmatch.fnmatch(fn, pat) for pat in include):
                full = os.path.join(dirpath, fn)
                yield os.path.relpath(full, root), full


def _sha(path):
    """Content hash + chunker version: a chunking change re-ingests everything."""
    with open(path, "rb") as f:
        return f"{documents.CHUNK_VERSION}:{hashlib.sha256(f.read()).hexdigest()}"


def _doc_ids_for(r, filename):
    return [d["doc_id"] for d in documents.list_documents(r, sop_layer.SOP_NAMESPACE)
            if d.get("filename") == filename]


def sync_source(src, r, embedder, guardrails, user="sync", base="."):
    """Sync one source. Returns counts {added, updated, removed, unchanged, errors}."""
    root = _checkout(src, base)
    if not os.path.isdir(root):
        raise RuntimeError(f"{root} is not a directory")
    name, include = src["name"], src.get("include") or DEFAULT_INCLUDE
    state_key = STATE_KEY + name
    known = r.hgetall(state_key) or {}
    seen = set()
    counts = {"added": 0, "updated": 0, "removed": 0, "unchanged": 0, "errors": []}
    for rel, full in _files(root, include):
        seen.add(rel)
        digest = _sha(full)
        if known.get(rel) == digest:
            counts["unchanged"] += 1
            continue
        try:
            # "<source>:<path>" is the doc's filename: unique across sources,
            # re-ingesting it supersedes the old version, and answers cite it.
            sop_layer.ingest_sop(r, embedder, guardrails, full, f"{name}:{rel}", user,
                                 documents_mod=documents, category=src.get("category", "info"))
        except Exception as e:
            counts["errors"].append(f"{rel}: {str(e)[:100]}")
            continue
        r.hset(state_key, rel, digest)
        counts["updated" if rel in known else "added"] += 1
    for rel in set(known) - seen:
        for doc_id in _doc_ids_for(r, f"{name}:{rel}"):
            documents.delete_document(r, sop_layer.SOP_NAMESPACE, doc_id)
        r.hdel(state_key, rel)
        counts["removed"] += 1
    return counts


def sync_all(config_path, r, embedder, guardrails, only=None):
    """Sync every configured source (or just `only`). Returns report text."""
    sources = load_sources(config_path)
    if only:
        sources = [s for s in sources if s["name"] == only]
        if not sources:
            return f"No knowledge source named '{only}' in client.yaml."
    if not sources:
        return "No knowledge_sources in client.yaml — see docs/knowledge-sources.md."
    lines = []
    for src in sources:
        try:
            c = sync_source(src, r, embedder, guardrails,
                            base=os.path.dirname(os.path.abspath(config_path)))
            lines.append(f"{src['name']}: +{c['added']} new, {c['updated']} updated, "
                         f"{c['removed']} removed, {c['unchanged']} unchanged"
                         + (f", {len(c['errors'])} failed" if c["errors"] else ""))
            lines += [f"   ! {e}" for e in c["errors"][:5]]
        except Exception as e:
            lines.append(f"{src['name']}: failed — {e}")
    return "\n".join(lines)
