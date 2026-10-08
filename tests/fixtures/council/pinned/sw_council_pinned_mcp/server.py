"""The pinned server's compliant stdio MCP loop (SW-COUNCIL-003).

It negotiates an *older* protocol version on purpose, so a test can prove the
client **records what the server returned** (and reports ``version_agreed`` as
false) rather than assuming its own requested version.
"""

import json
import sys

ADVERTISED_PROTOCOL_VERSION = "2025-03-26"

_TOOL = {
    "name": "perplexity_search",
    "description": "the pinned server's search tool",
    "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
}


def _send(obj):
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> int:
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
            _send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": ADVERTISED_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "sw-council-pinned-mcp", "version": "9.9.9"},
            }})
        elif method == "notifications/initialized":
            continue
        elif method == "notifications/cancelled":
            return 0
        elif method == "tools/list":
            if not initialized:
                _send({"jsonrpc": "2.0", "id": rid,
                       "error": {"code": -32002, "message": "server not initialized"}})
                continue
            _send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [_TOOL]}})
        elif method == "tools/call":
            _send({"jsonrpc": "2.0", "id": rid, "result": {
                "structuredContent": {
                    "citations": [
                        {"url": "https://pinned.example.org/source",
                         "title": "Pinned cited source", "snippet": "from the pinned server"},
                        {"url": "ftp://not-http.example.org/file", "title": "dropped"},
                    ]
                },
                "content": [{"type": "text", "text": "pinned synthesis"}],
            }})
        else:
            if rid is not None:
                _send({"jsonrpc": "2.0", "id": rid,
                       "error": {"code": -32601, "message": "method not found"}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
