"""
audit.py — Audit logging for Recalq
Place at: ~/memlayer/cache_layer/audit.py

Records every query to a Redis list (fast, queryable, capped size)
plus the plaintext log file for durability. Each entry captures:
  who, when, query (already PII-redacted), model, source (cache/llm),
  namespace, tokens, latency.

Admin panel reads recent entries via get_audit_log().
Respects namespace visibility: admin sees all, users see own namespace.
"""
import json
import time
import logging

log = logging.getLogger("recalq.audit")

AUDIT_KEY = "recalq:audit:log"      # Redis list key
AUDIT_MAX = 5000                   # keep last N entries (auto-trimmed)


def record(r, *, user, query, model, source, namespace,
           tokens_used=0, tokens_saved=0, latency_ms=0, blocked=False,
           pii_types=None):
    """
    Append one audit entry. Called from ask() after each query resolves.
    `r` is the shared redis client passed in from memlayer.
    Query text is assumed already redacted by guardrails before this point.
    """
    entry = {
        "ts":           time.time(),
        "time":         time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "user":         user or "unknown",
        "query":        (query or "")[:200],   # cap length, already redacted
        "model":        model or "-",
        "source":       source or "-",         # cache | llm-name | guardrails
        "namespace":    namespace or "default",
        "tokens_used":  tokens_used,
        "tokens_saved": tokens_saved,
        "latency_ms":   latency_ms,
        "blocked":      blocked,
        "pii_types":    pii_types or [],
    }
    try:
        r.lpush(AUDIT_KEY, json.dumps(entry))
        r.ltrim(AUDIT_KEY, 0, AUDIT_MAX - 1)   # keep newest AUDIT_MAX
    except Exception as e:
        log.warning(f"Audit record failed: {e}")

    # Also write a structured line to the plaintext log for durability
    log.info(
        f"AUDIT user={entry['user']} ns={entry['namespace']} "
        f"source={entry['source']} model={entry['model']} "
        f"tokens={entry['tokens_used']} blocked={entry['blocked']} "
        f"q='{entry['query'][:60]}'"
    )


def get_audit_log(r, *, namespace=None, all_namespaces=False, limit=200):
    """
    Return recent audit entries, newest first.
    all_namespaces=True  -> admin view, everything
    namespace='finance'  -> only that namespace's entries
    """
    try:
        raw_entries = r.lrange(AUDIT_KEY, 0, AUDIT_MAX - 1)
    except Exception as e:
        log.warning(f"Audit read failed: {e}")
        return []

    out = []
    for raw in raw_entries:
        try:
            e = json.loads(raw)
        except Exception:
            continue
        if not all_namespaces and namespace and e.get("namespace") != namespace:
            continue
        out.append(e)
        if len(out) >= limit:
            break
    return out


def get_audit_summary(r, *, namespace=None, all_namespaces=False):
    """Aggregate stats over the audit log for the admin dashboard."""
    entries = get_audit_log(r, namespace=namespace,
                            all_namespaces=all_namespaces, limit=AUDIT_MAX)
    total       = len(entries)
    cache_hits  = sum(1 for e in entries if e.get("source") == "cache")
    llm_calls   = sum(1 for e in entries if e.get("source") not in ("cache", "guardrails", "-"))
    blocked     = sum(1 for e in entries if e.get("blocked"))
    by_user     = {}
    by_ns       = {}
    for e in entries:
        by_user[e.get("user","?")] = by_user.get(e.get("user","?"), 0) + 1
        by_ns[e.get("namespace","?")] = by_ns.get(e.get("namespace","?"), 0) + 1
    return {
        "total":       total,
        "cache_hits":  cache_hits,
        "llm_calls":   llm_calls,
        "blocked":     blocked,
        "by_user":     by_user,
        "by_namespace": by_ns,
    }
