"""assert-based self-check for scheduler timing + bench model selection —
no Redis, no LLM. Run: python3 cache_layer/test_scheduler.py"""
import os
from datetime import datetime
import scheduler
import bench
import providers


class FakeRedis(dict):
    def get(self, k):
        return super().get(k)
    def set(self, k, v):
        self[k] = v


def test_last_slot():
    now = datetime(2026, 9, 26, 15, 0)  # a Saturday
    assert scheduler.last_slot("daily 09:00", now) == datetime(2026, 9, 26, 9, 0)
    assert scheduler.last_slot("daily 18:00", now) == datetime(2026, 9, 25, 18, 0)
    assert scheduler.last_slot("weekly mon 09:00", now) == datetime(2026, 9, 21, 9, 0)
    assert scheduler.last_slot("weekly sat 16:00", now) == datetime(2026, 9, 19, 16, 0)
    assert scheduler.last_slot("every 30m", now) == datetime(2026, 9, 26, 15, 0)
    assert scheduler.last_slot("every 6h", datetime(2026, 9, 26, 15, 10)).minute == 0
    try:
        scheduler.last_slot("sometimes", now)
        assert False
    except (ValueError, KeyError, IndexError):
        pass


def test_due_fires_once_per_slot_and_not_on_first_sight():
    r, job = FakeRedis(), {"name": "j", "when": "daily 09:00"}
    assert not scheduler.due(job, r, datetime(2026, 9, 26, 15, 0))   # new job: just marked
    assert not scheduler.due(job, r, datetime(2026, 9, 26, 20, 0))   # same slot
    assert scheduler.due(job, r, datetime(2026, 9, 27, 9, 1))         # next day's slot
    assert not scheduler.due(job, r, datetime(2026, 9, 27, 9, 30))    # only once


def test_bench_picks_a_model_that_passes_everything():
    def fake_run(task, alias, root, **kw):
        if alias != "good":
            return "gave up"
        src = open(os.path.join(root, "calc.py")).read()
        src = src.replace("def add(a, b):\n    return a - b", "def add(a, b):\n    return a + b")
        if "sub(" in task:
            src += "\n\ndef sub(a, b):\n    return a - b\n"
        open(os.path.join(root, "calc.py"), "w").write(src)
        return "done"
    saved = []
    orig = (bench.agent.run, providers.ranked_providers, providers.set_agent_provider,
            providers.agent_provider)
    bench.agent.run = fake_run
    providers.ranked_providers = lambda: ["bad", "good"]
    providers.set_agent_provider = saved.append
    providers.agent_provider = lambda: "bad"
    try:
        report, winner = bench.run(apply=True)
        assert winner == "good" and saved == ["good"], (winner, saved, report)
        assert "bad -> good" in report
        bench.agent.run = lambda *a, **k: "gave up"
        report, winner = bench.run(apply=True)
        assert winner is None and saved == ["good"] and "No provider passed" in report
    finally:
        (bench.agent.run, providers.ranked_providers, providers.set_agent_provider,
         providers.agent_provider) = orig


if __name__ == "__main__":
    test_last_slot()
    test_due_fires_once_per_slot_and_not_on_first_sight()
    test_bench_picks_a_model_that_passes_everything()
    print("ok")
