"""CMS process-qualification contract (SW-1511-CMS-FLOWS).

Five initial CMS paths must be *declared* before any of them is run: a generic
content request, a landing-page change, a content/SEO change, an
incident/rollback and a recurring maintenance. This module owns the frozen
declaration and the ``cms-process-qualification-v1`` record that an independent
scenario observer will fill in.

Three refusals carry the whole point of the contract:

* **no private runtime fork** — the four specialized paths are *capability-based
  reusable specializations* of the generic path
  (:data:`DECLARATION_SPECIALIZATION`). A declaration that calls itself a fork,
  or that stands a specialisation outside the generic path's capability
  contract, is refused with :class:`PrivateRuntimeForkRefused`. A scenario
  declaration is never allowed to become a second runtime.
* **the generic path mutates nothing** — the generic content path requires no
  source-code and no Git mutation
  (:attr:`CmsPath.requires_source_code_mutation` /
  :attr:`CmsPath.requires_git_mutation` must be ``False``). A declaration that
  reintroduces a build/code/Git step into the generic path is refused with
  :class:`GenericPathMutationRefused`.
* **no prewritten receipt** — a qualification record is either
  ``not-performed`` (the honest state until an observer actually runs the path)
  or ``performed``, and a ``performed`` claim must name the source-bound pins
  (controller, source digest, artifact digest) plus a real readback *and*
  restore. A ``performed`` claim that lacks any of them is refused with
  :class:`PrewrittenReceiptRefused` rather than downgraded silently.

The record is built fail-closed, mirroring
:class:`skillweave.blueprint.data_boundary_contract.DataContract.from_mapping`:
a missing or blank required field, an unknown key, an unknown path, a missing
or duplicated path, or a private fork are all refused by name, never defaulted.
The five required record fields are exactly the five the blueprinted
``cms-process-qualification-v1`` boundary names
(:data:`REQUIRED_QUALIFICATION_FIELDS`).

Like the boundary and trace contracts beside it, this module imports only the
standard library (GLE-020): it must never pull the ``skillweave`` runtime into a
core blueprint import.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

#: The frozen artifact id of the boundary contract this module serves.
SCHEMA_VERSION = "cms-process-qualification-v1"

#: The five required fields of the ``cms-process-qualification-v1`` record,
#: mirroring the blueprinted data-boundary contract of SW-1511-CMS-FLOWS.
REQUIRED_QUALIFICATION_FIELDS: tuple[str, ...] = (
    "artifact",
    "versioning",
    "producer",
    "consumer",
    "compatibility",
)

#: The subject kind this boundary carries. It is one of the integrated
#: WorkContract subject kinds (SW-159-WORK-001); the shipped test asserts that
#: this module and the boundary contract agree on the vocabulary member.
SUBJECT_KIND = "content"

#: The generic content path. Every specialized path declares it as its parent.
PATH_GENERIC = "cms-generic-request"
PATH_LANDING_PAGE = "cms-landing-page"
PATH_CONTENT_SEO = "cms-content-seo"
PATH_INCIDENT_ROLLBACK = "cms-incident-rollback"
PATH_RECURRING_MAINTENANCE = "cms-recurring-maintenance"

#: The five initial CMS paths, in declaration order. A record that omits,
#: duplicates or reorders them is refused; the order is part of the contract so
#: two observers cannot disagree about which path is "path 3".
PATH_IDS: tuple[str, ...] = (
    PATH_GENERIC,
    PATH_LANDING_PAGE,
    PATH_CONTENT_SEO,
    PATH_INCIDENT_ROLLBACK,
    PATH_RECURRING_MAINTENANCE,
)

#: The four specialized paths. They *specialize* the generic capability; they are
#: not separate runtimes.
SPECIALIZED_PATH_IDS: tuple[str, ...] = PATH_IDS[1:]

#: A scenario declaration that reuses the generic capability contract.
DECLARATION_SPECIALIZATION = "capability-based-reusable-specialization"
#: A scenario declaration that stands up its own runtime. Always refused.
DECLARATION_PRIVATE_FORK = "private-runtime-fork"

#: Every declaration kind the vocabulary admits. Anything else is refused as an
#: unknown declaration, not coerced into the specialization spelling.
DECLARATION_KINDS: tuple[str, ...] = (
    DECLARATION_SPECIALIZATION,
    DECLARATION_PRIVATE_FORK,
)

#: Live-run dispositions. ``not-performed`` is the honest default: an observer
#: that has not actually run the path must not claim it.
DISPOSITION_NOT_PERFORMED = "not-performed"
DISPOSITION_PERFORMED = "performed"

#: The outcomes the qualification must be able to *measure*. A path that cannot
#: report all six is not qualified; the measurement itself is the later lane's
#: job, the declaration of *what must be measurable* is this module's.
REQUIRED_OUTCOMES: tuple[str, ...] = (
    "success",
    "wait",
    "failure",
    "cancel",
    "resume",
    "publication-refusal",
)

#: The pins a ``performed`` claim must name, on top of readback and restore.
REQUIRED_PERFORMED_PINS: tuple[str, ...] = (
    "controller",
    "source_digest",
    "artifact_digest",
)


class CmsPathQualificationError(ValueError):
    """A CMS path declaration or qualification record failed validation.

    ``field`` names the offending path (for example
    ``paths[2].declaration_kind``) and ``path_id`` names the path, so a refusal
    is attributable to one path and one field rather than a bare NO. The shape
    mirrors :class:`skillweave.blueprint.data_boundary_contract.DataBoundaryContractError`.
    """

    def __init__(
        self,
        message: str,
        *,
        field: Optional[str] = None,
        path_id: Optional[str] = None,
    ):
        super().__init__(message)
        self.field = field
        self.path_id = path_id


class UnknownDeclarationKindError(CmsPathQualificationError):
    """A path declares a scenario-declaration kind outside the vocabulary."""


class PrivateRuntimeForkRefused(CmsPathQualificationError):
    """A specialized path is declared as a private runtime fork, not a reuse."""


class GenericPathMutationRefused(CmsPathQualificationError):
    """The generic content path requires a source-code or Git mutation."""


class PrewrittenReceiptRefused(CmsPathQualificationError):
    """A ``performed`` claim lacks the real readback/restore or the pins."""


class IncompleteQualificationError(CmsPathQualificationError):
    """The record does not cover exactly the five declared paths."""


class InvalidQualificationError(CmsPathQualificationError):
    """The record is malformed: wrong type, unknown key, or blank field."""


def _display(path_id: Optional[str]) -> str:
    return f"path '{path_id}'" if path_id else "path"


@dataclass(frozen=True)
class CmsPath:
    """One declared CMS path.

    ``declaration_kind`` is always :data:`DECLARATION_SPECIALIZATION`; the
    vocabulary admits :data:`DECLARATION_PRIVATE_FORK` only so that the refusal
    can name it. ``specialization_of`` is the generic path for the four
    specializations and empty for the generic path itself.

    ``requires_source_code_mutation`` and ``requires_git_mutation`` are the
    honest capability facts of the path: for the generic content path both are
    ``False`` by contract.
    """

    path_id: str
    label: str
    subject_kind: str
    specialization_of: str = ""
    declaration_kind: str = DECLARATION_SPECIALIZATION
    requires_source_code_mutation: bool = False
    requires_git_mutation: bool = False
    adapter_capabilities: tuple[str, ...] = ()
    prepared_by_operator: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "path_id": self.path_id,
            "label": self.label,
            "subject_kind": self.subject_kind,
            "specialization_of": self.specialization_of,
            "declaration_kind": self.declaration_kind,
            "requires_source_code_mutation": self.requires_source_code_mutation,
            "requires_git_mutation": self.requires_git_mutation,
            "adapter_capabilities": list(self.adapter_capabilities),
            "prepared_by_operator": self.prepared_by_operator,
        }


def _coerce_bool(raw: Any, *, field: str, path_id: str, default: bool) -> bool:
    """Read a declared boolean capability fact, refusing anything ambiguous.

    The declaration is an operator-supplied file, so a truthy string is *not*
    accepted as ``True``: a value that is not an actual ``bool`` is refused
    instead of coerced, so a path can never silently acquire (or lose) a
    mutation step through spelling.
    """
    if raw is None:
        return default
    if not isinstance(raw, bool):
        raise InvalidQualificationError(
            f"{_display(path_id)} field '{field}' must be a boolean, got "
            f"{type(raw).__name__}",
            field=field,
            path_id=path_id,
        )
    return raw


def _coerce_capabilities(raw: Any, *, path_id: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise InvalidQualificationError(
            f"{_display(path_id)} 'adapter_capabilities' must be a list, got "
            f"{type(raw).__name__}",
            field="adapter_capabilities",
            path_id=path_id,
        )
    caps: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            raise InvalidQualificationError(
                f"{_display(path_id)} capability must be a non-empty string, got "
                f"{entry!r}",
                field="adapter_capabilities",
                path_id=path_id,
            )
        caps.append(entry.strip())
    return tuple(caps)


def declaration_from_mapping(
    data: Any, *, path_id: Optional[str] = None
) -> CmsPath:
    """Build one :class:`CmsPath` from a declaration mapping, failing closed.

    Every refusal below is by name: an unknown ``declaration_kind``, a
    specialization that does not descend from the generic path, a private
    runtime fork, a generic path that mutates source or Git, and the generic
    path appearing as someone's specialization are all refused before the path
    is accepted.
    """
    if not isinstance(data, Mapping):
        raise InvalidQualificationError(
            f"{_display(path_id)} declaration must be a mapping, got "
            f"{type(data).__name__}",
            field="paths",
            path_id=path_id,
        )

    declared_id = data.get("path_id", path_id)
    if not isinstance(declared_id, str) or not declared_id.strip():
        raise InvalidQualificationError(
            f"{_display(path_id)} declaration leaves 'path_id' undefined (got "
            f"{declared_id!r})",
            field="paths.path_id",
            path_id=path_id,
        )
    declared_id = declared_id.strip()
    if declared_id not in PATH_IDS:
        raise InvalidQualificationError(
            f"{_display(declared_id)} is not one of the five declared CMS paths "
            f"{list(PATH_IDS)}",
            field="paths.path_id",
            path_id=declared_id,
        )

    label = data.get("label")
    if not isinstance(label, str) or not label.strip():
        raise InvalidQualificationError(
            f"{_display(declared_id)} declaration leaves 'label' undefined (got "
            f"{label!r})",
            field="paths.label",
            path_id=declared_id,
        )

    subject_kind = data.get("subject_kind")
    if not isinstance(subject_kind, str) or not subject_kind.strip():
        raise InvalidQualificationError(
            f"{_display(declared_id)} declaration leaves 'subject_kind' "
            f"undefined (got {subject_kind!r})",
            field="paths.subject_kind",
            path_id=declared_id,
        )
    subject_kind = subject_kind.strip()

    declaration_kind = data.get("declaration_kind", DECLARATION_SPECIALIZATION)
    if declaration_kind not in DECLARATION_KINDS:
        raise UnknownDeclarationKindError(
            f"{_display(declared_id)} declares unknown scenario-declaration kind "
            f"{declaration_kind!r}; expected one of {list(DECLARATION_KINDS)}",
            field="paths.declaration_kind",
            path_id=declared_id,
        )
    if declaration_kind == DECLARATION_PRIVATE_FORK:
        raise PrivateRuntimeForkRefused(
            f"{_display(declared_id)} is declared as a private runtime fork; a "
            f"scenario declaration must be a "
            f"{DECLARATION_SPECIALIZATION!r} of the generic path "
            f"{PATH_GENERIC!r}, never its own runtime",
            field="paths.declaration_kind",
            path_id=declared_id,
        )

    specialization_of = data.get("specialization_of", "")
    if not isinstance(specialization_of, str):
        raise InvalidQualificationError(
            f"{_display(declared_id)} 'specialization_of' must be a string, got "
            f"{type(specialization_of).__name__}",
            field="paths.specialization_of",
            path_id=declared_id,
        )
    specialization_of = specialization_of.strip()

    requires_source_code_mutation = _coerce_bool(
        data.get("requires_source_code_mutation"),
        field="requires_source_code_mutation",
        path_id=declared_id,
        default=False,
    )
    requires_git_mutation = _coerce_bool(
        data.get("requires_git_mutation"),
        field="requires_git_mutation",
        path_id=declared_id,
        default=False,
    )

    if declared_id == PATH_GENERIC:
        if specialization_of:
            raise InvalidQualificationError(
                f"{_display(declared_id)} is the generic path and cannot "
                f"specialize another path (got {specialization_of!r})",
                field="paths.specialization_of",
                path_id=declared_id,
            )
        if requires_source_code_mutation or requires_git_mutation:
            raise GenericPathMutationRefused(
                f"{_display(declared_id)} requires a source-code or Git mutation "
                f"(source_code={requires_source_code_mutation}, "
                f"git={requires_git_mutation}); the generic content path must "
                f"complete with no source-code and no Git mutation",
                field="paths.requires_git_mutation",
                path_id=declared_id,
            )
    else:
        if specialization_of != PATH_GENERIC:
            raise PrivateRuntimeForkRefused(
                f"{_display(declared_id)} specializes {specialization_of!r}; a "
                f"specialization must descend from the generic path "
                f"{PATH_GENERIC!r} so the same native controller and policy "
                f"contracts apply",
                field="paths.specialization_of",
                path_id=declared_id,
            )

    return CmsPath(
        path_id=declared_id,
        label=label.strip(),
        subject_kind=subject_kind,
        specialization_of=specialization_of,
        declaration_kind=declaration_kind,
        requires_source_code_mutation=requires_source_code_mutation,
        requires_git_mutation=requires_git_mutation,
        adapter_capabilities=_coerce_capabilities(
            data.get("adapter_capabilities"), path_id=declared_id
        ),
        prepared_by_operator=_coerce_bool(
            data.get("prepared_by_operator"),
            field="prepared_by_operator",
            path_id=declared_id,
            default=True,
        ),
    )


#: The frozen five-path declaration, built through the same fail-closed path a
#: caller-supplied declaration takes. It is a tuple so a caller cannot mutate
#: the contract in place.
INITIAL_CMS_PATHS: tuple[CmsPath, ...] = (
    declaration_from_mapping(
        {
            "path_id": PATH_GENERIC,
            "label": "Generic content request",
            "subject_kind": "content",
            "adapter_capabilities": ("content-read", "content-write"),
        }
    ),
    declaration_from_mapping(
        {
            "path_id": PATH_LANDING_PAGE,
            "label": "Landing-page change",
            "subject_kind": "content",
            "specialization_of": PATH_GENERIC,
            "adapter_capabilities": ("content-read", "content-write", "render-preview"),
        }
    ),
    declaration_from_mapping(
        {
            "path_id": PATH_CONTENT_SEO,
            "label": "Content/SEO change",
            "subject_kind": "content",
            "specialization_of": PATH_GENERIC,
            "adapter_capabilities": ("content-read", "content-write", "seo-facts"),
        }
    ),
    declaration_from_mapping(
        {
            "path_id": PATH_INCIDENT_ROLLBACK,
            "label": "Incident/rollback",
            "subject_kind": "incident",
            "specialization_of": PATH_GENERIC,
            "adapter_capabilities": ("content-read", "content-write", "restore"),
        }
    ),
    declaration_from_mapping(
        {
            "path_id": PATH_RECURRING_MAINTENANCE,
            "label": "Recurring maintenance",
            "subject_kind": "content",
            "specialization_of": PATH_GENERIC,
            "adapter_capabilities": ("content-read", "content-write", "schedule"),
        }
    ),
)

#: The immutable declaration index, for a caller that has to look a path up.
CMS_PATHS_BY_ID: Mapping[str, CmsPath] = {p.path_id: p for p in INITIAL_CMS_PATHS}


@dataclass(frozen=True)
class CmsProcessQualification:
    """One observer's ``cms-process-qualification-v1`` record.

    ``disposition`` is :data:`DISPOSITION_NOT_PERFORMED` until an observer has
    actually run the paths. A ``performed`` record must carry the source-bound
    pins and a real readback *and* restore; a record that claims ``performed``
    without them is refused, so a prewritten scenario receipt can never enter
    the contract.
    """

    artifact: str
    versioning: str
    producer: str
    consumer: str
    compatibility: str
    subject_kind: str = SUBJECT_KIND
    disposition: str = DISPOSITION_NOT_PERFORMED
    paths: tuple[CmsPath, ...] = INITIAL_CMS_PATHS
    pins: Mapping[str, str] = None  # type: ignore[assignment]
    readback_verified: bool = False
    restore_verified: bool = False
    measured_outcomes: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()

    @classmethod
    def not_performed(
        cls, *, producer: str, limitations: tuple[str, ...] = ()
    ) -> "CmsProcessQualification":
        """The honest default record: declared, and explicitly NOT PERFORMED.

        This is the only constructor a lane that has not actually run the five
        paths may use. It cannot be turned into a ``performed`` record by
        editing a field, because ``performed`` requires the pins and the real
        readback/restore that this record does not carry.
        """
        return cls(
            artifact=SCHEMA_VERSION,
            versioning=(
                "v1 draft contract; schema URI, revision and digest are frozen "
                "at the SW-1511 baseline task"
            ),
            producer=producer,
            consumer="release assessor",
            compatibility=(
                "additive opt-in contract; preserve existing v1 records and "
                "original-skill execution; unsupported versions fail closed"
            ),
            disposition=DISPOSITION_NOT_PERFORMED,
            pins={},
            measured_outcomes=(),
            limitations=tuple(limitations)
            or (
                "the five CMS paths are declared but not run: no live controller, "
                "provider or sandbox binding was exercised by this unit",
            ),
        )

    @classmethod
    def from_mapping(
        cls, data: Any, *, producer: Optional[str] = None
    ) -> "CmsProcessQualification":
        """Build a record from ``data``, failing closed on every required field.

        Mirrors :meth:`DataContract.from_mapping
        <skillweave.blueprint.data_boundary_contract.DataContract.from_mapping>`:
        a bare string or non-mapping is refused, a missing or blank required
        field is refused by name, an unknown key is refused (so no field is
        silently undefined), and — specific to this record — the path set must
        be exactly the five declared paths and a ``performed`` claim must carry
        the pins plus readback and restore.
        """
        if isinstance(data, str):
            raise InvalidQualificationError(
                f"a {SCHEMA_VERSION} record must define the structured fields "
                f"{list(REQUIRED_QUALIFICATION_FIELDS)}, not prose "
                f"({data[:60]!r})",
                field="data_contract",
            )
        if not isinstance(data, Mapping):
            raise InvalidQualificationError(
                f"a {SCHEMA_VERSION} record must be a mapping, got "
                f"{type(data).__name__}",
                field="data_contract",
            )

        values: dict[str, str] = {}
        for name in REQUIRED_QUALIFICATION_FIELDS:
            raw = data.get(name)
            if not isinstance(raw, str) or not raw.strip():
                raise InvalidQualificationError(
                    f"{SCHEMA_VERSION} record leaves required field '{name}' "
                    f"undefined (got {raw!r})",
                    field=f"data_contract.{name}",
                )
            values[name] = raw.strip()

        allowed = set(REQUIRED_QUALIFICATION_FIELDS) | {
            "subject_kind",
            "disposition",
            "paths",
            "pins",
            "readback_verified",
            "restore_verified",
            "measured_outcomes",
            "limitations",
        }
        unknown = sorted(k for k in data if k not in allowed)
        if unknown:
            raise InvalidQualificationError(
                f"{SCHEMA_VERSION} record carries undefined field(s) {unknown}; "
                f"expected only {sorted(allowed)}",
                field=f"data_contract.{unknown[0]}",
            )

        subject_kind = data.get("subject_kind", SUBJECT_KIND)
        if subject_kind != SUBJECT_KIND:
            raise InvalidQualificationError(
                f"{SCHEMA_VERSION} record subject_kind {subject_kind!r} is not "
                f"{SUBJECT_KIND!r}",
                field="data_contract.subject_kind",
            )

        disposition = data.get("disposition", DISPOSITION_NOT_PERFORMED)
        if disposition not in (DISPOSITION_NOT_PERFORMED, DISPOSITION_PERFORMED):
            raise InvalidQualificationError(
                f"{SCHEMA_VERSION} record declares unknown disposition "
                f"{disposition!r}; expected one of "
                f"{[DISPOSITION_NOT_PERFORMED, DISPOSITION_PERFORMED]}",
                field="disposition",
            )

        paths = cls._paths_from_mapping(data.get("paths"))

        pins_raw = data.get("pins", {})
        if not isinstance(pins_raw, Mapping):
            raise InvalidQualificationError(
                f"{SCHEMA_VERSION} record 'pins' must be a mapping, got "
                f"{type(pins_raw).__name__}",
                field="pins",
            )
        pins: dict[str, str] = {}
        for key, raw in pins_raw.items():
            if not isinstance(key, str) or not isinstance(raw, str) or not raw.strip():
                raise InvalidQualificationError(
                    f"{SCHEMA_VERSION} record pin {key!r} must be a non-empty "
                    f"string, got {raw!r}",
                    field="pins",
                )
            pins[key] = raw.strip()

        readback_verified = _coerce_bool(
            data.get("readback_verified"),
            field="readback_verified",
            path_id=PATH_INCIDENT_ROLLBACK,
            default=False,
        )
        restore_verified = _coerce_bool(
            data.get("restore_verified"),
            field="restore_verified",
            path_id=PATH_INCIDENT_ROLLBACK,
            default=False,
        )

        measured: tuple[str, ...] = ()
        if "measured_outcomes" in data:
            measured = _coerce_capabilities(
                data.get("measured_outcomes"), path_id=paths[0].path_id
            )
            unknown_outcomes = sorted(set(measured) - set(REQUIRED_OUTCOMES))
            if unknown_outcomes:
                raise InvalidQualificationError(
                    f"{SCHEMA_VERSION} record measures unknown outcome(s) "
                    f"{unknown_outcomes}; expected only {list(REQUIRED_OUTCOMES)}",
                    field="measured_outcomes",
                )

        limitations_raw = data.get("limitations", ())
        limitations = _coerce_capabilities(
            limitations_raw, path_id=paths[0].path_id
        )

        if disposition == DISPOSITION_PERFORMED:
            # A performed claim is exactly the thing a prewritten scenario
            # receipt would forge, so it is the most strictly checked branch:
            # the pins, the real readback and the real restore, and the full
            # measurable outcome set, or the claim is refused.
            missing_pins = [
                name for name in REQUIRED_PERFORMED_PINS if name not in pins
            ]
            if missing_pins:
                raise PrewrittenReceiptRefused(
                    f"a {DISPOSITION_PERFORMED!r} {SCHEMA_VERSION} record must "
                    f"name the source-bound pin(s) {missing_pins}",
                    field="pins",
                )
            if not readback_verified or not restore_verified:
                raise PrewrittenReceiptRefused(
                    f"a {DISPOSITION_PERFORMED!r} {SCHEMA_VERSION} record must "
                    f"include a real sandbox readback and restore "
                    f"(readback_verified={readback_verified}, "
                    f"restore_verified={restore_verified}); a prewritten "
                    f"scenario receipt is not a qualification",
                    field="readback_verified",
                )
            missing_outcomes = [
                name for name in REQUIRED_OUTCOMES if name not in set(measured)
            ]
            if missing_outcomes:
                raise PrewrittenReceiptRefused(
                    f"a {DISPOSITION_PERFORMED!r} {SCHEMA_VERSION} record must "
                    f"measure every required outcome; missing {missing_outcomes}",
                    field="measured_outcomes",
                )

        return cls(
            subject_kind=subject_kind,
            disposition=disposition,
            paths=paths,
            pins=pins,
            readback_verified=readback_verified,
            restore_verified=restore_verified,
            measured_outcomes=measured,
            limitations=limitations,
            **values,
        )

    @staticmethod
    def _paths_from_mapping(raw: Any) -> tuple[CmsPath, ...]:
        """Validate the declared path set is exactly the five frozen paths."""
        if raw is None:
            return INITIAL_CMS_PATHS
        if not isinstance(raw, (list, tuple)):
            raise IncompleteQualificationError(
                f"{SCHEMA_VERSION} record 'paths' must be a list, got "
                f"{type(raw).__name__}",
                field="paths",
            )
        declared: list[CmsPath] = []
        seen: list[str] = []
        for index, entry in enumerate(raw):
            if isinstance(entry, str):
                # A bare id resolves to the *frozen* declaration, so a record
                # can name a path without restating (or contradicting) it.
                path_id = entry.strip()
                frozen = CMS_PATHS_BY_ID.get(path_id)
                if frozen is None:
                    raise InvalidQualificationError(
                        f"{_display(path_id)} is not one of the five declared "
                        f"CMS paths {list(PATH_IDS)}",
                        field=f"paths[{index}].path_id",
                        path_id=path_id,
                    )
                declared.append(frozen)
            elif isinstance(entry, Mapping):
                declared.append(declaration_from_mapping(entry))
                path_id = str(entry.get("path_id"))
                if not entry.get("label"):
                    raise IncompleteQualificationError(
                        f"{_display(path_id)} declaration in the record leaves "
                        f"'label' undefined",
                        field=f"paths[{index}].label",
                        path_id=path_id,
                    )
            else:
                raise IncompleteQualificationError(
                    f"{SCHEMA_VERSION} record path[{index}] must be a path id or "
                    f"a declaration mapping, got {type(entry).__name__}",
                    field=f"paths[{index}]",
                )
            if path_id in seen:
                raise IncompleteQualificationError(
                    f"{SCHEMA_VERSION} record declares {_display(path_id)} more "
                    f"than once",
                    field=f"paths[{index}]",
                    path_id=path_id,
                )
            seen.append(path_id)
        if len(seen) != len(PATH_IDS) or set(seen) != set(PATH_IDS):
            missing = sorted(set(PATH_IDS) - set(seen))
            extra = sorted(set(seen) - set(PATH_IDS))
            raise IncompleteQualificationError(
                f"{SCHEMA_VERSION} record must qualify exactly the five declared "
                f"CMS paths; missing {missing}, unexpected {extra}",
                field="paths",
            )
        return tuple(declared)

    @property
    def is_performed(self) -> bool:
        """True only for a record that carries a real observed disposition.

        A ``not-performed`` record is not a qualification. Callers must branch on
        this rather than on the presence of the record.
        """
        return self.disposition == DISPOSITION_PERFORMED

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            name: getattr(self, name) for name in REQUIRED_QUALIFICATION_FIELDS
        }
        payload.update(
            {
                "subject_kind": self.subject_kind,
                "disposition": self.disposition,
                "paths": [p.to_dict() for p in self.paths],
                "pins": dict(self.pins),
                "readback_verified": self.readback_verified,
                "restore_verified": self.restore_verified,
                "measured_outcomes": list(self.measured_outcomes),
                "limitations": list(self.limitations),
            }
        )
        return payload
