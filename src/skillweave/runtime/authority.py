import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable, Mapping, Optional


class Role(str, Enum):
    OPS = "ops"
    REVIEWER = "reviewer"
    OBSERVER = "observer"
    OPERATOR = "operator"
    RELEASE_AUTHORITY = "release_authority"
    SUB_AGENT = "sub_agent"


@dataclass
class RoleAssignment:
    role: str
    actor_id: str
    scope: str
    valid_from: str
    valid_until: Optional[str]
    assigned_by: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        return {
            "role": self.role,
            "actor_id": self.actor_id,
            "scope": self.scope,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "assigned_by": self.assigned_by,
            "metadata": self.metadata,
        }


@dataclass
class DelegationRecord:
    from_role: str
    to_role: str
    delegated_by: str
    accepted_by: Optional[str]
    state: str
    scope: str
    delegated_at: str
    accepted_at: Optional[str] = None
    returned_at: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        return {
            "from_role": self.from_role,
            "to_role": self.to_role,
            "delegated_by": self.delegated_by,
            "accepted_by": self.accepted_by,
            "state": self.state,
            "scope": self.scope,
            "delegated_at": self.delegated_at,
            "accepted_at": self.accepted_at,
            "returned_at": self.returned_at,
            "metadata": self.metadata,
        }


@dataclass
class HumanApproval:
    actor: str
    timestamp: str
    scope: str
    policy_digest: str
    decision: str
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        return {
            "actor": self.actor,
            "timestamp": self.timestamp,
            "scope": self.scope,
            "policy_digest": self.policy_digest,
            "decision": self.decision,
            "reason": self.reason,
            "metadata": self.metadata,
        }


ROLE_CAPABILITY_MATRIX = {
    Role.OPS.value: {
        "can_mutate_run_state": True,
        "can_approve_gate": False,
        "can_review_gate": False,
        "can_release": False,
        "can_tag": False,
        "can_delegate": True,
        "is_read_only": False,
        "forbidden_transitions": ["approve_own_gate", "merge", "release", "tag"],
    },
    Role.REVIEWER.value: {
        "can_mutate_run_state": False,
        "can_approve_gate": True,
        "can_review_gate": True,
        "can_release": False,
        "can_tag": False,
        "can_delegate": False,
        "is_read_only": True,
        "forbidden_transitions": ["release", "tag", "merge", "write", "commit", "push"],
    },
    Role.OBSERVER.value: {
        "can_mutate_run_state": False,
        "can_approve_gate": False,
        "can_review_gate": False,
        "can_release": False,
        "can_tag": False,
        "can_delegate": False,
        "is_read_only": True,
        "forbidden_transitions": ["write_run_state", "approve_gate", "release", "tag", "merge"],
    },
    Role.OPERATOR.value: {
        "can_mutate_run_state": False,
        "can_approve_gate": True,
        "can_review_gate": True,
        "can_release": True,
        "can_tag": True,
        "can_delegate": True,
        "is_read_only": False,
        "forbidden_transitions": [],
    },
    Role.RELEASE_AUTHORITY.value: {
        "can_mutate_run_state": False,
        "can_approve_gate": True,
        "can_review_gate": True,
        "can_release": True,
        "can_tag": True,
        "can_delegate": False,
        "is_read_only": False,
        "forbidden_transitions": [],
    },
}

ROLE_CAPABILITY_VERSION = "1.0.0"


def can_approve_gate(role: str) -> bool:
    caps = ROLE_CAPABILITY_MATRIX.get(role, {})
    return caps.get("can_approve_gate", False)


def can_mutate_run_state(role: str) -> bool:
    caps = ROLE_CAPABILITY_MATRIX.get(role, {})
    return caps.get("can_mutate_run_state", False)


def is_read_only(role: str) -> bool:
    caps = ROLE_CAPABILITY_MATRIX.get(role, {})
    return caps.get("is_read_only", False)


def get_forbidden_transitions(role: str) -> list[str]:
    caps = ROLE_CAPABILITY_MATRIX.get(role, {})
    return list(caps.get("forbidden_transitions", []))


class AuthorityError(Exception):
    def __init__(self, role: str, action: str, reason: str, extra: Optional[dict] = None):
        self.role = role
        self.action = action
        self.reason = reason
        self.extra = extra or {}
        super().__init__(f"Role '{role}' cannot {action}: {reason}")


class AuthorityGuard:
    def __init__(self):
        self._assignments: list[RoleAssignment] = []
        self._delegations: list[DelegationRecord] = []
        self._capability_matrix = ROLE_CAPABILITY_MATRIX.copy()

    def get_capabilities(self, role: str) -> dict[str, Any]:
        return self._capability_matrix.get(role, {})

    def get_capability_version(self) -> str:
        return ROLE_CAPABILITY_VERSION

    def assign_role(self, assignment: RoleAssignment) -> RoleAssignment:
        self._assignments.append(assignment)
        return assignment

    def get_assignments(self, actor_id: Optional[str] = None) -> list[RoleAssignment]:
        if actor_id:
            return [a for a in self._assignments if a.actor_id == actor_id]
        return list(self._assignments)

    def delegate(self, record: DelegationRecord) -> DelegationRecord:
        source_role = record.from_role
        if not self._capability_matrix.get(source_role, {}).get("can_delegate", False):
            raise AuthorityError(
                source_role, "delegate",
                f"Role '{source_role}' is not authorized to delegate",
            )
        self._delegations.append(record)
        return record

    def accept_delegation(self, record: DelegationRecord) -> DelegationRecord:
        record.state = "accepted"
        record.accepted_at = datetime.now(timezone.utc).isoformat()
        return record

    def return_delegation(self, record: DelegationRecord) -> DelegationRecord:
        record.state = "returned"
        record.returned_at = datetime.now(timezone.utc).isoformat()
        return record

    def can_perform(self, role: str, action: str) -> bool:
        caps = self._capability_matrix.get(role, {})
        if caps.get("is_read_only", False):
            if action in (
                "mutate_run_state", "tag", "release", "merge",
                "write", "commit", "push",
            ):
                return False
        if action == "approve_gate":
            return caps.get("can_approve_gate", False)
        if action == "review_gate":
            return caps.get("can_review_gate", False)
        if action == "mutate_run_state":
            return caps.get("can_mutate_run_state", False)
        if action == "tag":
            return caps.get("can_tag", False)
        if action == "release":
            return caps.get("can_release", False)
        if action == "delegate":
            return caps.get("can_delegate", False)
        # Any write/commit/push action is denied unless the role is explicitly
        # non-read-only AND carries the matching capability. Reviewers fail
        # closed here: they are read-only and never granted these.
        if action == "write":
            return (not caps.get("is_read_only", False)) and caps.get("can_mutate_run_state", False)
        if action == "commit":
            return (not caps.get("is_read_only", False)) and caps.get("can_mutate_run_state", False)
        if action == "push":
            return (not caps.get("is_read_only", False)) and caps.get("can_mutate_run_state", False)
        return False

    def assert_can_write(self, role: str, action: str = "write") -> None:
        """Raise AuthorityError unless ``role`` may perform ``action``.

        Used at the seam before any reviewer write/commit/push/mutation is
        attempted, so the block happens BEFORE execution, not after."""
        if not self.can_perform(role, action):
            raise AuthorityError(role, action, f"Role '{role}' is read-only or lacks '{action}'")

    def approve(
        self,
        actor: str,
        role: str,
        scope: str,
        policy_digest: str,
        decision: str = "approved",
        reason: str = "",
    ) -> HumanApproval:
        action_map = {
            "approved": "approve_gate",
            "rejected": "review_gate",
        }
        action = action_map.get(decision, "review_gate")
        if not self.can_perform(role, action):
            raise AuthorityError(role, action, f"Role '{role}' lacks authority for '{decision}' on '{scope}'")

        return HumanApproval(
            actor=actor,
            timestamp=datetime.now(timezone.utc).isoformat(),
            scope=scope,
            policy_digest=policy_digest,
            decision=decision,
            reason=reason,
        )

    def validate_approval(self, approval: HumanApproval, approving_role: str) -> bool:
        if approving_role == Role.OPS.value:
            raise AuthorityError(
                approving_role, "approve_gate",
                "Ops role cannot approve gates — separation of duties violated",
                {"scope": approval.scope, "actor": approval.actor},
            )
        return self.can_perform(approving_role, "approve_gate")


# ===========================================================================
# SW-1510-MCP-AUTH — server-owned MCP principal authority
# ===========================================================================
#
# Everything above this banner is *descriptive policy*: a caller can construct
# an :class:`AuthorityGuard`, a :class:`HumanApproval` and read the role matrix.
# That is exactly why those helpers can never be authentication: a reachable
# local MCP server, or a caller passing ``role="reviewer"``, must not be proof
# of anything.
#
# The classes below are the *server-owned* seam. Only an object that answers
# :meth:`MCPPrincipalAuthority.serve_live` issues principals, and the only way
# to obtain one is to present an opaque session token minted in host process
# memory. Authority is looked up from the registry by constant-time digest
# comparison; it is never read from a caller-supplied field. A caller cannot
# mint a principal, cannot widen its own role, and cannot replay a consumed
# decision.

#: The SDK-owned, frozen ``execution-envelope-v1`` contract this middleware
#: consumes. Core does **not** copy the schema bytes: the wire shape is the one
#: authority (``skillweave_sdk.validator``, installed from
#: ``integrations/skillweave-sdk``) and the middleware validates against its
#: schema ``$id`` -- never a private, competing contract. See controller
#: finding 3.
EXECUTION_ENVELOPE_SCHEMA_ID = (
    "https://skillweave.dev/schemas/execution/execution-envelope/v1"
)

#: The SDK schema's frozen ``schema_version`` const (``$id`` above). An unknown
#: version is refused by name; there is no silent default.
EXECUTION_ENVELOPE_VERSION = "1.0.0"

#: The canonical digest of the SDK contract *set* the middleware was frozen
#: against. It is pinned (rather than read from the ambient package) so a
#: consumer refuses a drifted schema tree by name; when the SDK is present the
#: value is cross-checked against ``skillweave_sdk.validator``.
EXECUTION_ENVELOPE_SCHEMA_DIGEST = (
    "3dd247f604017bac4ff37ff4f3e9b46c5d1bc48fc8ef225de88ec4b6d14d2e64"
)

#: The namespaced extension key under which the MCP-principal facts ride. The
#: SDK envelope forbids additional top-level properties, so the middleware's
#: extra fields live inside the open ``scope`` object under a namespaced key --
#: an extension of the one contract, never a second competing one.
EXECUTION_ENVELOPE_MCP_EXTENSION = "skillweave:mcp"

#: The provider routes a client credential may never be forwarded to (kept in
#: sync with the middleware's ``NON_FORWARDABLE_ROUTES``).
_NON_FORWARDABLE_ROUTES = frozenset({"passthrough", "echo", "reflect"})

#: The four MCP session roles that must carry *distinct scoped authority*.
#: ``worker`` is the most constrained (execute one bound attempt, never
#: approve); ``reviewer`` may review/approve a bound run but is read-only for
#: state mutation; ``controller`` may mutate run state and admit gates but may
#: not self-review; ``observer`` may only read. A worker that asks for
#: ``reviewer`` is refused -- the role is read from the registry, not the wire.
MCP_ROLE_SCOPES: dict[str, dict[str, Any]] = {
    "worker": {
        "operations": frozenset({"read_run", "write_own_artifact", "start_own_attempt"}),
        "can_review": False,
        "can_approve": False,
        "can_mutate_run_state": False,
        "read_only": False,
        "requires_run_binding": True,
    },
    "observer": {
        "operations": frozenset({"read_run"}),
        "can_review": False,
        "can_approve": False,
        "can_mutate_run_state": False,
        "read_only": True,
        "requires_run_binding": True,
    },
    "reviewer": {
        "operations": frozenset({"read_run", "review_run", "approve_gate"}),
        "can_review": True,
        "can_approve": True,
        "can_mutate_run_state": False,
        "read_only": True,
        "requires_run_binding": True,
    },
    "controller": {
        "operations": frozenset(
            {"read_run", "write_run_state", "start_run", "cancel_run",
             "resume_run", "admit_gate", "forward"}
        ),
        "can_review": False,
        "can_approve": False,
        "can_mutate_run_state": True,
        "read_only": False,
        "requires_run_binding": False,
    },
}

MCP_ROLE_SCOPE_VERSION = "1.0.0"


class MCPAuthError(AuthorityError):
    """An MCP authorization refusal, carrying an auditable ``code``.

    Every refusal on the MCP authorization boundary is one of these: a stable
    machine-readable ``code`` plus a human ``message``. A caller never receives
    a boolean or a partial success for a refused operation.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        role: str = "",
        action: str = "",
        extra: Optional[dict] = None,
    ):
        self.code = code
        self.message = message
        self.extra = dict(extra or {})
        super().__init__(role or "unknown", action or code, message, self.extra)

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": self.message,
            "code": self.code,
            "role": self.role,
            "action": self.action,
            **self.extra,
        }


class UnauthenticatedPrincipal(MCPAuthError):
    def __init__(self, message: str = "no authenticated MCP principal", **kw: Any):
        super().__init__("unauthenticated", message, **kw)


class RoleEscalationRefused(MCPAuthError):
    def __init__(self, message: str, **kw: Any):
        super().__init__("role_escalation_refused", message, **kw)


class CrossClientAccessRefused(MCPAuthError):
    def __init__(self, message: str, **kw: Any):
        super().__init__("cross_client_access_refused", message, **kw)


class PathInjectionRefused(MCPAuthError):
    def __init__(self, message: str, **kw: Any):
        super().__init__("path_injection_refused", message, **kw)


class CommandInjectionRefused(MCPAuthError):
    def __init__(self, message: str, **kw: Any):
        super().__init__("command_injection_refused", message, **kw)


class DuplicateStartRefused(MCPAuthError):
    def __init__(self, message: str, **kw: Any):
        super().__init__("duplicate_start_refused", message, **kw)


class ReplayRefused(MCPAuthError):
    def __init__(self, message: str, **kw: Any):
        super().__init__("replay_refused", message, **kw)


class UnsupportedEnvelopeVersion(MCPAuthError):
    def __init__(self, message: str, **kw: Any):
        super().__init__("unsupported_envelope_version", message, **kw)


class EnvelopeSchemaInvalid(MCPAuthError):
    """An envelope whose shape violates the frozen SDK contract.

    Distinct from :class:`UnsupportedEnvelopeVersion`: the version is one this
    surface speaks, but the record does not satisfy the SDK
    ``execution-envelope-v1`` schema (a forbidden property, a missing required
    field, or an SDK-reported validation error). Refused by name, never
    partially accepted.
    """

    def __init__(self, message: str, **kw: Any):
        super().__init__("envelope_schema_invalid", message, **kw)


@dataclass(frozen=True)
class MCPPrincipal:
    """An *authenticated* MCP session principal.

    Constructed only by :meth:`MCPPrincipalAuthority.verify_session` from a
    registry record. Every field derives from host memory, never from a caller
    argument: a caller that passes ``role="reviewer"`` cannot change ``role``.
    """

    principal: str
    role: str
    client_id: str
    scopes: frozenset[str]
    allowed_run_ids: frozenset[str]
    session_id: str
    transport: str
    issued_at: str
    expires_at: Optional[str]

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def may_operate_on(self, run_id: str) -> bool:
        # An empty allow-set is *unbound*, not wildcard: such a principal may
        # act only where the registry does not require a run binding.
        if not self.allowed_run_ids:
            return False
        return run_id in self.allowed_run_ids

    def to_envelope(self) -> dict[str, Any]:
        return _envelope_for(self)


@dataclass(frozen=True)
class _MCPSessionRecord:
    token_digest: str
    principal: str
    role: str
    client_id: str
    scopes: frozenset[str]
    allowed_run_ids: frozenset[str]
    session_id: str
    transport: str
    issued_at: str
    not_before: str
    expires_at: Optional[str]
    allowed_roots: tuple[str, ...] = ()
    allowed_argv0: frozenset[str] = frozenset()
    allowed_routes: tuple[str, ...] = ()
    revoked: bool = False

    def is_live(self, at: str) -> bool:
        if self.revoked:
            return False
        if at < self.not_before:
            return False
        if self.expires_at is not None and at >= self.expires_at:
            return False
        return True


def _token_digest(token: str) -> str:
    """One-way digest of a high-entropy opaque session token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _digest(obj: Any) -> str:
    return hashlib.sha256(_canonical_json(obj).encode("utf-8")).hexdigest()


#: The eleven required top-level properties of the frozen SDK
#: ``execution-envelope-v1`` contract. ``additionalProperties: false`` means a
#: record carrying anything else is refused.
_EXECUTION_ENVELOPE_REQUIRED: tuple[str, ...] = (
    "schema_version",
    "envelope_id",
    "authorized_principal",
    "subject_digest",
    "brief_digest",
    "scope",
    "deadline_utc",
    "limit_policy",
    "allowed_routes",
    "irreversible_actions",
    "authority_evidence_digest",
)

#: The route a session that admits *no* provider route still carries -- the SDK
#: schema requires at least one entry. It names no provider, so forwarding to a
#: real provider is refused by ``allowed_routes``.
_LOCAL_ONLY_ROUTE = "skillweave-local"


def _sdk_validator_module():
    """The installed ``skillweave_sdk.validator`` module, or ``None``.

    The SDK is the contract authority (``integrations/skillweave-sdk``). It is
    used only when it is importable *and* its registry actually carries the
    frozen execution-envelope schema; a host whose ambient SDK predates the
    execution contract set yields ``None`` and the verifier falls back to the
    strict structural check below -- a *documented hold*, never a silent accept.
    """
    try:
        import skillweave_sdk.validator as validator
    except Exception:  # noqa: BLE001 - any import failure is a hold, not a crash
        return None
    try:
        registry = validator.load_registry()
    except Exception:  # noqa: BLE001
        return None
    if EXECUTION_ENVELOPE_SCHEMA_ID not in registry:
        return None
    return validator


def sdk_validator_available() -> bool:
    """Whether the SDK contract authority is usable in this process."""
    return _sdk_validator_module() is not None


def _envelope_for(principal: MCPPrincipal) -> dict[str, Any]:
    """Serialise a verified principal as an SDK ``execution-envelope-v1``.

    The producer is the authority service; the consumer is the MCP
    authorization middleware. The record is the *frozen SDK shape* (validated
    against ``skillweave_sdk.validator``), not a private contract: the MCP
    principal facts ride inside the open ``scope`` object under the namespaced
    :data:`EXECUTION_ENVELOPE_MCP_EXTENSION` key, never as competing top-level
    fields. Digests are host-computed from verified facts, so they are real
    ``^[a-f0-9]{64}$`` values rather than placeholders.
    """
    mcp_facts = {
        "principal": principal.principal,
        "role": principal.role,
        "client_id": principal.client_id,
        "scopes": sorted(principal.scopes),
        "allowed_run_ids": sorted(principal.allowed_run_ids),
        "session_id": principal.session_id,
        "issued_at": principal.issued_at,
        "expires_at": principal.expires_at,
        "transport": principal.transport,
    }
    return {
        "schema_version": EXECUTION_ENVELOPE_VERSION,
        "envelope_id": f"mcp-{principal.session_id}",
        "authorized_principal": principal.principal,
        "subject_digest": _digest({"session_id": principal.session_id}),
        "brief_digest": _digest(
            {"role": principal.role, "scopes": sorted(principal.scopes)}
        ),
        "scope": {EXECUTION_ENVELOPE_MCP_EXTENSION: mcp_facts},
        "deadline_utc": None,
        "limit_policy": {
            "policy": "observe_adapt",
            "max_request_bytes": 1,
            "max_output_tokens": 1,
            "max_inflight_requests": 1,
            "max_parallel_lanes": 1,
        },
        "allowed_routes": [_LOCAL_ONLY_ROUTE],
        "irreversible_actions": [],
        "authority_evidence_digest": _digest(mcp_facts),
    }


def mcp_principal_facts(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Read the namespaced MCP-principal facts back off a verified envelope.

    Returns ``{}`` when the extension is absent: the SDK envelope is valid
    without the middleware's extension, and the middleware never invents facts
    that were not serialised from a verified principal.
    """
    if not isinstance(envelope, Mapping):
        return {}
    scope = envelope.get("scope")
    if not isinstance(scope, Mapping):
        return {}
    facts = scope.get(EXECUTION_ENVELOPE_MCP_EXTENSION)
    return dict(facts) if isinstance(facts, Mapping) else {}


def verify_execution_envelope(envelope: Mapping[str, Any]) -> str:
    """Validate an envelope against the frozen SDK ``execution-envelope-v1``.

    Producer is the authority service; consumer is the MCP authorization
    middleware. The record is validated against the SDK schema
    ``skillweave_sdk.validator`` (schema ``$id`` :data:`EXECUTION_ENVELOPE_SCHEMA_ID`)
    -- it is the one contract, and any other shape is a *refusal by name*.

    An unknown ``schema_version``, a top-level property the SDK schema forbids,
    or any SDK validation error is refused rather than silently accepted. When
    the SDK contract authority is genuinely unavailable in this process (an
    ambient SDK predating the execution set) the verifier applies the strict
    structural half of the same schema -- required fields and no extras -- and
    records that hold; it never accepts without *some* contract check.
    Returns the version on success.
    """
    if not isinstance(envelope, Mapping):
        raise UnsupportedEnvelopeVersion("execution envelope must be a mapping")
    version = envelope.get("schema_version")
    if version != EXECUTION_ENVELOPE_VERSION:
        raise UnsupportedEnvelopeVersion(
            f"unsupported execution-envelope schema_version {version!r}; this "
            f"surface speaks {EXECUTION_ENVELOPE_VERSION!r}",
            extra={"schema_version": version, "supported": [EXECUTION_ENVELOPE_VERSION]},
        )

    validator = _sdk_validator_module()
    if validator is not None:
        try:
            validator.verify_canonical_digest()
        except Exception as exc:  # noqa: BLE001
            raise UnsupportedEnvelopeVersion(
                "execution-envelope schema set has drifted from the frozen "
                "canonical digest",
                extra={"expected": EXECUTION_ENVELOPE_SCHEMA_DIGEST,
                       "reason": str(exc)},
            ) from exc
        errors = validator.validate(dict(envelope), EXECUTION_ENVELOPE_SCHEMA_ID)
        if errors:
            raise EnvelopeSchemaInvalid(
                "execution envelope does not satisfy the SDK "
                "execution-envelope-v1 contract",
                extra={"errors": list(errors)},
            )
        return EXECUTION_ENVELOPE_VERSION

    # Documented hold: the installed SDK cannot validate the execution contract
    # set (ambient SDK predates SW-1510-CONTRACT). Apply the frozen structural
    # half -- required fields present, no properties the schema forbids.
    missing = [f for f in _EXECUTION_ENVELOPE_REQUIRED if f not in envelope]
    extra = sorted(set(envelope) - set(_EXECUTION_ENVELOPE_REQUIRED))
    if missing or extra:
        raise EnvelopeSchemaInvalid(
            "execution envelope does not satisfy the frozen "
            "execution-envelope-v1 shape (SDK validator unavailable)",
            extra={"missing": missing, "unexpected": extra,
                   "sdk_validator_available": False},
        )
    return EXECUTION_ENVELOPE_VERSION


class MCPPrincipalAuthority:
    """The server-owned seam that turns opaque session tokens into principals.

    A reachable local MCP server -- or a caller passing ``role="reviewer"`` --
    is not proof of authorization. Only this object, constructed once in the
    host process, mints and verifies MCP principals, and only an instance that
    answers :meth:`serve_live` is accepted by the server's authorization
    middleware.
    """

    def __init__(self, service_principal: str = "skillweave-mcp-authority"):
        self.service_principal = service_principal
        self._by_digest: dict[str, _MCPSessionRecord] = {}
        #: One-shot approval decisions: a decision token is consumed exactly
        #: once; a second presentation is a replay refusal.
        self._approval_decisions: dict[str, dict[str, Any]] = {}
        #: Consumed start scopes: a duplicate start of the same scope is refused.
        self._started_scopes: set[str] = set()

    def serve_live(self) -> bool:
        """Liveness probe: only a genuine authority answers this."""
        return True

    # -- issuing -----------------------------------------------------------

    def issue_session(
        self,
        *,
        role: str,
        client_id: str,
        transport: str,
        run_ids: Iterable[str] = (),
        allowed_roots: Iterable[str] = (),
        allowed_argv0: Iterable[str] = (),
        allowed_routes: Iterable[str] = (),
        ttl_seconds: Optional[float] = None,
    ) -> str:
        """Mint an opaque session token for *role* bound to *client_id*.

        The role must be one of :data:`MCP_ROLE_SCOPES`; the scope set is taken
        from that table, never from the caller. Returns the secret token; only
        its digest is retained.
        """
        if role not in MCP_ROLE_SCOPES:
            raise RoleEscalationRefused(
                f"unknown MCP role {role!r}; issuable roles are "
                f"{sorted(MCP_ROLE_SCOPES)}",
                extra={"requested_role": role},
            )
        if not isinstance(client_id, str) or not client_id:
            raise UnauthenticatedPrincipal("a session requires a client_id")
        if transport not in ("stdio", "http"):
            raise UnauthenticatedPrincipal(
                f"unknown MCP transport {transport!r}"
            )
        scope = MCP_ROLE_SCOPES[role]
        token = secrets.token_urlsafe(32)
        issued = _now_iso()
        expires = None
        if ttl_seconds is not None:
            expires = (
                datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
            ).isoformat()
        self._by_digest[_token_digest(token)] = _MCPSessionRecord(
            token_digest=_token_digest(token),
            principal=f"mcp:{role}:{client_id}",
            role=role,
            client_id=client_id,
            scopes=frozenset(scope["operations"]),
            allowed_run_ids=frozenset(run_ids),
            session_id=secrets.token_hex(8),
            transport=transport,
            issued_at=issued,
            not_before=issued,
            expires_at=expires,
            allowed_roots=tuple(allowed_roots),
            allowed_argv0=frozenset(allowed_argv0),
            allowed_routes=tuple(allowed_routes),
        )
        return token

    def revoke_token(self, token: str) -> None:
        """Test seam: revoke exactly one session token."""
        digest = _token_digest(token)
        record = self._by_digest.get(digest)
        if record is not None:
            self._by_digest[digest] = _MCPSessionRecord(
                **{**_record_fields(record), "revoked": True}
            )

    def expire(self, token: str) -> None:
        """Test seam: force a token past expiry without waiting."""
        digest = _token_digest(token)
        record = self._by_digest.get(digest)
        if record is not None:
            past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            self._by_digest[digest] = _MCPSessionRecord(
                **{**_record_fields(record), "expires_at": past}
            )

    # -- verifying ---------------------------------------------------------

    def verify_session(self, token: str) -> MCPPrincipal:
        """The only path from an opaque token to an authenticated principal.

        A missing, forged, expired or revoked token fails here; the role is
        read from the registry, never from the caller.
        """
        if not isinstance(token, str) or not token:
            raise UnauthenticatedPrincipal("no MCP session credential presented")
        digest = _token_digest(token)
        record = None
        for stored, candidate in self._by_digest.items():
            if hmac.compare_digest(stored, digest):
                record = candidate
        if record is None or not record.is_live(_now_iso()):
            raise UnauthenticatedPrincipal(
                "MCP session credential is unknown, expired or revoked"
            )
        return MCPPrincipal(
            principal=record.principal,
            role=record.role,
            client_id=record.client_id,
            scopes=frozenset(record.scopes),
            allowed_run_ids=frozenset(record.allowed_run_ids),
            session_id=record.session_id,
            transport=record.transport,
            issued_at=record.issued_at,
            expires_at=record.expires_at,
        )

    def _record_for(self, token: str) -> _MCPSessionRecord:
        principal = self.verify_session(token)  # raises when not live
        digest = _token_digest(token)
        for stored, candidate in self._by_digest.items():
            if hmac.compare_digest(stored, digest):
                return candidate
        raise UnauthenticatedPrincipal("session record vanished mid-verify")

    # -- authorization -----------------------------------------------------

    def authorize(
        self,
        token: str,
        operation: str,
        *,
        run_id: str = "",
        db_path: str = "",
        artifacts_path: str = "",
        command: str = "",
        approval_token: str = "",
        start_scope: str = "",
        provider_route: str = "",
        default_paths: Iterable[str] = (),
    ) -> MCPPrincipal:
        """Authorize *operation* for the principal behind *token*.

        Every branch is a refusal, never a partial success: an unauthenticated
        principal, a role that lacks the operation, cross-client run access, an
        arbitrary path or command, a duplicate start and a replayed approval all
        raise an :class:`MCPAuthError` with an auditable ``code``. Returns the
        verified principal only when the operation is permitted.

        ``default_paths`` names the server's own configured store paths, which a
        principal may always use; any *other* path must fall within the
        principal's bound roots or it is refused as injection.
        """
        principal = self.verify_session(token)
        scope = MCP_ROLE_SCOPES[principal.role]
        if operation not in scope["operations"]:
            raise MCPAuthError(
                "operation_not_permitted",
                f"role {principal.role!r} may not {operation!r}",
                role=principal.role,
                action=operation,
                extra={"requested_role": principal.role},
            )
        if scope["requires_run_binding"] and run_id:
            if not principal.may_operate_on(run_id):
                raise CrossClientAccessRefused(
                    f"principal {principal.principal!r} may not access run "
                    f"{run_id!r}; it is bound to a different client",
                    role=principal.role,
                    action=operation,
                    extra={"run_id": run_id, "client_id": principal.client_id},
                )
        self._guard_paths(
            principal, operation, db_path, artifacts_path, default_paths
        )
        self._guard_command(principal, operation, command)
        self._guard_provider_route(principal, operation, provider_route)
        if start_scope:
            if start_scope in self._started_scopes:
                raise DuplicateStartRefused(
                    f"a start for scope {start_scope!r} was already launched",
                    role=principal.role,
                    action=operation,
                    extra={"start_scope": start_scope},
                )
            self._started_scopes.add(start_scope)
        if approval_token:
            self._consume_approval(principal, approval_token, run_id=run_id)
        return principal

    def _guard_paths(
        self,
        principal: MCPPrincipal,
        operation: str,
        db_path: str,
        artifacts_path: str,
        default_paths: Iterable[str],
    ) -> None:
        record = self._record_for_principal(principal)
        roots = record.allowed_roots
        defaults = {
            os.path.realpath(os.path.abspath(p))
            for p in default_paths
            if p and p != ":memory:"
        }
        for label, value in (("db_path", db_path), ("artifacts_path", artifacts_path)):
            if not value or value == ":memory:":
                continue
            resolved = os.path.realpath(os.path.abspath(value))
            # The server's own configured store is not an injected path.
            if resolved in defaults:
                continue
            if not roots:
                raise PathInjectionRefused(
                    f"principal {principal.principal!r} may not set {label} "
                    f"outside the server default; it has no bound storage root",
                    role=principal.role,
                    action=operation,
                    extra={label: value},
                )
            if not any(
                resolved == root or resolved.startswith(root.rstrip(os.sep) + os.sep)
                for root in roots
            ):
                raise PathInjectionRefused(
                    f"refusing arbitrary {label} {value!r}: outside the "
                    f"principal's bound roots",
                    role=principal.role,
                    action=operation,
                    extra={label: value, "allowed_roots": list(roots)},
                )

    def _guard_command(
        self, principal: MCPPrincipal, operation: str, command: str
    ) -> None:
        record = self._record_for_principal(principal)
        if not command:
            return
        import shlex

        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise CommandInjectionRefused(
                f"command is not parseable: {exc}",
                role=principal.role,
                action=operation,
            ) from exc
        if not argv:
            return
        if not record.allowed_argv0:
            raise CommandInjectionRefused(
                f"principal {principal.principal!r} may not supply a command",
                role=principal.role,
                action=operation,
                extra={"argv0": argv[0]},
            )
        if argv[0] not in record.allowed_argv0:
            raise CommandInjectionRefused(
                f"refusing arbitrary command {argv[0]!r}: not in the "
                f"principal's allowed program set",
                role=principal.role,
                action=operation,
                extra={"argv0": argv[0], "allowed": sorted(record.allowed_argv0)},
            )

    def _guard_provider_route(
        self, principal: MCPPrincipal, operation: str, provider_route: str
    ) -> None:
        """Refuse forwarding a client credential to an unapproved provider.

        The client's MCP session token is authority over *this server*, never a
        provider bearer token. A route that only exists to echo/reflect a
        credential is always refused; any other route must be one the
        principal's session was explicitly bound to
        (:meth:`issue_session` ``allowed_routes``). An unbound session may
        forward nowhere -- there is no wildcard.
        """
        if not provider_route:
            return
        if provider_route in _NON_FORWARDABLE_ROUTES:
            raise MCPAuthError(
                "token_passthrough_refused",
                f"refusing to forward a client credential to route "
                f"{provider_route!r}",
                role=principal.role,
                action=operation,
                extra={"route": provider_route},
            )
        record = self._record_for_principal(principal)
        if provider_route not in record.allowed_routes:
            raise MCPAuthError(
                "unapproved_provider_route",
                f"route {provider_route!r} is not in the principal's approved "
                f"provider routes",
                role=principal.role,
                action=operation,
                extra={"route": provider_route,
                       "allowed_routes": list(record.allowed_routes)},
            )

    def _record_for_principal(self, principal: MCPPrincipal) -> _MCPSessionRecord:
        for record in self._by_digest.values():
            if record.session_id == principal.session_id:
                return record
        raise UnauthenticatedPrincipal("principal has no live registry record")

    # -- one-shot approvals ------------------------------------------------

    def mint_approval(
        self, token: str, *, run_id: str, decision: str
    ) -> str:
        """Mint a one-shot approval token, bound to a bound run and decision.

        Only a review-capable principal may mint; the token is consumed exactly
        once by :meth:`authorize`, so a replayed approval is refused.
        """
        principal = self.verify_session(token)
        scope = MCP_ROLE_SCOPES[principal.role]
        if not scope["can_approve"]:
            raise MCPAuthError(
                "operation_not_permitted",
                f"role {principal.role!r} may not approve",
                role=principal.role,
                action="approve_gate",
            )
        if principal.role in ("reviewer", "observer") and not principal.may_operate_on(run_id):
            raise CrossClientAccessRefused(
                f"principal {principal.principal!r} may not approve run {run_id!r}",
                role=principal.role,
                action="approve_gate",
                extra={"run_id": run_id},
            )
        approval = secrets.token_urlsafe(24)
        self._approval_decisions[approval] = {
            "run_id": run_id,
            "decision": decision,
            "principal": principal.principal,
        }
        return approval

    def _consume_approval(
        self, principal: MCPPrincipal, approval_token: str, *, run_id: str
    ) -> None:
        active = self._approval_decisions.pop(approval_token, None)  # one-shot
        if active is None:
            raise ReplayRefused(
                "approval token was already consumed, never issued, or forged",
                role=principal.role,
                action="approve_gate",
                extra={"run_id": run_id},
            )
        if active["run_id"] != run_id:
            raise CrossClientAccessRefused(
                "approval token is bound to a different run",
                role=principal.role,
                action="approve_gate",
                extra={"run_id": run_id, "bound_run_id": active["run_id"]},
            )

    def refuse_role_escalation(
        self, principal: MCPPrincipal, requested_role: str
    ) -> None:
        """Refuse a caller asking for authority its principal does not hold.

        This is the concrete answer to ``a caller cannot request reviewer
        rights by passing role="reviewer"``: the requested role is compared to
        the *authenticated* role and any difference is a refusal, never a grant.
        """
        if not requested_role or requested_role == principal.role:
            return
        raise RoleEscalationRefused(
            f"principal {principal.principal!r} is role {principal.role!r} and "
            f"cannot request role {requested_role!r}",
            role=principal.role,
            action="escalate",
            extra={"requested_role": requested_role, "actual_role": principal.role},
        )


def _record_fields(record: _MCPSessionRecord) -> dict[str, Any]:
    return {
        "token_digest": record.token_digest,
        "principal": record.principal,
        "role": record.role,
        "client_id": record.client_id,
        "scopes": record.scopes,
        "allowed_run_ids": record.allowed_run_ids,
        "session_id": record.session_id,
        "transport": record.transport,
        "issued_at": record.issued_at,
        "not_before": record.not_before,
        "expires_at": record.expires_at,
        "allowed_roots": record.allowed_roots,
        "allowed_argv0": record.allowed_argv0,
        "allowed_routes": record.allowed_routes,
        "revoked": record.revoked,
    }
