"""
example_tokens.py — a working example plugin: one new command, one hook.

- `tokens <text>` — rough token-count estimate for a piece of text
  (chars/4, the same crude-but-useful heuristic providers themselves
  quote before an exact count is available).
- A before_ask hook that collapses runs of whitespace/blank lines in the
  query — a real (if small) token saving, invisible to you, applied to
  every question automatically once this file exists.

Delete this file, or rename it with a leading "_", to turn it off.
"""
import re


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _tokens_command(arg: str):
    if not arg:
        print("  usage: tokens <text>")
        return
    print(f"  ~{_estimate_tokens(arg)} tokens ({len(arg)} chars)")


def _collapse_whitespace(query: str) -> str:
    return re.sub(r"\s+", " ", query).strip()


def register(api):
    api.add_command("tokens", _tokens_command, help="tokens <text> — estimate token count")
    api.on_before_ask(_collapse_whitespace)
