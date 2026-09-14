"""
plugins.py — minimal plugin loader for the CLI, in the spirit of how
Claude Code or oh-my-zsh let you drop a file in and get new behavior.

A plugin is any .py file in plugins/ (repo root) not starting with "_",
defining a `register(api)` function. It can:
  - api.add_command(name, handler, help="...")   — a new REPL command,
    called as handler(arg_string). Needs engine access (ask(), the
    current model, etc.)? Just `import memlayer` from the plugin file —
    it runs in the same process, same sys.path as everything in
    cache_layer/.
  - api.on_before_ask(fn)                        — fn(query) -> query,
    runs on the text right before it's sent to ask().
  - api.on_after_ask(fn)                         — fn(query, answer) ->
    answer, runs on the answer TEXT (not the full result dict — that
    shape is engine-internal and can change) right before it's printed.

No manifest, no marketplace, no sandboxing — it's local, trusted code you
put there yourself, same trust level as the rest of this repo.
"""
import os
import logging
import importlib.util

log = logging.getLogger("memlayer")


class PluginAPI:
    def __init__(self):
        self.commands = {}       # name -> (handler, help_text)
        self.before_hooks = []   # fn(query) -> query
        self.after_hooks = []    # fn(query, answer_text) -> answer_text
        self.loaded = []         # plugin names that loaded successfully

    def add_command(self, name, handler, help=""):
        self.commands[name] = (handler, help)

    def on_before_ask(self, fn):
        self.before_hooks.append(fn)

    def on_after_ask(self, fn):
        self.after_hooks.append(fn)

    def run_before(self, query):
        for fn in self.before_hooks:
            try:
                query = fn(query) or query
            except Exception as e:
                log.warning(f"plugin before_ask hook failed: {e}")
        return query

    def run_after(self, query, answer):
        for fn in self.after_hooks:
            try:
                answer = fn(query, answer) or answer
            except Exception as e:
                log.warning(f"plugin after_ask hook failed: {e}")
        return answer


def load_plugins(plugins_dir: str) -> PluginAPI:
    api = PluginAPI()
    if not os.path.isdir(plugins_dir):
        return api
    for fname in sorted(os.listdir(plugins_dir)):
        if not fname.endswith(".py") or fname.startswith("_"):
            continue
        path = os.path.join(plugins_dir, fname)
        name = fname[:-3]
        try:
            spec = importlib.util.spec_from_file_location(f"recalq_plugin_{name}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if hasattr(mod, "register"):
                mod.register(api)
                api.loaded.append(name)
        except Exception as e:
            log.warning(f"plugin '{fname}' failed to load: {e}")
    return api
