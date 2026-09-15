"""
agent.py — minimal coding-agent loop: read/write files and run shell
commands, via any litellm tool-calling-capable provider from client.yaml.

Every write_file/run_shell call is confirmed before it executes — there is
no sandbox here, just a permission prompt, same trust model as you running
the command yourself.
"""
import os
import re
import sys
import time
import json
import socket
import difflib
import ipaddress
import subprocess
from urllib.parse import urlparse

import numpy as np
import requests as _requests

from providers import chat_completion

MAX_STEPS = 15
SHELL_TIMEOUT = 60
DIFF_PREVIEW_LINES = 60

# ── search_code: semantic file search over the project ────────────────
# Reuses whatever embedder the caller already runs (memlayer's HTTP
# embedding service + local fallback, same one it uses for cache
# similarity) instead of the model blindly guessing paths via list_dir.
SEARCH_EXCLUDE_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv",
                       ".idea", ".vscode", "dist", "build", ".pytest_cache"}
SEARCH_EXCLUDE_EXTS = {".pyc", ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip",
                       ".lock", ".woff", ".ttf", ".ico", ".mp4", ".mp3", ".bin"}
MAX_FILE_BYTES_FOR_INDEX = 50_000
MAX_FILES_INDEXED = 300
INDEX_TTL_SECS = 300  # rebuild the index at most every 5 minutes per root

_INDEX_CACHE = {}  # abs root -> {"built_at", "vectors": np.ndarray, "paths": [str]}


def _iter_project_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SEARCH_EXCLUDE_DIRS and not d.startswith(".")]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in SEARCH_EXCLUDE_EXTS:
                continue
            full = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(full) > MAX_FILE_BYTES_FOR_INDEX:
                    continue
            except OSError:
                continue
            yield full


def _build_index(root, embedder):
    entries = []
    for full in list(_iter_project_files(root))[:MAX_FILES_INDEXED]:
        try:
            with open(full, "r", errors="ignore") as f:
                text = f.read()
        except Exception:
            continue
        if not text.strip():
            continue
        rel = os.path.relpath(full, root)
        # Embed the path alongside a content snippet, so a query can match
        # on either an obvious filename or what's actually inside the file.
        entries.append((rel, f"{rel}\n{text[:2000]}"))
    if not entries:
        return {"built_at": time.time(), "vectors": None, "paths": []}
    vectors = np.asarray(embedder.encode([s for _, s in entries], convert_to_tensor=False))
    return {"built_at": time.time(), "vectors": vectors, "paths": [p for p, _ in entries]}


def _get_index(root, embedder):
    root = os.path.abspath(root)
    cached = _INDEX_CACHE.get(root)
    if cached and time.time() - cached["built_at"] < INDEX_TTL_SECS:
        return cached
    index = _build_index(root, embedder)
    _INDEX_CACHE[root] = index
    return index


def search_code(root, embedder, query, top_k=8):
    if embedder is None:
        return "search_code unavailable (no embedder configured)"
    index = _get_index(root, embedder)
    if not index["paths"]:
        return "no indexable text files found under this root"
    q_vec = np.asarray(embedder.encode(query, convert_to_tensor=False), dtype=np.float32)
    mat = index["vectors"].astype(np.float32)
    qn = q_vec / (np.linalg.norm(q_vec) + 1e-9)
    mn = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
    scores = mn @ qn
    top_idx = np.argsort(-scores)[:top_k]
    return "\n".join(f"{index['paths'][i]}  (relevance={scores[i]:.2f})" for i in top_idx)


GREP_MAX_MATCHES = 60


def grep_code(root, pattern, path="."):
    """Exact/regex search across the project's files — complements
    search_code's semantic ranking for when you know the literal text
    (e.g. every call site of a function), which a semantic match can
    under-rank if the wording doesn't line up well."""
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"error: invalid pattern: {e}"
    try:
        search_root = _safe_path(root, path)
    except ValueError as e:
        return f"error: {e}"
    matches = []
    for full in _iter_project_files(search_root):
        try:
            with open(full, errors="ignore") as f:
                for i, line in enumerate(f, 1):
                    if rx.search(line):
                        rel = os.path.relpath(full, root)
                        matches.append(f"{rel}:{i}: {line.strip()[:200]}")
                        if len(matches) >= GREP_MAX_MATCHES:
                            break
        except Exception:
            continue
        if len(matches) >= GREP_MAX_MATCHES:
            break
    if not matches:
        return "no matches"
    suffix = f"\n... (stopped at {GREP_MAX_MATCHES} matches)" if len(matches) >= GREP_MAX_MATCHES else ""
    return "\n".join(matches) + suffix


# ── web_fetch: opt-in only — Recalq is offline-first by design, so this
# tool doesn't even appear in the schema unless explicitly enabled. ───────
WEB_FETCH_ENABLED = os.getenv("AGENT_WEB_FETCH", "0") == "1"
WEB_FETCH_MAX_CHARS = 8000
WEB_FETCH_TIMEOUT = 15


def _is_safe_url(url):
    """Refuse anything but a public http(s) host — a model-driven fetch
    tool is a textbook SSRF vector otherwise (e.g. tricking it into
    hitting a cloud metadata endpoint or an internal service)."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        ip = socket.gethostbyname(parsed.hostname)
        addr = ipaddress.ip_address(ip)
        return not (addr.is_private or addr.is_loopback or addr.is_link_local
                    or addr.is_reserved or addr.is_multicast)
    except Exception:
        return False


def web_fetch(url):
    if not _is_safe_url(url):
        return "error: refusing to fetch this URL (not a public http(s) address)"
    try:
        resp = _requests.get(url, timeout=WEB_FETCH_TIMEOUT,
                             headers={"User-Agent": "Recalq-agent/1.0"})
        resp.raise_for_status()
        text = re.sub(r"<[^>]+>", " ", resp.text)  # crude tag strip, no new dependency
        text = re.sub(r"\s+", " ", text).strip()
        return text[:WEB_FETCH_MAX_CHARS]
    except Exception as e:
        return f"error fetching url: {e}"


_TTY = sys.stdout.isatty()


def _color(code, text):
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def _colorize_diff(diff_text):
    if not _TTY:
        return diff_text
    out = []
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            out.append(_color(32, line))
        elif line.startswith("-") and not line.startswith("---"):
            out.append(_color(31, line))
        elif line.startswith("@@"):
            out.append(_color(36, line))
        else:
            out.append(line)
    return "\n".join(out)


def _make_diff(path, old_content, new_content, verb="write"):
    """Unified diff preview for a write_file/edit_file call — 'new file'
    framing when there's nothing to diff against, truncated so a huge file
    doesn't flood the confirmation prompt."""
    old_lines = (old_content or "").splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    label = "new file" if old_content is None else "modified"
    diff_lines = list(difflib.unified_diff(
        old_lines, new_lines, fromfile=f"a/{path}", tofile=f"b/{path}", lineterm=""))
    body = "\n".join(diff_lines[:DIFF_PREVIEW_LINES])
    if len(diff_lines) > DIFF_PREVIEW_LINES:
        body += f"\n  ... ({len(diff_lines) - DIFF_PREVIEW_LINES} more lines)"
    header = _color(33, f"  ⚠ {verb} {path} ({label}, {len(new_content)} chars):")
    return f"{header}\n{_colorize_diff(body)}"

TOOLS = [
    {"type": "function", "function": {
        "name": "search_code",
        "description": "Semantic search over the project's files — finds files relevant to a "
                        "plain-language description, without needing to know exact filenames or "
                        "paths. Prefer this FIRST over list_dir/read_file when you don't already "
                        "know exactly which file you need — don't guess a path and treat it not "
                        "existing as meaningful.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "What you're looking for, in plain language"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "grep_code",
        "description": "Exact/regex search over file contents — finds every literal occurrence "
                        "(e.g. every call site of a function or use of a variable name), unlike "
                        "search_code's semantic ranking. Use this when you know the exact text.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Regex pattern (plain text also works)"},
            "path": {"type": "string", "description": "Subdirectory to search, default the whole project"}},
            "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read a text file's contents.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path, relative to the working directory"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "Make a targeted edit to an EXISTING file: replace one exact, unique "
                        "occurrence of old_string with new_string. Prefer this over write_file "
                        "when editing an existing file — it doesn't require resending the whole "
                        "file, and can't accidentally clobber parts you didn't mean to touch. "
                        "old_string must match the file's current content exactly (including "
                        "whitespace) and occur exactly once — include enough surrounding context "
                        "to make it unique.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"}},
            "required": ["path", "old_string", "new_string"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Create a new file, or fully overwrite an existing one. For an existing "
                        "file, prefer edit_file unless you're deliberately replacing its entire "
                        "content.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "list_dir", "description": "List files in a directory.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Directory path, default '.'"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "run_shell", "description": "Run a shell command and return its stdout+stderr.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"}},
            "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "update_plan",
        "description": "Report your current step-by-step plan and progress on a multi-step task, "
                        "so the user can see what you're doing. Call once near the start with the "
                        "full plan, and again whenever a step's status changes. Skip this for a "
                        "simple one- or two-step task.",
        "parameters": {"type": "object", "properties": {
            "steps": {"type": "array", "items": {"type": "object", "properties": {
                "task": {"type": "string"},
                "status": {"type": "string", "enum": ["pending", "in_progress", "done"]}},
                "required": ["task", "status"]}}},
            "required": ["steps"]}}},
] + ([{"type": "function", "function": {
        "name": "web_fetch",
        "description": "Fetch a URL's text content — for checking current docs/APIs when local "
                        "knowledge might be stale or insufficient. Strips HTML, returns plain text.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}}, "required": ["url"]}}}] if WEB_FETCH_ENABLED else [])


def _safe_path(root, path):
    """Resolve `path` under `root`, refusing anything that escapes it —
    same directory-scoping Claude Code/Cursor apply to their file tools."""
    root_real = os.path.realpath(root)
    full = os.path.realpath(os.path.join(root, path))
    if full != root_real and not full.startswith(root_real + os.sep):
        raise ValueError(f"'{path}' escapes the working directory — refusing")
    return full


def _run_tool(name, args, root, confirm, embedder=None):
    if name == "search_code":
        return search_code(root, embedder, args["query"])
    if name == "grep_code":
        return grep_code(root, args["pattern"], args.get("path", "."))
    if name == "read_file":
        full = _safe_path(root, args["path"])
        with open(full) as f:
            return f.read()[:20000]
    if name == "list_dir":
        full = _safe_path(root, args.get("path", "."))
        return "\n".join(sorted(os.listdir(full)))
    if name == "edit_file":
        full = _safe_path(root, args["path"])
        if not os.path.exists(full):
            return f"error: {args['path']} does not exist — use write_file to create a new file"
        with open(full) as f:
            content = f.read()
        old_s, new_s = args.get("old_string", ""), args.get("new_string", "")
        count = content.count(old_s)
        if count == 0:
            return "error: old_string not found — it must match the file's current content exactly"
        if count > 1:
            return (f"error: old_string is not unique ({count} occurrences) — include more "
                    "surrounding context to make it unique")
        new_content = content.replace(old_s, new_s, 1)
        if not confirm(_make_diff(args["path"], content, new_content, verb="edit")):
            return "user declined this edit"
        with open(full, "w") as f:
            f.write(new_content)
        return f"edited {args['path']}"
    if name == "write_file":
        full = _safe_path(root, args["path"])
        new_content = args.get("content", "")
        old_content = None
        if os.path.exists(full):
            with open(full) as f:
                old_content = f.read()
        if not confirm(_make_diff(args["path"], old_content, new_content)):
            return "user declined this write"
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        with open(full, "w") as f:
            f.write(new_content)
        return f"wrote {args['path']}"
    if name == "run_shell":
        if not confirm(_color(33, f"  ⚠ run: {args['command']}")):
            return "user declined this command"
        proc = subprocess.run(args["command"], shell=True, cwd=root, capture_output=True,
                               text=True, timeout=SHELL_TIMEOUT)
        out = (proc.stdout + proc.stderr)[:8000]
        return out or f"(exit {proc.returncode}, no output)"
    if name == "update_plan":
        return "plan noted"  # purely a display mechanism (see on_step) — no state to keep
    if name == "web_fetch":
        return web_fetch(args["url"])
    return f"unknown tool: {name}"


def _default_confirm(desc):
    print(desc)
    return input("  allow? [y/N] ").strip().lower() == "y"


# ponytail: the exploring system prompt below nudges the model to list
# before guessing and not fixate on a wrong path, but a small/free default
# model (groq's gpt-oss-120b, gemini-flash-lite as its fallback) can still
# loop on a wrong guess or misreport a tool's own error text instead of
# relaying it — a model-capability ceiling, not a fixable prompt bug. If it
# matters more than the free tier's cost savings, the upgrade path is
# letting exploration use a stronger model than the chat default (e.g. an
# explicit `explore_model` param here, distinct from the answering model).
READONLY_TOOLS = [t for t in TOOLS if t["function"]["name"] in
                 ("search_code", "grep_code", "read_file", "list_dir", "web_fetch")]


def _assistant_msg_dict(msg, tool_calls):
    """Minimal, provider-neutral form of an assistant tool-call message to
    replay back into the next request. NOT the raw SDK object/model_dump():
    that carries provider-specific extra fields (images, refusal, audio,
    annotations, ...) which some providers (e.g. Groq) reject outright when
    they're echoed back on the next call — silently forcing a fallback to a
    slower/paid provider on every multi-step task."""
    return {
        "role": "assistant",
        "content": msg.content,
        "tool_calls": [{
            "id": tc.id, "type": "function",
            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
        } for tc in tool_calls],
    }


def _loop(messages, model, root, confirm, on_step, tools, max_steps, max_tokens, embedder=None):
    """Shared tool-use loop: call the model, run whatever tools it asks
    for, feed results back, repeat until it stops calling tools or
    max_steps is hit. Returns (answer_text, model_used, total_tokens,
    used_tools) — tokens summed across every call in the loop, since a
    multi-step task is more than one completion; used_tools is True iff at
    least one tool was actually called (the answer is grounded in project
    files, not just general knowledge — callers use this to decide whether
    the answer is safe to cache in a namespace shared across projects)."""
    confirm = confirm or _default_confirm
    model_used = model
    total_tokens = 0
    used_tools = False
    for _ in range(max_steps):
        resp = chat_completion(model, messages, max_tokens=max_tokens, tools=tools)
        model_used = getattr(resp, "model", None) or model_used
        usage = getattr(resp, "usage", None)
        if usage is not None:
            total_tokens += getattr(usage, "total_tokens", None) or (
                getattr(usage, "prompt_tokens", 0) + getattr(usage, "completion_tokens", 0))
        msg = resp.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            return (msg.content or "(no response)"), model_used, total_tokens, used_tools
        used_tools = True
        messages.append(_assistant_msg_dict(msg, tool_calls))
        for tc in tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if on_step:
                on_step(name, args)
            try:
                result = _run_tool(name, args, root, confirm, embedder)
            except Exception as e:
                result = f"error: {e}"
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(result)})
    # Hit the step cap mid-exploration — everything read so far is still in
    # `messages`, so force one last call instead of dropping it all on the
    # floor with a dead-end placeholder. Omitting `tools` alone isn't a
    # reliable enough signal across providers to stop calling tools and
    # actually answer (seen empty content back from it) — say so explicitly.
    messages.append({"role": "user", "content":
        "Give your best answer now based on everything you've read so far — no more tool calls."})
    resp = chat_completion(model, messages, max_tokens=max_tokens)
    model_used = getattr(resp, "model", None) or model_used
    usage = getattr(resp, "usage", None)
    if usage is not None:
        total_tokens += getattr(usage, "total_tokens", None) or (
            getattr(usage, "prompt_tokens", 0) + getattr(usage, "completion_tokens", 0))
    content = resp.choices[0].message.content
    return (content or "(stopped after max steps — task may be incomplete)"), model_used, total_tokens, used_tools


ARCHITECT_MAX_STEPS = 10
ARCHITECT_MAX_TOKENS = 1200


def _plan(task, history, architect_model, root, on_step, embedder):
    """Read-only planning pass (the Aider 'architect' technique): a
    (usually stronger) model explores the project and writes a concrete
    plan — which file(s), what change — but never touches anything itself.
    Returns the plan text."""
    messages = [
        {"role": "system", "content":
            f"You are a senior engineer planning a change in {os.path.abspath(root)}. Explore "
            "with search_code/read_file/list_dir as needed, then write a concise, concrete plan: "
            "exactly which file(s) to change and what the change should be. Do not write any "
            "code yourself, just the plan — someone else will execute it."},
        *(history or []),
        {"role": "user", "content": task},
    ]
    plan_text, _, _, _ = _loop(messages, architect_model, root, None, on_step, READONLY_TOOLS,
                               ARCHITECT_MAX_STEPS, ARCHITECT_MAX_TOKENS, embedder)
    return plan_text


def run(task, model, root=".", confirm=None, on_step=None, embedder=None, architect_model=None,
       history=None):
    """Full read/write/shell agent loop for an explicit task ('/agent ...'
    or an action-shaped auto-trigger). Returns the final text answer.

    confirm(desc) -> bool gates write_file/run_shell (default: prompt on
    stdin). on_step(name, args) is an optional progress callback. embedder,
    if given, backs search_code (see search_code() above) — without it,
    exploration falls back to list_dir/read_file guessing. architect_model,
    if given, runs a read-only planning pass on that model FIRST (see
    _plan() above) and seeds the edit pass with its plan — one extra LLM
    call, in exchange for much more reliable exploration on a hard/
    ambiguous task; leave it None (the default) to skip straight to editing
    exactly as before. history, if given, is the conversation so far
    ([{"role","content"}, ...], already trimmed by the caller) — without
    it, a follow-up like "do it" has no idea what "it" refers to."""
    plan = _plan(task, history, architect_model, root, on_step, embedder) if architect_model else None
    plan_context = (f"\n\nA plan has already been made for this task:\n{plan}\n\nExecute it "
                    "precisely — re-read a file first if you need its exact current contents "
                    "before editing.") if plan else ""
    messages = [
        {"role": "system", "content":
            f"You are a coding agent working in {os.path.abspath(root)}. Use the tools to "
            "read/write files and run shell commands to complete the user's task. Use "
            "search_code first to find the right file(s) instead of guessing a path with "
            "list_dir/read_file, and grep_code when you know the exact text/name you're looking "
            "for. Prefer edit_file over write_file for an existing file — it's a targeted "
            "replace, not a full resend, so it can't accidentally clobber parts you didn't mean "
            "to touch. Reserve write_file for new files or deliberate full rewrites. For a task "
            "with several distinct steps, call update_plan so progress is visible; skip it for "
            "something short. Keep changes minimal and scoped to the request. When done, reply "
            "with a short summary and make no further tool calls — and be precise in that "
            "summary: a write_file/edit_file/run_shell result of 'user declined this write/edit/"
            "command' means that change was NOT applied. Never describe a declined or failed "
            "change as done, addressed, or fixed; say plainly it wasn't applied and why."
            f"{plan_context}"},
        *(history or []),
        {"role": "user", "content": task},
    ]
    answer_text, _, _, _ = _loop(messages, model, root, confirm, on_step, TOOLS, MAX_STEPS, 2000, embedder)
    return answer_text


def answer(messages, model, root=".", max_steps=10, max_tokens=800, on_step=None, embedder=None):
    """Read-only variant for plain Q&A: search_code/read_file/list_dir only,
    no confirm needed (nothing here writes or executes anything). This is
    the actual fix for "why doesn't it know about my project" — same
    mechanism Claude Code/Cursor use, tools are just always available and
    the model decides per turn whether a question needs them, instead of a
    brittle keyword pre-filter deciding for it. `messages` is the caller's
    full turn (system prompt + history + the new user message already
    appended); this doesn't mutate that list. embedder, if given, backs
    search_code — pass the same embedder the caller uses elsewhere (e.g.
    memlayer's cache-similarity embedder) rather than adding a new one.
    Returns {"answer","model","tokens_used","grounded"} — grounded is True
    iff it actually read project files, so the caller knows this answer is
    project-specific and must not be shared into a cache pool other
    projects/users also read from."""
    exploring = {"role": "system", "content":
        f"You can look at the project's files if the question needs it — you're running in "
        f"{os.path.abspath(root)}. Most questions need no tool call at all; only read what's "
        f"directly relevant, and stop exploring once you have enough to answer well — you "
        f"have at most {max_steps} tool calls before you must answer with whatever you've "
        "seen, so don't try to read the whole project. Use search_code first to find the "
        "right file(s) by what they're about, or grep_code when you know the exact text/name "
        "you're looking for — don't guess a path with list_dir/read_file. "
        "You can ONLY read here — you have no way to write a file or run a command, full stop. "
        "If the user asks you to do/apply/implement/fix/change/create something, you MUST NOT "
        "claim you did it or describe changes as if you made them — you would be lying, since "
        "it is not possible. Instead say plainly that you can only describe what should change "
        "from here, and that they need to ask again as an action (e.g. 'fix the bug in x.py') "
        "or use /agent to actually have it applied."}
    answer_text, model_used, total_tokens, grounded = _loop(
        [exploring] + list(messages), model, root, None, on_step, READONLY_TOOLS,
        max_steps, max_tokens, embedder)
    return {"answer": answer_text, "model": model_used, "tokens_used": total_tokens, "grounded": grounded}
