#!/usr/bin/env python3
"""
compare_followups.py — measure what context-aware cache keys actually save.

Runs the same conversations twice and reports tokens per turn.

The honest expectation, stated up front so the numbers can't flatter:
  - FIRST time a follow-up is asked on a topic  -> full LLM cost (no change)
  - SECOND time the same follow-up on the same topic -> 0 tokens (the win)
  - Same follow-up on a DIFFERENT topic -> full LLM cost, correct answer

So the saving only appears on REPEATED follow-ups within the same topic.
If every follow-up in your usage is unique, this saves nothing.

Usage:  cd ~/memlayer && source .venv/bin/activate && python3 compare_followups.py
"""
import sys, logging

sys.path.insert(0, "cache_layer")
logging.getLogger().setLevel(logging.ERROR)   # keep the table readable

import memlayer as m

NS = "followup_bench"

NGINX = [{"role": "user", "content": "what is nginx"},
         {"role": "assistant", "content": "nginx is a web server and reverse proxy."}]
K8S = [{"role": "user", "content": "what is kubernetes"},
       {"role": "assistant", "content": "kubernetes is a container orchestrator."}]

TURNS = [
    ("how do I install that", NGINX, "follow-up, nginx, 1st"),
    ("how do I install that", NGINX, "follow-up, nginx, REPEAT"),
    ("how do I install that", K8S,   "follow-up, k8s, 1st"),
    ("how do I install that", K8S,   "follow-up, k8s, REPEAT"),
]


def cleanup():
    for k in m.r.keys(f"ml:v1:{NS}*"):
        m.r.delete(k)


def run():
    cleanup()
    print(f"\n  {'turn':<28} {'source':<10} {'tokens':>7}   answer")
    print("  " + "-" * 74)

    total = 0
    rows = []
    for q, hist, label in TURNS:
        r = m.ask(q, history=hist, namespace=NS, user="bench")
        src = r.get("source", "?")
        tok = r.get("tokens_used") or 0
        total += tok
        ans = (r.get("answer") or "").strip().replace("\n", " ")[:30]
        rows.append((label, src, tok, ans))
        print(f"  {label:<28} {src:<10} {tok:>7}   {ans}")

    print("  " + "-" * 74)
    print(f"  {'TOTAL':<28} {'':<10} {total:>7}")

    # Correctness check — the thing that must never break
    print("\n  CORRECTNESS")
    nginx_ans = rows[0][3].lower()
    k8s_ans = rows[2][3].lower()
    ok = True
    if "kubernetes" in nginx_ans:
        print("  FAIL: nginx follow-up returned a kubernetes answer"); ok = False
    if "nginx" in k8s_ans:
        print("  FAIL: kubernetes follow-up returned an nginx answer"); ok = False
    if ok:
        print("  OK: each topic got its own answer — no cross-contamination")

    # Where the saving actually came from
    print("\n  WHERE THE SAVING IS")
    for label, src, tok, _ in rows:
        verdict = "free (cache hit)" if src == "cache" else "paid (LLM)"
        print(f"    {label:<28} {verdict}")

    cleanup()
    print()


if __name__ == "__main__":
    run()
