from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
import hashlib
import json
import uuid

# SW-1511-RECOVERY criterion 2: the capability name an adapter attests before an
# actual session resume is admissible. Imported from the recovery contract so
# the vocabulary name has one definition; only the *name* is used here (the
# decision value type is imported lazily to keep module import acyclic).
from skillweave.runtime.native_process import (
    HARNESS_SESSION_RESUME_CAPABILITY,
)


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

    ``resume_capability_attested`` (SW-1511-RECOVERY criterion 2) is the
    *positive* attestation that this client can resume an actual provider
    session across a restart. It defaults to ``False`` -- absent a positive
    attestation the capability is *not* assumed, so a recovered controller falls
    closed to a checkpoint continuation rather than fabricating session
    continuity. An explicit attestation supplies it through the
    ``session-resume`` entry of ``features``.
    """

    client_id: str
    detected: bool
    transports: frozenset[str]
    features: dict[str, Any]
    reason: str
    resume_capability_attested: bool = False

    @property
    def supports_session_resume(self) -> bool:
        """Whether this client attested it can resume an actual session.

        Falls closed: only a genuine positive attestation is ``True``, so an
        undetected or unattested client never grants actual session resume.
        """
        return bool(self.resume_capability_attested)

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
            "resume_capability_attested": bool(self.resume_capability_attested),
        }

    def continuation_decision(
        self, run_id: str, *, prior_session_id: str = "", intent_id: str = ""
    ) -> "HarnessContinuation":
        """Decide how a recovered controller continues this client's work.

        The decision is driven by the *attested* capability, never assumed: an
        attested client with a recorded prior session resumes that session; any
        other client (unattested, or attested but with no recorded session)
        continues from the durable checkpoint and names the limitation. This is
        the production decision a recovered controller reaches through the
        client's capability surface (see
        :func:`skillweave.runtime.native_process.plan_harness_continuation`).
        """
        from skillweave.runtime.native_process import plan_harness_continuation

        return plan_harness_continuation(
            run_id,
            resume_capability_attested=self.supports_session_resume,
            prior_session_id=prior_session_id,
            intent_id=intent_id,
        )


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
        # SW-1511-RECOVERY criterion 2: a session-resume attestation is a
        # *positive* fact. An explicit attestation may supply it either as a
        # top-level ``session-resume`` key or within ``features``; anything else
        # (absent, false) falls closed to no session resume.
        resume = bool(
            attestation.get(HARNESS_SESSION_RESUME_CAPABILITY)
            or features.get(HARNESS_SESSION_RESUME_CAPABILITY)
        )
        if not transports:
            transports = frozenset({TRANSPORT_FILE_POLL})
        return HarnessClientCapability(
            client_id=client_id,
            detected=True,
            transports=transports,
            features=features,
            reason="explicit operator attestation",
            resume_capability_attested=resume,
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
# SW-1511-RECOVERY criterion 5: a child's capability, recorded from an actual
# headless launch -- never inferred from the interactive/desktop settings
# ---------------------------------------------------------------------------

#: Why a child capability could not be recorded from a real launch.
CHILD_CAPABILITY_UNOBSERVABLE = "child_launch_unobservable"
CHILD_CAPABILITY_UNKNOWN_HARNESS = "unknown_child_harness"
CHILD_CAPABILITY_TOKEN_MISMATCH = "child_session_token_mismatch"
CHILD_CAPABILITY_NO_SERVICE_BINDING = "child_no_process_service_binding"
CHILD_CAPABILITY_FOREIGN_SERVICE = "child_resolved_a_foreign_service"


class ChildCapabilityError(ValueError):
    """A child capability could not be honestly recorded from a real launch.

    Every refusal names a stable ``code`` so an operator reads *why* the child
    capability was not recorded -- an unobservable launch, an unattached child,
    a child that resolved a foreign service. It is never upgraded to a pass.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

    def to_dict(self) -> dict[str, Any]:
        return {"status": "refused", "code": self.code, "message": str(self)}


def _attested_transports(observed_env: Mapping[str, str]) -> frozenset[str]:
    """The transports the *observed child environment* attests, never the host.

    The declared attestation is the only transport signal here: the child's own
    ``SKILLWEAVE_CLIENT_CAPABILITY`` (if any). No config-file anchor, no
    skill-root probe, and above all no desktop/interactive setting is consulted
    -- criterion 5 requires the child's capability to come from the actual
    headless launch. A child that attests nothing falls closed to the file-poll
    adapter.
    """
    parsed = _parse_attestation(observed_env.get(CAPABILITY_ATTESTATION_ENV, ""))
    if not parsed:
        return frozenset({TRANSPORT_FILE_POLL})
    raw = parsed.get("transports") or []
    transports = frozenset(str(t) for t in raw)
    return transports or frozenset({TRANSPORT_FILE_POLL})


def capability_from_child_launch(
    *,
    client_id: str,
    grant: Any,
    observed_env: Optional[Mapping[str, str]],
    attempt: str = "",
) -> HarnessClientCapability:
    """Record a child client's capability from its *actual* headless launch.

    Criterion 5 is precise about the source: a child session's capabilities are
    recorded **from an actual headless launch**, not inferred from the
    interactive/desktop settings. So this reads only the child's own *observed*
    environment -- the effective MCP configuration the launch provisioned -- and
    never :data:`_MCP_CONFIG_ANCHORS` (the desktop anchors) or any skill-root
    probe. Fails closed with a named :class:`ChildCapabilityError` when the
    launch cannot be honestly characterised:

    * the child's effective environment is unobservable;
    * the child did not carry the server-issued session token (it never attached
      to this controller), or resolved no process-service binding;
    * the child resolved a *different* process service than the one the grant
      names (an isolated per-harness controller), refused through the same
      :func:`refuse_isolated_controller` seam the launch verification uses.

    The recorded capability carries **no environment value** -- only detected
    facts -- so a provider credential can never be captured into it.
    """
    from skillweave.mcp import bootstrap as _bootstrap

    if not client_id:
        raise ChildCapabilityError(
            CHILD_CAPABILITY_UNKNOWN_HARNESS,
            "a child capability requires the child harness's client id",
        )
    if observed_env is None:
        raise ChildCapabilityError(
            CHILD_CAPABILITY_UNOBSERVABLE,
            "the child launch did not expose its resolved environment, so its "
            "capabilities cannot be recorded from the actual launch",
        )
    token = observed_env.get(str(_bootstrap.ENV_MCP_SESSION_TOKEN), "")
    if not token or token != getattr(grant, "session_token", ""):
        raise ChildCapabilityError(
            CHILD_CAPABILITY_TOKEN_MISMATCH,
            "the child launch did not carry the granted session token; it did "
            "not attach to this controller",
        )
    observed_service = _bootstrap.service_from_environment(observed_env)
    if observed_service is None:
        raise ChildCapabilityError(
            CHILD_CAPABILITY_NO_SERVICE_BINDING,
            "the child launch carried no observable process-service binding",
        )
    try:
        _bootstrap.refuse_isolated_controller(grant.service, observed_service)
    except _bootstrap.ProcessServiceMismatchRefused as exc:
        raise ChildCapabilityError(CHILD_CAPABILITY_FOREIGN_SERVICE, str(exc))

    parsed = _parse_attestation(observed_env.get(CAPABILITY_ATTESTATION_ENV, ""))
    features = dict((parsed or {}).get("features") or {})
    resume = bool(
        (parsed or {}).get(HARNESS_SESSION_RESUME_CAPABILITY)
        or features.get(HARNESS_SESSION_RESUME_CAPABILITY)
    )
    return HarnessClientCapability(
        client_id=client_id,
        detected=True,
        transports=_attested_transports(observed_env),
        features={
            # Facts, never values: no credential, no path from the host.
            "recorded_from": "headless-launch",
            "session_token_present": True,
            "service_bound": True,
            "attempt": attempt,
        },
        reason="recorded from an actual headless child launch",
        resume_capability_attested=resume,
    )


# ---------------------------------------------------------------------------
# SW-1511-RECOVERY criterion 6: one recovered owned attempt per independent
# interruption, mailbox replay/ack by cursor, no duplicate dispatch
# ---------------------------------------------------------------------------

#: The two interruptions the criterion names, *independently*: the durable
#: controller service is killed and restarted, or the interactive main harness
#: is replaced. Each must produce exactly one recovered owned attempt.
RECOVERY_TRIGGER_CONTROLLER_RESTART = "controller_restart"
RECOVERY_TRIGGER_HARNESS_REPLACEMENT = "harness_replacement"
RECOVERY_TRIGGERS: tuple[str, ...] = (
    RECOVERY_TRIGGER_CONTROLLER_RESTART,
    RECOVERY_TRIGGER_HARNESS_REPLACEMENT,
)

#: Why no owned attempt was recovered.
RECOVERY_NO_OWNED_ATTEMPT = "no_owned_attempt"
RECOVERY_FIRST_ATTEMPT_NOT_VERIFIED = "first_attempt_not_verified"
RECOVERY_UNKNOWN_TRIGGER = "unknown_recovery_trigger"
RECOVERY_SCOPE_NOT_BOUND = "recovery_scope_not_bound"


@dataclass(frozen=True)
class RecoveredOwnedAttempt:
    """The single owned attempt an independent interruption recovers.

    ``recovered`` is ``True`` only when the *ordering-first* in-flight attempt's
    recorded identity still verifies. A first attempt that no longer verifies is
    a fail-closed refusal that never adopts a later attempt, so one interruption
    can never recover two attempts and a stolen frontier is never adopted. The
    ``scope`` records the record's own run/task/attempt, never a relabel of the
    caller's request.
    """

    trigger: str
    run_id: str
    task_id: str
    attempt: int
    intent_id: str
    session_id: str
    recovered: bool
    reason: str = ""
    cursor: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "recovered-owned-attempt-v1",
            "trigger": self.trigger,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "intent_id": self.intent_id,
            "session_id": self.session_id,
            "recovered": self.recovered,
            "reason": self.reason,
            "cursor": self.cursor,
        }


def _intent_scope(intent: Any) -> tuple[str, str, int]:
    return (
        str(getattr(intent, "run_id", "") or ""),
        str(getattr(intent, "task_id", "") or ""),
        int(getattr(intent, "attempt", 0) or 0),
    )


def plan_owned_attempt_recovery(
    *,
    trigger: str,
    run_id: str,
    intent_id: str,
    session_id: str,
    task_id: str = "",
    attempt: int = 0,
    intents: Optional[Sequence[Any]] = None,
    cursor: Optional[dict[str, Any]] = None,
) -> RecoveredOwnedAttempt:
    """Resolve exactly one recovered owned attempt for an interruption.

    The caller supplies the *already ordering-first, claiming-only* durable
    intents for the run (the frontier ``created_at``/``intent_id`` order the
    store returns) and the identity-verified first attempt it selected -- the
    same selection :func:`skillweave.runtime.native_process.plan_harness_continuation`
    and the controller's ``_resume_decision`` make. This resolver enforces the
    criterion's shape on that selection:

    * the ``trigger`` must be one of :data:`RECOVERY_TRIGGERS` -- controller
      restart and harness replacement are the only two admissions;
    * a ``run_id`` is required (an unbound recovery has no scope to recover);
    * the selected attempt's *own* run/task/attempt must match the requested
      scope -- a record that belongs to a different run/task/attempt is never
      relabelled and never adopted;
    * recovery is granted only when the frontier carried a verified owned
      attempt (``intent_id``/``session_id`` non-empty). A frontier with no
      attempt, or whose first attempt did not verify, is a named fail-closed
      refusal that adopts nothing else.
    """
    if trigger not in RECOVERY_TRIGGERS:
        return RecoveredOwnedAttempt(
            trigger=trigger, run_id=run_id, task_id=task_id, attempt=attempt,
            intent_id="", session_id="", recovered=False,
            reason=RECOVERY_UNKNOWN_TRIGGER,
        )
    if not run_id:
        return RecoveredOwnedAttempt(
            trigger=trigger, run_id=run_id, task_id=task_id, attempt=attempt,
            intent_id="", session_id="", recovered=False,
            reason=RECOVERY_SCOPE_NOT_BOUND,
        )
    ordered = list(intents or ())
    if not ordered or not intent_id or not session_id:
        return RecoveredOwnedAttempt(
            trigger=trigger, run_id=run_id, task_id=task_id, attempt=attempt,
            intent_id="", session_id="", recovered=False,
            reason=RECOVERY_NO_OWNED_ATTEMPT,
            cursor=cursor,
        )
    # The adopted record must be the frontier and must carry its *own* scope
    # equal to the requested scope: never a relabel of a foreign record.
    first = ordered[0]
    if str(getattr(first, "intent_id", "") or "") != intent_id:
        return RecoveredOwnedAttempt(
            trigger=trigger, run_id=run_id, task_id=task_id, attempt=attempt,
            intent_id="", session_id="", recovered=False,
            reason=RECOVERY_FIRST_ATTEMPT_NOT_VERIFIED,
            cursor=cursor,
        )
    rec_run, rec_task, rec_attempt = _intent_scope(first)
    scope_ok = rec_run == run_id and (
        (not task_id and not rec_task) or rec_task == task_id
    ) and (not attempt or rec_attempt == attempt)
    if not scope_ok:
        return RecoveredOwnedAttempt(
            trigger=trigger, run_id=run_id, task_id=task_id, attempt=attempt,
            intent_id="", session_id="", recovered=False,
            reason=RECOVERY_FIRST_ATTEMPT_NOT_VERIFIED,
            cursor=cursor,
        )
    return RecoveredOwnedAttempt(
        trigger=trigger, run_id=run_id, task_id=task_id, attempt=attempt,
        intent_id=intent_id, session_id=session_id, recovered=True,
        reason="", cursor=cursor,
    )


def mailbox_cursor_binding(
    *,
    after_seq: int = 0,
    last_digest: str = "",
) -> Any:
    """The durable ``shared-process-mailbox-binding-v1`` cursor value type.

    Built through the same bootstrap seam the child grant uses, so the cursor a
    recovered session resends is the *exact* schema the mailbox verifies against
    -- a cursor from a different mailbox or a rewritten/truncated channel is
    refused by :meth:`SharedProcessMailboxBinding.verify_against` / the mailbox
    read, never silently accepted.
    """
    from skillweave.mcp import bootstrap as _bootstrap

    return _bootstrap.MailboxCursorBinding(
        after_seq=int(after_seq), last_digest=last_digest
    )


def cursor_token(cursor: Any) -> tuple[int, str]:
    """``(after_seq, last_digest)`` decoded from a durable cursor token.

    A recovered session resends the **opaque token** a prior read emitted (the
    mailbox's own ``MailboxCursor`` encoding), never a caller-chosen offset: the
    token is decoded through :meth:`MailboxCursor.decode`, so a rewritten or
    foreign token is a :class:`MessageChannelError`, not an accepted position.
    An absent token is the empty position ``(0, "")``.
    """
    from skillweave.dispatch.messaging import MailboxCursor

    if not cursor or not isinstance(cursor, str):
        return 0, ""
    decoded = MailboxCursor.decode(cursor)
    if decoded is None:
        return 0, ""
    return int(decoded.after_seq), str(decoded.last_digest)


def replay_by_cursor(
    mailbox: Any,
    *,
    recipient: str,
    principal: str,
    cursor: Any = None,
) -> tuple[list[dict[str, Any]], str]:
    """Replay a mailbox from a durable cursor token, returning ``(records, token)``.

    This is criterion 6's *mailbox replay by cursor*: the read starts exactly
    after the position the prior session persisted, so a session that never
    observed the live notification still replays every durable message it had
    not yet accepted, in append order. The ``cursor`` is the **opaque token the
    mailbox itself emitted** (decoded via :meth:`MailboxCursor.decode`); the
    mailbox verifies the pinned digest against the record it names, so a
    truncated or rewritten channel is refused (:class:`MessageChannelError`)
    rather than silently resumed past lost work. The returned token is the
    mailbox's own next-cursor encoding, ready to persist.
    """
    from skillweave.dispatch.messaging import MailboxCursor

    after_seq, _last_digest = cursor_token(cursor)
    token = cursor if isinstance(cursor, str) else ""
    records = mailbox.read(
        recipient=recipient,
        principal=principal,
        resume_token=token,
        after_seq=after_seq,
    )
    if records:
        next_token = MailboxCursor(
            after_seq=int(records[-1].get("seq", 0)),
            last_digest=str(records[-1].get("digest", "")),
        ).encode()
    else:
        next_token = token
    return list(records), next_token


def ack_by_cursor(
    mailbox: Any,
    *,
    records: Sequence[Mapping[str, Any]],
    recipient: str,
    principal: str,
    note: str = "",
) -> list[dict[str, Any]]:
    """Acknowledge each replayed record through the durable ``ack`` reply.

    An ack is addressed to the message's *own* recipient under its *own* scoped
    principal, and is idempotent by a derived reply id -- so a replayed ack
    (a duplicate delivery after a second interruption) appends no second
    acknowledgement. A record addressed to a different recipient/principal is
    refused by the mailbox, never acknowledged on another's behalf.
    """
    acks: list[dict[str, Any]] = []
    for record in records:
        message_id = str(record.get("message_id", "") or "")
        if not message_id:
            continue
        acks.append(
            mailbox.ack(message_id, recipient=recipient, principal=principal, note=note)
        )
    return acks


# ---------------------------------------------------------------------------
# Transport adapters: the same authority contract over either transport
# ---------------------------------------------------------------------------


class HandoffTransportAdapter:
    """Base adapter: read pending handoffs through one transport.

    Every adapter carries the *same* authority contract -- a scoped ``principal``
    and an addressed ``recipient`` -- so switching a client between the live
    notification transport and the polling fallback never broadens what the
    client may see. The capability, not the transport, is what varies.

    The *scope* an adapter enforces is not the adapter's own: it is an explicit
    ``authority_contract`` supplied by the server-owned selection seam (see
    :func:`select_handoff_adapter`). Every adapter therefore fails closed on a
    read/write whose ``principal``/``recipient`` is outside that contract, so a
    transport can never carry a principal other than the one the server
    authenticated. A direct-caller adapter built without a contract carries no
    scope (the historical in-process contract) and enforces only the
    underlying mailbox's own ``(recipient, principal)`` addressing.
    """

    kind = ""

    def __init__(
        self,
        capability: HarnessClientCapability,
        authority_contract: Any = None,
    ) -> None:
        self.capability = capability
        self.authority_contract = authority_contract

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

    def _enforce_contract(self, *, principal: str, recipient: str, action: str) -> None:
        """Fail closed on a scope the server-owned contract did not grant.

        The authoritative (non-None) contract comes from the authenticated
        session, so a client can never read or send outside it: a differing
        ``principal`` is a scope_escape (the same refusal the messaging seam
        raises for a forged principal) and an un-addressed ``recipient`` is a
        named refusal, never a silent wildcard.
        """
        contract = self.authority_contract
        if contract is None:
            return
        if principal != contract.principal:
            raise HandoffError(
                f"principal {principal!r} is outside the {self.kind!r} "
                f"adapter's authority contract ({contract.principal!r})",
                code="scope_escape",
            )
        if recipient and recipient not in contract.recipients:
            raise HandoffError(
                f"recipient {recipient!r} is not addressed by the "
                f"{self.kind!r} adapter's authority contract",
                code="recipient_not_addressed",
            )

    def read(self, *, principal: str, recipient: str, resume_token: str = "") -> list[dict[str, Any]]:
        raise NotImplementedError

    def send(self, message: Any) -> dict[str, Any]:
        raise NotImplementedError

    def to_dict(self) -> dict[str, Any]:
        out = {"kind": self.kind, "capability": self.capability.to_dict()}
        if self.authority_contract is not None:
            out["authority_contract"] = self.authority_contract.to_dict()
        return out


class NotificationTransportAdapter(HandoffTransportAdapter):
    """Deliver handoffs over the live MCP notification channel.

    Used only when the client attested/attests the ``mcp-notifications``
    transport. Notifications are advisory; the durable mailbox is the source of
    truth, so this adapter reads the mailbox too (an undelivered notification
    never loses a message).
    """

    kind = TRANSPORT_MCP_NOTIFICATIONS

    def __init__(
        self,
        capability: HarnessClientCapability,
        mailbox: Any,
        authority_contract: Any = None,
    ) -> None:
        super().__init__(capability, authority_contract)
        if not capability.supports_notifications:
            raise HandoffError(
                f"client {capability.client_id!r} does not support MCP "
                f"notifications; use the polling adapter",
                code="TRANSPORT_NOT_SUPPORTED",
            )
        self._mailbox = mailbox
        self._notified: list[str] = []

    def read(self, *, principal: str, recipient: str, resume_token: str = "") -> list[dict[str, Any]]:
        self._enforce_contract(principal=principal, recipient=recipient, action="read")
        return self._mailbox.read(
            recipient=recipient, principal=principal, resume_token=resume_token
        )

    def send(self, message: Any) -> dict[str, Any]:
        self._enforce_contract(
            principal=getattr(message, "principal", ""),
            recipient=getattr(message, "recipient", ""),
            action="send",
        )
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

    def __init__(
        self,
        capability: HarnessClientCapability,
        mailbox: Any,
        authority_contract: Any = None,
    ) -> None:
        super().__init__(capability, authority_contract)
        self._mailbox = mailbox

    def read(self, *, principal: str, recipient: str, resume_token: str = "") -> list[dict[str, Any]]:
        self._enforce_contract(principal=principal, recipient=recipient, action="read")
        return self._mailbox.read(
            recipient=recipient, principal=principal, resume_token=resume_token
        )

    def send(self, message: Any) -> dict[str, Any]:
        self._enforce_contract(
            principal=getattr(message, "principal", ""),
            recipient=getattr(message, "recipient", ""),
            action="send",
        )
        record = self._mailbox.post(message)
        return {"transport": self.kind, "delivered": True, "record": record}

    def to_dict(self) -> dict[str, Any]:
        return super().to_dict()


#: The transports an adapter may be selected for. A transport outside this set
#: is a *named unsupported HOLD* -- never coerced to stdio/polling.
SUPPORTED_HANDOFF_TRANSPORTS = frozenset(
    {TRANSPORT_MCP_NOTIFICATIONS, TRANSPORT_FILE_POLL, TRANSPORT_CLI_POLL}
)


@dataclass(frozen=True)
class HandoffAuthorityContract:
    """The scope a selected transport adapter must enforce.

    The *same* authority contract every adapter carries: the authenticated
    ``principal`` (server-owned, never a caller field) and the ``recipients``
    that principal may address. It is built by the server-owned selection seam
    from the authenticated session, so a transport switch can never broaden
    what a client may read or write.
    """

    principal: str
    recipients: frozenset[str]

    def allows(self, *, recipient: str) -> bool:
        return recipient in self.recipients

    def to_dict(self) -> dict[str, Any]:
        return {
            "principal": self.principal,
            "recipients": sorted(self.recipients),
        }


def select_handoff_adapter(
    capability: HarnessClientCapability,
    mailbox: Any,
    *,
    authority_contract: Any = None,
    requested_transport: str = "",
) -> HandoffTransportAdapter:
    """The real production selection path: capability -> qualified adapter.

    An *explicit* ``requested_transport`` is honored only when it is a supported
    transport (a client that demands a transport it does not attest is a named
    ``TRANSPORT_NOT_SUPPORTED`` HOLD -- never a silent coerce). The default,
    capability-driven path selects the notification adapter only for a client
    that attested/detected notification support, and the file/CLI polling
    adapter for every other client (including an undetected one). Every selected
    adapter carries the same ``authority_contract``.
    """
    # An *unknown* requested transport is a named HOLD for *every* client class,
    # checked before any capability-driven branch: a client that cannot receive
    # notifications must never have an unrecognized transport silently coerced
    # to the polling adapter.
    if requested_transport and requested_transport not in SUPPORTED_HANDOFF_TRANSPORTS:
        raise HandoffError(
            f"unsupported handoff transport {requested_transport!r}; supported "
            f"transports are {sorted(SUPPORTED_HANDOFF_TRANSPORTS)}",
            code="TRANSPORT_NOT_SUPPORTED",
        )
    if not capability.supports_notifications:
        # A client that cannot receive MCP notifications must never be handed
        # the notification adapter: the fallback is the qualified polling
        # adapter, which carries the same authority contract.
        if requested_transport == TRANSPORT_MCP_NOTIFICATIONS:
            raise HandoffError(
                f"client {capability.client_id!r} does not support MCP "
                f"notifications; use the polling adapter",
                code="TRANSPORT_NOT_SUPPORTED",
            )
        return PollingTransportAdapter(
            capability, mailbox, authority_contract
        )
    return NotificationTransportAdapter(
        capability, mailbox, authority_contract
    )


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
