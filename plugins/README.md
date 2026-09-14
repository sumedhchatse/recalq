# Plugins

Drop a `.py` file here defining a `register(api)` function and it loads
automatically the next time you start `./recalq`. See `example_tokens.py`
for a working one.

```python
def register(api):
    api.add_command("hello", lambda arg: print(f"hi {arg}"), help="say hi")
    api.on_before_ask(lambda query: query.strip())
    api.on_after_ask(lambda query, answer: answer + "\n(via a plugin)")
```

- `api.add_command(name, handler, help="...")` — new REPL command, called
  as `handler(arg_string)` when you type `name ...` at the `You:` prompt.
- `api.on_before_ask(fn)` — `fn(query) -> query`, runs right before a
  question is sent to the engine.
- `api.on_after_ask(fn)` — `fn(query, answer_text) -> answer_text`, runs on
  the answer text right before it's printed.

Need engine access (the `ask()` function, current model, provider list)?
Just `import memlayer` from your plugin file — it runs in the same
process and `sys.path`.

A file starting with `_` is skipped (useful for `_scratch.py` while you're
still writing one). A plugin that raises on load is skipped with a warning,
not a crash — one bad plugin can't take down the CLI.
