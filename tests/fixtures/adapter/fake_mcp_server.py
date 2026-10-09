"""A minimal, compliant MCP stdio server for adapter tests (SW-ADAPT-001).

It is intentionally hand-rolled (no MCP SDK, no network): it speaks JSON-RPC
over stdin/stdout and implements the lifecycle a compliant server requires:

    initialize -> notifications/initialized -> tools/list -> tools/call

Modes (argv[1]):
    normal             the full lifecycle; tools/call returns a search result
    reject-first-call  answers any request that precedes initialize with -32002
                       (server not initialized) -- this is exactly what a
                       compliant server does to the old defect's bare tools/call
    error              tools/call returns a JSON-RPC error object
    unavailable        closes the stream immediately (an outage)
    auth_required      tools/list returns empty tools (anonymous/ask-only)
    missing_credential tools/list returns -32001 credential error
    expired_credential tools/list succeeds but tools/call returns -32001 auth error
    tool_unavailable   tools/list succeeds but specific tool is missing

The launched command is always a local interpreter + this file: no floating
``git+`` or URL install is ever constructed.
"""

import json
import sys

MODE = sys.argv[1] if len(sys.argv) > 1 else "normal"

_TOOL = {
    "name": "perplexity_search",
    "description": "Search via a fake but compliant server",
    "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
}


def _send(obj):
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _result(rid, result):
    _send({"jsonrpc": "2.0", "id": rid, "result": result})


def _error(rid, code, message):
    _send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def main():
    if MODE == "unavailable":
        return 0
    initialized = False
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        rid = msg.get("id")
        if method == "initialize":
            initialized = True
            _result(rid, {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "1.0.0"},
            })
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            if not initialized:
                _error(rid, -32002, "server not initialized")
                continue
            if MODE == "auth_required":
                _result(rid, {"tools": []})
                continue
            if MODE == "missing_credential":
                _error(rid, -32001, "Missing credential: PERPLEXITY_COOKIES not found")
                continue
            _result(rid, {"tools": [_TOOL]})
        elif method == "tools/call":
            if MODE == "reject-first-call" and not initialized:
                _error(rid, -32002, "server not initialized")
                continue
            if MODE == "expired_credential":
                _error(rid, -32001, "Credential expired: PERPLEXITY_COOKIES is no longer valid")
                continue
            if MODE == "tool_unavailable":
                _error(rid, -32602, f"Unknown tool: perplexity_search")
                continue
            if MODE == "error":
                _error(rid, -32603, "internal error")
                continue
            _result(rid, {
                "structuredContent": {
                    "results": [
                        {"title": "Fake result", "url": "https://example.org/x",
                         "snippet": "from the fake server", "source": "web"},
                        {"title": "Unsafe", "url": "javascript:alert(1)",
                         "snippet": "must be dropped", "source": "web"},
                    ]
                },
                "content": [{"type": "text", "text": "synthesized answer"}],
            })
        else:
            if rid is not None:
                _error(rid, -32601, "method not found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
