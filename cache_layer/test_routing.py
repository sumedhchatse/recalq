"""assert-based self-check for providers.chat_completion's failover +
circuit breaker — litellm is stubbed, no network. Run:
python3 cache_layer/test_routing.py"""
import os
import providers as p

REG = {
    "paid":  {"enabled": True, "provider_type": "x", "model": "paid",  "cost_per_1k_tokens": 0.01,
              "fallback_to": "free2"},
    "free1": {"enabled": True, "provider_type": "x", "model": "free1", "cost_per_1k_tokens": 0.0},
    "free2": {"enabled": True, "provider_type": "x", "model": "free2", "cost_per_1k_tokens": 0.0},
    "nokey": {"enabled": True, "provider_type": "x", "model": "nokey", "cost_per_1k_tokens": 0.0,
              "api_key_env": "RECALQ_TEST_UNSET_KEY"},
}


def _setup(dead):
    os.environ.pop("RECALQ_TEST_UNSET_KEY", None)
    p._PROVIDERS = REG
    p._cooldown_until.clear()
    calls = []

    def fake(**kw):
        calls.append(kw["model"])
        if kw["model"] in dead:
            raise RuntimeError("boom")
        return f"ok:{kw['model']}"
    p.litellm.completion = fake
    return calls


def test_follows_chain_then_cheapest():
    calls = _setup(dead={"x/paid", "x/free2"})
    assert p.chat_completion("paid", []) == "ok:x/free1"
    assert calls == ["x/paid", "x/free2", "x/free1"], calls


def test_failed_provider_is_skipped_next_time():
    calls = _setup(dead={"x/paid"})
    p.chat_completion("paid", [])
    calls.clear()
    assert p.chat_completion("paid", []) == "ok:x/free2"
    assert calls == ["x/free2"], calls  # didn't waste a call on 'paid' again
    assert "paid" in p.cooldowns()


def test_missing_key_falls_back_instead_of_raising():
    calls = _setup(dead=set())
    assert p.chat_completion("nokey", []) == "ok:x/free1"
    assert "nokey" not in p.ranked_providers()


def test_all_dead_raises_and_all_cooled_retries():
    _setup(dead={"x/paid", "x/free1", "x/free2"})
    try:
        p.chat_completion("paid", [])
        assert False, "should raise"
    except RuntimeError:
        pass
    p.MAX_ATTEMPTS, old = 10, p.MAX_ATTEMPTS
    try:
        calls = _setup(dead={"x/paid", "x/free1", "x/free2"})
        try:
            p.chat_completion("paid", [])
        except RuntimeError:
            pass
        calls.clear()
        p.litellm.completion = lambda **kw: calls.append(kw["model"]) or "back"
        assert p.chat_completion("paid", []) == "back"  # everything cooled -> retried anyway
    finally:
        p.MAX_ATTEMPTS = old


def test_ranked_cheapest_first_cooled_last():
    _setup(dead=set())
    assert p.ranked_providers() == ["free1", "free2", "paid"]
    p._cooldown_until["free1"] = p.time.time() + 60
    assert p.ranked_providers() == ["free2", "paid", "free1"]


class FakeRedis:
    def __init__(self):
        self.d = {}
    def set(self, k, v, ex=None):
        self.d[k] = ex
    def exists(self, k):
        return k in self.d
    def delete(self, k):
        self.d.pop(k, None)
    def ttl(self, k):
        return self.d.get(k, -2)


def test_cooldown_shared_through_redis():
    _setup(dead={"x/paid"})
    shared = FakeRedis()
    p.use_redis(shared)
    try:
        p.chat_completion("paid", [])
        p._cooldown_until.clear()  # simulate a *different* process: no local memory
        calls = []
        p.litellm.completion = lambda **kw: calls.append(kw["model"]) or "ok"
        p.chat_completion("paid", [])
        assert calls == ["x/free2"], calls  # skipped 'paid' because Redis said so
        assert "paid" in p.cooldowns()
    finally:
        p.use_redis(None)


def test_free_only_never_touches_paid():
    calls = _setup(dead=set())
    with p.free_only():
        assert p.chat_completion("paid", []) == "ok:x/free2"
    assert calls == ["x/free2"], calls
    assert p.chat_completion("paid", []) == "ok:x/paid"  # context ended
    p._PROVIDERS = {"paid": REG["paid"]}
    with p.free_only():
        try:
            p.chat_completion("paid", [])
            assert False, "should refuse"
        except RuntimeError as e:
            assert "budget" in str(e)


def test_exact_only_no_fallback_even_when_cooled():
    calls = _setup(dead={"x/paid"})
    p._cooldown_until["free2"] = p.time.time() + 60
    with p.exact_only():
        try:
            p.chat_completion("paid", [])
            assert False
        except RuntimeError:
            pass
        assert p.chat_completion("free2", []) == "ok:x/free2"  # tried despite cooldown
    assert calls == ["x/paid", "x/free2"], calls


if __name__ == "__main__":
    test_follows_chain_then_cheapest()
    test_failed_provider_is_skipped_next_time()
    test_missing_key_falls_back_instead_of_raising()
    test_all_dead_raises_and_all_cooled_retries()
    test_ranked_cheapest_first_cooled_last()
    test_cooldown_shared_through_redis()
    test_free_only_never_touches_paid()
    test_exact_only_no_fallback_even_when_cooled()
    print("ok")
