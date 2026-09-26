#!/usr/bin/env python3
"""
run_tests.py — every Recalq test in one go: `./recalq test`.
Needs Redis up (./start.sh). No LLM calls except in the --live set.
Exits non-zero if anything fails, so it can gate a commit or CI.

  ./recalq test          # the fast suite (~1-2 min)
  ./recalq test --live   # + tests that call real providers (costs a few tokens)
"""
import os
import sys
import time
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

SUITE = [
    "cache_layer/test_agent.py",
    "cache_layer/test_routing.py",
    "cache_layer/test_gitflow.py",
    "cache_layer/test_scheduler.py",
    "cache_layer/test_security.py",
    "cache_layer/test_team.py",
    "cache_layer/test_team_memory.py",
    "cache_layer/test_session_resume.py",
    "cache_layer/test_project_namespace.py",
    "test_telegram_agent.py",
    "test_recalq_mcp.py",
    "test_cache_accuracy.py",
    "wedge_test.py",
    ["-m", "pytest", "-q", "test_regression.py"],
]
LIVE = []  # add provider-calling checks here; kept separate so the default run is free


def main():
    tests = SUITE + (LIVE if "--live" in sys.argv else [])
    failed = []
    t_all = time.time()
    for t in tests:
        args = t if isinstance(t, list) else [t]
        name = args[-1]
        t0 = time.time()
        proc = subprocess.run([PY, *args], cwd=HERE, capture_output=True, text=True)
        ok = proc.returncode == 0
        print(f"  {'✓' if ok else '✗'} {name:42s} {time.time() - t0:5.1f}s", flush=True)
        if not ok:
            failed.append(name)
            tail = (proc.stdout + proc.stderr).strip().splitlines()[-15:]
            print("\n".join("      " + line for line in tail))
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed in {time.time() - t_all:.0f}s")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
