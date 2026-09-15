#!/usr/bin/env python3
"""
telegram_bot.py — chat with the MemLayer engine from Telegram.
No frameworks: long-polls Telegram's HTTP Bot API with stdlib urllib,
same pattern recalq_mcp.py used for its HTTP calls.

Setup:
  1. Create a bot with @BotFather, get its token.
  2. Message your new bot once, then run:
       python3 telegram_bot.py --whoami
     to see your numeric Telegram user id.
  3. Add to .env:
       TELEGRAM_BOT_TOKEN=123456:ABC...
       TELEGRAM_ALLOWED_USERS=<your id>[,<other id>...]
  4. Run:  python3 telegram_bot.py

Commands (same as the CLI): /model <name>, /providers, /stats, /cache, /reset, /help
Anything else is treated as a query to the engine. Each chat keeps a rolling
conversation history, so a follow-up like "what's that for?" after a photo
or a question works. /reset clears it.
Send a document (pdf/docx/txt, up to 20MB) to attach it — later questions in
that chat use it. Send a photo (caption optional) for vision Q&A.

TELEGRAM_ALLOWED_USERS is a hard allowlist — default-deny. Without it set,
the bot won't answer anyone; it just tells unknown users their id so the
owner can add them. LLM calls cost money and an open bot is a blank check.
"""
import os
import re
import sys
import json
import time
import logging
import tempfile
import urllib.request
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache_layer"))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import memlayer  # noqa: E402
import agent as _agent  # noqa: E402 — same engine-level agent loop the CLI's /agent uses

# memlayer's import above already claimed logging.basicConfig() (it's a
# no-op on later calls) and set the root console handler to WARNING-only,
# to keep per-request noise out of an interactive CLI. That would also
# silence this bot's own startup/error messages, so give this logger its
# own handler instead of relying on the root's.
log = logging.getLogger("telegram_bot")
log.setLevel(logging.INFO)
log.propagate = False
_h = logging.StreamHandler()
_h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(_h)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ALLOWED = {u.strip() for u in os.getenv("TELEGRAM_ALLOWED_USERS", "").split(",") if u.strip()}
API = f"https://api.telegram.org/bot{TOKEN}"

_chat_model = {}    # chat_id -> provider alias, in-memory only
_chat_doc = {}      # chat_id -> most recently attached doc_id, in-memory only
_chat_history = {}  # chat_id -> rolling [{"role","content"}, ...], in-memory only
_chat_root = {}     # chat_id -> agent working directory, in-memory only (/cd), default = bot's cwd

_offset = 0  # getUpdates cursor; module-level so a confirmation wait (below) can consume
             # updates without racing the main poll loop over the same offset


def _telegram_namespace(chat_id):
    """Combines this chat's own existing isolation (the privacy boundary
    between different Telegram users/conversations) with the actual project
    root (see /cd, _chat_root) — same fix as the CLI's project_namespace(),
    layered on top of the per-chat scope rather than replacing it, so
    switching projects within one chat can't mix their cache/docs either."""
    root = _chat_root.get(chat_id, os.getcwd())
    return f"telegram_{chat_id}_{memlayer.project_namespace(root)}"


def _get_history(chat_id):
    """In-memory first (fast path); falls back to Redis so a bot restart
    doesn't wipe every conversation — same persistence the CLI's /reset-
    able history uses, keyed by the same per-chat/project namespace."""
    if chat_id not in _chat_history:
        _chat_history[chat_id] = memlayer.load_history(_telegram_namespace(chat_id))
    return _chat_history[chat_id]


def _push_history(chat_id, user_text: str, assistant_text: str):
    h = _get_history(chat_id)
    h.append({"role": "user", "content": user_text})
    h.append({"role": "assistant", "content": assistant_text})
    del h[:-12]  # keep last 6 exchanges
    memlayer.save_history(h, _telegram_namespace(chat_id))


def _call(method: str, **params) -> dict:
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=35) as resp:
        return json.loads(resp.read().decode())


def _download_file(file_id: str) -> bytes:
    info = _call("getFile", file_id=file_id)
    file_path = info["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
    with urllib.request.urlopen(url, timeout=35) as resp:
        return resp.read()


def _plain_text(text: str) -> str:
    """Strip markdown syntax so replies read like a normal text message
    instead of showing raw **bold**, # headers, `code` etc. — the engine's
    answers are written assuming a markdown renderer, which Telegram's
    default plain sendMessage isn't."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"```[a-zA-Z]*\n?", "", text).replace("```", "")
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"^\s*[\*\+]\s+", "- ", text, flags=re.MULTILINE)
    return text.strip()


def send(chat_id, text: str):
    text = _plain_text(text)
    for i in range(0, len(text), 4000):  # Telegram's message length cap
        try:
            _call("sendMessage", chat_id=chat_id, text=text[i:i + 4000])
        except Exception as e:
            log.error(f"sendMessage failed: {e}")


def _get_updates(timeout=30):
    """Poll once, advancing the shared _offset. Returns the list of updates."""
    global _offset
    resp = _call("getUpdates", offset=_offset, timeout=timeout)
    updates = resp.get("result", [])
    for u in updates:
        _offset = max(_offset, u["update_id"] + 1)
    return updates


CONFIRM_TIMEOUT_S = 300


def _wait_for_reply(chat_id, user_id, timeout_s=CONFIRM_TIMEOUT_S):
    """Blocks until `user_id` replies in `chat_id`, or timeout_s elapses.
    Any other user's message that arrives meanwhile is still handled right
    away, so one pending agent confirmation doesn't freeze the bot for
    everyone else.
    ponytail: single-threaded wait — fine for a small trusted team (this
    bot has no concurrency anywhere else either); a per-chat worker or
    asyncio loop is the upgrade if the team outgrows that."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            updates = _get_updates(timeout=10)
        except Exception as e:
            log.error(f"getUpdates failed while waiting for confirmation: {e}")
            time.sleep(2)
            continue
        for update in updates:
            if "message" not in update:
                continue
            m = update["message"]
            if m["chat"]["id"] == chat_id and str(m.get("from", {}).get("id", "")) == user_id:
                return (m.get("text") or "").strip()
            try:
                handle_message(m)
            except Exception as e:
                log.error(f"handler error (during confirm wait): {e}")
    return None


def _telegram_confirm(chat_id, user_id):
    def confirm(desc):
        send(chat_id, f"{desc}\n\nReply yes or no.")
        reply = _wait_for_reply(chat_id, user_id)
        return bool(reply) and reply.lower().startswith("y")
    return confirm


def _telegram_on_step(chat_id):
    def on_step(name, args):
        if name == "update_plan":
            lines = ["plan:"]
            for s in args.get("steps", []):
                mark = {"done": "[x]", "in_progress": "[>]"}.get(s.get("status"), "[ ]")
                lines.append(f"  {mark} {s.get('task','')}")
            send(chat_id, "\n".join(lines))
            return
        preview = (args.get("path") or args.get("command") or args.get("query")
                   or args.get("pattern") or args.get("url", ""))
        send(chat_id, f"→ {name} {preview}")
    return on_step


def _run_agent(chat_id, user_id, task):
    """Thin Telegram-side adapter onto the engine's agent loop — same
    agent.run() the CLI's /agent uses, just with Telegram-shaped confirm/
    progress callbacks instead of stdin/print."""
    root = _chat_root.get(chat_id, os.getcwd())
    send(chat_id, f"\U0001f916 agent working in {root} ...")
    try:
        answer = _agent.run(task, _chat_model.get(chat_id, memlayer.default_model()),
                             root=root, confirm=_telegram_confirm(chat_id, user_id),
                             on_step=_telegram_on_step(chat_id), embedder=memlayer.embedder,
                             architect_model=memlayer.architect_provider(),
                             history=_get_history(chat_id))
    except Exception as e:
        log.error(f"agent.run failed: {e}")
        answer = f"Agent failed: {e}"
    send(chat_id, answer)
    _push_history(chat_id, task, answer)


def handle_command(chat_id, user_id, cmd, arg) -> bool:
    ns = _telegram_namespace(chat_id)
    if cmd in ("/start", "/help"):
        send(chat_id, "Commands: /model <name> | /providers | /stats | /cache | /reset | /help\n"
                       "/cd <path> | /agent <task> — point the agent at a project directory on "
                       "the server, then have it read/edit files and run shell commands there.\n"
                       "Send a PDF/DOCX/TXT to attach it (questions after use it).\n"
                       "Send a photo (with an optional caption) to ask about an image — "
                       "you can ask follow-ups about it afterward.\n"
                       "Anything else is a question for the engine — phrasing it as an action "
                       "(\"find the bug in x.py\", \"add a feature that...\") runs the agent too.")
    elif cmd == "/reset":
        _chat_history.pop(chat_id, None)
        memlayer.clear_history(_telegram_namespace(chat_id))
        _chat_doc.pop(chat_id, None)
        send(chat_id, "Conversation and attached-doc context cleared.")
    elif cmd == "/providers":
        lines = [f"{a:14s} -> {m}  {'ok' if ready else 'MISSING KEY'}"
                 for a, m, ready in memlayer.list_providers()]
        send(chat_id, "\n".join(lines))
    elif cmd == "/model":
        if not arg:
            send(chat_id, f"current: {_chat_model.get(chat_id, memlayer.default_model())}")
        else:
            _chat_model[chat_id] = arg
            send(chat_id, f"model set to {arg}")
    elif cmd == "/stats":
        s = memlayer.get_stats(ns)
        send(chat_id, f"queries={s['total_queries']} hits={s['cache_hits']} "
                       f"llm_calls={s['llm_calls']}")
    elif cmd == "/cache":
        entries = memlayer.list_cache_entries(ns)[:10]
        send(chat_id, "\n".join(f"[{e.get('hits',0)} hits] {e['query'][:70]}" for e in entries)
             or "(empty)")
    elif cmd == "/cd":
        if not arg:
            send(chat_id, f"current: {_chat_root.get(chat_id, os.getcwd())}")
        elif os.path.isdir(arg):
            _chat_root[chat_id] = os.path.abspath(arg)
            _chat_doc.pop(chat_id, None)  # a doc attached in the old project shouldn't
                                           # be "recently attached" context in the new one
            _chat_history.pop(chat_id, None)  # drop the old project's in-memory history so
                                               # the next _get_history() reloads the new
                                               # project's own (namespace changed with root)
            send(chat_id, f"agent working directory set to {_chat_root[chat_id]} "
                           f"(cache/docs/history now scoped to this project)")
        else:
            send(chat_id, f"no such directory: {arg}")
    elif cmd == "/agent":
        if not arg:
            send(chat_id, "usage: /agent <task>")
        else:
            _run_agent(chat_id, user_id, arg)
    else:
        return False
    return True


DOC_EXTS = {"pdf", "docx", "txt"}


def handle_document(chat_id, user_id, doc: dict):
    filename = doc.get("file_name", "file")
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext not in DOC_EXTS:
        send(chat_id, f"Unsupported file type .{ext} — only pdf, docx, txt.")
        return
    if doc.get("file_size", 0) > 20 * 1024 * 1024:
        send(chat_id, "File too large — Telegram bots can only download up to 20MB.")
        return
    try:
        data = _download_file(doc["file_id"])
        with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as f:
            f.write(data)
            path = f.name
        doc_id, meta = memlayer.ingest_document(
            path, filename, namespace=_telegram_namespace(chat_id), uploaded_by=user_id)
        _chat_doc[chat_id] = doc_id
        send(chat_id, f"Ingested '{meta['filename']}' — {meta['chunk_count']} chunks. Ask away.")
    except Exception as e:
        log.error(f"document ingest failed: {e}")
        send(chat_id, f"Failed to ingest document: {e}")
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


def handle_photo(chat_id, user_id, photo_sizes: list, caption: str):
    # Don't use the chat's text model here — it may not be vision-capable
    # (e.g. groq's configured model rejects image content outright). Let
    # ask_image fall back to its own vision-capable default.
    largest = max(photo_sizes, key=lambda p: p.get("file_size", 0))
    try:
        data = _download_file(largest["file_id"])
        result = memlayer.ask_image(data, "image/jpeg", caption,
                                     namespace=_telegram_namespace(chat_id), user=user_id)
        answer = result.get("answer", "(no answer)")
        send(chat_id, f"{answer}\n\n— via {result.get('source', '?')}")
        _push_history(chat_id, f"[sent an image] {caption}".strip(), answer)
    except Exception as e:
        log.error(f"ask_image failed: {e}")
        send(chat_id, f"Failed to read image: {e}")


def handle_message(msg: dict):
    chat_id = msg["chat"]["id"]
    user_id = str(msg.get("from", {}).get("id", ""))

    if ALLOWED and user_id not in ALLOWED:
        send(chat_id, f"Not authorized. Ask the bot owner to add your id ({user_id}) "
                       f"to TELEGRAM_ALLOWED_USERS.")
        log.warning(f"rejected message from unauthorized user {user_id}")
        return
    if not ALLOWED:
        send(chat_id, f"Bot has no allowlist configured. Your id is {user_id} — "
                       f"add it to TELEGRAM_ALLOWED_USERS in .env and restart the bot.")
        return

    if "document" in msg:
        handle_document(chat_id, user_id, msg["document"])
        return
    if "photo" in msg:
        handle_photo(chat_id, user_id, msg["photo"], (msg.get("caption") or "").strip())
        return

    text = (msg.get("text") or "").strip()
    if not text:
        return

    if text.startswith("/"):
        parts = text.split(maxsplit=1)
        cmd, arg = parts[0], (parts[1] if len(parts) > 1 else "")
        if handle_command(chat_id, user_id, cmd, arg.strip()):
            return

    if any(t in text.lower() for t in memlayer.AGENT_TRIGGERS):
        send(chat_id, "(sounds like an action, not just a question — running the agent; "
                       "say it plainly if you just wanted to talk about it)")
        _run_agent(chat_id, user_id, text)
        return

    model = _chat_model.get(chat_id, memlayer.default_model())
    recent_doc_id = _chat_doc.get(chat_id)
    try:
        result = memlayer.ask(text, model, history=_get_history(chat_id),
                              namespace=_telegram_namespace(chat_id), user=user_id,
                              recent_doc_id=recent_doc_id, has_attachments=bool(recent_doc_id),
                              root=_chat_root.get(chat_id, os.getcwd()))
        answer = result.get("answer") or "(no answer)"
        send(chat_id, f"{answer}\n\n— via {result.get('source', '?')}")
        _push_history(chat_id, text, answer)
    except Exception as e:
        log.error(f"ask() failed: {e}")
        send(chat_id, f"Error: {e}")


def main():
    if not TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN not set in .env")

    if "--whoami" in sys.argv:
        resp = _call("getUpdates", limit=5)
        msgs = [u["message"] for u in resp.get("result", []) if "message" in u]
        if not msgs:
            sys.exit("No messages seen yet — message your bot in Telegram first, then rerun.")
        for m in msgs[-5:]:
            u = m.get("from", {})
            print(f"{u.get('id')}  {u.get('username') or u.get('first_name')}: {m.get('text','')!r}")
        return
    if not ALLOWED:
        log.warning("TELEGRAM_ALLOWED_USERS not set — bot will reply to no one until it is")

    log.info("Telegram bot started, long-polling...")
    while True:
        try:
            updates = _get_updates(timeout=30)
        except Exception as e:
            log.error(f"getUpdates failed: {e} — retrying in 5s")
            time.sleep(5)
            continue
        for update in updates:
            if "message" in update:
                try:
                    handle_message(update["message"])
                except Exception as e:
                    log.error(f"handler error: {e}")


if __name__ == "__main__":
    main()
