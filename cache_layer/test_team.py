"""assert-based self-check for team mode (per-user usage, admin view,
agent runs audited) — needs Redis up, no LLM calls (agent is stubbed).
Run: python3 cache_layer/test_team.py"""
import json
import tempfile
import memlayer
import audit
import agent
import providers

TAG = "test_team_" + memlayer.hashlib.sha256(str(memlayer.time.time()).encode()).hexdigest()[:6]
ALICE, BOB = f"{TAG}_alice", f"{TAG}_bob"


def _cleanup():
    for u in (ALICE, BOB):
        memlayer.r.delete(f"{audit.SPEND_KEY}{u}:{memlayer.time.strftime('%Y-%m')}")
    for raw in memlayer.r.lrange(audit.AUDIT_KEY, 0, -1):
        if TAG in raw if isinstance(raw, str) else TAG.encode() in raw:
            memlayer.r.lrem(audit.AUDIT_KEY, 0, raw)


def test_usage_by_user_and_admin_view():
    audit.record(memlayer.r, user=ALICE, query="q", model="m", source="paid", namespace=TAG,
                 tokens_used=2000)
    audit.record(memlayer.r, user=ALICE, query="q", model="cache", source="cache", namespace=TAG,
                 tokens_saved=500)
    audit.record(memlayer.r, user=BOB, query="q", model="m", source="free", namespace=TAG,
                 tokens_used=100)
    rows = audit.usage_by_user(memlayer.r, {"paid": 0.01, "free": 0.0})
    a, b = rows[ALICE], rows[BOB]
    assert (a["queries"], a["cache_hits"], a["llm_calls"]) == (2, 1, 1), a
    assert (a["tokens_used"], a["tokens_saved"]) == (2000, 500), a
    assert abs(a["cost_usd"] - 0.02) < 1e-9, a
    assert b["cost_usd"] == 0.0 and b["llm_calls"] == 1, b

    memlayer.ADMINS.clear()
    own = memlayer.usage_report(ALICE)
    assert ALICE in own and BOB not in own, own
    memlayer.ADMINS.add(ALICE)
    try:
        team = memlayer.usage_report(ALICE)
        assert ALICE in team and BOB in team, team
    finally:
        memlayer.ADMINS.discard(ALICE)


def test_agent_run_is_audited():
    class Msg:
        content, tool_calls = "done", None
    resp = type("R", (), {"choices": [type("C", (), {"message": Msg()})()], "model": "fake-model",
                          "usage": type("U", (), {"total_tokens": 42})()})()
    agent.chat_completion = lambda *a, **kw: resp
    with tempfile.TemporaryDirectory() as root:
        assert memlayer.run_agent("do x", "fake-model", user=BOB, namespace=TAG, root=root) == "done"
    newest = json.loads(memlayer.r.lindex(audit.AUDIT_KEY, 0))
    assert newest["user"] == BOB and newest["tokens_used"] == 42, newest
    assert newest["query"] == "[agent] do x", newest


def test_budget_forces_free_models():
    audit.COST_PER_1K["paid"] = 0.01
    audit.record(memlayer.r, user=ALICE, query="q", model="m", source="paid", namespace=TAG,
                 tokens_used=100_000)  # $1.00
    assert abs(audit.month_spend(memlayer.r, ALICE) - 1.0) < 1e-9
    seen = []

    class Msg:
        content, tool_calls = "done", None
    resp = type("R", (), {"choices": [type("C", (), {"message": Msg()})()], "model": "m",
                          "usage": None})()

    def fake(*a, **kw):
        seen.append(providers._FREE_ONLY.get())
        return resp
    agent.chat_completion = fake
    old = memlayer.BUDGET_USD
    try:
        memlayer.BUDGET_USD = 0.5
        assert memlayer.over_budget(ALICE) and not memlayer.over_budget(BOB)
        with tempfile.TemporaryDirectory() as root:
            out = memlayer.run_agent("x", "paid", user=ALICE, namespace=TAG, root=root)
            assert seen == [True] and memlayer._BUDGET_NOTE in out, (seen, out)
            seen.clear()
            out = memlayer.run_agent("x", "paid", user=BOB, namespace=TAG, root=root)
            assert seen == [False] and out == "done", (seen, out)
        assert "of your $0.50 budget" in memlayer.usage_report(ALICE)
    finally:
        memlayer.BUDGET_USD = old


if __name__ == "__main__":
    try:
        test_usage_by_user_and_admin_view()
        test_agent_run_is_audited()
        test_budget_forces_free_models()
    finally:
        _cleanup()
    print("ok")
