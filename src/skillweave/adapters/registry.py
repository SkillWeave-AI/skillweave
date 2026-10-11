"""The adapter extension registry and the common facade (SW-ADAPT-001).

Two things live here:

* :class:`AdapterRegistry` -- the **extension point**. An adapter is registered
  at wiring time with its neutral descriptor and a backend callable. The registry
  holds no enumeration of concrete adapters and no vendor list, so adding one --
  including an external adapter the core has never heard of -- requires no core
  edit. A duplicate id is refused; an unregistered id is refused by name; a
  descriptor at an unsupported version is refused.

* :class:`AdapterFacade` -- resolves a registered adapter by
  ``category + operation + capabilities``, runs the admission policy (allowlist,
  egress, credential availability), invokes the backend, and returns a typed
  outcome plus a :class:`OperationReceipt`. A generic adapter is **never** used
  as an implicit fallback for a search operation (or vice versa): a request that
  names no registered adapter fails closed instead of being served by a
  neighbour.
"""

from __future__ import annotations

import hashlib
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from .descriptor import (
    ADAPTER_DESCRIPTOR_SCHEMA_VERSION,
    AdapterDescriptor,
    AdapterDescriptorError,
)
from .outcomes import (
    SEARCH_OUTCOME_CONTRACT,
    OperationReceipt,
    OutcomeError,
    SearchOutcome,
    ToolOutcome,
    descriptor_digest,
    query_digest,
)
from .policy import AdmissionDecision, EffectivePolicy, admit_invocation

__all__ = [
    "AdapterError",
    "UnknownAdapterError",
    "UnsupportedVersionError",
    "AdapterBackend",
    "AdapterEntry",
    "AdapterRegistry",
    "Resolution",
    "AdapterFacade",
]


#: The registry-owned contract -> typed-outcome binding. An operation names a
#: result contract; that contract is implemented by exactly one typed outcome
#: class. The facade proves an invocation's result conforms by *reading this
#: table* -- never by branching on a specific contract string. Adding a future
#: contract + outcome class is one row here; no facade edit.
_KNOWN_CONTRACT_OUTCOME_TYPES: dict[str, type] = {
    SEARCH_OUTCOME_CONTRACT: SearchOutcome,
    # A generic tool's own domain contract and its wire form.
    "tool-outcome/v1": ToolOutcome,
}

#: The admissible typed-outcome family. A generic tool may declare an arbitrary
#: domain ``result_contract`` unknown to Core; the facade still requires its
#: result to be a *typed* outcome from this family -- never a bare ``dict``,
#: ``None`` or ``str`` that a backend could smuggle past the contract check.
_TYPED_OUTCOME_BASE: tuple[type, ...] = (SearchOutcome, ToolOutcome)


class AdapterError(ValueError):
    """A registry/resolution/facade error, attributable via ``asset``."""

    def __init__(self, message: str, *, asset: Optional[str] = None) -> None:
        super().__init__(message)
        self.asset = asset


class UnknownAdapterError(AdapterError):
    """No adapter is registered for the requested id/category/operation."""


class UnsupportedVersionError(AdapterError):
    """A descriptor names an unsupported schema version."""


class AdapterBackend(ABC):
    """The callable behind a descriptor. Names no concrete adapter.

    A backend receives the resolved operation name and an argument mapping and
    returns a typed outcome (e.g. a :class:`~skillweave.adapters.outcomes.
    SearchOutcome` or ``ToolOutcome``). The facade owns admission and receipts;
    the backend owns only the transport-specific call.
    """

    @abstractmethod
    async def invoke(
        self, operation: str, argument: Mapping[str, Any], context: Mapping[str, Any]
    ) -> Any:
        """Perform ``operation`` and return a typed outcome object."""


@dataclass(frozen=True)
class AdapterEntry:
    """One registered adapter: its neutral descriptor and its backend."""

    descriptor: AdapterDescriptor
    backend: AdapterBackend
    source: str = "explicit_registration"

    @property
    def digest(self) -> str:
        return descriptor_digest(self.descriptor.to_dict())


class AdapterRegistry:
    """The wiring-time extension point for adapters (no enumeration)."""

    def __init__(self) -> None:
        self._entries: dict[str, AdapterEntry] = {}

    def register(
        self, descriptor: AdapterDescriptor, backend: AdapterBackend, *, source: str = "explicit_registration"
    ) -> AdapterEntry:
        if not isinstance(descriptor, AdapterDescriptor):
            raise AdapterError(
                "register() requires an AdapterDescriptor", asset="descriptor"
            )
        if not isinstance(backend, AdapterBackend):
            raise AdapterError(
                "register() requires an AdapterBackend", asset="backend"
            )
        if descriptor.schema_version != ADAPTER_DESCRIPTOR_SCHEMA_VERSION:
            raise UnsupportedVersionError(
                f"unsupported descriptor version {descriptor.schema_version!r}",
                asset="schema_version",
            )
        if descriptor.id in self._entries:
            raise AdapterError(
                f"adapter id {descriptor.id!r} is already registered; "
                "duplicate ids are refused",
                asset="id",
            )
        entry = AdapterEntry(descriptor=descriptor, backend=backend, source=source)
        self._entries[descriptor.id] = entry
        return entry

    def register_dict(
        self, payload: Mapping[str, Any], backend: AdapterBackend, *, source: str = "explicit_registration"
    ) -> AdapterEntry:
        """Register from a raw descriptor mapping (fails closed if malformed)."""
        return self.register(
            AdapterDescriptor.from_dict(payload), backend, source=source
        )

    def get(self, adapter_id: str) -> AdapterEntry:
        entry = self._entries.get(adapter_id)
        if entry is None:
            raise UnknownAdapterError(
                f"no adapter registered with id {adapter_id!r}; "
                f"registered: {sorted(self._entries)}",
                asset="id",
            )
        return entry

    def ids(self) -> tuple[str, ...]:
        return tuple(self._entries)

    def entries(self) -> tuple[AdapterEntry, ...]:
        return tuple(self._entries.values())

    def resolved_for(
        self, category: str, operation: str, required_capabilities: Sequence[str] = ()
    ) -> tuple[AdapterEntry, ...]:
        """Every registered adapter that serves ``category``/``operation``.

        Returns them in registration order. No implicit fallback: a caller that
        wants a specific one resolves by id first.
        """
        needed = set(required_capabilities)
        return tuple(
            entry
            for entry in self._entries.values()
            if entry.descriptor.serves(category, operation)
            and needed <= set(entry.descriptor.capabilities)
        )

    def __contains__(self, adapter_id: object) -> bool:
        return adapter_id in self._entries

    def __len__(self) -> int:
        return len(self._entries)


@dataclass(frozen=True)
class Resolution:
    """A resolved adapter plus the policy decision that admitted it."""

    entry: AdapterEntry
    admission: AdmissionDecision

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self.entry.descriptor


class AdapterFacade:
    """The common facade: resolve by category/operation/capability, admit, invoke.

    The facade is provider-neutral and format-neutral. It never branches on an
    adapter id, a transport name, or a package kind: it reads the descriptor as
    data, applies the effective policy, and returns a receipt.
    """

    def __init__(self, registry: AdapterRegistry, policy: EffectivePolicy) -> None:
        if not isinstance(registry, AdapterRegistry):
            raise AdapterError("facade requires an AdapterRegistry", asset="registry")
        if not isinstance(policy, EffectivePolicy):
            raise AdapterError("facade requires an EffectivePolicy", asset="policy")
        self._registry = registry
        self._policy = policy

    @property
    def registry(self) -> AdapterRegistry:
        return self._registry

    @property
    def policy(self) -> EffectivePolicy:
        return self._policy

    def resolve(
        self,
        *,
        category: str,
        operation: str,
        adapter_id: Optional[str] = None,
        required_capabilities: Sequence[str] = (),
        credential_availability: Optional[Mapping[str, str]] = None,
        endpoint: Optional[str] = None,
    ) -> Resolution:
        """Resolve and admit one adapter for a call, or fail closed.

        When ``adapter_id`` is named, it must be registered (an unknown id is a
        hard failure). When it is not named, the facade requires **exactly one**
        registered adapter to serve the request; zero matches is an unknown
        adapter, more than one is ambiguous and refused -- never an implicit
        fallback to a neighbour.
        """
        if adapter_id is not None:
            entry = self._registry.get(adapter_id)
        else:
            matches = self._registry.resolved_for(
                category, operation, required_capabilities
            )
            if not matches:
                raise UnknownAdapterError(
                    f"no registered adapter serves category={category!r} "
                    f"operation={operation!r}; a generic adapter is never an "
                    "implicit fallback",
                    asset="id",
                )
            if len(matches) > 1:
                raise AdapterError(
                    f"ambiguous resolution for {category}/{operation}: "
                    f"{[m.descriptor.id for m in matches]}; name an adapter id",
                    asset="id",
                )
            entry = matches[0]

        decision = admit_invocation(
            self._policy,
            entry.descriptor,
            category=category,
            operation=operation,
            required_capabilities=required_capabilities,
            credential_availability=credential_availability,
            endpoint=endpoint,
        )
        if not decision.admitted:
            raise AdapterError(decision.reason, asset=decision.asset)
        return Resolution(entry=entry, admission=decision)

    async def invoke(
        self,
        *,
        category: str,
        operation: str,
        argument: Mapping[str, Any],
        adapter_id: Optional[str] = None,
        required_capabilities: Sequence[str] = (),
        credential_availability: Optional[Mapping[str, str]] = None,
        endpoint: Optional[str] = None,
        invocation_id: Optional[str] = None,
    ) -> tuple[Any, OperationReceipt]:
        """Resolve, admit, invoke, and return ``(outcome, receipt)``.

        The facade does **not** trust the backend. The operation's
        ``result_contract`` names a typed outcome class (the binding is
        :data:`_KNOWN_CONTRACT_OUTCOME_TYPES`, owned here and by the descriptor,
        never a hard-coded contract-name special case), and the object the
        backend returns must be a conforming instance of it. When it is not, the
        facade substitutes a typed, ``status="error"`` failure outcome and a
        **non-success** receipt rather than returning a success that claims a
        contract it never satisfied.

        The receipt binds the invocation to the adapter, its descriptor digest,
        the operation, the domain contract and the transport -- and records the
        terminal status. The receipt carries no gate authority.
        """
        resolution = self.resolve(
            category=category,
            operation=operation,
            adapter_id=adapter_id,
            required_capabilities=required_capabilities,
            credential_availability=credential_availability,
            endpoint=endpoint,
        )
        descriptor = resolution.descriptor
        op = descriptor.operation(operation)

        context = {
            "adapter_id": descriptor.id,
            "adapter_version": descriptor.version,
            "transport": descriptor.transport,
            "operation": operation,
            "domain_contract": op.result_contract,
        }
        outcome = await resolution.entry.backend.invoke(operation, argument, context)

        # -- contract conformance: refuse to trust the returned object ---------
        contract = op.result_contract
        # A contract with an explicit registry binding admits only that typed
        # outcome; a generic tool's own domain contract admits any typed outcome
        # from the family (never a bare dict/None/str). Either way the contract
        # name itself is *data* -- no facade branch on a literal.
        accepted = _KNOWN_CONTRACT_OUTCOME_TYPES.get(contract, _TYPED_OUTCOME_BASE)
        if not isinstance(outcome, accepted):
            outcome, receipt = self._non_conforming(
                descriptor=descriptor,
                resolution=resolution,
                operation=operation,
                contract=contract,
                invocation_id=invocation_id,
                argument=argument,
                reason=(
                    f"backend returned {type(outcome).__name__}, which is not a "
                    f"conforming typed outcome for contract {contract!r}"
                ),
            )
            return outcome, receipt

        status, evidence, query_sha = self._receipt_bits(outcome)
        receipt = self._receipt(
            descriptor=descriptor,
            resolution=resolution,
            operation=operation,
            contract=contract,
            status=status,
            evidence=evidence,
            query_sha256=query_sha,
            invocation_id=invocation_id,
        )
        return outcome, receipt

    # ── receipt construction ────────────────────────────────────────────────

    @staticmethod
    def _receipt_bits(outcome: Any) -> tuple[str, str, Optional[str]]:
        """The status, evidence digest and query digest for a conforming result."""
        status = str(getattr(outcome, "status", "error"))
        evidence = hashlib.sha256(_canonical(outcome).encode("utf-8")).hexdigest()
        return status, evidence, getattr(outcome, "query_sha256", None)

    @staticmethod
    def _receipt(
        *,
        descriptor: AdapterDescriptor,
        resolution: "Resolution",
        operation: str,
        contract: str,
        status: str,
        evidence: str,
        query_sha256: Optional[str],
        invocation_id: Optional[str],
    ) -> OperationReceipt:
        return OperationReceipt(
            invocation_id=invocation_id or uuid.uuid4().hex,
            adapter_id=descriptor.id,
            adapter_version=descriptor.version,
            descriptor_digest=resolution.entry.digest,
            operation=operation,
            domain_contract=contract,
            transport=descriptor.transport,
            status=status,
            evidence_digest=evidence,
            provenance=(
                descriptor.package_ref.to_dict()
                if descriptor.package_ref is not None
                else None
            ),
            query_sha256=query_sha256,
        )

    def _non_conforming(
        self,
        *,
        descriptor: AdapterDescriptor,
        resolution: "Resolution",
        operation: str,
        contract: str,
        invocation_id: Optional[str],
        argument: Mapping[str, Any],
        reason: str,
    ) -> tuple[Any, OperationReceipt]:
        """Turn a non-conforming backend result into a typed failure.

        The returned outcome is a typed ``status="error"`` object (a
        :class:`SearchOutcome` for a search contract, else a :class:`ToolOutcome`)
        carrying ``error_code="malformed_outcome"``; the receipt records that
        terminal ``error`` status -- never ``success`` -- so a caller cannot be
        handed a success that never satisfied its contract. The *reason* travels
        in the failure outcome's ``synthesis`` (search) and its ``error_code``,
        and the receipt carries the same non-success ``status``.
        """
        failure = _failure_outcome(
            contract=contract,
            descriptor=descriptor,
            operation=operation,
            argument=argument,
            reason=reason,
        )
        status, evidence, query_sha = self._receipt_bits(failure)
        receipt = self._receipt(
            descriptor=descriptor,
            resolution=resolution,
            operation=operation,
            contract=contract,
            status=status,
            evidence=evidence,
            query_sha256=query_sha,
            invocation_id=invocation_id,
        )
        return failure, receipt


def _failure_outcome(
    *,
    contract: str,
    descriptor: AdapterDescriptor,
    operation: str,
    argument: Mapping[str, Any],
    reason: str,
) -> Any:
    """A typed, non-success outcome stand-in for a non-conforming result.

    It is shaped by the *contract* (data), not by a caller: a search operation
    yields a :class:`SearchOutcome` with no sources; any other operation yields a
    :class:`ToolOutcome`. Both carry ``error_code="malformed_outcome"``.
    """
    if contract == SEARCH_OUTCOME_CONTRACT:
        return SearchOutcome(
            query_sha256=query_digest(str(argument.get("query", ""))),
            requested_provider=descriptor.id,
            status="error",
            attempts=(),
            synthesis=reason,
            error_code="malformed_outcome",
        )
    return ToolOutcome(
        status="error",
        tool=operation,
        payload=None,
        error_code="malformed_outcome",
    )


def _canonical(value: Any) -> str:
    """A canonical JSON string for an outcome object (or its dict)."""
    import json

    payload = value.to_dict() if hasattr(value, "to_dict") else value
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
