"""assert-based self-check for team memory: stale-answer invalidation,
"first asked by", admin approve/reject, agent fix reuse, savings line —
needs Redis up, no LLM calls (agent is stubbed).
Run: python3 cache_layer/test_team_memory.py"""
import os
import json
import tempfile
import memlayer
import audit
import agent

NS = "test_memory_" + memlayer.hashlib.sha256(str(memlayer.time.time()).encode()).hexdigest()[:6]


def _cleanup():
    for k in memlayer.r.scan_iter(f"{memlayer.CACHE_PREFIX}{NS}*"):
        memlayer.r.delete(k)
    for raw in memlayer.r.lrange(audit.AUDIT_KEY, 0, -1):
        if NS in raw:
            memlayer.r.lrem(audit.AUDIT_KEY, 0, raw)


def test_answer_dropped_when_source_file_changes():
    with tempfile.TemporaryDirectory() as root:
        src = os.path.join(root, "calc.py")
        with open(src, "w") as f:
            f.write("def add(a, b): return a + b\n")
        q = "what does the add function in calc.py return"
        memlayer.save_to_cache(q, "a + b", "m", namespace=NS, files=[src])
        assert memlayer.find_exact_match(q, NS)[0] is not None
        assert memlayer.find_semantic_match(q + " exactly", NS)[0] is not None

        with open(src, "w") as f:
            f.write("def add(a, b): return a - b\n")
        assert memlayer.find_semantic_match(q + " exactly", NS)[0] is None
        assert memlayer.find_exact_match(q, NS)[0] is None
        assert not any(e["query"] == q for e in memlayer.list_cache_entries(NS))  # deleted


def test_provenance_and_approve_reject():
    q = "how do we rotate the staging database password"
    entry_id = memlayer.save_to_cache(q, "use the vault CLI", "m", namespace=NS, user="alice")
    res = memlayer.ask(q, "unused", namespace=NS, user="bob")
    assert res["cached"] and res["entry_id"] == entry_id, res
    assert "first asked by alice" in memlayer.cache_provenance(res, "bob")
    assert memlayer.cache_provenance(res, "alice") == ""  # don't tell you about yourself

    assert memlayer.approve_answer(entry_id, NS, "admin1") == q
    assert memlayer.r.ttl(memlayer._cache_key(entry_id, NS)) == -1  # no longer expires
    res = memlayer.ask(q, "unused", namespace=NS, user="bob")
    assert "verified by admin1" in memlayer.cache_provenance(res, "bob")
    assert memlayer.r.ttl(memlayer._cache_key(entry_id, NS)) == -1  # a hit keeps it permanent

    assert memlayer.reject_answer(entry_id, NS) == q
    assert memlayer.find_exact_match(q, NS)[0] is None


def test_agent_reuses_past_fix():
    prompts, steps = [], []

    def fake(model, messages, max_tokens=800, tools=None, **kw):
        prompts.append(messages[-1]["content"])
        wrote = any(m.get("role") == "tool" for m in messages)
        tc = None if wrote else [type("TC", (), {"id": "1", "function": type("F", (), {
            "name": "write_file", "arguments": json.dumps({"path": "calc.py", "content": "x"})})()})()]
        msg = type("M", (), {"content": "fixed add() in calc.py", "tool_calls": tc})()
        return type("R", (), {"choices": [type("C", (), {"message": msg})()], "model": "m",
                              "usage": None})()
    agent.chat_completion = fake
    with tempfile.TemporaryDirectory() as root:
        memlayer.run_agent("fix the bug in the add function in calc.py", "m", user="alice",
                           namespace=NS, root=root, confirm=lambda d: True)
        assert memlayer.similar_past_fix("fix the add function bug in calc.py", NS)
        assert memlayer.similar_past_fix("write a README for the deploy scripts", NS) is None

        prompts.clear()
        memlayer.run_agent("fix the add function bug in calc.py", "m", user="bob", namespace=NS,
                           root=root, confirm=lambda d: True,
                           on_step=lambda n, a: steps.append(n))
        assert "reuse_past_fix" in steps, steps
        assert "a similar task was already done" in prompts[0] and "by alice" in prompts[0]


def test_usage_shows_savings():
    audit.record(memlayer.r, user=f"{NS}_u", query="q", model="cache", source="cache",
                 namespace=NS, tokens_saved=1000)
    memlayer.ADMINS.add(f"{NS}_u")
    try:
        assert "savings: $" in memlayer.usage_report(f"{NS}_u")
    finally:
        memlayer.ADMINS.discard(f"{NS}_u")


if __name__ == "__main__":
    try:
        test_answer_dropped_when_source_file_changes()
        test_provenance_and_approve_reject()
        test_agent_reuses_past_fix()
        test_usage_shows_savings()
    finally:
        _cleanup()
    print("ok")
