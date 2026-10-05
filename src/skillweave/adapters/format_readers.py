"""Package-format readers producing neutral, consumer-owned adapter bindings.

A *reader* turns a package-format manifest into the neutral
:class:`~skillweave.adapters.descriptor.AdapterDescriptor` the common facade
already understands. It is **not** runtime authority: the facade never branches
on the format, and reading a manifest proves format conformance, never
callability or distribution. Two independent sources can produce *equivalent*
bindings:

* :class:`LocalRegistrationReader` -- an explicitly-registered local adapter
  (no package format involved), whose ``package_ref`` is ``None``;
* :class:`CapaciumFormatReader` -- the optional, versioned Capacium package-format
  reader, whose ``package_ref`` carries Capacium's own opaque ``kind`` and the
  pinned dialect version and the admitted member.

Because both produce the same category / operation / transport / capability
fields, they register through the **same** runtime port and are indistinguishable
to the facade except for provenance. Capacium's CLI or service is never invoked.

New formats plug in through :class:`ReaderRegistry` -- an extension seam with no
enumeration of dialects in the facade.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from .descriptor import (
    AdapterDescriptor,
    AdapterDescriptorError,
    OperationBinding,
    PackageRef,
)
from .registry import AdapterBackend, AdapterError, AdapterRegistry

__all__ = [
    "FormatReaderError",
    "UnsupportedDialectError",
    "UnadmittedMemberError",
    "AdapterBinding",
    "PackageFormatReader",
    "LocalRegistrationReader",
    "CapaciumFormatReader",
    "ReaderRegistry",
    "LOCAL_DIALECT",
    "CAPACIUM_DIALECT",
    "CAPACIUM_DIALECT_VERSION",
]

#: The dialect name for an explicitly-registered local adapter (no package
#: format). It is a reserved name; no package may claim it.
LOCAL_DIALECT = "local"

#: The Capacium package-format dialect and the single dialect version this
#: reader admits. A manifest naming any other version fails closed rather than
#: being best-effort parsed.
CAPACIUM_DIALECT = "capacium"
CAPACIUM_DIALECT_VERSION = "1"


class FormatReaderError(ValueError):
    """A manifest is malformed or names an unsupported fact."""

    def __init__(self, message: str, *, asset: Optional[str] = None) -> None:
        super().__init__(message)
        self.asset = asset


class UnsupportedDialectError(FormatReaderError):
    """A manifest names a dialect/version this reader does not admit."""


class UnadmittedMemberError(FormatReaderError):
    """A container-member manifest names a member that is not admitted."""


@dataclass(frozen=True)
class AdapterBinding:
    """A consumer-owned binding: a neutral descriptor plus its provenance.

    ``dialect`` records which reader produced it (``"local"`` or ``"capacium"``),
    and ``admitted_members`` names the members that were explicitly admitted.
    The descriptor is the *same shape* either way -- the facade cannot tell a
    local registration from a Capacium one except through ``package_ref``.
    """

    descriptor: AdapterDescriptor
    dialect: str
    admitted_members: tuple[str, ...] = ()

    def register_into(
        self,
        registry: AdapterRegistry,
        backend: AdapterBackend,
        *,
        source: Optional[str] = None,
    ) -> None:
        """Register this binding into the one runtime port (the registry)."""
        registry.register(
            self.descriptor,
            backend,
            source=source or f"reader:{self.dialect}",
        )


class PackageFormatReader(ABC):
    """The seam a package-format reader implements."""

    #: The dialect name this reader admits.
    dialect: str = ""

    @abstractmethod
    def read(self, manifest: Mapping[str, Any], *, admitted_members: Sequence[str] = ()) -> AdapterBinding:
        """Produce a consumer-owned :class:`AdapterBinding`.

        ``admitted_members`` is the consumer's explicit admission list; a
        container manifest whose named member is not admitted raises rather than
        loading an unapproved member.
        """


def _operation_payload(member: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    ops = member.get("operations")
    if not isinstance(ops, Sequence) or isinstance(ops, (str, bytes)) or not ops:
        raise FormatReaderError(
            "a member must declare at least one operation", asset="member.operations"
        )
    for op in ops:
        if not isinstance(op, Mapping):
            raise FormatReaderError(
                "each operation must be a mapping", asset="member.operations"
            )
    return list(ops)


def _descriptor_fields(member: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(member.get("id", "")),
        "version": str(member.get("version", "1.0.0")),
        "categories": tuple(str(c) for c in (member.get("categories") or ())),
        "operations": _operation_payload(member),
        "transport": str(member.get("transport", "")),
        "capabilities": tuple(str(c) for c in (member.get("capabilities") or ())),
        "credential_refs": tuple(str(r) for r in (member.get("credential_refs") or ())),
        "title": member.get("title"),
    }


class LocalRegistrationReader(PackageFormatReader):
    """Reads an explicitly-registered local adapter (no package format).

    The manifest *is* the neutral descriptor body; ``package_ref`` stays
    ``None``. This is the zero-format path an adopter uses for a local tool.
    """

    dialect = LOCAL_DIALECT

    def read(self, manifest: Mapping[str, Any], *, admitted_members: Sequence[str] = ()) -> AdapterBinding:
        if not isinstance(manifest, Mapping):
            raise FormatReaderError("a local registration must be a mapping", asset="manifest")
        member = manifest.get("member", manifest)
        if not isinstance(member, Mapping):
            raise FormatReaderError("local registration member must be a mapping", asset="member")
        descriptor = AdapterDescriptor.from_dict(_descriptor_fields(member))
        return AdapterBinding(descriptor=descriptor, dialect=self.dialect)


class CapaciumFormatReader(PackageFormatReader):
    """The optional, versioned Capacium package-format reader.

    It reuses the package's identity/entrypoint/interface metadata to produce
    the *neutral* descriptor, quarantining the format's ``kind`` inside
    ``package_ref`` (a free, opaque string -- never an enum the facade reads).
    No Capacium CLI or service is invoked; the manifest is read from bytes.
    """

    dialect = CAPACIUM_DIALECT

    def __init__(self, dialect_version: str = CAPACIUM_DIALECT_VERSION) -> None:
        self.dialect_version = dialect_version

    def read(self, manifest: Mapping[str, Any], *, admitted_members: Sequence[str] = ()) -> AdapterBinding:
        if not isinstance(manifest, Mapping):
            raise FormatReaderError("a capacium manifest must be a mapping", asset="manifest")
        if manifest.get("dialect") != CAPACIUM_DIALECT:
            raise UnsupportedDialectError(
                f"not a {CAPACIUM_DIALECT!r} manifest: dialect="
                f"{manifest.get('dialect')!r}",
                asset="dialect",
            )
        version = str(manifest.get("dialect_version", ""))
        if version != self.dialect_version:
            raise UnsupportedDialectError(
                f"unsupported capacium dialect_version {version!r}; this reader "
                f"admits {self.dialect_version!r}",
                asset="dialect_version",
            )
        package = manifest.get("package")
        if not isinstance(package, Mapping):
            raise FormatReaderError("capacium manifest needs a package block", asset="package")
        members = manifest.get("members")
        if not isinstance(members, Sequence) or isinstance(members, (str, bytes)):
            raise FormatReaderError("capacium manifest needs a members list", asset="members")

        container = bool(package.get("container", False))
        member_ref = package.get("member_ref")
        admitted = tuple(str(m) for m in admitted_members)

        if container and not member_ref:
            raise UnadmittedMemberError(
                "a container package must name the admitted member_ref",
                asset="package.member_ref",
            )

        chosen: Optional[Mapping[str, Any]] = None
        wanted_name = str(member_ref) if member_ref else None
        for member in members:
            if not isinstance(member, Mapping):
                continue
            name = str(member.get("name", ""))
            if wanted_name is not None:
                if name == wanted_name:
                    chosen = member
                    break
            elif len(members) == 1:
                chosen = member
        if chosen is None:
            target = f"member_ref {wanted_name!r}" if wanted_name else "the manifest"
            raise UnadmittedMemberError(
                f"no admitted member matched {target}",
                asset="members",
            )

        chosen_name = str(chosen.get("name", ""))
        if admitted and chosen_name not in admitted:
            raise UnadmittedMemberError(
                f"member {chosen_name!r} is not on the admitted member list "
                f"{list(admitted)}",
                asset="members",
            )

        package_ref = PackageRef(
            format=str(manifest.get("dialect")),
            format_version=version,
            name=str(package.get("name", "")),
            version=str(package.get("version", "")),
            digest=str(package.get("digest", "")),
            kind=(str(chosen["kind"]) if chosen.get("kind") is not None else None),
            schema_ref=(str(chosen["interface"]) if chosen.get("interface") is not None else None),
            container=container or None,
            member_ref=chosen_name,
        )
        descriptor = AdapterDescriptor.from_dict(
            {**_descriptor_fields(chosen), "package_ref": package_ref.to_dict()}
        )
        return AdapterBinding(
            descriptor=descriptor, dialect=self.dialect, admitted_members=admitted
        )


class ReaderRegistry:
    """The extension seam for package-format readers.

    A new format registers its reader here; the facade itself never learns the
    dialect name. Reading by an unknown dialect fails closed.
    """

    def __init__(self) -> None:
        self._readers: dict[str, PackageFormatReader] = {}

    def register(self, reader: PackageFormatReader) -> None:
        if not isinstance(reader, PackageFormatReader):
            raise AdapterError("register() requires a PackageFormatReader", asset="reader")
        dialect = reader.dialect
        if not dialect:
            raise FormatReaderError("a reader must declare a dialect", asset="dialect")
        if dialect in self._readers:
            raise FormatReaderError(
                f"a reader for dialect {dialect!r} is already registered",
                asset="dialect",
            )
        self._readers[dialect] = reader

    def get(self, dialect: str) -> PackageFormatReader:
        reader = self._readers.get(dialect)
        if reader is None:
            raise UnsupportedDialectError(
                f"no reader registered for dialect {dialect!r}; "
                f"registered: {sorted(self._readers)}",
                asset="dialect",
            )
        return reader

    def dialects(self) -> tuple[str, ...]:
        return tuple(self._readers)


def default_reader_registry() -> ReaderRegistry:
    """The one reader of each admitted dialect: local and Capacium."""
    registry = ReaderRegistry()
    registry.register(LocalRegistrationReader())
    registry.register(CapaciumFormatReader())
    return registry
