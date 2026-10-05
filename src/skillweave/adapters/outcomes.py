"""Typed, provider-neutral operation outcomes (SW-ADAPT-001).

Two neutral records live here:

* :class:`CredentialReference` -- a reference to a credential by identity and
  *availability*, never the value. ``missing`` is a first-class, reportable
  state; it is never simulated as success. The value itself is injected only
  into an owned child environment and is never persisted here.
* :class:`SearchOutcome` -- the typed outcome of a search operation. A success
  carries at least one real http(s) source; a non-success carries no sources, so
  an outage is never dressed up as a retrieved source. Attempts record which
  transport was tried and how it ended, so a configured fallback is visible in
  the record rather than silent.
* :class:`OperationReceipt` -- the common receipt binding one invocation to the
  adapter, its version and descriptor digest, the operation, the domain
  contract, the transport, provenance, status and evidence. A receipt records an
  execution; it cannot self-approve a gate.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

__all__ = [
    "CREDENTIAL_REFERENCE_SCHEMA_VERSION",
    "SEARCH_OUTCOME_SCHEMA_VERSION",
    "SEARCH_OUTCOME_CONTRACT",
    "SEARCH_STATUSES",
    "AVAILABILITY_STATES",
    "INJECTION_OWNED_CHILD_ENVIRONMENT",
    "OutcomeError",
    "CredentialReference",
    "SearchResultRecord",
    "SearchAttempt",
    "SearchOutcome",
    "OperationReceipt",
]

CREDENTIAL_REFERENCE_SCHEMA_VERSION = "1.0.0"
SEARCH_OUTCOME_SCHEMA_VERSION = "1.0.0"

#: The SDK-owned domain contract a typed :class:`SearchOutcome` is the wire form
#: of. It is defined here, next to the class that implements it, so the
#: registry's contract->outcome-type binding can name it without a duplicated
#: string literal and without importing the backend layer.
SEARCH_OUTCOME_CONTRACT = "search-outcome/v1"

#: The closed search-outcome status set.
SEARCH_STATUSES: tuple[str, ...] = (
    "success",
    "blocked",
    "error",
    "unavailable",
    "cancelled",
)

#: The closed credential availability set. ``missing`` is reportable state.
AVAILABILITY_STATES: tuple[str, ...] = ("available", "missing")

#: The only admissible injection route: an owned child environment.
INJECTION_OWNED_CHILD_ENVIRONMENT = "owned_child_environment_only"


class OutcomeError(ValueError):
    """An outcome/credential/receipt field is missing or malformed."""

    def __init__(self, message: str, *, asset: Optional[str] = None) -> None:
        super().__init__(message)
        self.asset = asset


def _is_http_url(value: Any) -> bool:
    return isinstance(value, str) and (
        value.startswith("http://") or value.startswith("https://")
    )


@dataclass(frozen=True)
class CredentialReference:
    """A reference to a credential; never the credential value.

    The reference declares where the value is resolved from and which
    environment variable names it maps to, its observed availability, and the
    fact that it is injected only into an owned child environment. A reference
    never persists a value (``persistent_values`` must be ``False``).
    """

    reference_id: str
    source: str
    variable_names: tuple[str, ...]
    availability: str = "missing"
    injection: str = INJECTION_OWNED_CHILD_ENVIRONMENT
    persistent_values: bool = False
    schema_version: str = CREDENTIAL_REFERENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CREDENTIAL_REFERENCE_SCHEMA_VERSION:
            raise OutcomeError(
                f"unsupported credential-reference schema_version "
                f"{self.schema_version!r}",
                asset="schema_version",
            )
        if not isinstance(self.reference_id, str) or not self.reference_id.strip():
            raise OutcomeError("reference_id must be non-empty", asset="reference_id")
        if not isinstance(self.source, str) or not self.source.strip():
            raise OutcomeError("source must be non-empty", asset="source")
        names = tuple(str(n) for n in self.variable_names)
        if not names:
            raise OutcomeError(
                "a credential reference must name at least one variable",
                asset="variable_names",
            )
        for name in names:
            if not name or not (name[0].isupper() or name[0] == "_") or not all(
                ch.isupper() or ch.isdigit() or ch == "_" for ch in name
            ):
                raise OutcomeError(
                    f"variable name {name!r} must match [A-Z_][A-Z0-9_]*",
                    asset="variable_names",
                )
        if self.availability not in AVAILABILITY_STATES:
            raise OutcomeError(
                f"unknown availability {self.availability!r}; "
                f"expected one of {list(AVAILABILITY_STATES)}",
                asset="availability",
            )
        if self.injection != INJECTION_OWNED_CHILD_ENVIRONMENT:
            raise OutcomeError(
                "injection must be an owned child environment",
                asset="injection",
            )
        if self.persistent_values is not False:
            raise OutcomeError(
                "a credential reference never persists a value",
                asset="persistent_values",
            )
        object.__setattr__(self, "variable_names", names)

    @property
    def available(self) -> bool:
        return self.availability == "available"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "reference_id": self.reference_id,
            "source": self.source,
            "variable_names": list(self.variable_names),
            "availability": self.availability,
            "injection": self.injection,
            "persistent_values": self.persistent_values,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CredentialReference":
        if not isinstance(data, Mapping):
            raise OutcomeError(
                "a credential reference must be a mapping", asset="reference"
            )
        return cls(
            reference_id=str(data.get("reference_id", "")),
            source=str(data.get("source", "")),
            variable_names=tuple(data.get("variable_names") or ()),
            availability=str(data.get("availability", "")),
            injection=str(data.get("injection", "")),
            persistent_values=data.get("persistent_values", False),
            schema_version=str(
                data.get("schema_version", CREDENTIAL_REFERENCE_SCHEMA_VERSION)
            ),
        )


@dataclass(frozen=True)
class SearchResultRecord:
    """One real source in a search outcome: an http(s) URL and its provenance."""

    title: str
    url: str
    snippet: str
    source: str
    provider: str
    date: str = ""

    def __post_init__(self) -> None:
        if not _is_http_url(self.url):
            raise OutcomeError(
                f"search result url {self.url!r} must be an http(s) URL",
                asset="results.url",
            )
        for name in ("title", "snippet", "source", "provider"):
            if getattr(self, name) is None:
                raise OutcomeError(
                    f"search result {name} must not be None", asset=f"results.{name}"
                )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source": self.source,
            "provider": self.provider,
        }
        if self.date:
            payload["date"] = self.date
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SearchResultRecord":
        return cls(
            title=str(data.get("title", "")),
            url=str(data.get("url", "")),
            snippet=str(data.get("snippet", "")),
            source=str(data.get("source", "")),
            provider=str(data.get("provider", "")),
            date=str(data.get("date", "")),
        )


@dataclass(frozen=True)
class SearchAttempt:
    """One transport attempt: who was tried, how, and how it ended."""

    provider: str
    transport: str
    status: str
    error_code: Optional[str] = None

    def __post_init__(self) -> None:
        for name in ("provider", "transport", "status"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise OutcomeError(
                    f"attempt {name} must be non-empty", asset=f"attempts.{name}"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "transport": self.transport,
            "status": self.status,
            "error_code": self.error_code,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SearchAttempt":
        return cls(
            provider=str(data.get("provider", "")),
            transport=str(data.get("transport", "")),
            status=str(data.get("status", "")),
            error_code=data.get("error_code"),
        )


@dataclass(frozen=True)
class SearchOutcome:
    """The typed outcome of a search operation.

    Invariants enforced at construction (the SDK schema enforces the same on the
    wire):

    * a ``success`` carries at least one real http(s) source;
    * a non-success carries **no** results -- a failure is not a retrieved
      source;
    * every configured attempt is recorded, so a fallback is visibly attributed.
    """

    query_sha256: str
    requested_provider: str
    status: str
    results: tuple[SearchResultRecord, ...] = ()
    attempts: tuple[SearchAttempt, ...] = ()
    synthesis: Optional[str] = None
    error_code: Optional[str] = None
    retry_after_seconds: Optional[float] = None
    schema_version: str = SEARCH_OUTCOME_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SEARCH_OUTCOME_SCHEMA_VERSION:
            raise OutcomeError(
                f"unsupported search-outcome schema_version {self.schema_version!r}",
                asset="schema_version",
            )
        if not isinstance(self.query_sha256, str) or len(self.query_sha256) != 64 or any(
            ch not in "0123456789abcdef" for ch in self.query_sha256
        ):
            raise OutcomeError(
                "query_sha256 must be a lowercase sha256 hex string",
                asset="query_sha256",
            )
        if not isinstance(self.requested_provider, str) or not self.requested_provider.strip():
            raise OutcomeError(
                "requested_provider must be non-empty", asset="requested_provider"
            )
        if self.status not in SEARCH_STATUSES:
            raise OutcomeError(
                f"unknown status {self.status!r}; expected one of "
                f"{list(SEARCH_STATUSES)}",
                asset="status",
            )
        results = tuple(self.results)
        attempts = tuple(self.attempts)
        for r in results:
            if not isinstance(r, SearchResultRecord):
                raise OutcomeError(
                    "results must be SearchResultRecord values", asset="results"
                )
        for a in attempts:
            if not isinstance(a, SearchAttempt):
                raise OutcomeError("attempts must be SearchAttempt values", asset="attempts")
        if self.status == "success" and not results:
            raise OutcomeError(
                "a success must carry at least one real source",
                asset="results",
            )
        if self.status != "success" and results:
            raise OutcomeError(
                f"a non-success status {self.status!r} must carry no results",
                asset="results",
            )
        if self.retry_after_seconds is not None and self.retry_after_seconds < 0:
            raise OutcomeError(
                "retry_after_seconds must be non-negative",
                asset="retry_after_seconds",
            )
        object.__setattr__(self, "results", results)
        object.__setattr__(self, "attempts", attempts)

    @property
    def succeeded(self) -> bool:
        return self.status == "success" and bool(self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "query_sha256": self.query_sha256,
            "requested_provider": self.requested_provider,
            "status": self.status,
            "results": [r.to_dict() for r in self.results],
            "synthesis": self.synthesis,
            "attempts": [a.to_dict() for a in self.attempts],
            "error_code": self.error_code,
            "retry_after_seconds": self.retry_after_seconds,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SearchOutcome":
        """Build from a raw mapping, failing closed on a malformed payload.

        A malformed search result (an unsafe URL scheme, a missing field) raises
        here rather than being accepted as evidence.
        """
        if not isinstance(data, Mapping):
            raise OutcomeError("a search outcome must be a mapping", asset="outcome")
        return cls(
            query_sha256=str(data.get("query_sha256", "")),
            requested_provider=str(data.get("requested_provider", "")),
            status=str(data.get("status", "")),
            results=tuple(
                SearchResultRecord.from_dict(r) for r in (data.get("results") or ())
            ),
            attempts=tuple(
                SearchAttempt.from_dict(a) for a in (data.get("attempts") or ())
            ),
            synthesis=data.get("synthesis"),
            error_code=data.get("error_code"),
            retry_after_seconds=data.get("retry_after_seconds"),
            schema_version=str(data.get("schema_version", SEARCH_OUTCOME_SCHEMA_VERSION)),
        )

    @classmethod
    def from_json(cls, text: str) -> "SearchOutcome":
        """Parse a JSON payload, failing closed on malformed JSON or shape."""
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise OutcomeError(
                f"search outcome is not valid JSON: {exc}", asset="outcome"
            ) from exc
        return cls.from_dict(parsed)


@dataclass(frozen=True)
class OperationReceipt:
    """The common receipt binding one invocation to what produced it.

    A receipt records: the invocation id, the adapter id/version and descriptor
    digest, the operation, the domain contract, the transport, the provenance
    (opaque package ref or ``None``), the terminal status and the evidence
    digest. It is a *record of an execution*: it carries no gate-decision
    authority, and :attr:`self_approving` is always ``False``.
    """

    invocation_id: str
    adapter_id: str
    adapter_version: str
    descriptor_digest: str
    operation: str
    domain_contract: str
    transport: str
    status: str
    evidence_digest: str
    provenance: Optional[Mapping[str, Any]] = None
    query_sha256: Optional[str] = None

    #: A receipt is never gate authority. Kept as a fixed, auditable field that
    #: only ever serializes as ``false``: ``True`` is rejected here (and by
    #: :meth:`from_dict`), so "a receipt cannot self-approve" is a property of the
    #: type, not a convention a caller could bypass.
    self_approving: bool = False

    def __post_init__(self) -> None:
        if self.self_approving is not False:
            raise OutcomeError(
                "a receipt cannot self-approve; self_approving must be False",
                asset="receipt.self_approving",
            )
        for name in (
            "invocation_id",
            "adapter_id",
            "adapter_version",
            "operation",
            "domain_contract",
            "transport",
            "status",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise OutcomeError(
                    f"receipt {name} must be non-empty", asset=f"receipt.{name}"
                )
        for name in ("descriptor_digest", "evidence_digest"):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) != 64 or any(
                ch not in "0123456789abcdef" for ch in value
            ):
                raise OutcomeError(
                    f"receipt {name} must be a lowercase sha256 hex string",
                    asset=f"receipt.{name}",
                )
        if self.status not in SEARCH_STATUSES:
            raise OutcomeError(
                f"receipt status {self.status!r} is not a known outcome status",
                asset="receipt.status",
            )

    @property
    def is_authority(self) -> bool:
        """A receipt is never gate/approval authority."""
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "invocation_id": self.invocation_id,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "descriptor_digest": self.descriptor_digest,
            "operation": self.operation,
            "domain_contract": self.domain_contract,
            "transport": self.transport,
            "status": self.status,
            "evidence_digest": self.evidence_digest,
            "provenance": dict(self.provenance) if self.provenance is not None else None,
            "query_sha256": self.query_sha256,
            "self_approving": self.self_approving,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OperationReceipt":
        """Rebuild a receipt from its durable mapping, failing closed.

        A serialized receipt that claims ``self_approving: true`` is refused here
        exactly as at construction, so a tampered or hand-written record cannot
        resurrect self-approval authority through the loader.
        """
        if not isinstance(data, Mapping):
            raise OutcomeError("a receipt must be a mapping", asset="receipt")
        return cls(
            invocation_id=str(data.get("invocation_id", "")),
            adapter_id=str(data.get("adapter_id", "")),
            adapter_version=str(data.get("adapter_version", "")),
            descriptor_digest=str(data.get("descriptor_digest", "")),
            operation=str(data.get("operation", "")),
            domain_contract=str(data.get("domain_contract", "")),
            transport=str(data.get("transport", "")),
            status=str(data.get("status", "")),
            evidence_digest=str(data.get("evidence_digest", "")),
            provenance=data.get("provenance"),
            query_sha256=data.get("query_sha256"),
            self_approving=data.get("self_approving", False),
        )


def canonical_json_bytes(value: Any) -> bytes:
    """Canonical JSON: sorted keys, compact separators, UTF-8."""
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def descriptor_digest(descriptor_dict: Mapping[str, Any]) -> str:
    """The sha256 of a descriptor's canonical JSON bytes."""
    return hashlib.sha256(canonical_json_bytes(descriptor_dict)).hexdigest()


def query_digest(query: str) -> str:
    """The sha256 of a query string (UTF-8)."""
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ToolOutcome:
    """The typed outcome of a *generic* (non-search) tool operation.

    A generic tool returns its own domain payload; the facade only requires that
    the outcome be typed and carry a terminal status, so a generic tool can never
    masquerade as a search source. ``payload`` is the tool's own result object
    (opaque to the facade).
    """

    status: str
    tool: str
    payload: Any = None
    error_code: Optional[str] = None

    def __post_init__(self) -> None:
        if self.status not in SEARCH_STATUSES:
            raise OutcomeError(
                f"unknown tool-outcome status {self.status!r}; expected one of "
                f"{list(SEARCH_STATUSES)}",
                asset="status",
            )
        if not isinstance(self.tool, str) or not self.tool.strip():
            raise OutcomeError("tool-outcome tool must be non-empty", asset="tool")

    @property
    def succeeded(self) -> bool:
        return self.status == "success"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "tool": self.tool,
            "payload": self.payload,
            "error_code": self.error_code,
        }


__all__ += ["ToolOutcome"]
