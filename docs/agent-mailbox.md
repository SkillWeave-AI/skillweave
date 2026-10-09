# Durable, harness-neutral handoff and the agent mailbox

SW-1510-MCP-AGENTS. This document is a pointer, not evidence: every claim here
is backed by the machine check in `tests/unit/test_1510_mcp_agents.py`, which
exercises the *actual consumers* -- the `skillweave.mcp.server` tool callables,
`skillweave.dispatch.messaging.AgentMailbox`, and the
`skillweave.runtime.handoff` capability/adapter seam -- never a re-implementation.

## The one idea

MCP is treated as an **advisory transport, not a durable bus**. The durable
mailbox is the source of truth; notifications are lossy. A client that misses a
notification recovers the same work by resuming from its durable cursor, and a
duplicated delivery is idempotent by `message_id`. Nothing about *which harness*
is speaking rides in core logic: capability is a detected, data-driven fact, and
every non-capable client falls back to file/CLI polling with the **same
authority contract**.

## The frozen contract

`agent-message-v1` is declared once, in
`src/skillweave/dispatch/messaging.py`:

- `AGENT_MESSAGE_ARTIFACT = "agent-message-v1"`
- `AGENT_MESSAGE_SCHEMA_VERSION = "1.0.0"`
- `AGENT_MESSAGE_NAMESPACE = "skillweave/agent-message"`
- `AGENT_MESSAGE_SCHEMA_DIGEST` -- the pinned canonical digest of
  `AGENT_MESSAGE_CONTRACT`, recomputable via `agent_message_contract_digest()`.
- `AGENT_MESSAGE_KINDS = {handoff, ack, input_required, status}`

This is a **new namespaced surface**. A stored record that declares another
`schema_version`, or whose bytes do not hash to its declared digest, is refused
by name: `UnsupportedMessageVersion` (`code="unsupported_message_version"`).
The pre-existing `MessageChannel`/`DirectMessage` (SW-158-RETRO-003) is left
intact as the explicit legacy adapter.

## Authority is scoped and server-owned

A message is routed only by **scoped principal** and
**run/task/attempt/message IDs** (`AgentMessage.scope()`), never a
caller-supplied role. At the MCP boundary each tool calls `gate_operation(...)`
and `_message_principal(...)`: the authenticated principal is authoritative, and
a caller that names a different `principal` is refused
(`principal_mismatch`). The mailbox never returns a message to an unrelated
principal (`AgentMailbox._addressed`), and only the addressed recipient under
its scoped principal may acknowledge (`AgentMailbox.ack`).

The server-owned mailbox path is resolved from `MAILBOX_PATH_ENV`
(`SKILLWEAVE_AGENT_MAILBOX`) or `MAILBOX_DEFAULT_PATH`; a caller cannot point an
agent tool at an arbitrary path.

## Idempotent reconnect

- `AgentMailbox.post` -- the same `message_id` with identical content returns the
  stored record and appends nothing; the same id with *different* content fails
  closed (`MessageMismatchRefused`, `code="message_id_mismatch"`). The
  server-assigned `created_at` is excluded from the identity digest, so a
  re-delivery of the same logical message is idempotent.
- `AgentMailbox.read` -- pure and cursor-based. `MailboxCursor` pins both the
  sequence (`after_seq`) and the digest of the anchored record, so a mailbox
  that was **truncated or rewritten** under a stale resume token is refused by
  name rather than silently skipping durable work.
- `AgentMailbox.replay` -- the reconnect seam; the full addressed history in
  append order, independent of observed notifications.
- `AgentMailbox.ack` -- the reply id is derived from the original
  (`{message_id}:ack:{recipient}`), so a repeated acknowledgement is the same
  durable record.

## Capability detection and the polling fallback

`skillweave.runtime.handoff` detects a client's surface, falling **closed** to
polling:

1. An explicit operator attestation (`CAPABILITY_ATTESTATION_ENV` /
   `SKILLWEAVE_CLIENT_CAPABILITY`) names the transports; an attestation that
   omits `mcp-notifications` still selects polling.
2. Otherwise a positive, file-backed presence signal (skill install root via the
   installer's `AGENT_CONFIG`, or an MCP config anchor such as
   `~/.codex/config.toml`) marks the client detected. Notification support is
   granted only when the MCP config anchor exists.
3. No signal at all -> `detected=False` and the CLI polling adapter.

`detect_capabilities()` defaults to the three named clients -- **codex,
antigravity, openclaude** -- so each one's support is explicitly detected rather
than assumed. `adapter_for_capability()` selects `NotificationTransportAdapter`
(live, advisory) or `PollingTransportAdapter` (file/CLI fallback); both read and
post through the same `AgentMailbox` and enforce the same scoped authority, and
neither is an authority source of its own (`HandoffTransportAdapter.principal`
raises rather than defaulting to a wildcard).

## MCP tools

`src/skillweave/mcp/server.py` exposes, each gated by `gate_operation(...)`:

- `mailbox_post` -- post one addressed `agent-message-v1` message.
- `mailbox_read` -- cursor-based subscription read (optional `kinds` filter for
  the `input_required`/`status` stream).
- `mailbox_replay` -- replay the durable history.
- `mailbox_ack` -- acknowledge by posting the durable reply.
- `agent_capabilities` -- registration/capability attestation; reports each
  client's transports/adapter and the frozen contract version + digest.

## Conformance

`python3 -m pytest tests/unit/test_1510_mcp_agents.py` -- the lane check covering
all three acceptance criteria, the frozen contract, and the polling fallback.
