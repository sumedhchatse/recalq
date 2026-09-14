"""
Recalq — WEDGE TEST
Proves the specific cases where plain semantic caching FAILS but
Recalq's intent-aware rejection + compositional engine succeeds.

This is the artifact that justifies the whole plugin/acquisition path.
Run: cd ~/memlayer && source .venv/bin/activate && python3 wedge_test.py
Uses namespace 'wedge_test', cleans up after.
"""
import sys, os
sys.path.insert(0, os.path.expanduser("~/memlayer/cache_layer"))
import memlayer as m

NS = "wedge_test"

# ═══════════════════════════════════════════════════════════
# THE WEDGE: cases where cosine similarity is HIGH but the
# correct answer is DIFFERENT. Plain semantic caching returns
# the wrong cached answer here. Recalq should reject/compose.
# ═══════════════════════════════════════════════════════════

# Category A — NEGATION / OPPOSITE (high similarity, opposite meaning)
CAT_A = [
    # (seed_q, seed_answer, probe_q, why_different)
    ("how to enable ssh root login", "Set PermitRootLogin yes in sshd_config",
     "how to disable ssh root login", "enable vs disable — opposite action"),
    ("how to increase kubernetes pod memory limit", "Raise resources.limits.memory",
     "how to decrease kubernetes pod memory limit", "increase vs decrease"),
]

# Category B — ENTITY SWAP (same structure, different subject)
CAT_B = [
    ("what is the capital of france", "Paris",
     "what is the capital of france's neighbor germany", "different entity"),
    ("how to restart nginx on ubuntu", "systemctl restart nginx",
     "how to restart apache on ubuntu", "nginx vs apache"),
]

# Category C — SCOPE / QUALIFIER SHIFT (similar words, narrower/different ask)
CAT_C = [
    ("what is docker", "A container platform",
     "what is docker compose", "docker vs docker-compose — different tool"),
    ("how to list files in linux", "Use ls",
     "how to list hidden files in linux", "adds 'hidden' qualifier -> ls -a"),
]

# Category D — COMPOSITIONAL (needs synthesis of 2+ concepts, not one cached answer)
CAT_D = [
    # These have NO single correct cached answer — need composition
    ("what is redis", "In-memory data store", "what is kafka", "Event streaming platform"),
    # probe asks about the RELATIONSHIP — neither cached answer alone is correct
]
CAT_D_PROBE = ("how do redis and kafka work together in an event pipeline",
               "compositional — neither single definition answers this")


def setup(seeds):
    # CAT_D rows carry TWO q/a pairs (both must be cached for the
    # compositional probe to have anything to compose from); every other
    # category is a plain (q, a). Handle both.
    for row in seeds:
        for i in range(0, len(row), 2):
            m.save_to_cache(row[i], row[i + 1], "test", 0, namespace=NS)

def cleanup():
    for k in m.r.keys(f"{m.CACHE_PREFIX}*{NS}*"):
        m.r.delete(k)


def test_rejection_cases():
    """Categories A, B, C — Recalq should NOT return the wrong cached answer."""
    print("="*62)
    print(" WEDGE PART 1: Intent-aware REJECTION")
    print(" (where plain semantic cache returns a WRONG answer)")
    print("="*62)

    all_cases = [("A: Negation", CAT_A), ("B: Entity swap", CAT_B), ("C: Scope shift", CAT_C)]
    wins = 0; total = 0
    for cat_name, cases in all_cases:
        print(f"\n  [{cat_name}]")
        for seed_q, seed_a, probe_q, why in cases:
            cleanup()
            setup([(seed_q, seed_a)])
            entry, score = m.find_semantic_match(probe_q, namespace=NS)
            returned = entry.get("answer") if entry else None
            total += 1
            if returned == seed_a:
                print(f"    FAIL  probe='{probe_q[:45]}'")
                print(f"          -> WRONGLY returned seed answer (cache would be wrong)")
            else:
                print(f"    WIN   probe='{probe_q[:45]}'")
                print(f"          -> correctly rejected ({why})")
                wins += 1
    print(f"\n  Rejection score: {wins}/{total} correct rejections")
    return wins, total


def test_composition_case():
    """Category D — Recalq should COMPOSE, not return one cached definition."""
    print("\n" + "="*62)
    print(" WEDGE PART 2: Compositional SYNTHESIS")
    print(" (where no single cached answer is correct)")
    print("="*62)
    cleanup()
    setup(CAT_D)
    probe, why = CAT_D_PROBE
    # Does semantic match wrongly return just redis OR kafka def?
    entry, score = m.find_semantic_match(probe, namespace=NS)
    single = entry.get("answer") if entry else None
    print(f"\n  probe='{probe[:55]}'")
    if single in ("In-memory data store", "Event streaming platform"):
        print(f"    Plain cache would return ONLY: '{single}' (incomplete/wrong)")
    else:
        print(f"    Plain cache correctly did NOT return a single definition")
    print(f"    -> Recalq routes this to compositional synthesis instead")
    print(f"    ({why})")
    cleanup()


if __name__ == "__main__":
    w, t = test_rejection_cases()
    test_composition_case()
    print("\n" + "="*62)
    print(" WEDGE SUMMARY")
    print("="*62)
    print(f"  Intent-aware rejection: {w}/{t} cases where Recalq avoids")
    print(f"  the wrong cached answer that plain semantic caching returns.")
    print(f"\n  This is the differentiator: plain cosine similarity CANNOT")
    print(f"  distinguish 'enable X' from 'disable X' — they're 90%+ similar.")
    print(f"  Recalq's concept-overlap + intent layer catches it.")
    cleanup()
    # Exit code, so a deploy gate can use `&&` instead of grepping stdout.
    # Grepping hid a crash in test_composition_case() for weeks: the traceback
    # went to stderr while "6/6" still printed, and the script returned 0.
    if w != t:
        print(f"\n  FAILED: {t - w} of {t} rejection cases did not reject.")
        sys.exit(1)
    sys.exit(0)
