from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional
import hashlib
import json
import uuid


class HandoffState(str, Enum):
    OFFERED = "offered"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"
    COMPLETED = "completed"


@dataclass
class ColdStartBundle:
    prd_uri: str
    prd_digest: str
    chain_uri: str
    chain_digest: str
    repo_uri: str
    worktree_path: str
    branch: str
    target_role: str
    sequence_id: str
    base_sha: str = ""
    remote_sha: str = ""
    fingerprint: Optional[dict[str, Any]] = None

    def to_dict(self):
        return {
            "prd_uri": self.prd_uri,
            "prd_digest": self.prd_digest,
            "chain_uri": self.chain_uri,
            "chain_digest": self.chain_digest,
            "repo_uri": self.repo_uri,
            "worktree_path": self.worktree_path,
            "branch": self.branch,
            "target_role": self.target_role,
            "sequence_id": self.sequence_id,
            "base_sha": self.base_sha,
            "remote_sha": self.remote_sha,
            "fingerprint": self.fingerprint,
        }

    def validate_digests(self, prd_digest: str, chain_digest: str) -> list[str]:
        errors = []
        if self.prd_digest != prd_digest:
            errors.append(f"PRD digest mismatch: expected {self.prd_digest}, got {prd_digest}")
        if self.chain_digest != chain_digest:
            errors.append(f"Chain digest mismatch: expected {self.chain_digest}, got {chain_digest}")
        return errors

    def integrity_bytes(self) -> bytes:
        """Canonical byte encoding of every released field. The self-integrity
        digest covers base/remote SHA and the environment fingerprint, so a
        bundle whose recorded SHA or fingerprint was edited no longer matches
        its own digest.

        NOTE: this digest is *not* the adversarial manipulation defence of
        SW-RESUME-001. It pins the bundle's own fields to each other so a
        fresh session can detect an internally inconsistent (edited) bundle.
        The real reject/reconstruct guarantee lives in ``resume.manager``,
        where the digest is re-derived from raw PRD/chain bytes and git
        identity instead of trusting the serialized fields."""
        payload = json.dumps(
            {
                "prd_uri": self.prd_uri,
                "prd_digest": self.prd_digest,
                "chain_uri": self.chain_uri,
                "chain_digest": self.chain_digest,
                "repo_uri": self.repo_uri,
                "worktree_path": self.worktree_path,
                "branch": self.branch,
                "target_role": self.target_role,
                "sequence_id": self.sequence_id,
                "base_sha": self.base_sha,
                "remote_sha": self.remote_sha,
                "fingerprint": self.fingerprint,
            },
            sort_keys=True,
        ).encode("utf-8")
        return payload

    def integrity_digest(self) -> str:
        return hashlib.sha256(self.integrity_bytes()).hexdigest()


@dataclass
class HandoffOffer:
    handoff_id: str
    from_role: str
    to_role: str
    scope: str
    cold_start_bundle: ColdStartBundle
    allowed_actions: list[str]
    input_digests: dict[str, str] = field(default_factory=dict)
    state: str = HandoffState.OFFERED.value
    owner: Optional[str] = None
    offered_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    accepted_at: Optional[str] = None
    completed_at: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        return {
            "handoff_id": self.handoff_id,
            "from_role": self.from_role,
            "to_role": self.to_role,
            "scope": self.scope,
            "cold_start_bundle": self.cold_start_bundle.to_dict(),
            "allowed_actions": self.allowed_actions,
            "input_digests": self.input_digests,
            "state": self.state,
            "owner": self.owner,
            "offered_at": self.offered_at,
            "accepted_at": self.accepted_at,
            "completed_at": self.completed_at,
            "metadata": self.metadata,
        }


class HandoffError(Exception):
    def __init__(self, reason: str, code: str = "HANDOFF_ERROR", context: Optional[dict] = None):
        self.reason = reason
        self.code = code
        self.context = context or {}
        super().__init__(f"[{code}] {reason}")


class HandoffBroker:
    def __init__(self):
        self._offers: dict[str, HandoffOffer] = {}
        self._history: list[dict[str, Any]] = []

    def offer(
        self,
        from_role: str,
        to_role: str,
        scope: str,
        cold_start: ColdStartBundle,
        allowed_actions: Optional[list[str]] = None,
        input_digests: Optional[dict[str, str]] = None,
    ) -> HandoffOffer:
        handoff_id = str(uuid.uuid4())[:12]
        offer = HandoffOffer(
            handoff_id=handoff_id,
            from_role=from_role,
            to_role=to_role,
            scope=scope,
            cold_start_bundle=cold_start,
            allowed_actions=allowed_actions or [],
            input_digests=input_digests or {},
        )
        self._offers[handoff_id] = offer
        self._history.append({"action": "offer", "handoff_id": handoff_id, "to": to_role, "timestamp": offer.offered_at})
        return offer

    def accept(
        self,
        handoff_id: str,
        actor: str,
        product: str,
        repo: str,
        prd_digest: str,
        chain_digest: str,
    ) -> HandoffOffer:
        offer = self._offers.get(handoff_id)
        if offer is None:
            raise HandoffError(f"Handoff '{handoff_id}' not found", code="NOT_FOUND")

        if offer.state != HandoffState.OFFERED.value:
            raise HandoffError(
                f"Handoff '{handoff_id}' is in state '{offer.state}', not OFFERED",
                code="ALREADY_CLAIMED",
            )

        if actor != offer.to_role and actor not in (offer.from_role,):
            raise HandoffError(
                f"Actor '{actor}' is not the target role '{offer.to_role}'",
                code="WRONG_RECIPIENT",
            )

        bundle = offer.cold_start_bundle
        errors = bundle.validate_digests(prd_digest, chain_digest)
        if errors:
            raise HandoffError(
                "Digest validation failed",
                code="DIGEST_MISMATCH",
                context={"errors": errors},
            )

        if repo and bundle.repo_uri != repo:
            raise HandoffError(
                f"Wrong repo: expected '{bundle.repo_uri}', got '{repo}'",
                code="WRONG_REPO",
            )

        offer.state = HandoffState.ACCEPTED.value
        offer.owner = actor
        offer.accepted_at = datetime.now(timezone.utc).isoformat()
        self._history.append({"action": "accept", "handoff_id": handoff_id, "actor": actor, "timestamp": offer.accepted_at})
        return offer

    def reject(self, handoff_id: str, reason: str = "") -> HandoffOffer:
        offer = self._offers.get(handoff_id)
        if offer is None:
            raise HandoffError(f"Handoff '{handoff_id}' not found", code="NOT_FOUND")
        if offer.state != HandoffState.OFFERED.value:
            raise HandoffError(f"Handoff '{handoff_id}' is in state '{offer.state}', not OFFERED", code="ALREADY_CLAIMED")
        offer.state = HandoffState.REJECTED.value
        self._history.append({"action": "reject", "handoff_id": handoff_id, "reason": reason})
        return offer

    def complete(self, handoff_id: str) -> HandoffOffer:
        offer = self._offers.get(handoff_id)
        if offer is None:
            raise HandoffError(f"Handoff '{handoff_id}' not found", code="NOT_FOUND")
        if offer.state != HandoffState.ACCEPTED.value:
            raise HandoffError(f"Handoff '{handoff_id}' must be ACCEPTED to complete, is '{offer.state}'", code="WRONG_STATE")
        offer.state = HandoffState.COMPLETED.value
        offer.completed_at = datetime.now(timezone.utc).isoformat()
        self._history.append({"action": "complete", "handoff_id": handoff_id, "timestamp": offer.completed_at})
        return offer

    def get_offer(self, handoff_id: str) -> Optional[HandoffOffer]:
        return self._offers.get(handoff_id)

    def list_offers(self, state: Optional[str] = None) -> list[HandoffOffer]:
        if state:
            return [o for o in self._offers.values() if o.state == state]
        return list(self._offers.values())


# ===========================================================================
# SW-1510-MCP-AGENTS — harness-neutral client capability attestation
# ===========================================================================
#
# The mailbox does not assume MCP is a reliable peer-to-peer event bus. What a
# client can actually *do* (receive pushed notifications, or only be polled) is
# therefore an explicitly detected, attested fact -- never assumed. Detection
# reads a positive, file-backed presence signal (an install root or an MCP
# server config), never merely "the client can run", because a client that can
# run but advertises no notification capability must fall back to polling. A
# client with no positive signal at all is *unknown*, and unknown falls closed
# to the polling adapter rather than being silently treated as capable.
#
# The capability vocabulary is the provider-neutral ``harness-capability``
# schema's, and detection is data-driven over an injected resolver so a test can
# prove the real decision without touching the host.

#: Notification transports a client may support. ``mcp-notifications`` is a
#: pushed MCP event channel (advisory, lossy); anything else is polled.
TRANSPORT_MCP_NOTIFICATIONS = "mcp-notifications"
TRANSPORT_FILE_POLL = "file-poll"
TRANSPORT_CLI_POLL = "cli-poll"

#: The environment variable a caller sets to *explicitly* attest a client's
#: capability surface, as a JSON object ``{"transports": [...], "features":
#: {...}}``. An explicit attestation is trusted over file probing (the operator
#: names the capability); an explicit attestation that omits notification
#: support still falls back to polling.
CAPABILITY_ATTESTATION_ENV = "SKILLWEAVE_CLIENT_CAPABILITY"


@dataclass(frozen=True)
class HarnessClientCapability:
    """One client's detected capability surface and the adapter it selects.

    ``client_id`` is the harness name (for example a vendor CLI). ``transports``
    is the detected/attested transport set. ``detected`` is ``True`` when a
    positive signal existed; ``False`` means the client was not observable and
    the surface was assumed *least capable*. ``reason`` names how the decision
    was reached, so a fallback is attributable rather than silent.
    """

    client_id: str
    detected: bool
    transports: frozenset[str]
    features: dict[str, Any]
    reason: str

    @property
    def supports_notifications(self) -> bool:
        """Whether this client can receive pushed MCP notifications."""
        return TRANSPORT_MCP_NOTIFICATIONS in self.transports

    def adapter_kind(self) -> str:
        """The adapter this capability selects.

        A client that supports MCP notifications uses the live notification
        adapter; every other client (including an undetected one) uses the
        file/CLI polling adapter -- the fallback always carries the same
        authority contract, so a missed notification never loses durable work.
        """
        return TRANSPORT_MCP_NOTIFICATIONS if self.supports_notifications else TRANSPORT_FILE_POLL

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "detected": self.detected,
            "transports": sorted(self.transports),
            "features": dict(self.features),
            "reason": self.reason,
            "adapter_kind": self.adapter_kind(),
        }


#: The positive, file-backed presence anchor per known client. The name is the
#: client id; the value is the MCP server config file whose *presence* is the
#: signal that the client is installed and configured for MCP at all. A config
#: hint without a real file is not a detection.
_MCP_CONFIG_ANCHORS: dict[str, str] = {
    "codex": "~/.codex/config.toml",
    "antigravity": "~/.gemini/antigravity/mcp.json",
    "openclaude": "~/.openclaude/mcp.json",
    "claude-code": "~/.claude/mcp.json",
    "gemini-cli": "~/.gemini/settings.json",
}


def _default_skill_root_resolver(client_id: str) -> Optional[str]:
    """The client's skill install root when it exists on this host, else None."""
    from ..installer import AGENT_CONFIG  # local import: keep handoff leaf-light

    for target in AGENT_CONFIG:
        if target.name == client_id:
            path = Path(target.path).expanduser()
            if path.exists():
                return str(path)
    return None


def _default_config_resolver(client_id: str) -> bool:
    anchor = _MCP_CONFIG_ANCHORS.get(client_id)
    if not anchor:
        return False
    return Path(anchor).expanduser().exists()


def _parse_attestation(raw: str) -> Optional[Mapping[str, Any]]:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, Mapping) else None


def detect_client_capability(
    client_id: str,
    *,
    attestation: Optional[Mapping[str, Any]] = None,
    skill_root_resolver: Any = None,
    config_resolver: Any = None,
) -> HarnessClientCapability:
    """Detect ``client_id``'s capability surface, falling closed to polling.

    Resolution order:

    1. An explicit ``attestation`` (or :data:`CAPABILITY_ATTESTATION_ENV`) names
       the transports the operator attests; it is trusted, but an attestation
       that omits :data:`TRANSPORT_MCP_NOTIFICATIONS` still selects polling.
    2. Otherwise a positive, file-backed presence signal -- a skill install root
       or an MCP config file -- marks the client ``detected``. When the signal
       carries no explicit transport set, a detected MCP-capable client is
       granted :data:`TRANSPORT_MCP_NOTIFICATIONS` *only* if its config anchor
       exists (an installed MCP server config); a skill-root-only signal selects
       the file/CLI polling adapter.
    3. No signal at all yields ``detected=False`` and the polling adapter.
    """
    import os as _os

    if not client_id:
        raise ValueError("a capability attestation requires a client_id")
    if attestation is None:
        attestation = _parse_attestation(
            _os.environ.get(CAPABILITY_ATTESTATION_ENV, "")
        )

    if attestation is not None:
        raw_transports = attestation.get("transports") or []
        transports = frozenset(str(t) for t in raw_transports)
        # A notification transport may not be claimed without also being the
        # client's own attested transport; an empty set is the polling fallback.
        features = dict(attestation.get("features") or {})
        if not transports:
            transports = frozenset({TRANSPORT_FILE_POLL})
        return HarnessClientCapability(
            client_id=client_id,
            detected=True,
            transports=transports,
            features=features,
            reason="explicit operator attestation",
        )

    skill_root = None
    if skill_root_resolver is not None:
        skill_root = skill_root_resolver(client_id)
    else:
        skill_root = _default_skill_root_resolver(client_id)

    config_present = False
    if config_resolver is not None:
        config_present = bool(config_resolver(client_id))
    else:
        config_present = _default_config_resolver(client_id)

    if skill_root and config_present:
        return HarnessClientCapability(
            client_id=client_id,
            detected=True,
            transports=frozenset({TRANSPORT_MCP_NOTIFICATIONS, TRANSPORT_FILE_POLL}),
            features={"skill_root": skill_root, "mcp_config": True},
            reason="installed skill root and MCP config present",
        )
    if skill_root:
        return HarnessClientCapability(
            client_id=client_id,
            detected=True,
            transports=frozenset({TRANSPORT_FILE_POLL}),
            features={"skill_root": skill_root, "mcp_config": False},
            reason="installed skill root; no MCP notification config detected",
        )
    if config_present:
        return HarnessClientCapability(
            client_id=client_id,
            detected=True,
            transports=frozenset({TRANSPORT_FILE_POLL}),
            features={"skill_root": None, "mcp_config": True},
            reason="MCP config present but no skill root; polling adapter",
        )
    return HarnessClientCapability(
        client_id=client_id,
        detected=False,
        transports=frozenset({TRANSPORT_CLI_POLL}),
        features={},
        reason="no positive presence signal; assumed least-capable (polling)",
    )


def detect_capabilities(
    client_ids: Optional[list[str]] = None,
    **kwargs: Any,
) -> dict[str, HarnessClientCapability]:
    """Detect several clients; defaults to the clients the criteria name.

    The default set is the three named clients -- ``codex``, ``antigravity`` and
    ``openclaude`` -- so each one's support is *explicitly* detected rather than
    assumed.
    """
    ids = client_ids or ["codex", "antigravity", "openclaude"]
    return {cid: detect_client_capability(cid, **kwargs) for cid in ids}


# ---------------------------------------------------------------------------
# Transport adapters: the same authority contract over either transport
# ---------------------------------------------------------------------------


class HandoffTransportAdapter:
    """Base adapter: read pending handoffs through one transport.

    Every adapter carries the *same* authority contract -- a scoped ``principal``
    and an addressed ``recipient`` -- so switching a client between the live
    notification transport and the polling fallback never broadens what the
    client may see. The capability, not the transport, is what varies.
    """

    kind = ""

    def __init__(self, capability: HarnessClientCapability) -> None:
        self.capability = capability

    @property
    def principal(self) -> str:
        """Adapters do not carry authority of their own; the caller supplies it.

        Present so a misuse that treats the adapter as an authority source fails
        loudly rather than defaulting to a wildcard principal.
        """
        raise HandoffError(
            "a transport adapter is not an authority source; pass an explicit "
            "scoped principal to read()",
            code="ADAPTER_IS_NOT_AUTHORITY",
        )

    def read(self, *, principal: str, recipient: str, resume_token: str = "") -> list[dict[str, Any]]:
        raise NotImplementedError

    def send(self, message: Any) -> dict[str, Any]:
        raise NotImplementedError

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "capability": self.capability.to_dict()}


class NotificationTransportAdapter(HandoffTransportAdapter):
    """Deliver handoffs over the live MCP notification channel.

    Used only when the client attested/attests the ``mcp-notifications``
    transport. Notifications are advisory; the durable mailbox is the source of
    truth, so this adapter reads the mailbox too (an undelivered notification
    never loses a message).
    """

    kind = TRANSPORT_MCP_NOTIFICATIONS

    def __init__(self, capability: HarnessClientCapability, mailbox: Any) -> None:
        super().__init__(capability)
        if not capability.supports_notifications:
            raise HandoffError(
                f"client {capability.client_id!r} does not support MCP "
                f"notifications; use the polling adapter",
                code="TRANSPORT_NOT_SUPPORTED",
            )
        self._mailbox = mailbox
        self._notified: list[str] = []

    def read(self, *, principal: str, recipient: str, resume_token: str = "") -> list[dict[str, Any]]:
        return self._mailbox.read(
            recipient=recipient, principal=principal, resume_token=resume_token
        )

    def send(self, message: Any) -> dict[str, Any]:
        record = self._mailbox.post(message)
        self._notified.append(str(record.get("message_id")))
        return {"transport": self.kind, "delivered": True, "record": record}

    def to_dict(self) -> dict[str, Any]:
        out = super().to_dict()
        out["notified"] = list(self._notified)
        return out


class PollingTransportAdapter(HandoffTransportAdapter):
    """Deliver handoffs by polling the durable mailbox (file/CLI fallback).

    The fallback for a client whose MCP surface cannot receive notifications --
    or was never detected. It reads the same durable mailbox by cursor and
    posts through the same mailbox, so it carries the identical authority
    contract and no message is lost when a notification is missed.
    """

    kind = TRANSPORT_FILE_POLL

    def __init__(self, capability: HarnessClientCapability, mailbox: Any) -> None:
        super().__init__(capability)
        self._mailbox = mailbox

    def read(self, *, principal: str, recipient: str, resume_token: str = "") -> list[dict[str, Any]]:
        return self._mailbox.read(
            recipient=recipient, principal=principal, resume_token=resume_token
        )

    def send(self, message: Any) -> dict[str, Any]:
        record = self._mailbox.post(message)
        return {"transport": self.kind, "delivered": True, "record": record}

    def to_dict(self) -> dict[str, Any]:
        return super().to_dict()


def adapter_for_capability(
    capability: HarnessClientCapability, mailbox: Any
) -> HandoffTransportAdapter:
    """Select the transport adapter *capability* dictates (never assumed).

    A client that supports MCP notifications gets the live adapter; every other
    client -- including one that was never detected -- gets the polling adapter.
    Both take the same ``mailbox`` and both enforce the same scoped authority.
    """
    if capability.supports_notifications:
        return NotificationTransportAdapter(capability, mailbox)
    return PollingTransportAdapter(capability, mailbox)
