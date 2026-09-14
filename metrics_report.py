#!/usr/bin/env python3
"""
metrics_report.py — measured Recalq metrics from the EXISTING audit log.

No new storage. Reads recalq:audit:log and reports what actually happened:
hit rate, path distribution, tokens saved vs spent, latency by path.

Usage:
    cd ~/memlayer && source .venv/bin/activate
    python3 metrics_report.py                 # all time
    python3 metrics_report.py --days 7        # last 7 days
    python3 metrics_report.py --json          # machine-readable
"""
import sys, json, argparse, time, collections

sys.path.insert(0, "cache_layer")

AUDIT_KEY = "recalq:audit:log"
# Paths that cost zero tokens
FREE_SOURCES = {"cache"}


def load_rows(r, days=None):
    """Read audit entries. Handles list or set storage; skips unparseable rows."""
    raw = []
    try:
        t = r.type(AUDIT_KEY)
        t = t.decode() if isinstance(t, bytes) else t
        if t == "list":
            raw = r.lrange(AUDIT_KEY, 0, -1)
        elif t == "set":
            raw = list(r.smembers(AUDIT_KEY))
        elif t == "none":
            return []
        else:
            print(f"warning: unexpected audit key type '{t}'", file=sys.stderr)
            return []
    except Exception as e:
        print(f"error reading {AUDIT_KEY}: {e}", file=sys.stderr)
        return []

    rows = []
    for x in raw:
        try:
            rows.append(json.loads(x))
        except Exception:
            continue

    if days:
        cutoff = time.time() - days * 86400
        kept = []
        for row in rows:
            ts = row.get("ts") or row.get("timestamp") or row.get("time")
            if ts is None:
                kept.append(row)          # no timestamp -> don't silently drop
                continue
            try:
                if float(ts) >= cutoff:
                    kept.append(row)
            except (TypeError, ValueError):
                kept.append(row)
        rows = kept

    return rows


def build(rows):
    total = len(rows)
    by_source = collections.Counter((r.get("source") or "unknown") for r in rows)

    hits = sum(v for k, v in by_source.items() if k in FREE_SOURCES)
    blocked = sum(1 for r in rows if r.get("blocked"))

    tok_saved = sum(int(r.get("tokens_saved") or 0) for r in rows)
    tok_spent = sum(int(r.get("tokens_used") or 0) for r in rows)

    lat = collections.defaultdict(list)
    for r in rows:
        ms = r.get("latency_ms")
        if isinstance(ms, (int, float)):
            lat[r.get("source") or "unknown"].append(ms)

    def pct(vals, p):
        if not vals:
            return None
        s = sorted(vals)
        i = min(int(round(p / 100 * (len(s) - 1))), len(s) - 1)
        return s[i]

    latency = {
        k: {"n": len(v), "p50": pct(v, 50), "p95": pct(v, 95)}
        for k, v in sorted(lat.items())
    }

    return {
        "total_queries": total,
        "cache_hits": hits,
        "hit_rate": (hits / total) if total else None,
        "blocked_pii": blocked,
        "by_source": dict(by_source.most_common()),
        "tokens_saved": tok_saved,
        "tokens_spent": tok_spent,
        "latency_ms": latency,
    }


def render(m, days):
    span = f"last {days} days" if days else "all time"
    print(f"\n  RECALQ — MEASURED METRICS ({span})")
    print("  " + "-" * 46)

    if not m["total_queries"]:
        print("  No audit entries found. Ask some questions first.\n")
        return

    hr = m["hit_rate"]
    print(f"  total queries   {m['total_queries']}")
    print(f"  cache hits      {m['cache_hits']}  ({hr:.1%})" if hr is not None else "")
    print(f"  PII blocked     {m['blocked_pii']}")

    print("\n  BY PATH")
    for k, v in m["by_source"].items():
        share = v / m["total_queries"]
        bar = "#" * max(1, round(share * 24))
        print(f"    {k:<14} {v:>5}  {share:>6.1%}  {bar}")

    print("\n  TOKENS")
    print(f"    saved (cache) {m['tokens_saved']:>10,}")
    print(f"    spent (llm)   {m['tokens_spent']:>10,}")
    denom = m["tokens_saved"] + m["tokens_spent"]
    if denom:
        print(f"    avoided       {m['tokens_saved'] / denom:>10.1%}")

    print("\n  LATENCY (ms)")
    for k, v in m["latency_ms"].items():
        p50 = f"{v['p50']:.0f}" if v["p50"] is not None else "-"
        p95 = f"{v['p95']:.0f}" if v["p95"] is not None else "-"
        print(f"    {k:<14} n={v['n']:<5} p50={p50:<8} p95={p95}")

    print("\n  Note: with few real users, hit rate reflects your own repeated")
    print("  test queries — not a signal about real-world repetition.\n")


def main():
    ap = argparse.ArgumentParser(description="Measured Recalq metrics from the audit log.")
    ap.add_argument("--days", type=int, default=None, help="only the last N days")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    a = ap.parse_args()

    try:
        import memlayer as m_
    except Exception as e:
        print(f"could not import memlayer (run from ~/memlayer with .venv active): {e}",
              file=sys.stderr)
        return 1

    rows = load_rows(m_.r, a.days)
    metrics = build(rows)

    if a.json:
        print(json.dumps(metrics, indent=2))
    else:
        render(metrics, a.days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
