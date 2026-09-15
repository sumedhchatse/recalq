"""assert-based self-check for save_history/load_history/clear_history —
needs Redis up (same as running the CLI itself, same as test_project_
namespace.py). Run: python3 cache_layer/test_session_resume.py"""
import memlayer


def _test_ns():
    return "test_session_resume_" + memlayer.hashlib.sha256(
        str(memlayer.time.time()).encode()).hexdigest()[:8]


def test_round_trip():
    ns = _test_ns()
    try:
        assert memlayer.load_history(ns) == []  # nothing saved yet
        h = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
        memlayer.save_history(h, ns)
        assert memlayer.load_history(ns) == h
    finally:
        memlayer.clear_history(ns)


def test_clear_removes_it():
    ns = _test_ns()
    memlayer.save_history([{"role": "user", "content": "x"}], ns)
    memlayer.clear_history(ns)
    assert memlayer.load_history(ns) == []


def test_trims_to_max_turns():
    ns = _test_ns()
    try:
        long_history = [{"role": "user", "content": str(i)} for i in range(50)]
        memlayer.save_history(long_history, ns)
        loaded = memlayer.load_history(ns)
        assert len(loaded) == memlayer.HISTORY_MAX_TURNS, len(loaded)
        assert loaded == long_history[-memlayer.HISTORY_MAX_TURNS:]
    finally:
        memlayer.clear_history(ns)


def test_different_namespaces_dont_mix():
    """Same bug class as project_namespace — one project's resumed
    conversation must never leak into another's."""
    ns_a, ns_b = _test_ns(), _test_ns() + "_b"
    try:
        memlayer.save_history([{"role": "user", "content": "project A stuff"}], ns_a)
        memlayer.save_history([{"role": "user", "content": "project B stuff"}], ns_b)
        assert memlayer.load_history(ns_a) != memlayer.load_history(ns_b)
        assert "A" in memlayer.load_history(ns_a)[0]["content"]
        assert "B" in memlayer.load_history(ns_b)[0]["content"]
    finally:
        memlayer.clear_history(ns_a)
        memlayer.clear_history(ns_b)


def test_meta_conversational_queries_never_cached():
    """Live-demonstrated bug: asking a fresh Telegram chat "what did I just
    ask you about?" returned an answer about a DIFFERENT project entirely,
    because that exact phrase had been cached (and shared to commons) by
    an unrelated CLI session earlier. These questions are only ever
    correct for the specific conversation that asked them."""
    for q in ["what did I just ask you about?", "what did I ask you?",
              "what did we discuss?", "what's my name?", "who am I?",
              "do you remember what I said?"]:
        assert memlayer.is_context_dependent(q), q


if __name__ == "__main__":
    test_round_trip()
    test_clear_removes_it()
    test_trims_to_max_turns()
    test_different_namespaces_dont_mix()
    test_meta_conversational_queries_never_cached()
    print("ok")
