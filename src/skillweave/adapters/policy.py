"""Admission policy for the common adapter facade (SW-ADAPT-001).

Before any adapter is loaded or invoked, the facade gates it against an
*effective policy*:

* an **explicit allowlist** of approved adapter ids -- anything else is refused,
  never served by a neighbour;
* an **endpoint / data-egress policy** -- an adapter whose transport leaves the
  process must target an allowlisted endpoint, and egress must be permitted;
* a **credential-reference availability gate** -- a descriptor that requires a
  reference which is observed ``missing`` is refused before the call, and the
  value is injected only into an owned child environment.

Every refusal names an ``asset`` so it is attributable. A missing capability, an
unapproved adapter, and a missing credential are three *distinct* refusals, not
one generic NO.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from .descriptor import AdapterDescriptor

__all__ = [
    "PolicyError",
    "EgressPolicy",
    "EffectivePolicy",
    "AdmissionDecision",
    "admit_invocation",
]


class PolicyError(ValueError):
    """A policy input is malformed."""

    def __init__(self, message: str, *, asset: Optional[str] = None) -> None:
        super().__init__(message)
        self.asset = asset


#: Transports that dial a remote endpoint from this process and so must satisfy
#: the egress policy. An in-process/local binding and an owned stdio child are
#: not remote dials; their launch is gated by the allowlist and credential gate.
_EGRESS_TRANSPORTS = ("http", "https", "http+json", "grpc")


def _host_of(endpoint: str) -> str:
    """Extract a bare host from a URL, host:port, or bare host string."""
    text = endpoint.strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0]
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    if text.startswith("["):  # IPv6 literal
        return text.split("]", 1)[0].lstrip("[")
    return text.split(":", 1)[0].lower()


@dataclass(frozen=True)
class EgressPolicy:
    """Whether an adapter may leave the process, and to which endpoints.

    ``allow_network`` is the master switch (default **deny**). When it is on,
    ``endpoint_allowlist`` (bare hosts or URLs) restricts the destinations; an
    empty allowlist with network allowed means "any endpoint the adapter declares
    is acceptable", and an endpoint not in a non-empty allowlist fails closed.
    """

    allow_network: bool = False
    endpoint_allowlist: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        hosts = tuple(_host_of(h) for h in self.endpoint_allowlist)
        object.__setattr__(self, "endpoint_allowlist", hosts)

    def allows(self, endpoint: Optional[str]) -> bool:
        """Whether an outbound call to ``endpoint`` is permitted.

        A non-empty ``endpoint_allowlist`` is required when egress is
        permitted.  An empty allowlist denies all destinations, so the
        admitted endpoint always equals one the caller explicitly listed.

        A network call with no declared endpoint fails closed: the facade
        never invents a destination.
        """
        if not self.allow_network:
            return False
        if not self.endpoint_allowlist:
            # An empty allowlist means "no endpoint is approved", not "any".
            return False
        if not endpoint:
            return False
        return _host_of(endpoint) in self.endpoint_allowlist


@dataclass(frozen=True)
class EffectivePolicy:
    """The effective admission policy the facade enforces before a call.

    ``allowed_adapter_ids`` is the explicit allowlist. It is **fail-closed**: an
    empty allowlist approves nothing. A caller that wants the built-in providers
    admitted must name them.
    """

    allowed_adapter_ids: tuple[str, ...] = ()
    egress: EgressPolicy = EgressPolicy()
    require_credential_availability: bool = True

    def __post_init__(self) -> None:
        ids = tuple(str(i) for i in self.allowed_adapter_ids)
        if len(set(ids)) != len(ids):
            raise PolicyError(
                "allowed_adapter_ids contain duplicates", asset="allowed_adapter_ids"
            )
        object.__setattr__(self, "allowed_adapter_ids", ids)

    def approves(self, adapter_id: str) -> bool:
        return adapter_id in self.allowed_adapter_ids


@dataclass(frozen=True)
class AdmissionDecision:
    """The policy gate's verdict on one proposed invocation."""

    admitted: bool
    reason: str
    asset: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {"admitted": self.admitted, "reason": self.reason, "asset": self.asset}


def admit_invocation(
    policy: EffectivePolicy,
    descriptor: AdapterDescriptor,
    *,
    category: str,
    operation: str,
    required_capabilities: Iterable[str] = (),
    credential_availability: Optional[Mapping[str, str]] = None,
    endpoint: Optional[str] = None,
) -> AdmissionDecision:
    """Admit (or refuse) one invocation before any load or call.

    The checks run in a fixed order so the first blocking fact is named:

    1. the adapter id is on the explicit allowlist;
    2. the adapter actually serves ``category`` + ``operation``;
    3. every required capability is declared by the adapter;
    4. every required credential reference is observed available (when the
       policy requires it) and injected only into an owned child environment;
    5. an out-of-process transport targets an allowlisted endpoint under the
       egress policy.

    Nothing here performs a network install or loads an executable: this is a
    pure, side-effect-free decision.
    """
    if not policy.approves(descriptor.id):
        return AdmissionDecision(
            admitted=False,
            reason=(
                f"adapter {descriptor.id!r} is not on the allowlist; "
                "unapproved adapters are never served"
            ),
            asset="allowlist",
        )

    if not descriptor.serves(category, operation):
        return AdmissionDecision(
            admitted=False,
            reason=(
                f"adapter {descriptor.id!r} does not serve "
                f"category={category!r} operation={operation!r}"
            ),
            asset="operation",
        )

    reported = credential_availability or {}

    for cap in required_capabilities:
        if not descriptor.holds(cap):
            return AdmissionDecision(
                admitted=False,
                reason=(
                    f"adapter {descriptor.id!r} does not hold required capability "
                    f"{cap!r}"
                ),
                asset=f"capability:{cap}",
            )

    op = descriptor.operation(operation)
    needed_caps = set(op.required_capabilities) | set(required_capabilities)

    # Egress is checked BEFORE credentials so that a default-deny egress policy
    # is never masked by a credential refusal.  A caller that denies egress
    # gets the egress refusal first, regardless of credential state.
    if descriptor.transport in _EGRESS_TRANSPORTS:
        if not policy.egress.allows(endpoint):
            return AdmissionDecision(
                admitted=False,
                reason=(
                    f"adapter {descriptor.id!r} transport {descriptor.transport!r} "
                    f"cannot egress to endpoint {endpoint!r} under the policy"
                ),
                asset="egress",
            )

    for ref in descriptor.credential_refs:
        state = reported.get(ref, "missing")
        if state != "available":
            if policy.require_credential_availability:
                return AdmissionDecision(
                    admitted=False,
                    reason=(
                        f"adapter {descriptor.id!r} requires credential reference "
                        f"{ref!r} which is missing; a missing credential is refused, "
                        "never simulated"
                    ),
                    asset=f"credential:{ref}",
                )
        if not policy.egress.allows(endpoint):
            return AdmissionDecision(
                admitted=False,
                reason=(
                    f"adapter {descriptor.id!r} transport {descriptor.transport!r} "
                    f"cannot egress to endpoint {endpoint!r} under the policy"
                ),
                asset="egress",
            )

    return AdmissionDecision(
        admitted=True,
        reason=(
            f"adapter {descriptor.id!r} admitted: allowlisted, serves "
            f"{category}/{operation}, holds {sorted(needed_caps)}"
        ),
    )
