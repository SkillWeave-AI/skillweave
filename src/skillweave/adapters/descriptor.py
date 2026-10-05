"""The neutral adapter descriptor (SW-ADAPT-001).

A descriptor is the common facade's view of *one* adapter: the runtime
categories it registers under, the operations it serves and the transport it is
invoked through. It is deliberately **neutral** -- it names no concrete provider
and carries no package-format kind vocabulary.

The independent axes
--------------------

Four facts about an adapter are independent and must never be collapsed into a
single "kind" taxonomy:

* **runtime category** -- the role the adapter plays at the facade
  (``search`` / ``generic``); the closed v1 set.
* **invocation transport** -- how it is called (``http``, ``mcp-stdio``,
  ``in-process``, ...); an open string, because a new transport needs no contract
  change.
* **package format / kind** -- the reader-specific packaging vocabulary
  (``capacium`` kind ``mcp-server``, or any other format's opaque kind). The
  facade reads this as an opaque string and *never* branches on it. It lives in
  :class:`PackageRef`, produced by a format reader, not by the facade.
* **distribution / provisioning** -- how the bytes arrive. Not stated here at
  all: reading a descriptor proves format conformance, never callability.

Only :data:`ADAPTER_CATEGORIES` (the runtime categories) is closed. Everything
else is open data so a second format, a new transport or an external tool needs
no core edit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

__all__ = [
    "ADAPTER_DESCRIPTOR_SCHEMA_VERSION",
    "ADAPTER_CATEGORIES",
    "CATEGORY_SEARCH",
    "CATEGORY_GENERIC",
    "AdapterDescriptorError",
    "OperationBinding",
    "PackageRef",
    "AdapterDescriptor",
]

#: The descriptor contract version this class produces. It is the version of the
#: *contract*, not of the adapter; it matches the SDK schema const ``1.0.0``.
ADAPTER_DESCRIPTOR_SCHEMA_VERSION = "1.0.0"

#: The v1 runtime-category set. A category is a *runtime role*, not a package
#: kind, and the set is closed: an unknown category fails closed rather than
#: being coerced onto a nearest match.
ADAPTER_CATEGORIES: tuple[str, ...] = ("search", "generic")

CATEGORY_SEARCH = "search"
CATEGORY_GENERIC = "generic"

#: A descriptor id is a consumer-unique, opaque handle that is written into the
#: descriptor digest and into every receipt's provenance. It must therefore be a
#: *plain identifier*, never a path or a smuggled tuple: the SDK contract pins the
#: same conservative shape (``^[a-z0-9][a-z0-9._-]*$``), so this validator and the
#: wire schema agree. An id with a path separator is refused rather than flowing
#: into a digest, a log line or a receipt.
_ADAPTER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

#: A transport is an invocation *binding token* (``http``, ``mcp-stdio``,
#: ``in-process``, ...). It is open (a new transport needs no core edit and is
#: not a closed enum), but it is still an identifier -- lowercase letters, digits
#: and ``._-`` -- never free text that could carry code or a path.
_TRANSPORT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

#: A credential reference is an *identifier*, never the secret value. It is
#: written into the descriptor digest and into receipts, and it is the key the
#: admission gate resolves availability against, so it must not be a
#: secret-looking literal (``sk-live-...``) or an environment-variable-shaped
#: value masquerading as an id. Lowercase is the wire shape; the available
#: alphabet tolerates the uppercase env-style ids the fixtures use.
_CREDENTIAL_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: Issuing systems prefix credential *values* with recognizable tokens. A
#: descriptor names a reference -- a stable key the gate resolves -- never a
#: value, so a ref carrying a known secret prefix is refused: it is a leaked
#: secret wearing a handle's clothes, and would otherwise flow into the
#: descriptor digest and every receipt's provenance. Matched case-insensitively.
_SECRET_LIKE_PREFIXES: tuple[str, ...] = (
    "sk-",
    "pk-",
    "rk-",
    "xox",
    "ghp_",
    "gho_",
    "ghs_",
    "ghr_",
    "github_pat_",
    "glpat-",
    "akia",
    "asia",
    "ya29.",
    "bearer ",
)

#: Upper bounds: an identifier/ref/token is a name, not a payload. Generous
#: enough for a long, namespaced id; small enough that a blob cannot pose as one.
_MAX_IDENTIFIER_LENGTH = 128


class AdapterDescriptorError(ValueError):
    """A descriptor field is missing, malformed, or outside a closed set.

    ``asset`` names the field that failed so the refusal is attributable rather
    than a bare "invalid descriptor".
    """

    def __init__(self, message: str, *, asset: Optional[str] = None) -> None:
        super().__init__(message)
        self.asset = asset


@dataclass(frozen=True)
class OperationBinding:
    """One operation an adapter serves, bound to its own domain contracts.

    The facade resolves by ``category + operation name + capability``; it never
    invents an operation. ``input_contract`` / ``result_contract`` are opaque
    contract references (typically SDK ``$id`` URIs or short form such as
    ``search-outcome/v1``); the facade composes the domain contract rather than
    replacing it.
    """

    name: str
    input_contract: str
    result_contract: str
    required_capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise AdapterDescriptorError(
                "an operation needs a non-empty name", asset="operations.name"
            )
        if not isinstance(self.input_contract, str) or not self.input_contract.strip():
            raise AdapterDescriptorError(
                f"operation {self.name!r} needs a non-empty input_contract",
                asset="operations.input_contract",
            )
        if not isinstance(self.result_contract, str) or not self.result_contract.strip():
            raise AdapterDescriptorError(
                f"operation {self.name!r} needs a non-empty result_contract",
                asset="operations.result_contract",
            )
        caps = tuple(str(c) for c in self.required_capabilities)
        if len(set(caps)) != len(caps):
            raise AdapterDescriptorError(
                f"operation {self.name!r} has duplicate required_capabilities",
                asset="operations.required_capabilities",
            )
        object.__setattr__(self, "required_capabilities", caps)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "input_contract": self.input_contract,
            "result_contract": self.result_contract,
        }
        if self.required_capabilities:
            payload["required_capabilities"] = list(self.required_capabilities)
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OperationBinding":
        if not isinstance(data, Mapping):
            raise AdapterDescriptorError(
                "an operation binding must be a mapping", asset="operations"
            )
        return cls(
            name=str(data.get("name", "")),
            input_contract=str(data.get("input_contract", "")),
            result_contract=str(data.get("result_contract", "")),
            required_capabilities=tuple(data.get("required_capabilities", ()) or ()),
        )


@dataclass(frozen=True)
class PackageRef:
    """Opaque provenance for a descriptor produced by a package-format reader.

    This is exactly the place where a reader-specific ``kind`` is quarantined.
    ``kind`` is a **free string**, never an enum: the common facade must not
    branch on a format-specific vocabulary. A ``container`` ref must name the
    admitted ``member_ref`` it binds -- a container with no member has nothing
    to load and is refused.
    """

    format: str
    format_version: str
    name: str
    version: str
    digest: str
    kind: Optional[str] = None
    schema_ref: Optional[str] = None
    container: Optional[bool] = None
    member_ref: Optional[str] = None

    def __post_init__(self) -> None:
        for field_name in ("format", "format_version", "name", "version", "digest"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise AdapterDescriptorError(
                    f"package_ref.{field_name} must be a non-empty string",
                    asset=f"package_ref.{field_name}",
                )
        if len(self.digest) != 64 or any(
            ch not in "0123456789abcdef" for ch in self.digest
        ):
            raise AdapterDescriptorError(
                "package_ref.digest must be a lowercase sha256 hex string",
                asset="package_ref.digest",
            )
        if self.kind is not None and (
            not isinstance(self.kind, str) or not self.kind.strip()
        ):
            raise AdapterDescriptorError(
                "package_ref.kind must be an opaque non-empty string when present",
                asset="package_ref.kind",
            )
        if self.container is True and not (self.member_ref or "").strip():
            raise AdapterDescriptorError(
                "a container-root package_ref must name the admitted member_ref",
                asset="package_ref.member_ref",
            )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "format": self.format,
            "format_version": self.format_version,
            "name": self.name,
            "version": self.version,
            "digest": self.digest,
        }
        if self.schema_ref is not None:
            payload["schema_ref"] = self.schema_ref
        if self.kind is not None:
            payload["kind"] = self.kind
        if self.container is not None:
            payload["container"] = self.container
        if self.member_ref is not None:
            payload["member_ref"] = self.member_ref
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PackageRef":
        if not isinstance(data, Mapping):
            raise AdapterDescriptorError(
                "package_ref must be a mapping", asset="package_ref"
            )
        return cls(
            format=str(data.get("format", "")),
            format_version=str(data.get("format_version", "")),
            name=str(data.get("name", "")),
            version=str(data.get("version", "")),
            digest=str(data.get("digest", "")),
            kind=data.get("kind"),
            schema_ref=data.get("schema_ref"),
            container=data.get("container"),
            member_ref=data.get("member_ref"),
        )


@dataclass(frozen=True)
class AdapterDescriptor:
    """The neutral, format-independent descriptor of one adapter.

    ``package_ref`` is ``None`` for an explicitly-registered local adapter (no
    package-format reader involved) and a :class:`PackageRef` when a reader
    produced the descriptor. Either way the facade sees the same category /
    operation / transport fields and treats the adapter identically.
    """

    id: str
    version: str
    categories: tuple[str, ...]
    operations: tuple[OperationBinding, ...]
    transport: str
    capabilities: tuple[str, ...] = ()
    credential_refs: tuple[str, ...] = ()
    package_ref: Optional[PackageRef] = None
    title: Optional[str] = None
    schema_version: str = ADAPTER_DESCRIPTOR_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != ADAPTER_DESCRIPTOR_SCHEMA_VERSION:
            raise AdapterDescriptorError(
                f"unsupported descriptor schema_version {self.schema_version!r}; "
                f"expected {ADAPTER_DESCRIPTOR_SCHEMA_VERSION!r}",
                asset="schema_version",
            )
        if not isinstance(self.id, str) or not self.id.strip():
            raise AdapterDescriptorError("descriptor id must be non-empty", asset="id")
        if len(self.id) > _MAX_IDENTIFIER_LENGTH or not _ADAPTER_ID_RE.match(self.id):
            raise AdapterDescriptorError(
                f"descriptor id {self.id!r} must be a plain identifier matching "
                f"{_ADAPTER_ID_RE.pattern} and at most {_MAX_IDENTIFIER_LENGTH} "
                "characters; a path, a separator or a code fragment is not an id",
                asset="id",
            )
        if not isinstance(self.version, str) or not self.version.strip():
            raise AdapterDescriptorError(
                "descriptor version must be non-empty", asset="version"
            )
        cats = tuple(self.categories)
        if not cats:
            raise AdapterDescriptorError(
                "a descriptor must register at least one category", asset="categories"
            )
        if len(set(cats)) != len(cats):
            raise AdapterDescriptorError(
                "descriptor categories contain duplicates", asset="categories"
            )
        for cat in cats:
            if cat not in ADAPTER_CATEGORIES:
                raise AdapterDescriptorError(
                    f"unknown category {cat!r}; the v1 set is closed: "
                    f"{list(ADAPTER_CATEGORIES)}",
                    asset="categories",
                )
        ops = tuple(self.operations)
        if not ops:
            raise AdapterDescriptorError(
                "a descriptor must serve at least one operation", asset="operations"
            )
        names = [op.name for op in ops]
        if len(set(names)) != len(names):
            raise AdapterDescriptorError(
                "descriptor operation names contain duplicates", asset="operations"
            )
        if not isinstance(self.transport, str) or not self.transport.strip():
            raise AdapterDescriptorError(
                "descriptor transport must be a non-empty string", asset="transport"
            )
        if (
            len(self.transport) > _MAX_IDENTIFIER_LENGTH
            or not _TRANSPORT_RE.match(self.transport)
        ):
            raise AdapterDescriptorError(
                f"descriptor transport {self.transport!r} must be a binding token "
                f"matching {_TRANSPORT_RE.pattern} and at most "
                f"{_MAX_IDENTIFIER_LENGTH} characters; free text (a path or a code "
                "fragment) is refused",
                asset="transport",
            )
        caps = tuple(str(c) for c in self.capabilities)
        if len(set(caps)) != len(caps):
            raise AdapterDescriptorError(
                "descriptor capabilities contain duplicates", asset="capabilities"
            )
        refs = tuple(str(r) for r in self.credential_refs)
        if len(set(refs)) != len(refs):
            raise AdapterDescriptorError(
                "descriptor credential_refs contain duplicates",
                asset="credential_refs",
            )
        for ref in refs:
            if len(ref) > _MAX_IDENTIFIER_LENGTH or not _CREDENTIAL_REF_RE.match(ref):
                raise AdapterDescriptorError(
                    f"credential reference {ref!r} must be a plain reference id "
                    f"matching {_CREDENTIAL_REF_RE.pattern} and at most "
                    f"{_MAX_IDENTIFIER_LENGTH} characters; a descriptor names a "
                    "reference, never the credential value itself",
                    asset="credential_refs",
                )
            lowered = ref.lower()
            if any(lowered.startswith(prefix) for prefix in _SECRET_LIKE_PREFIXES):
                raise AdapterDescriptorError(
                    f"credential reference {ref!r} looks like a secret value "
                    "(it carries a known credential-value prefix); a descriptor "
                    "names a reference, never the credential value itself",
                    asset="credential_refs",
                )
        object.__setattr__(self, "categories", cats)
        object.__setattr__(self, "operations", ops)
        object.__setattr__(self, "capabilities", caps)
        object.__setattr__(self, "credential_refs", refs)

    # -- construction ------------------------------------------------------

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AdapterDescriptor":
        """Build from a raw mapping, failing closed on any malformed field.

        A malformed payload never partially loads: an unknown category, a
        missing contract ref, or a container ref with no member raises here.
        """
        if not isinstance(data, Mapping):
            raise AdapterDescriptorError(
                "a descriptor must be a mapping", asset="descriptor"
            )
        package_ref = data.get("package_ref")
        return cls(
            id=str(data.get("id", "")),
            version=str(data.get("version", "")),
            categories=tuple(str(c) for c in (data.get("categories") or ())),
            operations=tuple(
                OperationBinding.from_dict(op) for op in (data.get("operations") or ())
            ),
            transport=str(data.get("transport", "")),
            capabilities=tuple(str(c) for c in (data.get("capabilities") or ())),
            credential_refs=tuple(str(r) for r in (data.get("credential_refs") or ())),
            package_ref=(
                None if package_ref is None else PackageRef.from_dict(package_ref)
            ),
            title=data.get("title"),
            schema_version=str(
                data.get("schema_version", ADAPTER_DESCRIPTOR_SCHEMA_VERSION)
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "id": self.id,
            "version": self.version,
            "categories": list(self.categories),
            "operations": [op.to_dict() for op in self.operations],
            "transport": self.transport,
            "capabilities": list(self.capabilities),
        }
        if self.title is not None:
            payload["title"] = self.title
        if self.credential_refs:
            payload["credential_refs"] = list(self.credential_refs)
        if self.package_ref is not None:
            payload["package_ref"] = self.package_ref.to_dict()
        return payload

    # -- lookups -----------------------------------------------------------

    def serves(self, category: str, operation: str) -> bool:
        return category in self.categories and any(
            op.name == operation for op in self.operations
        )

    def operation(self, name: str) -> OperationBinding:
        for op in self.operations:
            if op.name == name:
                return op
        raise AdapterDescriptorError(
            f"descriptor {self.id!r} does not serve operation {name!r}",
            asset="operations",
        )

    def holds(self, *capabilities: str) -> bool:
        held = set(self.capabilities)
        return all(cap in held for cap in capabilities)
