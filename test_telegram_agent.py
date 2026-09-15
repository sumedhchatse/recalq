"""assert-based self-check for telegram_bot.py's agent wiring — no real
Telegram API calls, _call/_agent.run are monkeypatched. Needs the Redis/
embedding backend running (same as the bot itself). Run:
    python3 test_telegram_agent.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache_layer"))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_ALLOWED_USERS", "111")

import telegram_bot as tb  # noqa: E402

# memlayer.py loads .env with override=True, so it wins over the
# setdefault() above once `import telegram_bot` pulls memlayer in — set
# the test allowlist directly instead of fighting that.
tb.ALLOWED = {"111"}


def test_cd_sets_root():
    with tempfile.TemporaryDirectory() as d:
        tb.send = lambda chat_id, text: None
        assert tb.handle_command(1, "111", "/cd", d) is True
        assert tb._chat_root[1] == os.path.abspath(d)


def test_agent_command_calls_shared_agent_run():
    calls = {}

    def fake_run(task, model, root=None, confirm=None, on_step=None, **kw):
        calls["task"] = task
        calls["root"] = root
        return "done"

    tb._agent.run = fake_run
    sent = []
    tb.send = lambda chat_id, text: sent.append(text)
    tb._chat_root[2] = "/tmp"
    assert tb.handle_command(2, "111", "/agent", "do the thing") is True
    assert calls["task"] == "do the thing"
    assert calls["root"] == "/tmp"
    assert "done" in sent


def test_wait_for_reply_matches_same_user_and_relays_others():
    other_msg = {"update_id": 1, "message": {"chat": {"id": 99}, "from": {"id": 222}, "text": "hi"}}
    target_msg = {"update_id": 2, "message": {"chat": {"id": 5}, "from": {"id": 111}, "text": "yes"}}
    rounds = [[other_msg], [target_msg]]

    def fake_get_updates(timeout=30):
        return rounds.pop(0) if rounds else []

    tb._get_updates = fake_get_updates
    dispatched = []
    orig_handle_message = tb.handle_message
    tb.handle_message = lambda m: dispatched.append(m)
    try:
        reply = tb._wait_for_reply(5, "111", timeout_s=5)
    finally:
        tb.handle_message = orig_handle_message
    assert reply == "yes", reply
    assert len(dispatched) == 1 and dispatched[0]["from"]["id"] == 222


def test_auto_trigger_routes_to_agent():
    calls = {}
    tb._agent.run = lambda task, model, root=None, confirm=None, on_step=None, **kw: calls.setdefault("task", task) or "ok"
    tb.send = lambda chat_id, text: None
    tb.handle_message({"chat": {"id": 3}, "from": {"id": 111},
                        "text": "please fix the bug in app.py"})
    assert calls["task"] == "please fix the bug in app.py"


if __name__ == "__main__":
    test_cd_sets_root()
    test_agent_command_calls_shared_agent_run()
    test_wait_for_reply_matches_same_user_and_relays_others()
    test_auto_trigger_routes_to_agent()
    print("ok")
