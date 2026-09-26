"""assert-based self-check for agent.py — no network/API key needed,
chat_completion is monkeypatched. Run: python3 cache_layer/test_agent.py"""
import os
import tempfile
import agent

agent.CHECKPOINT_DIR = tempfile.mkdtemp()  # never touch the real ~/.recalq during tests
agent.SANDBOX = ""  # litellm auto-loads .env; tests that want the sandbox set it themselves


def test_write_file_then_stop():
    calls = {"n": 0}

    class FakeMsg:
        def __init__(self, content=None, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls

        def model_dump(self):
            return {"role": "assistant", "content": self.content}

    class FakeCall:
        def __init__(self, name, arguments):
            self.id = "call_1"
            self.function = type("F", (), {"name": name, "arguments": arguments})()

    class FakeResp:
        def __init__(self, msg):
            self.choices = [type("C", (), {"message": msg})()]

    def fake_chat_completion(model, messages, max_tokens=800, tools=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResp(FakeMsg(tool_calls=[
                FakeCall("write_file", '{"path": "out.txt", "content": "hi"}')]))
        return FakeResp(FakeMsg(content="done"))

    agent.chat_completion = fake_chat_completion

    with tempfile.TemporaryDirectory() as root:
        answer = agent.run("write hi to out.txt", "fake-model", root=root,
                            confirm=lambda desc: True)
        assert answer == "done", answer
        with open(os.path.join(root, "out.txt")) as f:
            assert f.read() == "hi"
        assert calls["n"] == 2


def test_write_declined():
    def fake_chat_completion(model, messages, max_tokens=800, tools=None, **kw):
        class M:
            content = None
            tool_calls = [type("C", (), {
                "id": "1",
                "function": type("F", (), {"name": "write_file",
                                            "arguments": '{"path": "x.txt", "content": "y"}'})()
            })()]

            def model_dump(self):
                return {"role": "assistant", "content": None}
        return type("R", (), {"choices": [type("C", (), {"message": M()})()]})()

    agent.chat_completion = fake_chat_completion
    with tempfile.TemporaryDirectory() as root:
        agent.run("x", "fake-model", root=root, confirm=lambda desc: False)
        assert not os.path.exists(os.path.join(root, "x.txt"))


def test_path_escape_blocked():
    with tempfile.TemporaryDirectory() as root:
        try:
            agent._safe_path(root, "../../etc/passwd")
            assert False, "should have raised"
        except ValueError:
            pass


def test_readonly_tools_exclude_write_and_shell():
    names = {t["function"]["name"] for t in agent.READONLY_TOOLS}
    assert {"search_code", "grep_code", "read_file", "list_dir"} <= names, names
    for writey in ("write_file", "edit_file", "run_shell", "update_plan"):
        assert writey not in names, names


class _FakeEmbedder:
    """Deterministic bag-of-words 'embedding' — good enough to prove
    search_code ranks a matching file above an unrelated one, without a
    real embedding model or the network service."""
    _VOCAB = ["auth", "login", "password", "cache", "redis", "unrelated", "banana"]

    def encode(self, texts, convert_to_tensor=False, **kw):
        single = isinstance(texts, str)
        batch = [texts] if single else list(texts)
        vecs = [[float(t.lower().count(w)) for w in self._VOCAB] for t in batch]
        return vecs[0] if single else vecs


def test_search_code_ranks_relevant_file_first():
    with tempfile.TemporaryDirectory() as root:
        with open(os.path.join(root, "auth.py"), "w") as f:
            f.write("def login(password): check auth credentials")
        with open(os.path.join(root, "fruit.py"), "w") as f:
            f.write("banana banana unrelated unrelated")
        agent._INDEX_CACHE.clear()
        result = agent.search_code(root, _FakeEmbedder(), "how does password login work")
        lines = result.splitlines()
        assert lines[0].startswith("auth.py"), result


def test_search_code_tool_dispatches_through_run_tool():
    with tempfile.TemporaryDirectory() as root:
        with open(os.path.join(root, "auth.py"), "w") as f:
            f.write("def login(password): check auth credentials")
        agent._INDEX_CACHE.clear()
        result = agent._run_tool("search_code", {"query": "login password"}, root,
                                  confirm=lambda desc: True, embedder=_FakeEmbedder())
        assert "auth.py" in result, result


def test_answer_reads_project_file_no_confirm_needed():
    """This is the actual fix for 'it doesn't know about my project' —
    plain Q&A (agent.answer, what ask() now calls) should be able to read
    a file to ground its response, with no confirm() ever required since
    only read-only tools are offered."""
    calls = {"n": 0}

    class FakeMsg:
        def __init__(self, content=None, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls

        def model_dump(self):
            return {"role": "assistant", "content": self.content}

    class FakeCall:
        def __init__(self, name, arguments):
            self.id = "call_1"
            self.function = type("F", (), {"name": name, "arguments": arguments})()

    class FakeResp:
        def __init__(self, msg):
            self.choices = [type("C", (), {"message": msg})()]
            self.model = "fake-model/v1"
            self.usage = type("U", (), {"prompt_tokens": 10, "completion_tokens": 5,
                                         "total_tokens": 15})()

    def fake_chat_completion(model, messages, max_tokens=800, tools=None, **kw):
        assert tools == agent.READONLY_TOOLS  # never offered write_file/run_shell here
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResp(FakeMsg(tool_calls=[FakeCall("read_file", '{"path": "README.md"}')]))
        return FakeResp(FakeMsg(content="grounded answer"))

    agent.chat_completion = fake_chat_completion

    with tempfile.TemporaryDirectory() as root:
        with open(os.path.join(root, "README.md"), "w") as f:
            f.write("this project does X")
        result = agent.answer([{"role": "user", "content": "what does this project do?"}],
                               "fake-model", root=root)
        assert result["answer"] == "grounded answer", result
        assert result["tokens_used"] == 30, result  # 15 + 15 across the two calls
        assert calls["n"] == 2
        assert result["grounded"] is True, result  # it actually called read_file


def test_answer_not_grounded_when_no_tool_used():
    """A question the model answers from general knowledge alone (no tool
    call) must report grounded=False — callers use this to decide whether
    the answer is safe to share into a cross-project cache pool."""
    class FakeMsg:
        content = "general knowledge answer"
        tool_calls = None

    class FakeResp:
        choices = [type("C", (), {"message": FakeMsg()})()]
        model = "fake-model/v1"
        usage = None

    agent.chat_completion = lambda model, messages, max_tokens=800, tools=None, **kw: FakeResp()
    result = agent.answer([{"role": "user", "content": "what is 2+2?"}], "fake-model", root=".")
    assert result["grounded"] is False, result


def test_run_without_architect_model_skips_planning_pass():
    """Default behavior (architect_model=None) must be unchanged — one
    model, no extra planning call."""
    calls_by_model = {}

    class FakeResp:
        choices = [type("C", (), {"message": type("M", (), {"content": "done", "tool_calls": None})()})()]

    def fake_chat_completion(model, messages, max_tokens=800, tools=None, **kw):
        calls_by_model[model] = calls_by_model.get(model, 0) + 1
        return FakeResp()

    agent.chat_completion = fake_chat_completion
    with tempfile.TemporaryDirectory() as root:
        answer = agent.run("do something", "editor-model", root=root, confirm=lambda d: True)
        assert answer == "done"
        assert calls_by_model == {"editor-model": 1}, calls_by_model  # no architect-model call at all


def test_architect_mode_plans_then_edits_with_plan_in_context():
    """architect_model set -> a read-only planning call on THAT model runs
    first, its plan text gets injected into the editing model's system
    prompt, then the editing model (with full write/shell tools) executes."""
    calls = []

    def fake_chat_completion(model, messages, max_tokens=800, tools=None, **kw):
        calls.append((model, tools))
        if model == "architect-model":
            assert tools == agent.READONLY_TOOLS  # planning pass never gets write/shell
            content = "Plan: change out.txt to contain 'hi'"
        else:
            # the editing model's system prompt must carry the plan forward
            assert "Plan: change out.txt" in messages[0]["content"]
            content = "applied the plan"
        msg = type("M", (), {"content": content, "tool_calls": None})()
        return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()

    agent.chat_completion = fake_chat_completion
    with tempfile.TemporaryDirectory() as root:
        answer = agent.run("fix out.txt", "editor-model", root=root, confirm=lambda d: True,
                            architect_model="architect-model")
        assert answer == "applied the plan", answer
        assert [m for m, _ in calls] == ["architect-model", "editor-model"], calls


def test_run_threads_history_into_messages():
    """A follow-up like 'do it' is meaningless without the prior turn — the
    caller's conversation history must reach the model."""
    seen_messages = {}

    def fake_chat_completion(model, messages, max_tokens=800, tools=None, **kw):
        seen_messages["messages"] = messages
        msg = type("M", (), {"content": "done", "tool_calls": None})()
        return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()

    agent.chat_completion = fake_chat_completion
    history = [{"role": "user", "content": "how can we improve this project?"},
               {"role": "assistant", "content": "add tests and fix the config"}]
    with tempfile.TemporaryDirectory() as root:
        agent.run("do it", "fake-model", root=root, confirm=lambda d: True, history=history)
        msgs = seen_messages["messages"]
        assert history[0] in msgs and history[1] in msgs, msgs
        assert msgs[-1] == {"role": "user", "content": "do it"}, msgs


def test_answer_prompt_forbids_claiming_untaken_writes():
    """Canary for the hallucination bug: on the read-only path (no
    write_file/run_shell offered), the model must be told point-blank not
    to claim it made changes it structurally cannot make. If this wording
    gets refactored away, this test should fail so it isn't silently lost."""
    captured = {}

    def fake_chat_completion(model, messages, max_tokens=800, tools=None, **kw):
        captured["system_prompt"] = messages[0]["content"]
        msg = type("M", (), {"content": "ok", "tool_calls": None})()
        return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()

    agent.chat_completion = fake_chat_completion
    agent.answer([{"role": "user", "content": "do it for me"}], "fake-model", root=".")
    prompt = captured["system_prompt"].lower()
    assert "must not claim" in prompt, prompt
    assert "no way to write" in prompt or "only read" in prompt, prompt


def test_edit_file_replaces_unique_occurrence():
    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "x.py")
        with open(path, "w") as f:
            f.write("def add(a, b):\n    return a - b\n")
        result = agent._run_tool(
            "edit_file",
            {"path": "x.py", "old_string": "return a - b", "new_string": "return a + b"},
            root, confirm=lambda desc: True)
        assert result == "edited x.py", result
        with open(path) as f:
            assert f.read() == "def add(a, b):\n    return a + b\n"


def test_edit_file_rejects_missing_and_ambiguous_matches():
    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "x.py")
        with open(path, "w") as f:
            f.write("foo\nfoo\n")
        # not found
        r1 = agent._run_tool("edit_file", {"path": "x.py", "old_string": "bar", "new_string": "baz"},
                              root, confirm=lambda d: True)
        assert "not found" in r1, r1
        # not unique
        r2 = agent._run_tool("edit_file", {"path": "x.py", "old_string": "foo", "new_string": "baz"},
                              root, confirm=lambda d: True)
        assert "not unique" in r2, r2
        with open(path) as f:
            assert f.read() == "foo\nfoo\n"  # untouched


def test_edit_file_declined_leaves_file_untouched():
    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "x.py")
        with open(path, "w") as f:
            f.write("a = 1\n")
        agent._run_tool("edit_file", {"path": "x.py", "old_string": "a = 1", "new_string": "a = 2"},
                        root, confirm=lambda d: False)
        with open(path) as f:
            assert f.read() == "a = 1\n"


def test_grep_code_finds_literal_matches():
    with tempfile.TemporaryDirectory() as root:
        with open(os.path.join(root, "a.py"), "w") as f:
            f.write("def handle_login():\n    pass\n")
        with open(os.path.join(root, "b.py"), "w") as f:
            f.write("handle_login()\n")
        result = agent.grep_code(root, "handle_login")
        assert "a.py:1" in result and "b.py:1" in result, result


def test_grep_code_invalid_pattern_errors_cleanly():
    with tempfile.TemporaryDirectory() as root:
        result = agent.grep_code(root, "(unclosed[")
        assert result.startswith("error:"), result


def test_web_fetch_disabled_by_default():
    assert agent.WEB_FETCH_ENABLED is False
    names = {t["function"]["name"] for t in agent.TOOLS}
    assert "web_fetch" not in names, names


def test_web_fetch_blocks_private_addresses():
    for url in ("http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/",
                "http://10.0.0.5/", "ftp://example.com/", "not a url"):
        assert agent._is_safe_url(url) is False, url


def test_read_file_flags_truncation():
    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "big.txt")
        with open(path, "w") as f:
            f.write("x" * 25000)
        result = agent._run_tool("read_file", {"path": "big.txt"}, root, None)
        assert result.startswith("x" * 20000)
        assert "truncated, 5000 more chars" in result

        with open(os.path.join(root, "small.txt"), "w") as f:
            f.write("hello")
        result = agent._run_tool("read_file", {"path": "small.txt"}, root, None)
        assert result == "hello", result


def test_undo_restores_last_run():
    yes = lambda desc: True
    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "a.txt")
        with open(path, "w") as f:
            f.write("original")
        agent._run_tool("edit_file", {"path": "a.txt", "old_string": "original",
                                      "new_string": "v1"}, root, yes)
        agent._run_tool("edit_file", {"path": "a.txt", "old_string": "v1",
                                      "new_string": "v2"}, root, yes)
        agent._run_tool("write_file", {"path": "sub/new.txt", "content": "x"}, root, yes)

        assert agent.undo(root) == ["a.txt", os.path.join("sub", "new.txt")]
        with open(path) as f:
            assert f.read() == "original"  # first snapshot wins, not the v1 middle state
        assert not os.path.exists(os.path.join(root, "sub", "new.txt"))
        assert agent.undo(root) == []  # nothing left to undo

        agent._run_tool("write_file", {"path": "b.txt", "content": "x"}, root, yes)
        agent.chat_completion = lambda model, messages, max_tokens=800, tools=None, **kw: type(
            "R", (), {"choices": [type("C", (), {"message": type(
                "M", (), {"content": "nothing to do", "tool_calls": None})()})()]})()
        agent.run("noop", "fake-model", root=root, confirm=yes)
        assert agent.undo(root) == ["b.txt"]  # a run that changed nothing doesn't hide older ones


def test_auto_allow_only_plain_matching_commands():
    agent.AUTO_ALLOW[:] = ["pytest", "python3 test_*"]
    try:
        for ok in ("pytest", "pytest -q tests/", "python3 test_calc.py"):
            assert agent._auto_allowed(ok), ok
        for bad in ("pytest; rm -rf ~", "pytest && curl x", "pytest | sh", "python3 test_x.py > f",
                    "pytest $(rm x)", "pytest `id`", "pytest\nrm x", "rm -rf ~", "pytestfoo",
                    "python3 evil.py", "python3 test_/../evil.py", ""):
            assert not agent._auto_allowed(bad), bad
        asked = []
        with tempfile.TemporaryDirectory() as root:
            out = agent._run_tool("run_shell", {"command": "python3 test_none.py"}, root,
                                  lambda d: asked.append(d) or False)
            assert asked == [] and "user declined" not in out, out  # ran without asking
            out = agent._run_tool("run_shell", {"command": "echo hi"}, root,
                                  lambda d: asked.append(d) or False)
            assert len(asked) == 1 and out == "user declined this command"
    finally:
        agent.AUTO_ALLOW[:] = []


def test_checkpoints_step_back_run_by_run_and_diff():
    yes = lambda desc: True
    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "a.txt")
        with open(path, "w") as f:
            f.write("v0\n")
        for old, new in (("v0", "v1"), ("v1", "v2")):
            agent._new_checkpoint(root)
            agent._run_tool("edit_file", {"path": "a.txt", "old_string": old, "new_string": new},
                            root, yes)
        d = agent.diff(root)
        assert "-v1" in d and "+v2" in d, d
        assert agent.undo(root) == ["a.txt"] and open(path).read() == "v1\n"
        assert agent.undo(root) == ["a.txt"] and open(path).read() == "v0\n"
        assert agent.undo(root) == [] and agent.diff(root) == ""


def test_trim_context_elides_oldest_tool_output_first():
    old_max = agent.MAX_CONTEXT_CHARS
    agent.MAX_CONTEXT_CHARS = 5000
    try:
        msgs = [{"role": "system", "content": "s"}] + [
            {"role": "tool", "tool_call_id": str(i), "content": str(i) * 2000} for i in range(8)]
        agent._trim_context(msgs)
        assert msgs[1]["content"].endswith(agent._ELIDED)       # oldest trimmed
        assert msgs[-1]["content"] == "7" * 2000                # recent kept whole
        assert sum(len(m["content"]) for m in msgs) <= 5000 + 6 * 2000
    finally:
        agent.MAX_CONTEXT_CHARS = old_max


def test_project_instructions_reach_the_prompt():
    seen = []
    agent.chat_completion = lambda model, messages, max_tokens=800, tools=None, **kw: (
        seen.append(messages[0]["content"]) or type("R", (), {"choices": [type("C", (), {
            "message": type("M", (), {"content": "ok", "tool_calls": None})()})()]})())
    with tempfile.TemporaryDirectory() as root:
        agent.run("x", "m", root=root)
        assert "Project instructions" not in seen[-1]
        with open(os.path.join(root, "AGENTS.md"), "w") as f:
            f.write("Always run make check.")
        agent.run("x", "m", root=root)
        assert "Always run make check." in seen[-1]
        agent.answer([{"role": "user", "content": "q"}], "m", root=root)
        assert "Always run make check." in seen[-1]


def test_sandbox_runs_without_asking_and_is_isolated():
    old = agent.SANDBOX
    agent.SANDBOX = "podman"
    try:
        argv = agent._sandbox_argv("pytest", "/tmp")
        assert argv[:2] == ["podman", "run"] and "--network=none" in argv
        assert "--userns=keep-id" in argv and argv[-3:] == ["sh", "-c", "pytest"]
        real_run, calls = agent.subprocess.run, []
        agent.subprocess.run = lambda argv, **kw: calls.append(argv) or type(
            "P", (), {"stdout": "ok", "stderr": "", "returncode": 0})()
        try:
            out = agent._run_tool("run_shell", {"command": "rm -rf /"}, "/tmp",
                                  lambda d: (_ for _ in ()).throw(AssertionError("asked")))
        finally:
            agent.subprocess.run = real_run
        assert out == "ok" and calls[0][0] == "podman"
    finally:
        agent.SANDBOX = old


def test_checkpoints_survive_restart():
    with tempfile.TemporaryDirectory() as root:
        with open(os.path.join(root, "a.txt"), "w") as f:
            f.write("v0")
        agent._new_checkpoint(root)
        agent._run_tool("edit_file", {"path": "a.txt", "old_string": "v0", "new_string": "v1"},
                        root, lambda d: True)
        agent._undo.clear()  # simulate a new process
        assert agent.undo(root) == ["a.txt"] and open(os.path.join(root, "a.txt")).read() == "v0"
        agent._undo.clear()
        assert agent.undo(root) == []  # the undo itself was persisted too


def test_explore_subagents_run_in_parallel_with_fresh_context():
    import threading
    active, peak, lock = [0], [0], threading.Lock()
    main_turns = []

    def resp(content=None, calls=None):
        msg = type("M", (), {"content": content, "tool_calls": calls})()
        return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()

    def call(i, q):
        return type("TC", (), {"id": str(i), "function": type("F", (), {
            "name": "explore", "arguments": '{"question": "%s"}' % q})()})()

    def fake(model, messages, max_tokens=800, tools=None, **kw):
        if messages[0]["content"].startswith("You are a coding agent"):  # main agent
            main_turns.append(messages)
            if len(main_turns) == 1:
                return resp(calls=[call(1, "where is auth"), call(2, "where is billing")])
            return resp(content="done")
        with lock:  # a sub-agent: fresh context = just its own system prompt + question
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        assert len(messages) == 2 and messages[1]["content"].startswith("where is"), messages
        agent.time.sleep(0.3)
        with lock:
            active[0] -= 1
        return resp(content="found in " + messages[1]["content"][9:] + ".py")
    agent.chat_completion = fake
    with tempfile.TemporaryDirectory() as root:
        assert agent.run("x", "m", root=root) == "done"
    assert peak[0] == 2, peak  # both sub-agents ran at the same time
    tool_msgs = [m["content"] for m in main_turns[1] if m.get("role") == "tool"]
    assert tool_msgs == ["found in auth.py", "found in billing.py"], tool_msgs  # order kept


if __name__ == "__main__":
    test_write_file_then_stop()
    test_write_declined()
    test_path_escape_blocked()
    test_readonly_tools_exclude_write_and_shell()
    test_answer_reads_project_file_no_confirm_needed()
    test_answer_not_grounded_when_no_tool_used()
    test_search_code_ranks_relevant_file_first()
    test_search_code_tool_dispatches_through_run_tool()
    test_run_without_architect_model_skips_planning_pass()
    test_architect_mode_plans_then_edits_with_plan_in_context()
    test_run_threads_history_into_messages()
    test_answer_prompt_forbids_claiming_untaken_writes()
    test_edit_file_replaces_unique_occurrence()
    test_edit_file_rejects_missing_and_ambiguous_matches()
    test_edit_file_declined_leaves_file_untouched()
    test_grep_code_finds_literal_matches()
    test_grep_code_invalid_pattern_errors_cleanly()
    test_web_fetch_disabled_by_default()
    test_web_fetch_blocks_private_addresses()
    test_read_file_flags_truncation()
    test_undo_restores_last_run()
    test_auto_allow_only_plain_matching_commands()
    test_checkpoints_step_back_run_by_run_and_diff()
    test_trim_context_elides_oldest_tool_output_first()
    test_project_instructions_reach_the_prompt()
    test_sandbox_runs_without_asking_and_is_isolated()
    test_checkpoints_survive_restart()
    test_explore_subagents_run_in_parallel_with_fresh_context()
    print("ok")
