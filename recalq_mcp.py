#!/usr/bin/env python3
"""
recalq_mcp.py — MCP server for Recalq. Pure Python, stdio transport.

Exposes ONE tool, `recalq_ask`, which calls the MemLayer engine
(cache_layer/memlayer.py) directly, in-process — no HTTP, no web UI,
no auth token. Answers come from the cache -> granted knowledge -> LLM
ladder, same as the CLI, using whatever provider/API key is configured
in client.yaml or set via environment variables (any provider, or a
local Ollama model).

Register with an MCP client, e.g. Claude Code:
  claude mcp add recalq -- python3 /path/to/recalq_mcp.py
"""
import sys, os, json

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache_layer"))
import memlayer  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "recalq_ask",
        "description": (
            "Ask Recalq — a semantic cache + org-knowledge layer. Returns a "
            "cached, knowledge-grounded, or LLM answer. Answers are safety-"
            "checked (polarity/number/version/entity identity) before being "
            "served from cache. Use for factual, procedural, and org-specific "
            "questions instead of calling a raw LLM."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The question to ask.",
                },
                "model": {
                    "type": "string",
                    "description": ("Optional provider alias (e.g. 'gemini', 'claude', "
                                    "'groq') or raw litellm model string (e.g. "
                                    "'ollama/llama3.1'). Defaults to client.yaml's "
                                    "auto_provider."),
                },
            },
            "required": ["query"],
        },
    }
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


def _handle(msg: dict):
    mid = msg.get("id")
    method = msg.get("method", "")

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "recalq", "version": "1.0.0"},
        }}

    if method == "notifications/initialized":
        return None  # notification — no response

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}

    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name != "recalq_ask":
            return {"jsonrpc": "2.0", "id": mid, "error": {
                "code": -32602, "message": f"unknown tool: {name}"}}
        query = (args.get("query") or "").strip()
        if not query:
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "error: empty query"}],
                "isError": True}}
        try:
            model = args.get("model") or memlayer.default_model()
            d = memlayer.ask(query, model)
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": _tool_result_text(d)}]}}
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
