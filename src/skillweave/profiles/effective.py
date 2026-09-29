"""Immutable effective-profile snapshot (SW1312-PROFILE-RESOLVE-001).

This module owns the *resolution* of explicit profile sources into one
content-addressed, fully explained, immutable snapshot that a run may pin.
It is the single consumer-side seam between the SDK profile contract and the
promptchain/dispatch consumers (SW1312-CHAIN-001); nothing else under
``skillweave/`` may re-resolve a profile.

The canonical precedence chain, exactly as the PRD orders it::

    run override > project profile > organization profile
        > domain pack > category pack > core defaults

A source is any *typed preview artifact* (a WorkProfile, LifecycleProfile,
deliverable/evidence contract, or a pack) together with its provenance: a
source id, a source version, and the SDK schema version/digest it was authored
against. The resolver walks the chain from the strongest source (run override)
down to the weakest (core defaults), merging overlay key-by-key. Nothing is
silently dropped: every key that a source declares is either (a) carried into
the snapshot at its winning precedence, (b) merged merge-by-merge, or (c)
refused as a *non-mergeable conflict* naming the exact source paths.

The result is a :class:`EffectiveProfileSnapshot`:

* ``resolved`` — the merged effective content, with no hidden default: every
  field in the effective output is attributable to exactly one winning source.
* ``provenance`` — per-key source id / source version for every inherited
  value, so "why is this value here" is answerable without re-reading a file.
* ``sources`` — the ordered, complete list of every source that contributed,
  each with its id, version, schema version and schema digest, making the
  input set itself part of the snapshot.
* ``sdk`` — the pinned SDK schema version and digest under which the snapshot
  is bound.
* ``preview_dimensions`` — every preview-only runtime dimension carried as a
  *declaration* (ordered phases, K0-K6 mappings, topology, control, human
  coupling, change surfaces, autonomy bounds, provider capability names).
  These are preserved verbatim and reported; they are **not** executed.
  Requesting execution of an unsupported dimension fails explicitly.

The snapshot digest is content-addressed over canonical bytes of the resolved
content in source order, so identical ordered inputs produce byte-identical
snapshots and the same digest — and a source changed *after* the snapshot is
built cannot alter it, because the resolver copies data in, it never holds a
live reference.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

# ── SDK contract pinning ───────────────────────────────────────────────────
#
# The SDK (skillweave-sdk) is contract authority. It owns the preview schema
# version and the canonical schema digest; this module imports those values
# from the installed SDK rather than hardcoding unpublished bytes. A source
# that diverges from the canonical SDK is refused — never a silent drift.

import skillweave_sdk
import skillweave_sdk.validator as _sdk_validator

#: The SDK preview schema version this resolver binds to.
SDK_PREVIEW_SCHEMA_VERSION = skillweave_sdk.SCHEMA_VERSION

#: The canonical SDK schema digest every source must have been authored
#: against. A source that claims this version but was signed against different
#: bytes (same version, different bytes) is refused, not trusted.
SDK_EXPECTED_SCHEMA_DIGEST = _sdk_validator.EXPECTED_SCHEMA_DIGEST

#: The six source kinds in precedence order (strongest first).
SOURCE_KINDS = (
    "run_override",
    "project_profile",
    "organization_profile",
    "domain_pack",
    "category_pack",
    "core_defaults",
)


class EffectiveProfileError(ValueError):
    """The effective profile could not be resolved to an immutable snapshot."""


class SchemaBindingError(EffectiveProfileError):
    """A source bound to an incompatible SDK schema version or digest."""


class ConflictError(EffectiveProfileError):
    """Two sources set the same key to differing non-mergeable values.

    Carries the exact source paths so the conflict is attributable, never a
    silent prefer-the-stronger-resolution.
    """

    def __init__(
        self,
        key: str,
        higher: str,
        higher_value: Any,
        lower: str,
        lower_value: Any,
    ):
        self.key = key
        self.higher = higher
        self.higher_value = higher_value
        self.lower = lower
        self.lower_value = lower_value
        super().__init__(
            f"non-mergeable conflict on '{key}': {higher} set "
            f"{higher_value!r} but {lower} set {lower_value!r}"
        )


class PreviewExecutionError(EffectiveProfileError):
    """A caller requested execution of a preview-only (declaration-only) dimension."""


# ── Source model ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProfileSource:
    """One typed preview artifact plus its provenance.

    ``content`` is the raw mapping/declaration image the source owns (a
    WorkProfile, LifecycleProfile, deliverable/evidence contract, or pack).
    ``kind`` is one of :data:`SOURCE_KINDS`. ``source_id`` is stable
    (profile/pack id or profileId), ``source_version`` is the immutable
    artifact version, and ``schema_version``/``schema_digest`` record what the
    source was authored against.
    """

    kind: str
    source_id: str
    source_version: str
    schema_version: str
    schema_digest: str
    content: Mapping[str, Any]

    @classmethod
    def from_dict(cls, spec: Mapping[str, Any]) -> "ProfileSource":
        kind = spec.get("kind")
        if kind not in SOURCE_KINDS:
            raise EffectiveProfileError(
                f"unknown source kind {kind!r}; expected one of {SOURCE_KINDS}"
            )
        source_id = spec.get("source_id") or spec.get("id") or spec.get("profileId")
        if not source_id:
            raise EffectiveProfileError(f"source kind {kind!r} requires 'source_id'/'id'")
        source_version = spec.get("source_version") or spec.get("version")
        if not source_version:
            raise EffectiveProfileError(
                f"source '{source_id}' requires 'source_version'/'version'"
            )
        schema_version = spec.get("schemaVersion") or spec.get("schema_version")
        schema_digest = spec.get("schemaDigest") or spec.get("schema_digest")
        content = spec.get("content")
        if not isinstance(content, Mapping):
            raise EffectiveProfileError(
                f"source '{source_id}' requires a mapping 'content'"
            )
        return cls(
            kind=kind,
            source_id=str(source_id),
            source_version=str(source_version),
            schema_version=str(schema_version) if schema_version else "",
            schema_digest=str(schema_digest) if schema_digest else "",
            content=dict(content),
        )

    @property
    def precedence(self) -> int:
        # Strong source == lower index == higher precedence.
        return SOURCE_KINDS.index(self.kind)

    def path(self) -> str:
        """A human-readable, exact provenance path for this source."""
        return f"{self.kind}/{self.source_id}@{self.source_version}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source_id": self.source_id,
            "source_version": self.source_version,
            "schema_version": self.schema_version,
            "schema_digest": self.schema_digest,
        }


# ── Canonical bytes for determinism ────────────────────────────────────────


def _canonical_bytes(data: Any) -> bytes:
    """Deterministic, order-independent serialisation for hashing.

    Mirrors ``trace.contracts._canonical_bytes``: sorted keys, non-lossy JSON,
    compact separators, so two structurally equal snapshots hash identically.
    """
    if isinstance(data, bytes):
        return data
    if isinstance(data, str):
        return data.encode("utf-8")
    return json.dumps(
        data, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def _sha256_hex(data: Any) -> str:
    return hashlib.sha256(_canonical_bytes(data)).hexdigest()


def canonical_json_bytes(content: Mapping[str, Any]) -> bytes:
    """The canonical byte image of a resolved effective profile.

    Byte-identical for structurally equal content; the snapshot's *content
    digest* is the sha256 of these bytes.
    """
    return _canonical_bytes(content)


def content_digest(content: Mapping[str, Any]) -> str:
    """The content digest of a resolved profile (criterion 3/4)."""
    return _sha256_hex(content)


# ── Preview-only (declaration) dimensions ──────────────────────────────────

# The runtime dimensions the preview contract *declares* but does not execute
# in 1.3.12. They are preserved as declarations and reported; requesting their
# execution fails explicitly (criterion 7). The set here matches the
# WorkProfile/LifecycleProfile schema fields that describe runtime behavior.
PREVIEW_DIMENSIONS = (
    "phases",
    "kernel_stage",
    "topology",
    "control",
    "human_coupling",
    "humanCoupling",
    "change_surfaces",
    "changeSurfaces",
    "autonomy_bounds",
    "autonomyBounds",
    "skills",
    "skillComposition",
    "capabilities",
    "capabilityComposition",
)


def preview_dimensions_of(content: Mapping[str, Any]) -> dict[str, Any]:
    """Extract the preview-only declaration dimensions carried by ``content``."""
    out: dict[str, Any] = {}
    for key in PREVIEW_DIMENSIONS:
        if key in content:
            out[key] = content[key]
    return out


def _canonical_schema_digest(schema_set: Optional[Sequence[tuple[str, str]]]) -> str:
    """Compute the digest a caller supplies for a schema fileset.

    ``schema_set`` is an optional sequence of ``(filename, canonical_json)``;
    when empty, the resolver falls back to the pinned ``SDK_EXPECTED_SCHEMA_DIGEST``.
    This keeps the resolver testable against the real SDK canonical digest
    while remaining importable without the SDK package present.
    """
    if schema_set:
        hasher = hashlib.sha256()
        for filename, canonical_json in sorted(schema_set):
            hasher.update(filename.encode("utf-8"))
            hasher.update(b"\x00")
            payload = (
                canonical_json.encode("utf-8")
                if isinstance(canonical_json, str)
                else canonical_json
            )
            hasher.update(payload)
            hasher.update(b"\x00")
        return hasher.hexdigest()
    return SDK_EXPECTED_SCHEMA_DIGEST


# ── Resolution ─────────────────────────────────────────────────────────────


def _validate_source_binding(source: ProfileSource, bound_digest: str) -> None:
    """Refuse an incompatible or same-version-different-bytes source.

    Criterion 3: a source must pin the SDK schema version this resolver binds,
    and its digest must equal the canonical digest for that version. A source
    that claims the right version but was signed against different bytes is
    refused, not silently trusted.
    """
    if source.schema_version and source.schema_version != SDK_PREVIEW_SCHEMA_VERSION:
        raise SchemaBindingError(
            f"source '{source.path()}' bound to schema version "
            f"{source.schema_version!r}, expected {SDK_PREVIEW_SCHEMA_VERSION!r}"
        )
    if source.schema_digest and source.schema_digest != bound_digest:
        raise SchemaBindingError(
            f"source '{source.path()}' bound to schema digest "
            f"{source.schema_digest[:16]}..., expected {bound_digest[:16]}... "
            f"(same version, different bytes)"
        )


def _record_provenance(
    provenance: dict[str, ProfileSource],
    path: str,
    value: Any,
    source: ProfileSource,
) -> None:
    """Record ``source`` as the winning owner of ``path`` and every nested leaf.

    When the first writer contributes a nested mapping, it owns both the parent
    key and each nested leaf. Recording leaf provenance up front means a later
    same-precedence source that diverges on a nested scalar finds a real winner
    to conflict with, and a weaker nested override stays attributable in the
    snapshot instead of being silently dropped.
    """
    provenance[path] = source
    if isinstance(value, dict):
        for sub_key, sub_value in value.items():
            _record_provenance(provenance, f"{path}.{sub_key}", sub_value, source)


def resolve_effective_profile(
    sources: Sequence[Mapping[str, Any]],
    *,
    schema_set: Optional[Sequence[tuple[str, str]]] = None,
) -> "EffectiveProfileSnapshot":
    """Resolve an ordered set of profile sources into an immutable snapshot.

    ``sources`` are provider-neutral typed artifacts plus provenance, ordered
    from strongest to weakest. Order is significant: for a *mergeable* overlay
    the stronger source's value wins; for a non-mergeable conflict, resolution
    fails naming both source paths rather than silently preferring the
    stronger one.

    ``schema_set`` is an optional injection of ``(filename, canonical json)``
    used to compute the bound SDK digest in tests; omit it to bind the pinned
    canonical digest.
    """
    if not sources:
        raise EffectiveProfileError("at least one profile source is required")

    parsed = [s if isinstance(s, ProfileSource) else ProfileSource.from_dict(s) for s in sources]

    # A later (weaker) source must never out-rank an earlier (stronger) one.
    seen_precedence: Optional[int] = None
    for source in parsed:
        if seen_precedence is not None and source.precedence < seen_precedence:
            raise EffectiveProfileError(
                f"sources must be ordered strongest first: "
                f"{source.path()} out-ranks the source before it"
            )
        seen_precedence = source.precedence

    bound_digest = _canonical_schema_digest(schema_set)
    for source in parsed:
        _validate_source_binding(source, bound_digest)

    resolved: dict[str, Any] = {}
    provenance: dict[str, ProfileSource] = {}
    overrides: list[dict[str, Any]] = []

    for source in parsed:
        for key, value in source.content.items():
            if key not in resolved:
                resolved[key] = copy.deepcopy(value)
                _record_provenance(provenance, key, value, source)
                continue
            # Key already set by a stronger (earlier) source. Overlay rules:
            # dicts merge recursively; a differing non-mergeable value in a
            # *weaker* source is recorded as a visible override (higher wins),
            # and a differing non-mergeable value in a source of the *same*
            # precedence fails closed (no order exists between them).
            _overlay(resolved, provenance, overrides, key, value, source)

    return EffectiveProfileSnapshot(
        resolved=resolved,
        provenance=provenance,
        sources=parsed,
        overrides=overrides,
        sdk_version=SDK_PREVIEW_SCHEMA_VERSION,
        sdk_digest=bound_digest,
    )


def _overlay(
    resolved: dict[str, Any],
    provenance: dict[str, ProfileSource],
    overrides: list[dict[str, Any]],
    key: str,
    value: Any,
    source: ProfileSource,
    path: Optional[str] = None,
) -> None:
    """Apply one key from ``source`` onto the already-resolved value.

    ``key`` is the leaf key inside ``resolved`` (used for lookup); ``path`` is
    the compound dotted path (defaulting to ``key`` at the top level) used for
    provenance/override/conflict attribution, so nested wins stay attributable.
    Dict keys merge recursively; a non-mergeable value differing in a *weaker*
    source is recorded as a visible override (higher wins), while one differing
    in a source of the *same* precedence fails naming both exact paths.
    """
    path = path if path is not None else key
    existing = resolved[key]
    winner = provenance.get(path)

    if isinstance(existing, dict) and isinstance(value, dict):
        for sub_key, sub_value in value.items():
            sub_path = f"{path}.{sub_key}"
            if sub_key not in existing:
                existing[sub_key] = copy.deepcopy(sub_value)
                _record_provenance(provenance, sub_path, sub_value, source)
            else:
                _overlay(
                    existing,
                    provenance,
                    overrides,
                    key=sub_key,
                    value=sub_value,
                    source=source,
                    path=sub_path,
                )
        return

    if existing == value:
        return

    if winner is not None and winner.precedence == source.precedence:
        raise ConflictError(
            key=path,
            higher=winner.path(),
            higher_value=existing,
            lower=source.path(),
            lower_value=value,
        )

    # Stronger source (or a source winning a nested leaf fill) keeps its value;
    # record the divergence so it stays visible (criterion 2).
    if winner is not None:
        overrides.append(
            {
                "key": path,
                "winner": winner.path(),
                "winner_value": existing,
                "overridden": source.path(),
                "overridden_value": copy.deepcopy(value),
            }
        )


# ── Snapshot ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EffectiveProfileSnapshot:
    """An immutable, content-addressed effective-profile snapshot.

    ``resolved`` is the merged effective content; ``provenance`` maps each
    (possibly compound) key to the single source that won it, so no hidden
    default changes behavior unseen. ``sources`` is the complete ordered input
    set. ``sdk_version``/``sdk_digest`` pin the schema authority.
    """

    resolved: dict[str, Any]
    provenance: dict[str, ProfileSource]
    sources: list[ProfileSource]
    sdk_version: str
    sdk_digest: str
    overrides: list[dict[str, Any]] = field(default_factory=list)

    @property
    def digest(self) -> str:
        """The content digest over the resolved profile's canonical bytes."""
        return content_digest(self.resolved)

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.resolved)

    @property
    def preview_dimensions(self) -> dict[str, Any]:
        """The preview-only runtime dimensions carried as declarations."""
        return preview_dimensions_of(self.resolved)

    def preview_dimension(self, key: str) -> Any:
        """Return one declared preview dimension, or ``None`` if absent."""
        return self.preview_dimensions.get(key)

    def require_execution(self, dimension: str) -> Any:
        """Refuse execution of a preview-only runtime dimension (criterion 7).

        Preview-only dimensions exist in the snapshot only as declarations.
        A caller that asks to *run* them (rather than merely read the
        declaration) is refused here, so an unsupported runtime request fails
        explicitly instead of being ignored or silently falling back.
        """
        if dimension in PREVIEW_DIMENSIONS:
            raise PreviewExecutionError(
                f"'{dimension}' is a preview-only declaration in 1.3.12 and "
                f"cannot be executed; read it as a declaration, do not run it"
            )
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sdk_version": self.sdk_version,
            "sdk_digest": self.sdk_digest,
            "digest": self.digest,
            "sources": [s.to_dict() for s in self.sources],
            "overrides": list(self.overrides),
            "resolved": dict(self.resolved),
        }

    def to_canonical_json(self) -> str:
        """The byte-stable canonical JSON image (byte-identical for equal input)."""
        return json.dumps(
            self.to_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )


__all__ = [
    "EffectiveProfileError",
    "SchemaBindingError",
    "ConflictError",
    "PreviewExecutionError",
    "ProfileSource",
    "SOURCE_KINDS",
    "SDK_PREVIEW_SCHEMA_VERSION",
    "SDK_EXPECTED_SCHEMA_DIGEST",
    "PREVIEW_DIMENSIONS",
    "resolve_effective_profile",
    "EffectiveProfileSnapshot",
    "content_digest",
    "canonical_json_bytes",
]
