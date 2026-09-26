"""
Recalq — Regression Test Suite
Catches the class of bug that silently breaks features on a change
(e.g. feedback crashing on document keys).

Run:  cd ~/memlayer && source .venv/bin/activate
      pip install pytest
      python3 -m pytest test_regression.py -v

Uses a dedicated 'test_regr' namespace; cleans up after itself.
Does NOT touch your real cache/users.
"""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache_layer"))

import memlayer as m
import pytest

NS = "test_regr"

def _cleanup():
    for k in m.r.keys(f"{m.CACHE_PREFIX}*{NS}*"):
        m.r.delete(k)

@pytest.fixture(autouse=True)
def clean():
    _cleanup()
    yield
    _cleanup()


# ── CACHE ──────────────────────────────────────────────────
def test_save_and_exact_hit():
    m.save_to_cache("regr unique question alpha", "answer-alpha", "test", 0, namespace=NS)
    entry, score = m.find_exact_match("regr unique question alpha", namespace=NS)
    assert entry is not None
    assert entry.get("answer") == "answer-alpha"

def test_exact_miss():
    entry, score = m.find_exact_match("nonexistent question zzz", namespace=NS)
    assert entry is None

def test_semantic_rephrase_hits():
    m.save_to_cache("what is kubernetes orchestration", "k8s answer", "test", 0, namespace=NS)
    entry, score = m.find_semantic_match("explain kubernetes orchestration", namespace=NS)
    assert entry is not None
    assert entry.get("answer") == "k8s answer"

def test_semantic_different_rejected():
    m.save_to_cache("what is the capital of france", "Paris", "test", 0, namespace=NS)
    entry, score = m.find_semantic_match("what is the capital of germany", namespace=NS)
    # Must NOT return the France answer for a Germany question
    assert entry is None or entry.get("answer") != "Paris"


# ── NAMESPACE ISOLATION ────────────────────────────────────
def test_namespace_isolation():
    m.save_to_cache("secret finance figure", "42 million", "test", 0, namespace=NS + "_fin")
    # A different namespace must not see it
    entry, score = m.find_exact_match("secret finance figure", namespace=NS + "_hr")
    assert entry is None
    # cleanup the extra namespaces
    for suffix in ["_fin", "_hr"]:
        for k in m.r.keys(f"{m.CACHE_PREFIX}*{NS}{suffix}*"):
            m.r.delete(k)


# ── PII GUARDRAILS ─────────────────────────────────────────
def test_pii_aadhaar_blocked_or_redacted():
    from guardrails import check_query
    res = check_query("my aadhaar is 1234 5678 9012", mode="redact")
    # Either blocked or the number is redacted out of clean_text
    assert res["action"] in ("block", "redact")
    if res["action"] == "redact":
        assert "1234 5678 9012" not in res["clean_text"]

def test_clean_query_passes():
    from guardrails import check_query
    res = check_query("what is kubernetes", mode="redact")
    assert res["action"] == "allow" or res.get("clean_text") == "what is kubernetes"




def test_context_dependent_not_cached():
    """
    Regression for the nginx/kubernetes cache-poisoning bug: a follow-up like
    "how do I install that" has no standalone meaning, so two different
    conversations produce the identical string and B would hit A's answer at 1.0.
    """
    import memlayer as m
    ns = "test_ctx_regression"
    m.save_to_cache("how do I install that", "NGINX INSTALL ANSWER", "gemini", 100, ns)
    entry, score = m.find_exact_match("how do I install that", ns)
    assert entry is None, "context-dependent query must never be cached or served"

    m.save_to_cache("how do I install nginx", "REAL ANSWER", "gemini", 100, ns)
    entry, score = m.find_exact_match("how do I install nginx", ns)
    assert entry is not None, "self-contained queries must still cache"

    # wipe the whole throwaway namespace — including exact-match pointer keys,
    # which don't contain the answer text and so survive a content-based purge
    for k in m.r.keys(f"ml:v1:{ns}*"):
        m.r.delete(k)
    # save_to_cache also writes to commons; remove only this test's litter
    for k in m.r.keys("ml:v1:commons*"):
        raw = m.r.get(k)
        if raw and ("REAL ANSWER" in str(raw) or "NGINX INSTALL ANSWER" in str(raw)):
            m.r.delete(k)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
