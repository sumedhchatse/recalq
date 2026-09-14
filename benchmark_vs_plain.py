"""
Recalq vs Plain Semantic Caching — Head-to-Head Benchmark
Reproducible, no special infra. Uses the SAME embedding model for both,
so the only difference is the matching logic.

PLAIN  = cosine similarity + threshold (how Portkey/basic caches work)
RECALQ = plain + concept-overlap + polarity/negation awareness

Run: cd ~/memlayer && source .venv/bin/activate && python3 benchmark_vs_plain.py
"""
import sys, os, numpy as np
sys.path.insert(0, os.path.expanduser("~/memlayer/cache_layer"))
import memlayer as m

NS = "benchmark"
THRESHOLD = 0.78   # same threshold both use

# ── Benchmark cases: (seed_q, seed_answer, probe_q, should_match) ──
# should_match=False means a correct cache MUST NOT return the seed answer.
CASES = [
    # Negation — the killer cases (high similarity, opposite meaning)
    ("how to enable ssh root login", "Set PermitRootLogin yes", "how to disable ssh root login", False),
    ("how to increase pod memory limit", "Raise resources.limits.memory", "how to decrease pod memory limit", False),
    ("how to allow port 22 in firewall", "ufw allow 22", "how to block port 22 in firewall", False),
    ("how to start the nginx service", "systemctl start nginx", "how to stop the nginx service", False),
    ("how to mount a usb drive", "use mount command", "how to unmount a usb drive", False),
    # Entity swap
    ("how to restart nginx", "systemctl restart nginx", "how to restart apache", False),
    ("what is the capital of france", "Paris", "what is the capital of germany", False),
    # Scope shift
    ("how to list files in linux", "ls", "how to list hidden files in linux", False),
    ("what is docker", "container platform", "what is docker compose", False),
    # TRUE positives — legitimate rephrasings that SHOULD match (both must get these right)
    ("what is kubernetes", "orchestration platform", "explain kubernetes", True),
    ("how to restart nginx service", "systemctl restart nginx", "how do I restart the nginx service", True),
]


def plain_semantic_match(probe, seed_q, seed_vec, probe_vec):
    """Baseline: pure cosine similarity + threshold. Returns True if it would serve the cached answer."""
    cos = float(np.dot(seed_vec, probe_vec) / (np.linalg.norm(seed_vec)*np.linalg.norm(probe_vec) + 1e-9))
    return cos >= THRESHOLD, cos


def run():
    print("="*70)
    print(" RECALQ vs PLAIN SEMANTIC CACHE — Head to Head")
    print(" (same embedding model + threshold; only matching logic differs)")
    print("="*70)

    plain_wrong = 0
    recalq_wrong = 0
    plain_missed = 0
    recalq_missed = 0

    print(f"\n{'CASE':<45} {'PLAIN':<12} {'RECALQ':<12}")
    print("-"*70)

    for seed_q, seed_a, probe_q, should_match in CASES:
        # cleanup + seed
        for k in m.r.keys(f"{m.CACHE_PREFIX}*{NS}*"): m.r.delete(k)
        m.save_to_cache(seed_q, seed_a, "test", 0, namespace=NS)

        # embeddings (same model both use)
        sv = np.asarray(m.embedder.encode(m.normalise_query(seed_q), convert_to_numpy=True), dtype=np.float32)
        pv = np.asarray(m.embedder.encode(m.normalise_query(probe_q), convert_to_numpy=True), dtype=np.float32)

        # PLAIN result
        plain_hit, cos = plain_semantic_match(probe_q, seed_q, sv, pv)
        # RECALQ result
        entry, score = m.find_semantic_match(probe_q, namespace=NS)
        recalq_hit = (entry is not None and entry.get("answer") == seed_a)

        # score correctness
        def verdict(hit):
            if should_match:
                return "OK match" if hit else "MISS"
            else:
                return "WRONG!" if hit else "rejected"

        pv_txt = verdict(plain_hit)
        rv_txt = verdict(recalq_hit)

        if not should_match and plain_hit: plain_wrong += 1
        if not should_match and recalq_hit: recalq_wrong += 1
        if should_match and not plain_hit: plain_missed += 1
        if should_match and not recalq_hit: recalq_missed += 1

        label = probe_q[:43]
        print(f"{label:<45} {pv_txt:<12} {rv_txt:<12}")

    # cleanup
    for k in m.r.keys(f"{m.CACHE_PREFIX}*{NS}*"): m.r.delete(k)

    total_danger = sum(1 for c in CASES if not c[3])
    total_good = sum(1 for c in CASES if c[3])

    print("="*70)
    print(f" RESULTS")
    print("="*70)
    print(f"  Dangerous cases (must reject): {total_danger}")
    print(f"    PLAIN  returned WRONG answer: {plain_wrong}/{total_danger}")
    print(f"    RECALQ returned WRONG answer: {recalq_wrong}/{total_danger}")
    print(f"  Legit rephrasings (should match): {total_good}")
    print(f"    PLAIN  missed: {plain_missed}/{total_good}")
    print(f"    RECALQ missed: {recalq_missed}/{total_good}")
    print("-"*70)
    print(f"  HEADLINE: Plain caching served {plain_wrong} wrong answers.")
    print(f"            Recalq served {recalq_wrong} wrong answers.")
    if plain_wrong > recalq_wrong:
        print(f"\n  >> Recalq prevented {plain_wrong - recalq_wrong} incorrect cache hits")
        print(f"     that plain semantic caching would have served. <<")


if __name__ == "__main__":
    run()
