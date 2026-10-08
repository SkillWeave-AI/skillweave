"""A hand-rolled, network-free MCP stdio server for Council tests (SW-COUNCIL-003).

It speaks JSON-RPC 2.0 over stdin/stdout and implements the full lifecycle a
compliant server requires::

    initialize -> notifications/initialized -> tools/list -> tools/call

It is deliberately *noisy* in ``normal`` mode: before each real response it emits
an unsolicited notification and a **response to a different request id**, so a
client that merely reads "the next line" is proven wrong and a client that
correctly correlates request ids is proven right.

Modes (argv[1]):
    normal             full lifecycle; interleaves a notification + a stale-id
                       response before each real response; the tool returns
                       structuredContent.results, a citations list and a text
                       content block
    reject-first-call  answers any pre-initialize request with -32002 (the exact
                       behaviour a compliant server shows the old defect's bare
                       tools/call)
    error              tools/call -> JSON-RPC error -32603
    unavailable        closes the stream immediately (an outage)
    missing-tool       tools/list advertises a *different* tool
    malformed          tools/call answers with a non-JSON garbage line
    iserror            tools/call result carries isError=true
    text-only          tools/call returns a text content block and NO citation
    hang               never answers tools/call (drives the timeout path)
    stderr-secret      writes a secret sentinel to stderr, then behaves normally

Only a local interpreter + this file ever launches it: no floating ``git+`` or
URL install is constructed anywhere.
"""

import json
import sys
import time

MODE = sys.argv[1] if len(sys.argv) > 1 else "normal"

_TOOL = {
    "name": "perplexity_search",
    "description": "Search via a fake but compliant server",
    "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
}
_OTHER_TOOL = {
    "name": "not_perplexity",
    "description": "a different tool",
    "inputSchema": {"type": "object", "properties": {}},
}
_SECRET = "pplx-FAKESECRET0123456789"


def _send(obj):
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _result(rid, result):
    _send({"jsonrpc": "2.0", "id": rid, "result": result})


def _error(rid, code, message):
    _send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def _interleave():
    """Emit a notification and a stale-id response before a real one."""
    _send({"jsonrpc": "2.0", "method": "notifications/progress",
           "params": {"progressToken": "t", "progress": 1}})
    _result(999999, {"stale": True})


def main():
    if MODE == "unavailable":
        return 0
    if MODE == "stderr-secret":
        sys.stderr.write(f"server boot: using token {_SECRET}\n")
        sys.stderr.flush()
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
            _interleave()
            _result(rid, {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "council-fake", "version": "2.1.0"},
                "instructions": "answer with citations",
            })
        elif method == "notifications/initialized":
            continue
        elif method == "notifications/cancelled":
            return 0
        elif method == "tools/list":
            if not initialized:
                _error(rid, -32002, "server not initialized")
                continue
            _interleave()
            tool = _OTHER_TOOL if MODE == "missing-tool" else _TOOL
            _result(rid, {"tools": [tool]})
        elif method == "tools/call":
            if MODE == "reject-first-call" and not initialized:
                _error(rid, -32002, "server not initialized")
                continue
            if MODE == "hang":
                time.sleep(3600)
                continue
            if MODE == "error":
                _error(rid, -32603, "internal error")
                continue
            if MODE == "malformed":
                sys.stdout.write("this is not json\n")
                sys.stdout.flush()
                continue
            if MODE == "iserror":
                _interleave()
                _result(rid, {
                    "isError": True,
                    "content": [{"type": "text", "text": "tool failed upstream"}],
                })
                continue
            if MODE == "text-only":
                _interleave()
                _result(rid, {
                    "content": [{"type": "text", "text": "A synthesized answer with no citation."}],
                })
                continue
            _interleave()
            _result(rid, {
                "structuredContent": {
                    "results": [
                        {"title": "Cited result", "url": "https://example.org/cited",
                         "snippet": "a real http source", "source": "web"},
                        {"title": "Unsafe", "url": "javascript:alert(1)",
                         "snippet": "must be dropped", "source": "web"},
                    ],
                    "citations": ["https://citation.example.net/a"],
                },
                "content": [{"type": "text", "text": "synthesized answer"}],
            })
        else:
            if rid is not None:
                _error(rid, -32601, "method not found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
