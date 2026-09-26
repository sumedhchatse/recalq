"""
cli.py — the interactive `./recalq` terminal: slash commands, readline
completion, and printing. All the engine logic lives in memlayer.py (and
agent/gitflow/bench/...); this file is only the REPL around it.
"""
from memlayer import *  # noqa: F401,F403 — the CLI is a thin shell over the engine's public API

import readline  # noqa: F401 — importing it wires input() up with arrow-key history
from plugins import load_plugins
import agent as _agent

_plugins_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugins")
_plugin_api = load_plugins(_plugins_dir)

# Commands are slash-prefixed so a real question ("what's the status of
# my order?") can never be mistaken for a built-in command.
_CLI_WORDS = ["/quit", "/stats", "/cache", "/providers", "/status", "/add", "/project",
              "/plugins", "/model", "/doc", "/image", "/agent", "/undo", "/usage",
              "/approve", "/reject", "/diff", "/pr", "/review", "/bench", "/sync", "/reset"] + [
              f"/{c}" for c in _plugin_api.commands.keys()]

_TTY = sys.stdout.isatty()

def _c(code, text):
    return f"\033[{code}m{text}\033[0m" if _TTY else text

_RULE = _c("2", "─" * 60)

def _cli_completer(text, state):
    buf = readline.get_line_buffer()
    if buf.startswith("/model "):
        matches = [a for a, _, _ in list_providers() if a.startswith(text)]
    elif buf.startswith("/doc ") or buf.startswith("/image "):
        import glob
        matches = glob.glob(text + "*")
    else:
        matches = [w for w in _CLI_WORDS if w.startswith(text)]
    try:
        return matches[state]
    except IndexError:
        return None

readline.set_completer_delims(" \t\n")  # keep '/', '.', '-' in words — needed for paths and aliases
readline.set_completer(_cli_completer)
readline.parse_and_bind("tab: complete")

_cli_namespace = project_namespace(os.getcwd())
import getpass
_cli_user = os.getenv("RECALQ_USER") or getpass.getuser()
# Cache/docs are shared by everyone working in this project on this
# host (that's the team win); the conversation itself is per person.
_history_ns = f"{_cli_namespace}_{_cli_user}"

print()
print(_c("1;36", "🧠 Recalq"), _c("2", f"— {os.getcwd()}"))
print(_RULE)
print(_c("2", "  /quit /stats /usage /cache /providers /status /add /project /plugins /reset "
               "/model <name> /doc <path> /image <path> [q] /agent <task> /diff /undo /approve /reject"))
print(_c("2", "  /pr <task> (agent on a branch → PR) /review [pr#|base] /bench (pick best agent model) /sync [source]"))
print(_c("2", "  Tab completes commands/models/paths · ↑/↓ history"))
print(_c("2", f"  cache/docs scoped to '{_cli_namespace}' — different project dirs never mix"))
print(_c("2", f"  signed in as '{_cli_user}'" + (" (admin)" if is_admin(_cli_user) else "")))
if _plugin_api.loaded:
    print(_c("2", f"  plugins loaded: {', '.join(_plugin_api.loaded)}"))
print(_RULE)
print()
model = default_model()
_model_chosen = False  # True after /model — then /agent uses it too, not agent_provider

def _run_agent_and_print(task):
    print()
    print(_c("2", f"🤖 agent working in {os.getcwd()} ..."))
    def _on_step(name, args):
        if name == "update_plan":
            print(_c("36", "  📋 plan:"))
            for s in args.get("steps", []):
                mark = {"done": "✓", "in_progress": "→"}.get(s.get("status"), "·")
                print(_c("36", f"     {mark} {s.get('task','')}"))
            return
        preview = args.get("path") or args.get("command") or args.get("query") or args.get("pattern") or args.get("url", "")
        print(_c("33", f"  → {name} {preview}"))
    _agent_model = model if _model_chosen else (agent_provider() or model)
    if _agent_model != model:
        print(_c("2", f"  (agent uses {_agent_model} — client.yaml agent_provider; /model overrides)"))
    answer = run_agent(task, _agent_model, user=_cli_user, namespace=_cli_namespace,
                       root=os.getcwd(), on_step=_on_step, embedder=embedder,
                       architect_model=architect_provider(), history=history)
    print()
    print(f"{_c('1;32', 'Recalq')} › {answer}")
    print(_RULE)
    print()
    history.append({"role": "user", "content": task})
    history.append({"role": "assistant", "content": answer})
    _persist_history()

recent_doc_id = None
_project_scanned = False
_last_answer = (None, None)  # (cache entry id, its namespace) for /approve, /reject
# Resumed from Redis (same namespace as cache/docs) rather than starting
# blank each run — a follow-up like "do it" still means something even
# in a brand-new `./recalq` process, as long as you're in the same project.
history = load_history(_history_ns)
if history:
    print(_c("2", f"  resumed previous conversation ({len(history)//2} exchanges) — /reset to start fresh"))
    print()

def _persist_history():
    history[:] = history[-HISTORY_MAX_TURNS:]
    save_history(history, _history_ns)
_PROJECT_TRIGGERS = ("analyze this project", "analyse this project", "analyze the project",
                     "explain this project", "explain this codebase", "explain this repo",
                     "what does this project do", "what is this project", "summarize this project",
                     "summarize this repo", "summarize this codebase")
while True:
    try:
        user_input = input(f"{_c('1;36', 'You')} {_c('2', f'[{model}]')} › ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\nBye.")
        break
    if not user_input:         continue
    if user_input == "/quit":  break
    if user_input == "/stats":
        s = get_stats(_cli_namespace)
        print(f"\n📊 queries={s['total_queries']} hits={s['cache_hits']} "
              f"saved~{s['tokens_saved_est']} llm_calls={s['llm_calls']}\n")
        continue
    if user_input in ("/approve", "/reject"):
        if not is_admin(_cli_user):
            print("  only admins (RECALQ_ADMINS) can approve or reject answers\n")
        elif not _last_answer[0]:
            print("  the last answer isn't in the cache, nothing to approve/reject\n")
        elif user_input == "/approve":
            q = approve_answer(*_last_answer, _cli_user)
            print(f"  ✓ verified: '{q}' — kept permanently, shown as verified to the team\n"
                  if q else "  that answer is no longer in the cache\n")
        else:
            q = reject_answer(*_last_answer)
            _last_answer = (None, None)
            print(f"  ✗ removed: '{q}' — the next ask gets a fresh answer\n"
                  if q else "  that answer is no longer in the cache\n")
        continue
    if user_input == "/usage":
        print("\n" + usage_report(_cli_user) + "\n")
        continue
    if user_input == "/cache":
        for e in list_cache_entries(_cli_namespace)[:10]:
            print(f"  [{e.get('hits',0)} hits] {e['query'][:70]}")
        continue
    if user_input == "/reset":
        history.clear()
        clear_history(_history_ns)
        recent_doc_id = None
        _project_scanned = False
        print("  conversation and attached-doc context cleared\n")
        continue
    if user_input == "/providers":
        _cool = cooldowns()
        for s in provider_status(live=False):
            mark = "✓" if s["ready"] else "✗ (missing API key)"
            shared = f"  [shares key with: {', '.join(s['shared_with'])}]" if s["shared_with"] else ""
            if s["alias"] in _cool:
                mark += f" (failed recently — skipped for {_cool[s['alias']]}s)"
            print(f"  {s['alias']:14s} → {s['model']:35s} {mark}{shared}")
        print("  (or type any litellm model string, e.g. ollama/llama3.1, openai/gpt-4o)")
        print("  '/status' does a live check | '/add' registers a new provider\n")
        continue
    if user_input == "/status":
        print("\nChecking providers (one real call each — costs a few tokens per provider)...\n")
        results = provider_status(live=True)
        ok       = [s for s in results if s["live_ok"] is True]
        failed   = [s for s in results if s["live_ok"] is False]
        unconfig = [s for s in results if not s["ready"]]

        def _line(s):
            shared = f"  [shares key with: {', '.join(s['shared_with'])}]" if s["shared_with"] else ""
            return f"  {s['alias']:14s} → {s['model']:35s}{shared}"

        print(f"✓ OK ({len(ok)})")
        for s in ok:
            print(_line(s))
        print(f"\n✗ FAILED ({len(failed)})")
        for s in failed:
            print(_line(s))
            print(f"      {s['live_error']}")
        if unconfig:
            print(f"\n○ NOT CONFIGURED — missing API key ({len(unconfig)})")
            for s in unconfig:
                print(_line(s))
        print()
        continue
    if user_input == "/plugins":
        if not _plugin_api.loaded:
            print(f"\n  none loaded (drop a .py file in {_plugins_dir}/ — see plugins/README.md)\n")
        else:
            print(f"\n  loaded: {', '.join(_plugin_api.loaded)}")
            for cname, (_handler, chelp) in _plugin_api.commands.items():
                print(f"    /{cname:11s} {chelp}")
            print()
        continue
    if user_input in ("/add", "/add provider"):
        try:
            alias = input("  short name (e.g. 'openai'): ").strip()
            if not alias:
                print("  cancelled\n"); continue
            provider_type = input("  provider type (openai/anthropic/gemini/groq/mistral/"
                                   "cohere/azure/bedrock/ollama/openrouter): ").strip()
            model_name = input("  model name (e.g. gpt-4o, llama3.1, or for openrouter a slug "
                                "like meta-llama/llama-3.1-8b-instruct:free): ").strip()
            api_base = input("  api_base (blank unless it's an OpenAI-compatible endpoint "
                              "or local Ollama): ").strip() or None
            key_input = input("  API key — paste a new key, or an EXISTING env var name "
                               "to reuse it (e.g. NVIDIA_MODEL_API_KEY), or blank for none: ").strip()
            key_env = None
            if key_input:
                existing_key_envs = {s["key_env"] for s in provider_status(live=False) if s["key_env"]}
                if key_input.isupper() and key_input in existing_key_envs:
                    key_env = key_input
                    print(f"  reusing existing key ${key_env} (shared with other providers using it)")
                elif key_input.isupper() and key_input.replace("_", "").isalnum() and os.getenv(key_input):
                    key_env = key_input  # a real env var not in client.yaml yet, but already exported
                else:
                    key_env = f"{alias.upper().replace('-', '_')}_API_KEY"
                    save_api_key(key_env, key_input)
                    print(f"  saved to .env as {key_env}")
            fallback_to = input("  fallback provider alias (blank to skip): ").strip() or None
            add_provider(alias, provider_type, model_name, api_key_env=key_env,
                         api_base=api_base, fallback_to=fallback_to)
            print(f"  Added '{alias}' — ready to use now (no restart needed): model {alias}\n")
        except (KeyboardInterrupt, EOFError):
            print("\n  cancelled\n")
        except Exception as e:
            print(f"  Failed to add provider: {e}\n")
        continue
    if user_input.startswith("/model "):
        model = user_input.split(" ",1)[1].strip()
        _model_chosen = True
        print(f"Model: {model}\n")
        continue
    if user_input == "/project":
        cwd = os.getcwd()
        print(f"\n📁 Scanning {cwd} ...")
        try:
            summary = scan_project(cwd)
            import tempfile as _tempfile
            with _tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
                f.write(summary)
                tmp_path = f.name
            doc_id, meta = ingest_document(tmp_path, "PROJECT_OVERVIEW.txt",
                                           namespace=_cli_namespace, uploaded_by="cli")
            os.unlink(tmp_path)
            recent_doc_id = doc_id
            _project_scanned = True
            print(f"Indexed {meta['chunk_count']} chunks from this folder. "
                  f"Ask things like 'what does this project do?'\n")
        except Exception as e:
            print(f"Failed to scan project: {e}\n")
        continue
    if user_input.startswith("/doc "):
        path = user_input.split(" ", 1)[1].strip().strip('"')
        if not os.path.isfile(path):
            print(f"No such file: {path}\n")
            continue
        try:
            doc_id, meta = ingest_document(path, os.path.basename(path),
                                           namespace=_cli_namespace, uploaded_by="cli")
            recent_doc_id = doc_id
            print(f"\n📄 Ingested '{meta['filename']}' — {meta['chunk_count']} chunks. "
                  f"Ask away, it'll use this doc.\n")
        except Exception as e:
            print(f"Failed to ingest: {e}\n")
        continue
    if user_input.startswith("/image "):
        rest = user_input.split(" ", 1)[1].strip()
        path, _, question = rest.partition(" ")
        path = path.strip('"')
        if not os.path.isfile(path):
            print(f"No such file: {path}\n")
            continue
        ext = path.lower().rsplit(".", 1)[-1]
        mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "gif": "image/gif", "webp": "image/webp"}.get(ext)
        if not mime:
            print(f"Unsupported image type: .{ext}\n")
            continue
        with open(path, "rb") as f:
            img_bytes = f.read()
        result = ask_image(img_bytes, mime, question, namespace=_cli_namespace, user=_cli_user)
        print()
        print(_c("2", f"◆ vision · {result['source']}"))
        print(f"{_c('1;32', 'Recalq')} › {result['answer']}")
        print(_RULE)
        print()
        history.append({"role": "user", "content": f"[sent an image] {question}".strip()})
        history.append({"role": "assistant", "content": result["answer"]})
        _persist_history()
        continue
    if user_input == "/diff":
        d = _agent.diff(os.getcwd())
        print(("\n" + d + "\n") if d else "  no agent changes to show\n")
        continue
    if user_input == "/undo":
        restored = _agent.undo(os.getcwd())
        print(f"  reverted: {', '.join(restored)}\n" if restored
              else "  nothing to undo — no agent changes left in this session\n")
        continue
    if user_input.startswith("/pr "):
        _task = user_input.split(" ", 1)[1].strip()
        _agent_model = model if _model_chosen else (agent_provider() or model)
        print(_c("2", f"🌿 agent working on a new branch in {os.getcwd()} ..."))
        _out = run_agent_pr(_task, _agent_model, user=_cli_user, namespace=_cli_namespace,
                            root=os.getcwd(), embedder=embedder,
                            on_step=lambda n, a: print(_c("33", f"  → {n} "
                                f"{a.get('path') or a.get('command') or a.get('question') or a.get('query') or ''}")),
                            architect_model=architect_provider(), history=history)
        print(f"\n{_c('1;32', 'Recalq')} › {_out}\n{_RULE}\n")
        continue
    if user_input == "/review" or user_input.startswith("/review "):
        print(_c("2", "🔎 reviewing ..."))
        _out = review_code(os.getcwd(), model if _model_chosen else (agent_provider() or model),
                           user_input[len("/review"):], user=_cli_user, namespace=_cli_namespace,
                           embedder=embedder,
                              on_step=lambda n, a: print(_c("2", f"  🔍 {n} {a.get('path') or a.get('query') or a.get('pattern') or ''}")))
        print(f"\n{_c('1;32', 'Recalq')} › {_out}\n{_RULE}\n")
        continue
    if user_input == "/sync" or user_input.startswith("/sync "):
        print(_c("2", "📚 syncing knowledge sources ..."))
        print("\n" + sync_knowledge(user_input[len("/sync"):].strip() or None) + "\n")
        continue
    if user_input == "/bench":
        import bench
        print(_c("2", "🏁 benchmarking every ready provider on small agent tasks "
                       "(a few minutes, uses real API calls) ..."))
        print("\n" + bench.run(embedder=embedder)[0] + "\n")
        continue
    if user_input.startswith("/agent "):
        _run_agent_and_print(user_input.split(" ", 1)[1].strip())
        continue
    _first_word = user_input.split(maxsplit=1)[0]
    if _first_word.startswith("/") and _first_word[1:] in _plugin_api.commands:
        _cname = _first_word[1:]
        _handler, _ = _plugin_api.commands[_cname]
        _arg = user_input[len(_first_word):].strip()
        try:
            _handler(_arg)
        except Exception as e:
            print(f"  plugin command '/{_cname}' failed: {e}")
        print()
        continue
    if not _project_scanned and any(t in user_input.lower() for t in _PROJECT_TRIGGERS):
        cwd = os.getcwd()
        print(f"\n📁 (auto) scanning {cwd} first, so this isn't answered blind...")
        try:
            summary = scan_project(cwd)
            import tempfile as _tempfile
            with _tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
                f.write(summary)
                tmp_path = f.name
            doc_id, _meta = ingest_document(tmp_path, "PROJECT_OVERVIEW.txt",
                                            namespace=_cli_namespace, uploaded_by="cli")
            os.unlink(tmp_path)
            recent_doc_id = doc_id
            _project_scanned = True
        except Exception as e:
            print(f"  (project scan failed, answering without it: {e})")

    if any(t in user_input.lower() for t in AGENT_TRIGGERS):
        print(_c("2", "  (sounds like an action, not just a question — routing to /agent; "
                       "say it plainly if you just wanted to talk about it)"))
        _run_agent_and_print(user_input)
        continue

    _query = _plugin_api.run_before(user_input)
    def _on_explore(name, args):
        preview = args.get("path") or args.get("query") or args.get("pattern") or args.get("url", "")
        print(_c("2", f"  🔍 {name} {preview}"))
    result = ask(_query, model, history=history, recent_doc_id=recent_doc_id,
                 has_attachments=bool(recent_doc_id), root=os.getcwd(), on_explore=_on_explore,
                 namespace=_cli_namespace, user=_cli_user)
    result["answer"] = _plugin_api.run_after(_query, result["answer"])
    src = result["source"]
    _last_answer = (result.get("entry_id"), result.get("cache_ns"))
    if src == "cache":
        meta = f"⚡ cache hit · sim={result['similarity']} · hits={result['hits']}"
        _prov = cache_provenance(result, _cli_user)
        if _prov:
            meta += f" · {_prov}"
    elif result.get("compositional"):
        meta = (f"🧩 compositional · {result.get('cache_entries_used',0)} cached "
                f"+ {len(result.get('missing_concepts',[]))} new · intent={result.get('intent')}")
    else:
        meta = f"◆ {src} · tokens={result.get('tokens_used',0)}"
    print()
    print(_c("2", meta))
    print(f"{_c('1;32', 'Recalq')} › {result['answer']}")
    print(_RULE)
    print()
    history.append({"role": "user", "content": user_input})
    history.append({"role": "assistant", "content": result["answer"]})
    _persist_history()
