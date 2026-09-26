"""
bench.py — /bench: run a few small, fixed coding tasks (a scoped fix, a
feature, an exact docs edit, a multi-file rename) through /agent on
every ready provider and pick the best one for client.yaml's
agent_provider. Free models appear, change and disappear constantly; this
keeps /agent on one that actually works without anyone hand-testing.

Grading reads the resulting files — it never executes code a model wrote.
Unless AGENT_SANDBOX is on, the agent's own shell commands are refused too.
"""
import os
import re
import time
import tempfile
import contextvars
from concurrent.futures import ThreadPoolExecutor

import agent
import providers

CALC = "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a + b\n"


def _src(d, name="calc.py"):
    try:
        with open(os.path.join(d, name)) as f:
            return f.read()
    except OSError:
        return ""


def _func(src, name):
    m = re.search(rf"def {name}\(.*?(?=\ndef |\Z)", src, re.S)
    return m.group(0) if m else ""


NOTES = ("# Notes\n\n## Setup\n\n- Install Python 3.11.\n- Copy .env.example to .env.\n\n"
         "## Deploy\n\n- Tag a release.\n")
NOTES_DONE = NOTES.replace("- Copy .env.example to .env.\n",
                           "- Copy .env.example to .env.\n- Run ./app test before committing.\n")


def _lines(text):
    """Lines without trailing whitespace or blank-line noise at the end."""
    return [line.rstrip() for line in text.rstrip().splitlines()]


# Each: files to create, the task, and a check(dir) -> bool on the result.
TASKS = [
    {"name": "scoped fix",
     "files": {"calc.py": CALC,
               "test_calc.py": "from calc import add\nassert add(2, 3) == 5\nprint('ok')\n"},
     "task": "fix add() in calc.py so test_calc.py passes",
     "check": lambda d: re.search(r"return\s+a\s*\+\s*b", _func(_src(d), "add")) is not None
     and re.search(r"return\s+a\s*\+\s*b", _func(_src(d), "mul")) is not None},  # mul untouched
    {"name": "add feature",
     "files": {"calc.py": "def add(a, b):\n    return a + b\n",
               "test_calc.py": "from calc import add, sub\nassert sub(5, 3) == 2\nprint('ok')\n"},
     "task": "add a sub(a, b) function to calc.py so test_calc.py passes",
     "check": lambda d: re.search(r"return\s+a\s*-\s*b", _func(_src(d), "sub")) is not None
     and _src(d, "test_calc.py").startswith("from calc import add, sub")},  # didn't edit the test
    # The kind of edit a model failed live (2026-09-26): it described the
    # change at length instead of calling edit_file. Graded exactly.
    {"name": "docs edit",
     "files": {"NOTES.md": NOTES},
     "task": "In NOTES.md, add the bullet '- Run ./app test before committing.' as the last "
             "bullet of the '## Setup' section. Change nothing else.",
     "check": lambda d: _lines(_src(d, "NOTES.md")) == _lines(NOTES_DONE)},
    {"name": "multi-file rename",
     "files": {"util.py": "def slugify(text):\n    return text.lower().replace(' ', '-')\n",
               "main.py": "from util import slugify\n\n\ndef title_slug(t):\n    return slugify(t)\n\n\n"
                          "def path_slug(p):\n    return '/' + slugify(p)\n"},
     "task": "Rename the function slugify to make_slug everywhere in this project.",
     "check": lambda d: "slugify" not in _src(d, "util.py") + _src(d, "main.py")
     and "def make_slug(text)" in _src(d, "util.py")
     and _src(d, "main.py").count("make_slug") == 3},
]


def _reason(e):
    """Short human reason instead of a provider's raw JSON error blob."""
    text = str(e).lower()
    for needle, why in (("rate", "rate limited / out of quota"), ("quota", "rate limited / out of quota"),
                        ("suspend", "account suspended"), ("auth", "bad API key"),
                        ("api key", "bad API key"), ("did not respond", "timed out"),
                        ("not found", "model not found")):
        if needle in text:
            return why
    return str(e).splitlines()[0][:60]


def _confirm(desc):
    return "run:" not in desc  # file edits in the temp dir yes, host shell no


def _bench_one(alias, embedder):
    passed, t0, errors = 0, time.time(), []
    for spec in TASKS:
        with tempfile.TemporaryDirectory() as d:
            for name, body in spec["files"].items():
                with open(os.path.join(d, name), "w") as f:
                    f.write(body)
            try:
                with providers.exact_only():
                    agent.run(spec["task"], alias, root=d, confirm=_confirm, embedder=embedder)
                ok = spec["check"](d)
            except Exception as e:
                ok = False
                errors.append(_reason(e))
            agent._undo.pop(os.path.realpath(d), None)
            try:
                os.remove(agent._ckpt_file(os.path.realpath(d)))
            except OSError:
                pass
            passed += ok
    return {"alias": alias, "passed": passed, "secs": time.time() - t0,
            "cost": providers.provider_registry().get(alias, {}).get("cost_per_1k_tokens", 0),
            "error": errors[0] if errors else ""}


def run(embedder=None, apply=True, workers=4):
    """Benchmark every ready provider; returns (report_text, winner_alias)."""
    aliases = providers.ranked_providers()
    ctx = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(lambda a: ctx.copy().run(_bench_one, a, embedder), aliases))
    # Best = most tasks passed, then cheapest, then fastest.
    rows.sort(key=lambda r: (-r["passed"], r["cost"], r["secs"]))
    lines = [f"{'provider':26s} {'passed':>6s} {'time':>6s}  note"]
    for r in rows:
        note = (f"unavailable: {r['error']}" if r["error"] and not r["passed"] else r["error"])
        lines.append(f"{r['alias']:26s} {r['passed']:>3d}/{len(TASKS)} {r['secs']:5.0f}s  {note}")
    best = rows[0] if rows and rows[0]["passed"] == len(TASKS) else None
    current = providers.agent_provider()
    cur_row = next((r for r in rows if r["alias"] == current), None)
    if best and cur_row and cur_row["passed"] == len(TASKS) and cur_row["cost"] <= best["cost"]:
        best = cur_row  # timing noise alone isn't worth switching models over
    if not best:
        lines.append("\nNo provider passed every task — agent_provider left as "
                     f"{current or '(unset)'}.")
        return "\n".join(lines), None
    if apply and best["alias"] != current:
        providers.set_agent_provider(best["alias"])
        lines.append(f"\nagent_provider: {current or '(unset)'} -> {best['alias']} "
                     "(saved to client.yaml)")
    else:
        lines.append(f"\nagent_provider stays {best['alias']} — still the best.")
    return "\n".join(lines), best["alias"]
