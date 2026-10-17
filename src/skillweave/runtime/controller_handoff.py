"""Exactly-once controller handoff across real harnesses (SW-1511A-HANDOFF-001).

Criterion 1 -- *a typed offered/accepted/active/released or rejected state
machine transfers the controller lease to a server-issued destination principal
without carrying provider credentials* -- is implemented here as the owner of
the ``controller-handoff/v2`` and ``harness-controller-attestation/v1`` records
(``schemas/controller-handoff.schema.json``,
``schemas/harness-controller-attestation.schema.json``).

Criterion 2 -- *race, stale lease, original-controller recovery and
cancel-during-quota-wait prove exactly-once continuation and no duplicate living
attempt* -- is *arbitrated against durable state*, never against an in-memory
claim:

* the **race** and the **stale lease** are decided by the destination's own
  acquisition -- activation commits only against a fencing generation that was
  actually *durably* observed at a strictly higher value, so two offers for one
  scope can never both activate, and a superseded controller's activation is
  refused;
* **original-controller recovery** is refused by name: the offering holder
  cannot re-take a handoff it released;
* **cancel-during-quota-wait** is refused by reading the run's own durable
  record: a provider-quota wait is a *live continuation* (the same owned
  attempt), so a controller transfer while it is recorded is a second
  continuation and is refused.

This module is the **producer** of both records.  It deliberately does *not*
learn the provider identity, the model, the route or the harness transport: the
attested record names the harness's *configured route* and the principal a
server-issued handoff must be addressed to, and the lease transfer carries a
*non-secret* ``gateway://host:port`` binding -- never a provider credential.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

from skillweave.redaction import contains_secret, scrub_secrets

__all__ = [
    "CONTROLLER_HANDOFF_SCHEMA_ID",
    "CONTROLLER_HANDOFF_SCHEMA_VERSION",
    "HARNESS_CONTROLLER_ATTESTATION_SCHEMA_ID",
    "HARNESS_CONTROLLER_ATTESTATION_SCHEMA_VERSION",
    "HANDOFF_STATE_OFFERED",
    "HANDOFF_STATE_ACCEPTED",
    "HANDOFF_STATE_ACTIVE",
    "HANDOFF_STATE_RELEASED",
    "HANDOFF_STATE_REJECTED",
    "HANDOFF_LIVE_STATES",
    "HANDOFF_TERMINAL_STATES",
    "HANDOFF_HOLD_ATTESTATION_ABSENT",
    "HANDOFF_HOLD_ATTESTATION_STALE",
    "HANDOFF_HOLD_ATTESTATION_ROUTE",
    "HANDOFF_HOLD_ATTESTATION_PRINCIPAL",
    "HANDOFF_HOLD_TRANSFER_UNSUPPORTED",
    "HANDOFF_HOLD_LIVE_HANDOFF",
    "HANDOFF_HOLD_QUOTA_WAIT",
    "HANDOFF_HOLD_STALE_GENERATION",
    "HANDOFF_HOLD_NOT_OFFERED",
    "HANDOFF_HOLD_ORIGINAL_CONTROLLER",
    "HANDOFF_HOLD_RELEASE_UNSUPPORTED",
    "HANDOFF_HOLD_EVIDENCE_UNVERIFIED",
    "HANDOFF_HOLD_PRINCIPAL_NOT_SERVER_ISSUED",
    "HANDOFF_HOLD_UNKNOWN_ACTION",
    "HANDOFF_ACTION_OFFER",
    "HANDOFF_ACTION_ACCEPT",
    "HANDOFF_ACTION_ACTIVATE",
    "HANDOFF_ACTION_RELEASE",
    "HANDOFF_ACTION_REJECT",
    "HANDOFF_ACTIONS",
    "HANDOFF_METADATA_KEY",
    "WAIT_METADATA_KEY",
    "ControllerHandoffRefused",
    "ControllerHandoffRecord",
    "HandoffAttestation",
    "HandoffReleaseRefused",
    "drive_handoff",
    "live_handoff_scope_hold",
    "load_handoff_schema",
]

# ── the two contract identities ─────────────────────────────────────────────

CONTROLLER_HANDOFF_SCHEMA_ID = "controller-handoff/v2"
CONTROLLER_HANDOFF_SCHEMA_VERSION = "2.0.0"

HARNESS_CONTROLLER_ATTESTATION_SCHEMA_ID = "harness-controller-attestation/v1"
HARNESS_CONTROLLER_ATTESTATION_SCHEMA_VERSION = "1.0.0"

# ── the typed states (criterion 1) ──────────────────────────────────────────

HANDOFF_STATE_OFFERED = "offered"
HANDOFF_STATE_ACCEPTED = "accepted"
HANDOFF_STATE_ACTIVE = "active"
HANDOFF_STATE_RELEASED = "released"
HANDOFF_STATE_REJECTED = "rejected"

#: The states in which the controller lease is (about to be, or is) carried by
#: the destination.  Exactly one handoff per scope may be live.
HANDOFF_LIVE_STATES = frozenset(
    {HANDOFF_STATE_OFFERED, HANDOFF_STATE_ACCEPTED, HANDOFF_STATE_ACTIVE}
)
HANDOFF_TERMINAL_STATES = frozenset(
    {HANDOFF_STATE_RELEASED, HANDOFF_STATE_REJECTED}
)

#: The legal state machine.  ``released``/``rejected`` are terminal: a released
#: or rejected handoff can never reactivate.
_LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    HANDOFF_STATE_OFFERED: frozenset(
        {HANDOFF_STATE_ACCEPTED, HANDOFF_STATE_REJECTED}
    ),
    HANDOFF_STATE_ACCEPTED: frozenset(
        {HANDOFF_STATE_ACTIVE, HANDOFF_STATE_REJECTED}
    ),
    HANDOFF_STATE_ACTIVE: frozenset({HANDOFF_STATE_RELEASED}),
    HANDOFF_STATE_RELEASED: frozenset(),
    HANDOFF_STATE_REJECTED: frozenset(),
}

# ── the named HOLDs ─────────────────────────────────────────────────────────
#
# Every refusal below is a *named* hold with an actionable code, never a silent
# downgrade and never an inferred capability.

#: No attestation was presented for the destination harness.
HANDOFF_HOLD_ATTESTATION_ABSENT = "ATTESTATION_ABSENT"
#: The attestation is older than the freshness window it asserts.
HANDOFF_HOLD_ATTESTATION_STALE = "ATTESTATION_STALE"
#: The attestation's configured route does not match the configured route the
#: server observed (a route named in a record is not the route in use).
HANDOFF_HOLD_ATTESTATION_ROUTE = "ATTESTATION_ROUTE_MISMATCH"
#: The handoff is not addressed to the principal the harness attested.
HANDOFF_HOLD_ATTESTATION_PRINCIPAL = "ATTESTATION_PRINCIPAL_MISMATCH"
#: The harness attested it cannot accept a controller handoff at all.
HANDOFF_HOLD_TRANSFER_UNSUPPORTED = "HANDOFF_TRANSFER_UNSUPPORTED"
#: A second live handoff was offered for a scope that already carries one.
HANDOFF_HOLD_LIVE_HANDOFF = "LIVE_HANDOFF_FOR_SCOPE"
#: A provider-quota wait is recorded for the run: it is a live continuation.
HANDOFF_HOLD_QUOTA_WAIT = "QUOTA_WAIT_ACTIVE"
#: The offered handoff is not in the ``offered`` state (never re-entered).
HANDOFF_HOLD_NOT_OFFERED = "HANDOFF_NOT_OFFERED"
#: Activation presented a generation the destination did not durably acquire at
#: a strictly higher value (a race, or a superseded offer).
HANDOFF_HOLD_STALE_GENERATION = "STALE_FENCING_GENERATION"
#: A released/rejected handoff was presented for activation by its own holder.
HANDOFF_HOLD_ORIGINAL_CONTROLLER = "ORIGINAL_CONTROLLER_RECOVERY_REFUSED"
#: The record is terminal, so it can never be released again.
HANDOFF_HOLD_RELEASE_UNSUPPORTED = "HANDOFF_TERMINAL"
#: The attestation claims a configured route with gateway counters the server
#: could not verify (an absent or zero observed request count, an upstream the
#: evidence itself records as failed, or a route that contradicts the route the
#: server observed).  A route claim the server cannot verify is a named HOLD.
HANDOFF_HOLD_EVIDENCE_UNVERIFIED = "HANDOFF_EVIDENCE_UNVERIFIED"
#: The destination principal claims to be server-issued (an ``mcp:`` principal)
#: but its form is not one the MCP authority can mint.  The claim is refused --
#: no server-issued identity is invented to satisfy it.
HANDOFF_HOLD_PRINCIPAL_NOT_SERVER_ISSUED = "PRINCIPAL_NOT_SERVER_ISSUED"
#: A route claim's observed gateway request count went *backwards*: the offer
#: reports fewer requests for a run/route than the server's last recorded
#: observation for that same run and route.  A gateway counter is monotonic
#: across observations of one run, so a lower one is a named HOLD -- never
#: silently admitted as a newer, smaller observation.
HANDOFF_HOLD_COUNTER_REGRESSION = "HANDOFF_GATEWAY_COUNTER_REGRESSION"
#: The caller asked the handoff boundary to drive an *action* it does not own.
#: The action vocabulary is the state machine's own (below), so an unowned
#: action is refused by name rather than silently falling through to an offer --
#: never a fabricated transition.
HANDOFF_HOLD_UNKNOWN_ACTION = "HANDOFF_UNKNOWN_ACTION"

# ── the action vocabulary (each typed transition's production entry point) ──
#
# The state machine's moves are *driven* by a caller naming one of these
# actions at the MCP boundary (``handoff_adapter``'s ``action`` argument).  The
# module owns the vocabulary -- alongside ``_LEGAL_TRANSITIONS`` -- so a ledger
# of the typed moves can never drift from the moves themselves, and the boundary
# never hand-types the strings.

#: Offer the lease to the destination principal (the admission default).
HANDOFF_ACTION_OFFER = "offer"
#: The destination accepts the offer (``offered`` -> ``accepted``).
HANDOFF_ACTION_ACCEPT = "accept"
#: The destination activates against its own durably acquired lease.
HANDOFF_ACTION_ACTIVATE = "activate"
#: The current holder releases the active handoff (``active`` -> ``released``).
HANDOFF_ACTION_RELEASE = "release"
#: The destination rejects the handoff before activation (terminal).
HANDOFF_ACTION_REJECT = "reject"

#: Every action the state machine can be driven by.  ``offer`` is the
#: admission's default so every pre-existing ``handoff_adapter`` call keeps its
#: exact behaviour; the other four are the moves that previously had no
#: production entry point (finding F3).
HANDOFF_ACTIONS = frozenset(
    {
        HANDOFF_ACTION_OFFER,
        HANDOFF_ACTION_ACCEPT,
        HANDOFF_ACTION_ACTIVATE,
        HANDOFF_ACTION_RELEASE,
        HANDOFF_ACTION_REJECT,
    }
)

#: Where the live handoff for a run is recorded in that run's durable metadata.
HANDOFF_METADATA_KEY = "controller_handoff"

#: Where the *last observed* gateway request count per run/route is recorded in
#: the run's durable metadata.  This is the cross-observation memory criterion 3
#: needs to refuse a counter that goes backwards: the arbiter is the store, not
#: a process's memory, so the observation survives a restart.
HANDOFF_COUNTER_METADATA_KEY = "controller_handoff_gateway_counter"
#: The schema tag of the durable counter observation blob.  A blob under
#: :data:`HANDOFF_COUNTER_METADATA_KEY` that does not carry it is *unreadable*
#: and is refused by name rather than read as "no prior observation".
HANDOFF_COUNTER_SCHEMA = "handoff-gateway-counter/v1"

#: The durable run-metadata key a provider-quota wait is recorded under.  This
#: is *imported* from the native CMS flow qualification -- the module that
#: actually records a measured wait -- so the two can never disagree about which
#: record is a recorded wait (a hand-typed copy would silently drift and the
#: cancel-during-quota-wait guard would gap).
from .cms_flow_qualification import WAIT_METADATA_KEY as WAIT_METADATA_KEY

# ── the secret-shape refusal (never the value) ──────────────────────────────

#: A credential binding that *looks like* a secret is refused.  This is a shape
#: check: it never needs to read a secret, only to recognise one, so a credential
#: value can neither be carried by the lease transfer nor echoed into an error.
#:
#: SW-1511A-HANDOFF-001 finding F-1511A-03 (review-001): the anchored shapes
#: below are retained, but the ``^``-anchor alone missed an ``env``-file line
#: (``PROVIDER_API_KEY=abc123``) -- the identifier characters *before* the
#: keyword suppressed the match.  Refusal therefore also consults the **shared**
#: secret rule set in :mod:`skillweave.redaction` (imported, never restated), so
#: a secret is masked by exactly one rule and this copy cannot drift from it.
_SECRET_SHAPES: Tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*bearer\s+\S+", re.IGNORECASE),
    re.compile(r"^sk-[A-Za-z0-9_-]{8,}$"),
    re.compile(r"^(?:api[_-]?key|token|secret)\s*[:=]\s*\S+", re.IGNORECASE),
)


class ControllerHandoffRefused(Exception):
    """A controller handoff refusal, carrying its *named*, actionable hold.

    ``code`` is the machine-readable hold; ``reason`` is the human statement.
    The refusal never echoes a credential value (a secret-shaped binding is
    refused by shape, and the shape is not repeated back).
    """

    def __init__(self, code: str, reason: str, **context: Any):
        self.code = code
        self.reason = reason
        self.context = context
        super().__init__(f"{code}: {reason}")

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "reason": self.reason, **self.context}


def looks_like_secret(value: Any) -> bool:
    """True when ``value`` has the shape of a credential (bindings must not).

    Two questions, one answer: the anchored ``_SECRET_SHAPES`` (this module's
    own, historical shapes) *or* the shared :mod:`skillweave.redaction` rule
    set.  The shared rule is the single source of truth (finding F-1511A-03),
    so an ``env``-file line the child could print -- ``PROVIDER_API_KEY=abc123``
    -- is refused here too, rather than only in the trace/stream seam.
    """
    if not isinstance(value, str):
        return False
    if any(pattern.match(value) for pattern in _SECRET_SHAPES):
        return True
    return contains_secret(value)


# ── the destination attestation (criterion 3 admission) ─────────────────────


@dataclass(frozen=True)
class HandoffAttestation:
    """A ``harness-controller-attestation/v1`` record (the admission input).

    Fail-closed at construction, exactly as the declared property says: the
    *absence* of a capability, or of the attestation itself, is a HOLD -- never
    an inferred default.  ``notifications``/``session_resume`` are explicit
    booleans; the configured route and the recipient principal are named (or the
    record is refused), never guessed.
    """

    client_id: str
    gateway_host: str
    gateway_port: int
    recipient_principal: str
    notifications: bool
    session_resume: bool
    can_accept_controller_handoff: bool = True
    adapter_kind: str = ""
    route: str = ""
    alias: str = ""
    credential_binding: str = ""
    observed_at: str = ""
    max_age_seconds: int = 0
    mcp_config_anchors: Tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.client_id:
            raise ValueError("a harness attestation must name its client_id")
        if not self.gateway_host or not int(self.gateway_port or 0):
            raise ValueError(
                "a harness attestation must name the configured gateway route "
                "it observed (host and port); an unobserved route is a HOLD, "
                "never an inferred one"
            )
        if not self.recipient_principal:
            raise ValueError(
                "a harness attestation must name the recipient principal a "
                "server-issued handoff is addressed to"
            )
        if self.credential_binding and looks_like_secret(self.credential_binding):
            raise ValueError(
                "a harness attestation carries a non-secret credential "
                "reference (for example gateway://host:port); a secret-shaped "
                "binding is refused and never echoed"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": HARNESS_CONTROLLER_ATTESTATION_SCHEMA_ID,
            "schema_version": HARNESS_CONTROLLER_ATTESTATION_SCHEMA_VERSION,
            "client_id": self.client_id,
            "adapter_kind": self.adapter_kind or None,
            "configured_route": {
                "gateway_host": self.gateway_host,
                "gateway_port": int(self.gateway_port),
                "route": self.route or None,
                "alias": self.alias or None,
                "credential_binding": self.credential_binding or None,
            },
            "recipient_principal": self.recipient_principal,
            "capabilities": {
                "session_resume": bool(self.session_resume),
                "notifications": bool(self.notifications),
                "mcp_config_anchors": list(self.mcp_config_anchors),
                "can_accept_controller_handoff": bool(
                    self.can_accept_controller_handoff
                ),
            },
            "observed_at": self.observed_at or None,
            "max_age_seconds": int(self.max_age_seconds or 0),
            "evidence": dict(self.evidence),
        }


# ── the typed handoff record (criterion 1) ──────────────────────────────────


@dataclass(frozen=True)
class ControllerHandoffRecord:
    """A ``controller-handoff/v2`` record: the lease transfer, typed and held.

    ``destination_principal`` is a *server-issued* value (the caller presents
    what it was issued, never what it claims); ``fencing_generation`` is the
    generation the **offering controller currently holds**, read from the
    durable lease -- so ``resulting_generation`` can only ever be strictly
    higher, and improving it on a lower one is the race/stale refusal.
    """

    handoff_id: str
    from_holder: str
    destination_principal: str
    scope: str
    fencing_generation: int
    state: str
    destination_attestation: Optional[HandoffAttestation] = None
    resulting_generation: Optional[int] = None
    credential_binding: str = ""
    rejected_reason: str = ""
    offered_at: str = ""
    accepted_at: str = ""
    activated_at: str = ""
    released_at: str = ""
    rejected_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.handoff_id:
            raise ValueError("a controller handoff must name its handoff_id")
        if not self.from_holder:
            raise ValueError("a controller handoff must name the holder releasing it")
        if not self.destination_principal:
            raise ValueError(
                "a controller handoff must name the server-issued destination "
                "principal; a caller-supplied authority claim is refused"
            )
        if not self.scope:
            raise ValueError("a controller handoff must name its exactly-once scope")
        if int(self.fencing_generation or 0) < 1:
            raise ValueError(
                "a controller handoff must carry the fencing generation the "
                "offering controller holds (>= 1)"
            )
        if self.state not in _LEGAL_TRANSITIONS:
            raise ValueError(
                f"controller handoff state {self.state!r} is not one of "
                f"{sorted(_LEGAL_TRANSITIONS)}"
            )
        if self.state == HANDOFF_STATE_REJECTED and self.resulting_generation is not None:
            raise ValueError(
                "a rejected handoff never records a resulting generation: no "
                "lease was transferred"
            )
        if self.credential_binding and looks_like_secret(self.credential_binding):
            raise ValueError(
                "the lease transfer carries a non-secret credential reference "
                "(for example gateway://host:port); a provider credential is "
                "refused and never echoed"
            )

    # -- derived facts (never caller fields)

    @property
    def live(self) -> bool:
        """True while the handoff still holds (or is about to hold) the lease."""
        return self.state in HANDOFF_LIVE_STATES

    @property
    def activated_generation(self) -> Optional[int]:
        """The generation the destination durably acquired, when activated."""
        return self.resulting_generation if self.state == HANDOFF_STATE_ACTIVE else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CONTROLLER_HANDOFF_SCHEMA_ID,
            "schema_version": CONTROLLER_HANDOFF_SCHEMA_VERSION,
            "handoff_id": self.handoff_id,
            "from_holder": self.from_holder,
            "destination_principal": self.destination_principal,
            "destination_attestation": (
                self.destination_attestation.to_dict()
                if self.destination_attestation is not None
                else None
            ),
            "scope": self.scope,
            "fencing_generation": int(self.fencing_generation),
            "resulting_generation": (
                int(self.resulting_generation)
                if self.resulting_generation is not None
                else None
            ),
            "credential_binding": self.credential_binding,
            "state": self.state,
            "rejected_reason": self.rejected_reason or None,
            "offered_at": self.offered_at or None,
            "accepted_at": self.accepted_at or None,
            "activated_at": self.activated_at or None,
            "released_at": self.released_at or None,
            "rejected_at": self.rejected_at or None,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ControllerHandoffRecord":
        """Rebuild a record from its durable form (round-trip, no invention).

        ``controller-handoff/v1`` manual packages remain readable: a v1 record
        carried no destination principal, no fencing generation and no
        credential binding, so it is reconstructed as ``offered``-only state
        with an explicit named-limitation metadata marker -- it is *readable*,
        never activatable (activation requires a v2 acknowledgement).
        """
        if not isinstance(data, dict):
            raise ValueError("a controller handoff record must be an object")
        schema = data.get("schema", "")
        if schema not in (CONTROLLER_HANDOFF_SCHEMA_ID, "controller-handoff/v1"):
            raise ValueError(
                f"refusing controller handoff schema {schema!r}; expected "
                f"{CONTROLLER_HANDOFF_SCHEMA_ID!r}"
            )
        if schema == "controller-handoff/v1":
            metadata = dict(data.get("metadata") or {})
            metadata["v1_manual_package"] = True
            metadata.setdefault(
                "limitation",
                "a v1 manual package is offered-only; activation requires a v2 "
                "acknowledgement carrying the destination principal and the "
                "fencing generation",
            )
            state = HANDOFF_STATE_OFFERED
            if state not in _LEGAL_TRANSITIONS:
                raise ValueError(state)  # pragma: no cover - unreachable guard
            return cls(
                handoff_id=str(data.get("handoff_id") or ""),
                from_holder=str(data.get("from_holder") or data.get("from_role") or ""),
                destination_principal="v1-manual-package",
                scope=str(data.get("scope") or ""),
                fencing_generation=1,
                state=state,
                metadata=metadata,
            )
        attestation_raw = data.get("destination_attestation")
        attestation = None
        if isinstance(attestation_raw, dict) and attestation_raw.get("schema") == (
            HARNESS_CONTROLLER_ATTESTATION_SCHEMA_ID
        ):
            route = attestation_raw.get("configured_route") or {}
            caps = attestation_raw.get("capabilities") or {}
            attestation = HandoffAttestation(
                client_id=str(attestation_raw.get("client_id") or ""),
                gateway_host=str(route.get("gateway_host") or ""),
                gateway_port=int(route.get("gateway_port") or 0),
                recipient_principal=str(
                    attestation_raw.get("recipient_principal") or ""
                ),
                notifications=bool(caps.get("notifications")),
                session_resume=bool(caps.get("session_resume")),
                can_accept_controller_handoff=bool(
                    caps.get("can_accept_controller_handoff", True)
                ),
                adapter_kind=str(attestation_raw.get("adapter_kind") or ""),
                route=str(route.get("route") or ""),
                alias=str(route.get("alias") or ""),
                credential_binding=str(route.get("credential_binding") or ""),
                observed_at=str(attestation_raw.get("observed_at") or ""),
                max_age_seconds=int(attestation_raw.get("max_age_seconds") or 0),
                mcp_config_anchors=tuple(caps.get("mcp_config_anchors") or ()),
                evidence=dict(attestation_raw.get("evidence") or {}),
            )
        return cls(
            handoff_id=str(data.get("handoff_id") or ""),
            from_holder=str(data.get("from_holder") or ""),
            destination_principal=str(data.get("destination_principal") or ""),
            scope=str(data.get("scope") or ""),
            fencing_generation=int(data.get("fencing_generation") or 0),
            state=str(data.get("state") or ""),
            destination_attestation=attestation,
            resulting_generation=(
                int(data["resulting_generation"])
                if data.get("resulting_generation") is not None
                else None
            ),
            credential_binding=str(data.get("credential_binding") or ""),
            rejected_reason=str(data.get("rejected_reason") or ""),
            offered_at=str(data.get("offered_at") or ""),
            accepted_at=str(data.get("accepted_at") or ""),
            activated_at=str(data.get("activated_at") or ""),
            released_at=str(data.get("released_at") or ""),
            rejected_at=str(data.get("rejected_at") or ""),
            metadata=dict(data.get("metadata") or {}),
        )


# ── durable helpers (the arbiter is the store, never memory) ────────────────


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def record_handoff(
    store: Any,
    record: ControllerHandoffRecord,
    *,
    run_id: str,
    timestamp: str = "",
) -> ControllerHandoffRecord:
    """Persist *record* as the run's live handoff (durable, idempotent).

    The record is written to the run's own durable metadata under
    :data:`HANDOFF_METADATA_KEY`.  The write is idempotent at the same
    ``handoff_id`` (a re-observation appends nothing and changes nothing), and a
    *different* live handoff for a scope that already carries one is refused by
    name -- two live handoffs for one scope would be two live continuations.
    """
    existing = read_handoff(store, run_id)
    if (
        existing is not None
        and existing.handoff_id != record.handoff_id
        and existing.live
        and record.live
    ):
        raise ControllerHandoffRefused(
            HANDOFF_HOLD_LIVE_HANDOFF,
            f"the scope {record.scope!r} already carries the live handoff "
            f"{existing.handoff_id!r} (state {existing.state!r}); a second live "
            f"handoff would be a second continuator for one scope",
            scope=record.scope,
            held_handoff_id=existing.handoff_id,
        )
    if existing is not None and existing.handoff_id == record.handoff_id:
        # Idempotent at the same identity: a repeated observation is not a
        # state change and must not append or reset timestamps.
        return existing
    payload = record.to_dict()
    payload["recorded_at"] = timestamp or _now()
    store.update_metadata(run_id, **{HANDOFF_METADATA_KEY: payload})
    reread = read_handoff(store, run_id)
    if reread is None:  # pragma: no cover - the write above would have raised
        raise ControllerHandoffRefused(
            HANDOFF_HOLD_LIVE_HANDOFF,
            f"the handoff {record.handoff_id!r} did not survive in the durable "
            f"run metadata for {run_id!r}; an unrecorded handoff is not a "
            f"handoff",
        )
    return reread


def read_handoff(store: Any, run_id: str) -> Optional[ControllerHandoffRecord]:
    """Read the run's *durable* handoff, or ``None`` when none is recorded."""
    record = store.get_run(run_id)
    if record is None:
        return None
    raw = (getattr(record, "metadata", None) or {}).get(HANDOFF_METADATA_KEY)
    if not isinstance(raw, dict):
        return None
    return ControllerHandoffRecord.from_dict(raw)


class HandoffReleaseRefused(Exception):
    """The store's own refusal of the holder's release -- surfaced unchanged.

    ``release_controller_lease`` raises its own typed refusal (a stale fencing
    token, or a lease not held).  The store is the arbiter of the release, so
    its refusal is never converted into a success; the module re-raises it under
    this name so the production driver can turn it into a *named* HOLD without
    importing the store's exception types (the module deliberately never learns
    the store's native types).
    """

    def __init__(self, reason: str, **context: Any):
        self.reason = reason
        self.context = context
        super().__init__(reason)


def drive_handoff(
    store: Any,
    *,
    action: str,
    run_id: str,
    handoff_id: str = "",
    reason: str = "",
    actor: str = "",
    acquire: Any = None,
    release: Any = None,
    timestamp: str = "",
) -> ControllerHandoffRecord:
    """Drive one typed handoff transition on behalf of the MCP boundary (F3).

    This is the **production driver** the state machine was missing: the MCP
    boundary authorizes the caller, resolves the *identity* rules it alone can
    enforce (the store's live lease holder) and then drives the module-owned
    transition here -- the boundary never re-implements a move.

    ``action`` is one of :data:`HANDOFF_ACTIONS` (refused by name otherwise).
    ``offer`` is *not* handled here: an offer needs the attested admission the
    boundary performs, so it is driven through
    :func:`offer_handoff`/:func:`record_handoff` at the boundary and this driver
    refuses it explicitly (never a silent no-op).

    The record is read from the run's **durable** metadata (never a caller
    blob), so a restart sees the same state.  The three identity rules are
    enforced against what only the store knows:

    * **only the destination may accept/activate/reject** -- a different actor
      is :data:`HANDOFF_HOLD_ATTESTATION_PRINCIPAL`;
    * **only the current holder may release** -- a different actor is
      :data:`HANDOFF_HOLD_ORIGINAL_CONTROLLER`;
    * an optional ``handoff_id`` must name the durable record, so a caller
      cannot transition a handoff it did not name.

    Each successful move is persisted durably at the *same* ``handoff_id``
    (a state advance is not a second handoff, so ``record_handoff``'s
    idempotency must not swallow it -- the advanced record is written
    directly).  Every refusal is a named
    :class:`ControllerHandoffRefused`; the store's own acquisition/release
    refusals are surfaced by name, never converted into a success.
    """
    if action not in HANDOFF_ACTIONS:
        raise ControllerHandoffRefused(
            HANDOFF_HOLD_UNKNOWN_ACTION,
            f"the handoff boundary does not own the action {action!r}; the "
            f"actions it can drive are {sorted(HANDOFF_ACTIONS)}",
            action=str(action),
        )
    if action == HANDOFF_ACTION_OFFER:
        raise ControllerHandoffRefused(
            HANDOFF_HOLD_UNKNOWN_ACTION,
            "the 'offer' action is driven by the attested offer admission, not "
            "by the transition driver",
            action=action,
        )
    if not run_id:
        raise ControllerHandoffRefused(
            HANDOFF_HOLD_NOT_OFFERED,
            "a handoff transition must name the run whose durable handoff it "
            "advances",
            action=action,
        )
    record = read_handoff(store, run_id)
    if record is None:
        raise ControllerHandoffRefused(
            HANDOFF_HOLD_NOT_OFFERED,
            f"run {run_id!r} has no recorded controller handoff to advance",
            run_id=run_id,
            action=action,
        )
    if handoff_id and handoff_id != record.handoff_id:
        raise ControllerHandoffRefused(
            HANDOFF_HOLD_NOT_OFFERED,
            f"the run {run_id!r} carries handoff {record.handoff_id!r}, not "
            f"{handoff_id!r}; a transition cannot name a handoff the run does "
            f"not carry",
            run_id=run_id,
            action=action,
            requested_handoff_id=handoff_id,
            recorded_handoff_id=record.handoff_id,
        )

    if action in (HANDOFF_ACTION_ACCEPT, HANDOFF_ACTION_ACTIVATE, HANDOFF_ACTION_REJECT):
        # Only the destination principal that the handoff is addressed to may
        # move it forward (or reject it): that principal is the one the server
        # issued the handoff to.
        if actor != record.destination_principal:
            raise ControllerHandoffRefused(
                HANDOFF_HOLD_ATTESTATION_PRINCIPAL,
                f"the handoff {record.handoff_id!r} is addressed to "
                f"{record.destination_principal!r}; {actor or 'an unnamed actor'!r} "
                f"cannot perform {action!r} on it",
                handoff_id=record.handoff_id,
                destination_principal=record.destination_principal,
                actor=actor,
                action=action,
            )

    if action == HANDOFF_ACTION_ACCEPT:
        updated = accept_handoff(record, timestamp=timestamp)
    elif action == HANDOFF_ACTION_REJECT:
        updated = reject_handoff(record, reason=reason, timestamp=timestamp)
    elif action == HANDOFF_ACTION_ACTIVATE:
        if acquire is None:
            raise ControllerHandoffRefused(
                HANDOFF_HOLD_STALE_GENERATION,
                "an activation must carry the destination's real lease "
                "acquisition; an activation without one transfers no lease",
                handoff_id=record.handoff_id,
            )
        try:
            updated = activate_handoff(record, acquire=acquire, timestamp=timestamp)
        except ControllerHandoffRefused:
            raise
        except Exception as exc:
            # The store's own acquisition refusal (a live controller still holds
            # the lease) is surfaced *by name*, never converted into a success.
            raise ControllerHandoffRefused(
                HANDOFF_HOLD_STALE_GENERATION,
                f"the destination could not durably acquire the controller "
                f"lease ({type(exc).__name__}: {exc}); the store refused the "
                f"acquisition, so the handoff is not activated",
                handoff_id=record.handoff_id,
            ) from exc
    else:  # HANDOFF_ACTION_RELEASE
        holder = ""
        try:
            lease = store.get_controller_lease()
            holder = str(getattr(lease, "holder", "") or "")
        except Exception:  # pragma: no cover - a store without the lease table
            holder = ""
        if actor != holder:
            raise ControllerHandoffRefused(
                HANDOFF_HOLD_ORIGINAL_CONTROLLER,
                f"only the current controller holder ({holder or 'none'!r}) may "
                f"release the handoff {record.handoff_id!r}; "
                f"{actor or 'an unnamed actor'!r} is not the holder",
                handoff_id=record.handoff_id,
                holder=holder,
                actor=actor,
            )
        try:
            if release is not None:
                release()
        except Exception as exc:
            raise HandoffReleaseRefused(
                f"the holder {holder!r} could not release the controller lease "
                f"({type(exc).__name__}: {exc})",
                handoff_id=record.handoff_id,
                holder=holder,
            ) from exc
        updated = release_handoff(record, timestamp=timestamp)

    # Persist the advanced record at the same identity.  ``record_handoff`` is
    # idempotent at the same ``handoff_id`` (it would return the *existing*
    # record unchanged), so the advanced state is written directly -- exactly
    # the durable seam the state machine already uses -- and re-read.
    store.update_metadata(run_id, **{HANDOFF_METADATA_KEY: updated.to_dict()})
    reread = read_handoff(store, run_id)
    if reread is None:  # pragma: no cover - the write above would have raised
        raise ControllerHandoffRefused(
            HANDOFF_HOLD_NOT_OFFERED,
            f"the handoff {updated.handoff_id!r} did not survive in the durable "
            f"run metadata for {run_id!r}; an unrecorded transition is not a "
            f"transition",
            run_id=run_id,
            action=action,
        )
    return reread


class GatewayCounterObservationError(Exception):
    """The durable counter observation could not be *read* (fail closed).

    A stored value that is present but unreadable is neither "no prior
    observation" nor "some prior observation": it is an observation the server
    cannot trust, so the boundary refuses by name rather than falling open onto
    the stored bytes.
    """


def read_gateway_counter_observation(
    store: Any, run_id: str, route: str
) -> Optional[int]:
    """The last *durably recorded* observed count for ``(run_id, route)``.

    ``None`` means the server holds **no** prior observation for this run and
    route, so a first observation is admitted exactly as before.  A stored value
    that is unreadable -- not a tagged mapping, or whose count is not a positive
    integer -- raises :class:`GatewayCounterObservationError`: unreadable is
    never silently "no prior observation" (that would fail *open*), and it is
    never silently "some prior observation" (that would refuse an honest first
    offer).  The reader returns only the integer stored under this run's own
    route key, so no other run's or route's value can leak into this series.
    """
    if store is None or not run_id or not route:
        return None
    record = store.get_run(run_id)
    if record is None:
        return None
    raw = (getattr(record, "metadata", None) or {}).get(HANDOFF_COUNTER_METADATA_KEY)
    if raw is None:
        return None
    if not isinstance(raw, dict) or raw.get("schema") != HANDOFF_COUNTER_SCHEMA:
        raise GatewayCounterObservationError(
            f"the durable gateway-counter observation for {run_id!r} is not a "
            f"{HANDOFF_COUNTER_SCHEMA!r} record; an unreadable prior observation "
            f"is refused rather than read as absent"
        )
    routes = raw.get("routes")
    if not isinstance(routes, dict):
        raise GatewayCounterObservationError(
            f"the durable gateway-counter observation for {run_id!r} carries no "
            f"routes mapping; it is unreadable and the claim is refused"
        )
    if route not in routes:
        # A *different* route has no stored value: its counters are an
        # independent series and never interfere with this one.
        return None
    value = routes[route]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GatewayCounterObservationError(
            f"the durable gateway-counter observation for route {route!r} on "
            f"{run_id!r} is not a positive integer; it is unreadable and the "
            f"claim is refused"
        )
    return value


def record_gateway_counter_observation(
    store: Any, run_id: str, route: str, count: int
) -> None:
    """Durably record *count* as the last observation for ``(run_id, route)``.

    Only the integer is stored (plus its route key and the schema tag): the
    observed count is not a secret, and nothing else from the caller's evidence
    blob is written here, so the durable observation can never carry a
    credential.  The write is keyed by run and route, so one run/route's series
    never overwrites another's.
    """
    if store is None or not run_id or not route:
        return
    record = store.get_run(run_id)
    existing = (getattr(record, "metadata", None) or {}).get(
        HANDOFF_COUNTER_METADATA_KEY
    )
    routes: dict[str, int] = {}
    if isinstance(existing, dict) and existing.get("schema") == HANDOFF_COUNTER_SCHEMA:
        carried = existing.get("routes")
        if isinstance(carried, dict):
            for key, value in carried.items():
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    routes[str(key)] = value
    routes[route] = int(count)
    store.update_metadata(
        run_id,
        **{
            HANDOFF_COUNTER_METADATA_KEY: {
                "schema": HANDOFF_COUNTER_SCHEMA,
                "routes": routes,
            }
        },
    )


def live_handoff_scope_hold(
    store: Any, scope: str, *, exclude_run_id: str = ""
) -> Optional[str]:
    """The run carrying a **live** handoff for *scope*, or ``None``.

    A v1 manual package is readable but never activatable, so it is not a live
    handoff.  The scan is over durable run records: the arbiter of "one live
    handoff per scope" is the store, not a process's memory.
    """
    # ``limit=-1`` is SQLite's "no limit": the scan must see every run, since
    # the one existing live handoff may be the oldest row.  ``limit=0`` would
    # scan nothing and silently report "no live handoff" -- a fail-open.
    for run in store.list_runs(limit=-1):
        run_id = getattr(run, "run_id", "")
        if run_id == exclude_run_id:
            continue
        raw = (getattr(run, "metadata", None) or {}).get(HANDOFF_METADATA_KEY)
        if not isinstance(raw, dict):
            continue
        if raw.get("schema") == "controller-handoff/v1":
            # v1 manual packages are offered-only and never activatable, so they
            # are not a live handoff.
            continue
        if raw.get("state") in HANDOFF_LIVE_STATES and raw.get("scope") == scope:
            return run_id
    return None


def quota_wait_hold(store: Any, run_id: str) -> Optional[str]:
    """The recorded provider-quota wait for *run_id*, or ``None``.

    ``cancel-during-quota-wait`` (criterion 2): a recorded wait is a *live
    continuation* of the same owned attempt -- the run is waiting for capacity,
    not abandoned.  A controller transfer presented while that wait is recorded
    would start a second continuator for one scope, so it is refused by name.
    The wait is read from the run's durable metadata under the same key the
    native flow qualification records it with.
    """
    record = store.get_run(run_id)
    if record is None:
        return None
    wait = (getattr(record, "metadata", None) or {}).get(WAIT_METADATA_KEY)
    if not isinstance(wait, dict):
        return None
    decision = wait.get("decision", "")
    if decision:
        return str(decision)
    return "recorded"


# ── the state machine (criterion 1) ─────────────────────────────────────────


def _transition(
    record: ControllerHandoffRecord, to_state: str, **changes: Any
) -> ControllerHandoffRecord:
    allowed = _LEGAL_TRANSITIONS[record.state]
    if to_state not in allowed:
        raise ControllerHandoffRefused(
            HANDOFF_HOLD_NOT_OFFERED,
            f"a controller handoff in state {record.state!r} cannot move to "
            f"{to_state!r}; the legal moves are {sorted(allowed)}",
            state=record.state,
            attempted=to_state,
        )
    payload = {**_as_fields(record), **changes, "state": to_state}
    return ControllerHandoffRecord(**payload)


def _as_fields(record: ControllerHandoffRecord) -> dict[str, Any]:
    """The record's own data as constructor kwargs (a faithful copy)."""
    return {
        "handoff_id": record.handoff_id,
        "from_holder": record.from_holder,
        "destination_principal": record.destination_principal,
        "scope": record.scope,
        "fencing_generation": record.fencing_generation,
        "state": record.state,
        "destination_attestation": record.destination_attestation,
        "resulting_generation": record.resulting_generation,
        "credential_binding": record.credential_binding,
        "rejected_reason": record.rejected_reason,
        "offered_at": record.offered_at,
        "accepted_at": record.accepted_at,
        "activated_at": record.activated_at,
        "released_at": record.released_at,
        "rejected_at": record.rejected_at,
        "metadata": dict(record.metadata),
    }


def offer_handoff(
    *,
    from_holder: str,
    destination_principal: str,
    scope: str,
    destination_attestation: Optional[HandoffAttestation],
    observed_route: Optional[tuple[str, int]],
    credential_binding: str,
    handoff_id: str = "",
    timestamp: str = "",
) -> ControllerHandoffRecord:
    """Offer the lease to a *server-issued* destination principal (offered).

    The four admission axes of the destination are attested, never assumed:

    * the **attestation exists** -- absent is :data:`HANDOFF_HOLD_ATTESTATION_ABSENT`;
    * it is **fresh** (within the window it asserts);
    * its **configured route** equals the route the server observed, not the
      route the record merely names;
    * the handoff's destination principal is the **recipient principal** the
      harness attested it would accept a server-issued handoff as.

    A harness that attests it cannot accept a controller handoff is an
    unsupported named HOLD -- never a downgrade to a lesser transport.
    ``fencing_generation`` is the generation the *offering* controller holds;
    the offer never chooses it, so activation can only ever be strictly higher.
    """
    if destination_attestation is None:
        raise ControllerHandoffRefused(
            HANDOFF_HOLD_ATTESTATION_ABSENT,
            "no harness controller attestation was presented for the "
            "destination; an absent attestation is a named HOLD, never an "
            "inferred capability",
            scope=scope,
            destination_principal=destination_principal,
        )
    attestation = destination_attestation
    if not attestation.can_accept_controller_handoff:
        raise ControllerHandoffRefused(
            HANDOFF_HOLD_TRANSFER_UNSUPPORTED,
            f"the harness {attestation.client_id!r} attests it cannot accept a "
            f"controller handoff; an unsupported client remains a named HOLD, "
            f"never a downgrade",
            client_id=attestation.client_id,
            scope=scope,
        )
    if _is_stale(attestation, timestamp):
        raise ControllerHandoffRefused(
            HANDOFF_HOLD_ATTESTATION_STALE,
            f"the harness {attestation.client_id!r} attestation observed at "
            f"{attestation.observed_at!r} is older than its own "
            f"{attestation.max_age_seconds}s window; a stale attestation is a "
            f"named HOLD, never silently trusted",
            client_id=attestation.client_id,
            scope=scope,
        )
    if observed_route is not None:
        observed_host, observed_port = observed_route
        if (
            attestation.gateway_host != observed_host
            or int(attestation.gateway_port) != int(observed_port)
        ):
            raise ControllerHandoffRefused(
                HANDOFF_HOLD_ATTESTATION_ROUTE,
                f"the harness {attestation.client_id!r} attests the configured "
                f"route {attestation.gateway_host}:{attestation.gateway_port}, "
                f"not the observed {observed_host}:{observed_port}; a route that "
                f"is not the configured one is a named HOLD",
                client_id=attestation.client_id,
                scope=scope,
            )
    if destination_principal != attestation.recipient_principal:
        raise ControllerHandoffRefused(
            HANDOFF_HOLD_ATTESTATION_PRINCIPAL,
            f"the handoff is addressed to {destination_principal!r} but the "
            f"harness {attestation.client_id!r} accepts a server-issued handoff "
            f"only as {attestation.recipient_principal!r}",
            client_id=attestation.client_id,
            scope=scope,
        )
    if credential_binding and looks_like_secret(credential_binding):
        raise ControllerHandoffRefused(
            HANDOFF_HOLD_TRANSFER_UNSUPPORTED,
            "the lease transfer carries a non-secret credential reference; a "
            "provider credential is refused (and never echoed)",
            scope=scope,
        )
    ident = handoff_id or f"{scope}:{from_holder}:{destination_principal}"
    return ControllerHandoffRecord(
        handoff_id=ident,
        from_holder=from_holder,
        destination_principal=destination_principal,
        scope=scope,
        fencing_generation=1,
        state=HANDOFF_STATE_OFFERED,
        destination_attestation=attestation,
        credential_binding=credential_binding,
        offered_at=timestamp or _now(),
        metadata={"producer": "controller_handoff.offer"},
    )


def _is_stale(attestation: HandoffAttestation, now: str) -> bool:
    """Whether the attestation is older than the window it asserts."""
    window = int(attestation.max_age_seconds or 0)
    if window <= 0 or not attestation.observed_at:
        return False
    from datetime import datetime

    try:
        observed = datetime.fromisoformat(attestation.observed_at)
        current = datetime.fromisoformat(now) if now else datetime.now(observed.tzinfo)
    except ValueError:
        # An unparseable timestamp cannot be shown fresh, so it is stale.
        return True
    if observed.tzinfo is None and current.tzinfo is not None:
        current = current.replace(tzinfo=None)
    elif observed.tzinfo is not None and current.tzinfo is None:
        observed = observed.replace(tzinfo=None)
    return (current - observed).total_seconds() > window


def bind_offered_generation(
    record: ControllerHandoffRecord, fencing_generation: int
) -> ControllerHandoffRecord:
    """Bind the *offering* controller's generation read from the durable lease.

    The generation is not chosen at offer time: it is re-read from the durable
    controller lease (``current_fencing_token``) at the moment of the offer, so
    the value in the record is the generation the offerer actually holds.
    """
    if int(fencing_generation) < 1:
        raise ControllerHandoffRefused(
            HANDOFF_HOLD_STALE_GENERATION,
            "the offering controller holds no fencing generation; the lease must "
            "be acquired before it can be handed off",
        )
    return ControllerHandoffRecord(
        **{**_as_fields(record), "fencing_generation": int(fencing_generation)}
    )


def accept_handoff(record: ControllerHandoffRecord, *, timestamp: str = "") -> ControllerHandoffRecord:
    """The destination accepts the offer (offered -> accepted)."""
    return _transition(
        record, HANDOFF_STATE_ACCEPTED, accepted_at=timestamp or _now()
    )


def activate_handoff(
    record: ControllerHandoffRecord,
    *,
    acquire: Any,
    observed_generation: Optional[int] = None,
    timestamp: str = "",
) -> ControllerHandoffRecord:
    """Activate the handoff against a *strictly higher, durably observed* generation.

    ``acquire`` is the destination's **real lease acquisition** (a call taking no
    arguments and returning the acquired lease, or raising the store's own
    :class:`~skillweave.runtime.native_process.LeaseRefusedError`).  The transfer
    commits **only** when the generation the destination actually acquired is
    strictly greater than the offered ``fencing_generation``: this is where the
    *race* (two offered handoffs for one scope) and the *stale lease* (a
    superseded controller presenting its activation) both fail closed.  The
    acquired generation is read from the durable lease, never from a caller
    field -- the destination cannot *assert* that it took the lease, it must
    have taken it.
    """
    if record.state in HANDOFF_TERMINAL_STATES:
        # Original-controller recovery: a released/rejected handoff is terminal,
        # so the holder that gave the lease away cannot take it back through a
        # handoff it already terminated.
        raise ControllerHandoffRefused(
            HANDOFF_HOLD_ORIGINAL_CONTROLLER,
            f"the handoff {record.handoff_id!r} is {record.state!r}; a released "
            f"or rejected handoff can never reactivate, so the original "
            f"controller cannot recover the lease through it",
            handoff_id=record.handoff_id,
            state=record.state,
        )
    if record.state not in (HANDOFF_STATE_OFFERED, HANDOFF_STATE_ACCEPTED):
        raise ControllerHandoffRefused(
            HANDOFF_HOLD_NOT_OFFERED,
            f"the handoff {record.handoff_id!r} is {record.state!r}, not offered",
            handoff_id=record.handoff_id,
            state=record.state,
        )

    # The destination must actually acquire its own lease.  A refusal here is
    # the store's own (a live controller holds it): surfaced unchanged, never
    # converted into a success.
    lease = acquire()
    acquired = int(getattr(lease, "fencing_token", 0) or 0)
    if acquired <= 0:
        raise ControllerHandoffRefused(
            HANDOFF_HOLD_STALE_GENERATION,
            "the destination acquired no fencing generation; an activation "
            "without a durable acquisition transfers no lease",
            handoff_id=record.handoff_id,
        )
    if observed_generation is not None and int(observed_generation) != acquired:
        raise ControllerHandoffRefused(
            HANDOFF_HOLD_STALE_GENERATION,
            f"the destination observed generation {observed_generation} but "
            f"durably acquired {acquired}; only the durable acquisition decides "
            f"activation",
            handoff_id=record.handoff_id,
            observed=int(observed_generation),
        )
    if acquired <= int(record.fencing_generation):
        raise ControllerHandoffRefused(
            HANDOFF_HOLD_STALE_GENERATION,
            f"the destination acquired generation {acquired}, which is not "
            f"strictly higher than the offered generation "
            f"{record.fencing_generation}; a stale or concurrently-offered "
            f"handoff never carries the lease",
            handoff_id=record.handoff_id,
            offered=int(record.fencing_generation),
            acquired=acquired,
        )
    if acquired != int(record.fencing_generation) + 1:
        # The lease token is dense: every *successful* acquisition is exactly
        # the previous generation + 1 (a refused acquisition does not advance
        # it).  A destination that acquired more than the offerer's generation
        # + 1 therefore acquired a lease that had already passed through a
        # foreign holder -- the handoff was superseded (a stale lease), and the
        # lease it now holds was not carried by this handoff.
        raise ControllerHandoffRefused(
            HANDOFF_HOLD_STALE_GENERATION,
            f"the destination acquired generation {acquired}, but the offerer "
            f"held {record.fencing_generation}; the lease advanced by more than "
            f"one generation, so it passed through another holder and this "
            f"handoff did not carry it",
            handoff_id=record.handoff_id,
            offered=int(record.fencing_generation),
            acquired=acquired,
        )
    return _transition(
        record,
        HANDOFF_STATE_ACTIVE,
        resulting_generation=acquired,
        activated_at=timestamp or _now(),
    )


def reject_handoff(
    record: ControllerHandoffRecord, *, reason: str, timestamp: str = ""
) -> ControllerHandoffRecord:
    """Reject the handoff terminally, naming why the lease did not transfer."""
    if not reason:
        raise ControllerHandoffRefused(
            HANDOFF_HOLD_NOT_OFFERED,
            "a rejected handoff must name the reason it did not transfer the lease",
        )
    return _transition(
        record,
        HANDOFF_STATE_REJECTED,
        rejected_reason=reason,
        rejected_at=timestamp or _now(),
    )


def release_handoff(
    record: ControllerHandoffRecord, *, timestamp: str = ""
) -> ControllerHandoffRecord:
    """Release an active handoff terminally (the lease is no longer carried)."""
    if record.state in HANDOFF_TERMINAL_STATES:
        raise ControllerHandoffRefused(
            HANDOFF_HOLD_RELEASE_UNSUPPORTED,
            f"the handoff {record.handoff_id!r} is already {record.state!r}, "
            f"which is terminal",
            handoff_id=record.handoff_id,
        )
    return _transition(
        record, HANDOFF_STATE_RELEASED, released_at=timestamp or _now()
    )


# ── the schema loader (the record attests the bytes it speaks) ──────────────


def load_handoff_schema(which: str = "handoff") -> dict[str, Any]:
    """Load a controller-handoff schema from the shipped package data.

    ``which`` is ``handoff`` (the default) or ``attestation``. The schema is
    resolved from ``skillweave/assets/schemas/`` package data, so an installed
    wheel resolves it from inside the package rather than reaching for a
    repository-root ``schemas/`` directory that does not exist once installed.
    """
    from ..assets import packaged_schema_path

    names = {
        "handoff": "controller-handoff.schema.json",
        "attestation": "harness-controller-attestation.schema.json",
    }
    try:
        filename = names[which]
    except KeyError:
        raise ValueError(
            f"unknown schema {which!r}; expected one of {sorted(names)}"
        ) from None
    path = packaged_schema_path(filename)
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)
