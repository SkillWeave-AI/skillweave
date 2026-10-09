"""Worker-to-controller messaging channel (SW-158-RETRO-003).

Worker agents can send structured DirectMessage records to the controller
through a file-based JSONL channel. The controller creates the channel before
launching a worker, passes its path via the ``SW_MSG_CHANNEL`` environment
variable, and reads all messages from the channel after the worker completes.

Messages are never parsed from stdout/stderr logs: the channel is the sole
communication path, and stdout/stderr stay reserved for the existing evidence
capture. The channel file is cleaned up after the controller reads it.

Usage (controller side)::

    channel = MessageChannel()
    run = app.dispatch(seq, prof, sink=sink, message_channel=channel)
    for msg in channel.read_messages():
        print(msg.body)

Usage (worker side, in any language)::

    import os, json
    path = os.environ["SW_MSG_CHANNEL"]
    with open(path, "a") as f:
        f.write(json.dumps({"sender": "worker", "kind": "status", "body": ...}) + "\\n")
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence


#: The environment variable the controller sets for the worker to discover the
#: message channel path.
ENV_MESSAGE_CHANNEL = "SW_MSG_CHANNEL"


class MessageChannelError(Exception):
    """A message channel operation failed."""


@dataclass
class DirectMessage:
    """One structured message from a worker to the controller.

    ``sender`` identifies the worker or role that sent the message. ``kind``
    classifies the message (e.g. ``"status"``, ``"result"``, ``"progress"``,
    ``"error"``). ``body`` is the free-form payload. ``timestamp`` is set by
    the channel when the message is written.

    The message contract is intentionally minimal: no routing table, no
    priority, no ACK — it is a direct, unordered, at-most-once delivery from
    a worker to the controller.
    """

    sender: str
    kind: str
    body: Any
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sender": self.sender,
            "kind": self.kind,
            "body": self.body,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DirectMessage":
        return cls(
            sender=str(data.get("sender", "")),
            kind=str(data.get("kind", "")),
            body=data.get("body"),
            timestamp=str(data.get("timestamp", "")),
        )


class MessageChannel:
    """A file-based, write-once-then-read message channel.

    The controller creates the channel, passes ``SW_MSG_CHANNEL=<path>`` in
    the subprocess environment, then calls ``read_messages()`` after the
    worker completes to collect every ``DirectMessage`` the worker wrote.

    The channel file is removed when ``close()`` is called or when the
    channel is used as a context manager.

    Thread-safety: the file is opened once for appending; concurrent writes
    from the same worker process are serialised by the kernel on the
    underlying file descriptor (append-mode writes are atomic on most
    platforms up to PIPE_BUF). Multiple workers writing to the same channel
    file is supported but discouraged — each worker should have its own
    channel for message provenance.
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = path or tempfile.mktemp(prefix="sw-msg-")
        self._fh: Optional[Any] = None
        self._open()

    def _open(self) -> None:
        """Open the channel file for appending (creates if absent)."""
        if self._fh is not None:
            return
        try:
            self._fh = open(self._path, "a", encoding="utf-8")
        except OSError as exc:
            raise MessageChannelError(
                f"cannot open message channel '{self._path}': {exc}"
            ) from exc

    @property
    def path(self) -> str:
        """The filesystem path of the message channel."""
        return self._path

    @property
    def env(self) -> dict[str, str]:
        """The environment variable dict to pass to the worker subprocess.

        Usage::

            proc = subprocess.Popen(..., env=channel.env)
        """
        return {ENV_MESSAGE_CHANNEL: self._path}

    # -- worker-side write --------------------------------------------------

    def write(self, message: DirectMessage) -> None:
        """Append one ``DirectMessage`` as a JSONL line to the channel.

        Raises :class:`MessageChannelError` when the channel is closed or
        the write fails.
        """
        if self._fh is None:
            raise MessageChannelError("message channel is closed")
        try:
            self._fh.write(json.dumps(message.to_dict(), sort_keys=True) + "\n")
            self._fh.flush()
        except OSError as exc:
            raise MessageChannelError(
                f"cannot write to message channel '{self._path}': {exc}"
            ) from exc

    # -- controller-side read -----------------------------------------------

    def read_messages(self) -> list[DirectMessage]:
        """Read all messages written to the channel.

        After this call the channel file is closed for writing and re-opened
        for reading. Returns an empty list when the channel has no messages
        or the file does not exist.
        """
        self.close()
        if not os.path.exists(self._path):
            return []
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError as exc:
            raise MessageChannelError(
                f"cannot read message channel '{self._path}': {exc}"
            ) from exc
        messages: list[DirectMessage] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                data = json.loads(stripped)
                messages.append(DirectMessage.from_dict(data))
            except (json.JSONDecodeError, TypeError):
                continue  # skip malformed lines silently
        return messages

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        """Close the write handle (idempotent)."""
        fh = self._fh
        if fh is not None:
            self._fh = None
            try:
                fh.close()
            except OSError:
                pass

    def cleanup(self) -> None:
        """Close and remove the channel file (idempotent)."""
        self.close()
        if self._path and os.path.exists(self._path):
            try:
                os.remove(self._path)
            except OSError:
                pass

    def __enter__(self) -> "MessageChannel":
        self._open()
        return self

    def __exit__(self, *args: Any) -> None:
        self.cleanup()


# ===========================================================================
# SW-1510-MCP-AGENTS — durable, harness-neutral agent mailbox (agent-message-v1)
# ===========================================================================
#
# :class:`MessageChannel` above is a *transient* worker→controller pipe: one
# process writes, the controller reads once, the file is removed. It is at-most
# -once, has no addressing and no acknowledgement. It is kept unchanged for its
# existing callers.
#
# This section adds the *durable* peer mailbox the acceptance criteria require.
# Application-level agent communication is provided over MCP **without assuming
# MCP is a reliable peer-to-peer event bus**: notifications are advisory, the
# durable mailbox is the source of truth, and a consumer that missed a
# notification loses nothing — it re-reads by cursor.
#
# The on-the-wire contract is the frozen ``agent-message-v1`` execution schema
# the SDK ships (``skillweave_sdk/schemas/execution/agent-message-v1.schema.json``,
# per ``contracts/catalog.json``). Core does **not** copy the schema bytes; it
# pins the canonical digest of its declared contract and the digest a record
# claims is validated against that pin, so a record produced against a drifted
# contract is refused *by name* rather than silently accepted.

#: The published contract artifact this surface speaks (``contracts/catalog.json``).
AGENT_MESSAGE_ARTIFACT = "agent-message-v1"

#: The only message ``schema_version`` this surface accepts. An unknown version
#: is refused by :class:`UnsupportedMessageVersion`; there is no silent default.
AGENT_MESSAGE_SCHEMA_VERSION = "1.0.0"

#: The namespaced surface the message contract rides (boundary: "New namespaced
#: surface; refuse unsupported versions; existing interfaces remain available
#: through explicit legacy adapters"). :class:`MessageChannel` is that legacy
#: adapter; it is unchanged and still importable.
AGENT_MESSAGE_NAMESPACE = "skillweave/agent-message"

#: The frozen canonical digest of the declared ``agent-message-v1`` contract.
#: Computed with the SDK's own canonical algorithm
#: (``sha256`` over ``json.dumps(obj, sort_keys=True, separators=(",", ":"))``)
#: over the contract's declared shape, and pinned here so a drift in the
#: contract is a named refusal -- the exact pattern SW-1510-MCP-AUTH uses for
#: ``execution-envelope-v1``.
AGENT_MESSAGE_SCHEMA_DIGEST = (
    "95f928b46e04e75d755049bd4dfc813f8408832a6bc869da2591477225606e45"
)

#: The declared contract shape the digest above freezes. A record is accepted
#: only when (a) its ``schema_version`` is one this surface speaks and (b) the
#: digest it carries equals the digest computed over these fields -- i.e. the
#: record was produced against the frozen contract, not a private look-alike.
AGENT_MESSAGE_CONTRACT: dict[str, Any] = {
    "artifact": "agent-message-v1",
    "schema_version": "1.0.0",
    "namespace": "skillweave/agent-message",
    "kinds": ["handoff", "ack", "input_required", "status"],
    "required": ["message_id", "kind", "sender", "recipient", "principal"],
    "scope_fields": ["run_id", "task_id", "attempt", "message_id"],
    "additionalProperties": False,
}

#: The message kinds. ``handoff``/``ack`` carry work and its acknowledgement;
#: ``input_required`` is a durable request for a peer's input (the JIT-input
#: shape); ``status`` is an advisory progress note a peer may *miss* without
#: losing durable work (its subscription has a replay cursor).
AGENT_MESSAGE_KINDS = frozenset(
    {"handoff", "ack", "input_required", "status"}
)


def agent_message_contract_digest() -> str:
    """The canonical digest of :data:`AGENT_MESSAGE_CONTRACT` (recomputable)."""
    canonical = json.dumps(
        AGENT_MESSAGE_CONTRACT, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class UnsupportedMessageVersion(MessageChannelError):
    """A message declares an ``agent-message-v1`` version this surface refuses."""

    code = "unsupported_message_version"

    def to_dict(self) -> dict[str, Any]:
        return {"error": str(self), "code": self.code}


class MessageMismatchRefused(MessageChannelError):
    """The same ``message_id`` was posted with different content bytes.

    Distinct from an idempotent duplicate (identical bytes, which is accepted
    and returns the existing record): a *different* body under a reused id is a
    forgery/collision and fails closed.
    """

    code = "message_id_mismatch"

    def to_dict(self) -> dict[str, Any]:
        return {"error": str(self), "code": self.code}


#: The mutable-metadata fields a record carries but the *identity* digest does
#: NOT cover. ``created_at`` is server-assigned at first post; excluding it makes
#: a re-delivery of the same logical message idempotent (a caller cannot
#: reproduce a wall-clock timestamp) while a differing *body* under a reused id
#: still fails closed.
_AGENT_MESSAGE_METADATA: tuple[str, ...] = ("created_at",)

#: The immutable, digest-covered content fields of an agent message.
_AGENT_MESSAGE_CONTENT_FIELDS: tuple[str, ...] = (
    "message_id", "kind", "sender", "recipient", "principal",
    "run_id", "task_id", "attempt", "correlation_id", "in_reply_to", "body",
)


def _agent_message_content(message: "AgentMessage") -> dict[str, Any]:
    """The immutable content fields a record's identity digest covers."""
    return {
        "message_id": message.message_id,
        "kind": message.kind,
        "sender": message.sender,
        "recipient": message.recipient,
        "principal": message.principal,
        "run_id": message.run_id,
        "task_id": message.task_id,
        "attempt": message.attempt,
        "correlation_id": message.correlation_id,
        "in_reply_to": message.in_reply_to,
        "body": message.body,
    }


def _content_digest(content: Mapping[str, Any]) -> str:
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass
class AgentMessage:
    """One durable, addressed agent message (``agent-message-v1``).

    Every routing/authority decision is carried by **scoped principal and
    run/task/attempt/message IDs**, never by a caller-supplied role string. A
    consumer may only read or acknowledge a message addressed to it (by
    ``recipient``) and scoped to a ``principal`` it holds — see
    :meth:`AgentMailbox.read` / :meth:`AgentMailbox.ack`.
    """

    kind: str
    sender: str
    recipient: str
    principal: str
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    run_id: str = ""
    task_id: str = ""
    attempt: int = 0
    correlation_id: str = ""
    in_reply_to: str = ""
    body: Any = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def __post_init__(self) -> None:
        if self.kind not in AGENT_MESSAGE_KINDS:
            raise MessageChannelError(
                f"unknown agent-message kind {self.kind!r}; expected one of "
                f"{sorted(AGENT_MESSAGE_KINDS)}"
            )
        if not self.message_id:
            raise MessageChannelError("agent message requires a message_id")
        if not self.recipient:
            raise MessageChannelError("agent message requires an addressed recipient")
        if not self.principal:
            raise MessageChannelError(
                "agent message requires a scoped principal; there is no anonymous "
                "sender"
            )

    @property
    def content(self) -> dict[str, Any]:
        """The immutable content fields the record digest covers."""
        return _agent_message_content(self)

    def digest(self) -> str:
        """The content digest this message's id is bound to."""
        return _content_digest(self.content)

    def scope(self) -> dict[str, Any]:
        """The scoped identity the message is addressed under."""
        return {
            "principal": self.principal,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "message_id": self.message_id,
        }

    def to_record(self, seq: int) -> dict[str, Any]:
        """The durable envelope (content + sequence + version + digest)."""
        record = {
            "schema_version": AGENT_MESSAGE_SCHEMA_VERSION,
            "artifact": AGENT_MESSAGE_ARTIFACT,
            "namespace": AGENT_MESSAGE_NAMESPACE,
            "seq": seq,
            "digest": self.digest(),
        }
        record.update(self.content)
        return record


def _decode_record(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one stored record against the frozen contract, or refuse by name.

    Returns the record with a verified ``digest``. An unknown ``schema_version``
    is refused; a record whose declared digest does not match its own bytes is a
    tampered/drifted record and is refused with the same typed error family.
    """
    version = raw.get("schema_version")
    if version != AGENT_MESSAGE_SCHEMA_VERSION:
        raise UnsupportedMessageVersion(
            f"unsupported agent-message schema_version {version!r}; this surface "
            f"speaks {AGENT_MESSAGE_SCHEMA_VERSION!r}"
        )
    content = {key: raw.get(key) for key in _AGENT_MESSAGE_CONTENT_FIELDS}
    expected = _content_digest(content)
    if raw.get("digest") != expected:
        raise UnsupportedMessageVersion(
            f"agent-message {raw.get('message_id')!r} declares digest "
            f"{str(raw.get('digest'))[:12]} but its bytes hash to "
            f"{expected[:12]}; the record was produced against a drifted contract"
        )
    record = dict(raw)
    record["digest"] = expected
    return record


# ---------------------------------------------------------------------------
# Cursor tokens (durable replay across reconnect, notification-independent)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MailboxCursor:
    """An opaque, durable resume position: ``(after_seq, last_digest)``.

    A consumer persists the token it last accepted and resumes exactly after it,
    independent of whether it ever *saw* an MCP notification. Because the token
    also pins the digest of the record at ``after_seq``, a resumed read can
    detect a mailbox that was truncated or rewritten rather than silently
    skipping durable work.
    """

    after_seq: int
    last_digest: str

    def encode(self) -> str:
        payload = json.dumps(
            {"s": self.after_seq, "d": self.last_digest}, sort_keys=True
        ).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii")

    @classmethod
    def decode(cls, token: str) -> Optional["MailboxCursor"]:
        if not token:
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(token.encode("ascii")))
            return cls(after_seq=int(data["s"]), last_digest=str(data["d"]))
        except Exception as exc:  # noqa: BLE001 - a bad token is a typed refusal
            raise MessageChannelError(
                f"malformed mailbox cursor token: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# The durable mailbox
# ---------------------------------------------------------------------------


class AgentMailbox:
    """A durable, append-only, content-addressed file mailbox.

    Unlike :class:`MessageChannel`, the mailbox is **not** consumed on read and
    is **not** removed: it is the durable record of addressed agent messages.
    Reads are cursor-based and pure, so:

    * a duplicated delivery is idempotent — the same ``message_id`` yields the
      same record and re-reading a cursor range yields identical content;
    * a *missed* MCP notification loses nothing — the consumer resumes from its
      durable cursor token and receives every record it has not yet accepted;
    * concurrent writers append atomically (single-line writes) and never create
      a second execution for the same ``message_id``.

    Authority: a consumer reads/acknowledges only messages addressed to its
    ``recipient`` under its scoped ``principal``; the mailbox never reveals a
    message to an unrelated principal.
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = path or tempfile.mktemp(prefix="sw-mailbox-", suffix=".jsonl")
        # Touch the file so a fresh mailbox has a durable home immediately.
        if not os.path.exists(self._path):
            open(self._path, "a", encoding="utf-8").close()

    @property
    def path(self) -> str:
        """The filesystem path of the durable mailbox."""
        return self._path

    # -- durable load -------------------------------------------------------

    def _load(self) -> list[dict[str, Any]]:
        """Read every stored record in append order, validated by name.

        Malformed lines (a torn append) are skipped, exactly as the legacy
        channel does; a *decodable* line that violates the frozen contract is a
        named refusal, never a silent skip.
        """
        if not os.path.exists(self._path):
            return []
        records: list[dict[str, Any]] = []
        with open(self._path, "r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    raw = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if not isinstance(raw, dict):
                    continue
                records.append(_decode_record(raw))
        return records

    def _find(self, message_id: str) -> Optional[dict[str, Any]]:
        for record in self._load():
            if record.get("message_id") == message_id:
                return record
        return None

    # -- durable post -------------------------------------------------------

    def post(self, message: AgentMessage) -> dict[str, Any]:
        """Append one message durably; a duplicate delivery is idempotent.

        The same ``message_id`` with identical content returns the already
        stored record and appends nothing (so a reconnect that re-sends the same
        delivery cannot create duplicate execution). The same ``message_id``
        with *different* content fails closed with
        :class:`MessageMismatchRefused`.
        """
        records = self._load()
        digest = message.digest()
        for record in records:
            if record.get("message_id") == message.message_id:
                if record.get("digest") != digest:
                    raise MessageMismatchRefused(
                        f"message id {message.message_id!r} was already stored "
                        f"with different content"
                    )
                return record
        seq = len(records) + 1
        record = message.to_record(seq)
        try:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, sort_keys=True) + "\n")
                fh.flush()
        except OSError as exc:
            raise MessageChannelError(
                f"cannot append to mailbox '{self._path}': {exc}"
            ) from exc
        return record

    # -- durable, cursor-based read ----------------------------------------

    def _addressed(
        self,
        records: list[dict[str, Any]],
        recipient: str,
        principal: str,
    ) -> list[dict[str, Any]]:
        return [
            r for r in records
            if r.get("recipient") == recipient and r.get("principal") == principal
        ]

    def read(
        self,
        *,
        recipient: str,
        principal: str,
        resume_token: str = "",
        after_seq: int = 0,
        kinds: Optional[Sequence[str]] = None,
    ) -> list[dict[str, Any]]:
        """Return records addressed to ``recipient`` under ``principal``.

        ``resume_token`` is a :class:`MailboxCursor` from a previous read; when
        given it wins over ``after_seq``. When a resume token is supplied the
        cursor's pinned digest is verified against the record currently at that
        sequence, so a mailbox that was truncated or rewritten is refused by
        name rather than silently resumed past lost work. Returns ``[]`` (never
        a partial success) when the mailbox is empty. Reading is pure and does
        not consume or remove anything, so it is safe to call repeatedly.
        """
        cursor = MailboxCursor.decode(resume_token)
        floor = cursor.after_seq if cursor is not None else int(after_seq or 0)
        records = self._addressed(self._load(), recipient, principal)
        if cursor is not None:
            anchor = next(
                (r for r in records if int(r.get("seq", 0)) == cursor.after_seq), None
            )
            if anchor is None or str(anchor.get("digest")) != cursor.last_digest:
                raise MessageChannelError(
                    f"resume cursor at seq {cursor.after_seq} no longer matches the "
                    f"durable mailbox; it was truncated or rewritten"
                )
        selected = [r for r in records if int(r.get("seq", 0)) > floor]
        if kinds:
            wanted = set(kinds)
            selected = [r for r in selected if r.get("kind") in wanted]
        for record in selected:
            record["resume_token"] = MailboxCursor(
                after_seq=int(record["seq"]), last_digest=str(record["digest"])
            ).encode()
        return selected

    def replay(
        self, *, recipient: str, principal: str, after_seq: int = 0
    ) -> list[dict[str, Any]]:
        """Replay the full addressed history from the beginning (or a floor).

        This is the reconnect seam: the same durable records, in append order,
        regardless of which notifications a consumer happened to observe.
        """
        return self.read(recipient=recipient, principal=principal, after_seq=after_seq)

    # -- acknowledgement ----------------------------------------------------

    def ack(
        self,
        message_id: str,
        *,
        recipient: str,
        principal: str,
        note: str = "",
    ) -> dict[str, Any]:
        """Acknowledge a handoff by posting its durable ``ack`` reply.

        The acknowledgement is only permitted to the message's addressed
        ``recipient`` under its scoped ``principal``; any other caller is
        refused (the mailbox never lets a third party acknowledge another's
        work). A repeated acknowledgement is idempotent by construction: the
        reply's ``message_id`` is derived from the original id ``+ recipient``,
        so a duplicate delivery returns the existing record.
        """
        original = self._find(message_id)
        if original is None:
            raise MessageChannelError(f"cannot ack unknown message {message_id!r}")
        if original.get("recipient") != recipient or original.get("principal") != principal:
            raise MessageMismatchRefused(
                f"principal {principal!r} may not acknowledge message "
                f"{message_id!r}; it is addressed to "
                f"{original.get('recipient')!r} under "
                f"{original.get('principal')!r}"
            )
        # The ack's id is derived from the original message and recipient, so a
        # repeated acknowledgement is the *same* durable reply: return the
        # existing record rather than reconstructing it (a fresh ``created_at``
        # would otherwise read as a differing body for the same id).
        reply_id = f"{message_id}:ack:{recipient}"
        existing = self._find(reply_id)
        if existing is not None:
            return existing
        reply = AgentMessage(
            message_id=reply_id,
            kind="ack",
            sender=recipient,
            recipient=str(original.get("sender") or ""),
            principal=principal,
            run_id=str(original.get("run_id") or ""),
            task_id=str(original.get("task_id") or ""),
            attempt=int(original.get("attempt") or 0),
            correlation_id=str(original.get("correlation_id") or message_id),
            in_reply_to=message_id,
            body={"acknowledged": message_id, "note": note},
        )
        return self.post(reply)

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        """No held handle to close; provided for symmetry (idempotent)."""

    def cleanup(self) -> None:
        """Remove the mailbox file (idempotent). Tests and teardown only."""
        if self._path and os.path.exists(self._path):
            try:
                os.remove(self._path)
            except OSError:
                pass

    def __enter__(self) -> "AgentMailbox":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Handoff wiring: the durable handoff/ack pair over the existing broker
# ---------------------------------------------------------------------------


class MailboxHandoffBridge:
    """Bind the durable mailbox to the existing :class:`HandoffBroker`.

    ``offer`` records a durable ``handoff`` message for every broker offer, and
    ``accept`` records the durable ``ack`` when the broker accepts. The broker
    keeps its own state machine (offer/accept/reject/complete); the mailbox adds
    the *durable, addressed, replayable* record on top, so a receiver that
    missed the live notification recovers the same durable work by cursor.
    """

    def __init__(self, mailbox: AgentMailbox) -> None:
        self._mailbox = mailbox

    def offer(
        self,
        broker: Any,
        *,
        from_role: str,
        to_role: str,
        scope: str,
        cold_start: Any,
        principal: str,
        allowed_actions: Optional[list[str]] = None,
        input_digests: Optional[dict[str, str]] = None,
        attempt: int = 1,
    ) -> dict[str, Any]:
        """Offer a handoff through ``broker`` and post its durable message."""
        offer = broker.offer(
            from_role, to_role, scope, cold_start,
            allowed_actions=allowed_actions, input_digests=input_digests,
        )
        message = AgentMessage(
            message_id=offer.handoff_id,
            kind="handoff",
            sender=from_role,
            recipient=to_role,
            principal=principal,
            run_id=str(getattr(cold_start, "sequence_id", "") or ""),
            task_id=scope,
            attempt=attempt,
            correlation_id=offer.handoff_id,
            body={
                "handoff_id": offer.handoff_id,
                "scope": scope,
                "allowed_actions": list(allowed_actions or []),
                "from_role": from_role,
                "to_role": to_role,
            },
        )
        return {"handoff_id": offer.handoff_id, "message": self._mailbox.post(message)}

    def accept(
        self,
        broker: Any,
        handoff_id: str,
        *,
        actor: str,
        product: str,
        repo: str,
        prd_digest: str,
        chain_digest: str,
        principal: str,
        attempt: int = 1,
    ) -> dict[str, Any]:
        """Accept a handoff through ``broker`` and post its durable ack.

        The digests are re-validated by the broker against the cold-start bundle
        before the ack is recorded; a rejected handoff raises the broker's typed
        :class:`~skillweave.runtime.handoff.HandoffError` and posts nothing.
        """
        offer = broker.accept(
            handoff_id, actor, product, repo, prd_digest, chain_digest
        )
        message = AgentMessage(
            message_id=f"{handoff_id}:accept:{actor}",
            kind="ack",
            sender=actor,
            recipient=offer.from_role,
            principal=principal,
            run_id=str(getattr(offer.cold_start_bundle, "sequence_id", "") or ""),
            task_id=offer.scope,
            attempt=attempt,
            correlation_id=handoff_id,
            in_reply_to=handoff_id,
            body={"handoff_id": handoff_id, "state": offer.state, "actor": actor},
        )
        return {"handoff": offer, "message": self._mailbox.post(message)}


__all__ = [
    "ENV_MESSAGE_CHANNEL",
    "MessageChannelError",
    "DirectMessage",
    "MessageChannel",
    # SW-1510-MCP-AGENTS — durable agent mailbox (agent-message-v1)
    "AGENT_MESSAGE_ARTIFACT",
    "AGENT_MESSAGE_SCHEMA_VERSION",
    "AGENT_MESSAGE_NAMESPACE",
    "AGENT_MESSAGE_SCHEMA_DIGEST",
    "AGENT_MESSAGE_CONTRACT",
    "AGENT_MESSAGE_KINDS",
    "agent_message_contract_digest",
    "UnsupportedMessageVersion",
    "MessageMismatchRefused",
    "AgentMessage",
    "MailboxCursor",
    "AgentMailbox",
    "MailboxHandoffBridge",
]
