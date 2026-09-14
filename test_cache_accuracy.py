"""
Recalq — Cache Accuracy Test Suite
Protects against the #1 risk: returning a WRONG cached answer.

Tests two failure modes:
  - FALSE POSITIVE (dangerous): different questions wrongly matched
  - FALSE NEGATIVE (wasteful): true rephrasings wrongly missed

Run:  cd ~/memlayer && source .venv/bin/activate && python3 test_cache_accuracy.py
Uses a dedicated 'test_accuracy' namespace so your real cache is untouched.
"""
import sys, os
sys.path.insert(0, os.path.expanduser("~/memlayer/cache_layer"))
import memlayer as m

NS = "test_accuracy"

# ── Test data ─────────────────────────────────────────────────
# Each seed: a canonical question + its cached answer.
SEEDS = [
    ("what is kubernetes", "Kubernetes is a container orchestration platform."),
    ("how to reset a linux password", "Use passwd command as root."),
    ("what is the capital of france", "Paris."),
    ("explain tcp handshake", "SYN, SYN-ACK, ACK three-way handshake."),
    ("what is our q3 revenue target", "The Q3 revenue target is 50 million."),
]

# SHOULD MATCH — genuine rephrasings of a seed (expect cache HIT to same answer)
SHOULD_MATCH = [
    ("what is kubernetes", "can you explain kubernetes"),
    ("what is kubernetes", "tell me about kubernetes"),
    ("how to reset a linux password", "how do I reset a password on linux"),
    ("what is the capital of france", "what's france's capital city"),
    ("explain tcp handshake", "describe the tcp handshake process"),
]

# SHOULD NOT MATCH — different questions that must NOT return a seed's answer
SHOULD_NOT_MATCH = [
    ("what is kubernetes", "what is docker"),            # related but different
    ("what is the capital of france", "what is the capital of germany"),
    ("how to reset a linux password", "how to reset a windows password"),
    ("what is our q3 revenue target", "what is our q3 hiring target"),
    ("explain tcp handshake", "explain udp protocol"),
]


def cleanup():
    for k in m.r.keys(f"{m.CACHE_PREFIX}{NS}:*"):
        m.r.delete(k)
    # also exact-match keys
    for k in m.r.keys(f"{m.CACHE_PREFIX}*{NS}*"):
        m.r.delete(k)


def seed():
    for q, a in SEEDS:
        m.save_to_cache(q, a, "test", 0, namespace=NS)


def run():
    print("Setting up test cache...")
    cleanup()
    seed()

    seed_answers = {q: a for q, a in SEEDS}
    fp = 0  # false positives (dangerous)
    fn = 0  # false negatives (wasteful)
    ok = 0

    print("\n=== SHOULD MATCH (rephrasings) — expect HIT ===")
    for canonical, variant in SHOULD_MATCH:
        entry, score = m.find_semantic_match(variant, namespace=NS)
        matched_answer = entry.get("answer") if entry else None
        expected = seed_answers[canonical]
        if matched_answer == expected:
            print(f"  PASS  '{variant[:40]}' -> matched")
            ok += 1
        else:
            print(f"  MISS  '{variant[:40]}' -> NO match (false negative)")
            fn += 1

    print("\n=== SHOULD NOT MATCH (different Qs) — expect NO wrong hit ===")
    for seed_q, different in SHOULD_NOT_MATCH:
        entry, score = m.find_semantic_match(different, namespace=NS)
        matched_answer = entry.get("answer") if entry else None
        wrong = seed_answers[seed_q]
        if matched_answer == wrong:
            print(f"  FAIL  '{different[:40]}' -> WRONGLY matched seed! (FALSE POSITIVE)")
            fp += 1
        else:
            print(f"  PASS  '{different[:40]}' -> correctly rejected")
            ok += 1

    cleanup()

    print("\n" + "="*50)
    print(f"  Passed:            {ok}")
    print(f"  False negatives:   {fn}  (wasteful — missed a real rephrasing)")
    print(f"  False positives:   {fp}  (DANGEROUS — returned a wrong answer)")
    print("="*50)
    if fp > 0:
        print("  ** FALSE POSITIVES DETECTED — cache may return wrong answers. **")
        print(f"  ** Consider raising SIMILARITY_CUTOFF (currently {m.SIMILARITY_CUTOFF}). **")
        return 1
    if fn > len(SHOULD_MATCH) // 2:
        print("  ** Many false negatives — cache too strict, wasting LLM calls. **")
        print(f"  ** Consider lowering SIMILARITY_CUTOFF (currently {m.SIMILARITY_CUTOFF}). **")
    print("  Cache accuracy acceptable." if fp == 0 else "")
    return 0


if __name__ == "__main__":
    sys.exit(run())
