#!/usr/bin/env python3
"""
recalq_mcp.py — MCP server for Recalq. Pure Python, stdio transport.

Exposes Recalq's memory/cache/RAG layer (cache_layer/memlayer.py) directly,
in-process — no HTTP, no web UI, no auth token — using whatever provider/
API key is configured in client.yaml or set via environment variables.

Deliberately does NOT expose the write-capable /agent loop (file edits,
shell commands): an MCP client like Claude Code already has better native
tools for that. What Recalq adds that a coding assistant doesn't have on
its own is the persistent, cross-session, cost-saving cache/RAG layer —
that's what's exposed here.

Every tool takes an optional `path` naming the project it's about. This
matters because the MCP server's own working directory (wherever it was
launched from) won't generally match the calling client's active project —
`path` scopes caching/grounding/documents to that project specifically
(same project_namespace() isolation the CLI and Telegram use), defaulting
to the server's own cwd only if omitted.

Register with an MCP client, e.g. Claude Code:
  claude mcp add recalq -- python3 /path/to/recalq_mcp.py
"""
import sys, os, json, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache_layer"))
import memlayer  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"

_IMAGE_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
               "gif": "image/gif", "webp": "image/webp"}

TOOLS = [
    {
        "name": "recalq_ask",
        "description": (
            "Ask Recalq — a semantic cache + project-grounded + org-knowledge "
            "layer, on whatever LLM provider is configured (often a free/cheap "
            "one, distinct from your own). Returns a cached, project-grounded, "
            "knowledge-grounded, or fresh LLM answer. When `path` names a "
            "project, the answer is read-only grounded in that project's actual "
            "files and cached/scoped to it specifically — never mixed with "
            "another project's answers. Use for a cheap second opinion, "
            "org-specific knowledge, or anything worth caching for reuse across "
            "the team/other connectors (Telegram, CLI)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The question to ask."},
                "model": {
                    "type": "string",
                    "description": ("Optional provider alias (e.g. 'gemini', 'claude', "
                                    "'groq') or raw litellm model string (e.g. "
                                    "'ollama/llama3.1'). Defaults to client.yaml's "
                                    "auto_provider."),
                },
                "path": {
                    "type": "string",
                    "description": ("Project directory this question is about — scopes "
                                    "caching and file-grounding to it. Defaults to the "
                                    "MCP server's own working directory."),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "recalq_ingest_document",
        "description": (
            "Ingest a document (PDF/DOCX/TXT) into Recalq's persistent knowledge "
            "store for a project, so recalq_ask can answer questions from it "
            "later — from any connector (CLI, Telegram, this MCP session), not "
            "just this one call."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the document to ingest."},
                "path": {
                    "type": "string",
                    "description": ("Project directory to scope this document to. "
                                    "Defaults to the MCP server's own working directory."),
                },
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "recalq_scan_project",
        "description": (
            "Scan a project directory and ingest a generated overview into "
            "Recalq's persistent knowledge store, so recalq_ask (from this MCP "
            "session or any other connector) can answer questions like 'what "
            "does this project do' afterward without rescanning."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Project directory to scan. Defaults to the MCP server's own working directory.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "recalq_ask_image",
        "description": (
            "Ask a question about an image using Recalq's configured vision-"
            "capable provider — a cheap/free second opinion without needing "
            "your own vision capability."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "Path to the image file."},
                "question": {"type": "string", "description": "What to ask about the image."},
                "path": {
                    "type": "string",
                    "description": "Project directory to scope this to. Defaults to the MCP server's own working directory.",
                },
            },
            "required": ["image_path", "question"],
        },
    },
]


def _tool_result_text(d: dict) -> str:
    answer = d.get("answer") or ""
    source = d.get("source") or "?"
    parts = [answer, f"\n---\n[source: {source}"]
    if d.get("similarity"):
        parts.append(f", similarity: {d['similarity']:.3f}")
    if d.get("sop_sources"):
        parts.append(f", knowledge: {', '.join(d['sop_sources'])}")
    parts.append("]")
    return "".join(parts)


def _resolve_path(args: dict) -> str:
    return os.path.abspath(args.get("path") or os.getcwd())


def _call_recalq_ask(args: dict):
    query = (args.get("query") or "").strip()
    if not query:
        return "error: empty query", True
    root = _resolve_path(args)
    model = args.get("model") or memlayer.default_model()
    d = memlayer.ask(query, model, namespace=memlayer.project_namespace(root), root=root)
    return _tool_result_text(d), d.get("source") == "error"


def _call_ingest_document(args: dict):
    file_path = (args.get("file_path") or "").strip()
    if not file_path or not os.path.isfile(file_path):
        return f"error: no such file: {file_path}", True
    root = _resolve_path(args)
    doc_id, meta = memlayer.ingest_document(
        file_path, os.path.basename(file_path),
        namespace=memlayer.project_namespace(root), uploaded_by="mcp")
    return f"Ingested '{meta['filename']}' — {meta['chunk_count']} chunks, scoped to {root}.", False


def _call_scan_project(args: dict):
    root = _resolve_path(args)
    try:
        summary = memlayer.scan_project(root)
    except Exception as e:
        return f"error scanning project: {e}", True
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(summary)
        tmp_path = f.name
    try:
        doc_id, meta = memlayer.ingest_document(
            tmp_path, "PROJECT_OVERVIEW.txt",
            namespace=memlayer.project_namespace(root), uploaded_by="mcp")
    finally:
        os.unlink(tmp_path)
    return (f"Scanned {root} — indexed {meta['chunk_count']} chunks. "
            "recalq_ask can now answer questions about this project."), False


def _call_ask_image(args: dict):
    image_path = (args.get("image_path") or "").strip()
    question = (args.get("question") or "").strip()
    if not image_path or not os.path.isfile(image_path):
        return f"error: no such file: {image_path}", True
    ext = image_path.lower().rsplit(".", 1)[-1]
    mime = _IMAGE_MIME.get(ext)
    if not mime:
        return f"error: unsupported image type .{ext}", True
    root = _resolve_path(args)
    with open(image_path, "rb") as f:
        data = f.read()
    d = memlayer.ask_image(data, mime, question, namespace=memlayer.project_namespace(root), user="mcp")
    return _tool_result_text(d), d.get("source") == "error"


TOOL_HANDLERS = {
    "recalq_ask": _call_recalq_ask,
    "recalq_ingest_document": _call_ingest_document,
    "recalq_scan_project": _call_scan_project,
    "recalq_ask_image": _call_ask_image,
}


def _handle(msg: dict):
    mid = msg.get("id")
    method = msg.get("method", "")

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "recalq", "version": "2.0.0"},
        }}

    if method == "notifications/initialized":
        return None  # notification — no response

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}

    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            return {"jsonrpc": "2.0", "id": mid, "error": {
                "code": -32602, "message": f"unknown tool: {name}"}}
        try:
            text, is_error = handler(args)
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": text}], "isError": is_error}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": f"Recalq error: {e}"}],
                "isError": True}}

    if mid is not None:  # unknown request — proper JSON-RPC error
        return {"jsonrpc": "2.0", "id": mid, "error": {
            "code": -32601, "message": f"method not found: {method}"}}
    return None  # unknown notification — ignore


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        out = _handle(msg)
        if out is not None:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
