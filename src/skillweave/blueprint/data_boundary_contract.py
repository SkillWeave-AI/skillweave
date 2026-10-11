"""Blueprint PRD data-boundary contract (SW-159-BP-CONTRACT-001).

A PRD is prose until a task crosses an architectural boundary. At that moment
the two sides of the boundary are separate failure domains, and an
*undocumented* interface between them is the exact defect this contract
refuses: a task that says "store the result" or "call the API" without naming
the artifact, its versioning, who produces it, who consumes it, and how the
two stay compatible.

This module owns the contract that makes a *crossing* explicit:

* **classification** — the boundary kinds ``storage``, ``process``, ``adapter``,
  ``telemetry`` and ``public-api`` are *contract-requiring*; an unknown kind is
  refused rather than guessed (:class:`UnknownBoundaryKindError`).
* **the contract itself** — every contract-requiring boundary must carry a
  :class:`DataContract` defining the five required fields ``artifact``,
  ``versioning``, ``producer``, ``consumer`` and ``compatibility``. A field that
  is absent or blank is an *undefined required field* and is refused, naming the
  task and the field (:class:`UndefinedContractFieldError`).
* **prose-only refusal** — a boundary whose contract is a bare string (or
  absent) is a prose-only definition, not a contract, and is refused with a
  task-specific diagnostic (:class:`ProseOnlyBoundaryError`).
* **compatibility** — a task with no data-boundary change declares no
  ``boundaries`` and validates exactly as before
  (:data:`NO_DATA_BOUNDARY`). The check is opt-in per task.

The vocabulary of a boundary's *subject* is not re-invented here. When a
boundary names the artifact's subject kind it must use the integrated
``WorkContract`` subject vocabulary (``repository``, ``content``,
``configuration``, ``deployment``, ``incident`` — SW-159-WORK-001), so a
boundary and the work contract that authorizes it speak the same language:
:data:`WORK_CONTRACT_SUBJECT_KINDS` mirrors ``SUBJECT_KINDS`` and the shipped
test asserts the two agree.

Like the discovery trace contract beside it, this module imports only the
standard library. It is a *core* module (GLE-020): importing it must never pull
in the ``skillweave`` runtime or the dispatch package, which transitively loads
that runtime. The integrated contract is consumed by *vocabulary parity* and a
focused integration test, never by a top-level import of a heavier package.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

#: The dispositions a task can receive. A task either crosses a contract-requiring
#: boundary or it does not; there is no third, weaker state.
CONTRACT_REQUIRING = "contract-requiring"
NO_DATA_BOUNDARY = "no-data-boundary"

#: The architectural boundary kinds that require a data contract. Each one is a
#: seam between separate failure domains: storage (persistence), process (a
#: separate executing context), adapter (a foreign system), telemetry (an
#: observational sink), public-api (a published surface).
BOUNDARY_STORAGE = "storage"
BOUNDARY_PROCESS = "process"
BOUNDARY_ADAPTER = "adapter"
BOUNDARY_TELEMETRY = "telemetry"
BOUNDARY_PUBLIC_API = "public-api"

#: Every boundary kind, in declaration order. A kind outside this set is refused
#: before any validation — an unknown boundary is never classified by default.
BOUNDARY_KINDS: tuple[str, ...] = (
    BOUNDARY_STORAGE,
    BOUNDARY_PROCESS,
    BOUNDARY_ADAPTER,
    BOUNDARY_TELEMETRY,
    BOUNDARY_PUBLIC_API,
)

#: The fields a data contract must define. Absence or blankness of any field is
#: an *undefined required field*, not a warning.
REQUIRED_DATA_CONTRACT_FIELDS: tuple[str, ...] = (
    "artifact",
    "versioning",
    "producer",
    "consumer",
    "compatibility",
)

#: The subject vocabulary a boundary defers to when it names its artifact's
#: subject kind. This mirrors ``skillweave.dispatch.work_contract.SUBJECT_KINDS``
#: (SW-159-WORK-001) and must stay identical — the shipped test asserts the two
#: tuples are equal, so a boundary can never name a subject the work contract
#: does not know. It is *not* copied by import: importing the dispatch package
#: would pull the skillweave runtime into this core module (GLE-020).
WORK_CONTRACT_SUBJECT_KINDS: tuple[str, ...] = (
    "repository",
    "content",
    "configuration",
    "deployment",
    "incident",
)

#: Accepted spellings of a boundary kind, normalised to the canonical hyphen
#: form. ``public_api`` / ``public api`` are the same boundary as ``public-api``;
#: anything else is unknown and refused.
_KIND_ALIASES = {
    "public_api": BOUNDARY_PUBLIC_API,
    "public api": BOUNDARY_PUBLIC_API,
    "publicapi": BOUNDARY_PUBLIC_API,
}


class DataBoundaryContractError(ValueError):
    """A PRD's data-boundary declaration failed validation.

    Raised before any work is dispatched. ``field`` names the offending path and
    ``task_id`` names the task, so the refusal is attributable to a specific
    task and field rather than a bare NO. The ``field`` attribute mirrors
    ``skillweave.dispatch.work_contract.WorkContractError`` (SW-159-WORK-001).
    """

    def __init__(
        self,
        message: str,
        *,
        field: Optional[str] = None,
        task_id: Optional[str] = None,
    ):
        super().__init__(message)
        self.field = field
        self.task_id = task_id


class UnknownBoundaryKindError(DataBoundaryContractError):
    """A boundary names a kind that is not contract-requiring and not known."""


class ProseOnlyBoundaryError(DataBoundaryContractError):
    """A contract-requiring boundary has only prose, not a structured contract.

    Either the boundary carries no contract at all, or it carries a bare string.
    Both are a description of a crossing rather than a contract for it.
    """


class UndefinedContractFieldError(DataBoundaryContractError):
    """A required data-contract field is absent or blank (undefined)."""


class InvalidDataContractError(DataBoundaryContractError):
    """A data contract is malformed: wrong type, or carries an undefined key."""


def _display(task_id: Optional[str]) -> str:
    """A task-specific diagnostic prefix."""
    return f"task '{task_id}'" if task_id else "task"


def normalize_boundary_kind(kind: Any) -> str:
    """Return the canonical boundary kind for ``kind``.

    Unknown kinds raise :class:`UnknownBoundaryKindError` — never classify an
    unknown boundary as if it were known.
    """
    if not isinstance(kind, str) or not kind.strip():
        raise UnknownBoundaryKindError(
            f"boundary kind must be a non-empty string, got {kind!r}",
            field="boundaries.kind",
        )
    canonical = kind.strip().lower().replace("_", "-")
    canonical = _KIND_ALIASES.get(canonical, canonical)
    if canonical not in BOUNDARY_KINDS:
        raise UnknownBoundaryKindError(
            f"unknown boundary kind {kind!r}; expected one of {list(BOUNDARY_KINDS)}",
            field="boundaries.kind",
        )
    return canonical


def is_contract_requiring(kind: Any) -> bool:
    """True when ``kind`` is one of the contract-requiring boundary kinds."""
    return normalize_boundary_kind(kind) in BOUNDARY_KINDS


@dataclass(frozen=True)
class DataContract:
    """The five required fields of an exact versioned data contract.

    ``artifact`` names what crosses the boundary; ``versioning`` names how it is
    versioned; ``producer`` and ``consumer`` name the two sides; and
    ``compatibility`` states how the consumer tolerates producer change. All five
    are required and must be non-empty — an unspecified field is undefined.

    ``subject_kind`` is optional and, when present, must come from the integrated
    ``WorkContract`` subject vocabulary (:data:`WORK_CONTRACT_SUBJECT_KINDS`), so
    a boundary on, say, a deployment names the same subject kind the work
    contract does.
    """

    artifact: str
    versioning: str
    producer: str
    consumer: str
    compatibility: str
    subject_kind: str = ""

    @classmethod
    def from_mapping(
        cls, data: Any, *, task_id: Optional[str] = None, kind: str = ""
    ) -> "DataContract":
        """Build a contract from ``data``, failing closed on every required field.

        A bare string is a prose-only definition and is refused by name, naming
        the crossing ``kind`` when known. A missing or blank required field
        raises :class:`UndefinedContractFieldError` naming the task and the
        field. An unknown key is refused so no boundary field is silently
        undefined.
        """
        crossing = f"'{kind}' boundary" if kind else "data boundary"
        if isinstance(data, str):
            raise ProseOnlyBoundaryError(
                f"{_display(task_id)} describes a {crossing} in prose "
                f"({data[:60]!r}); a contract-requiring boundary must define the "
                f"structured fields {list(REQUIRED_DATA_CONTRACT_FIELDS)}",
                field="boundaries.data_contract",
                task_id=task_id,
            )
        if not isinstance(data, Mapping):
            raise InvalidDataContractError(
                f"{_display(task_id)} data contract must be a mapping, got "
                f"{type(data).__name__}",
                field="boundaries.data_contract",
                task_id=task_id,
            )

        values: dict[str, str] = {}
        for name in REQUIRED_DATA_CONTRACT_FIELDS:
            raw = data.get(name)
            if not isinstance(raw, str) or not raw.strip():
                raise UndefinedContractFieldError(
                    f"{_display(task_id)} data contract leaves required field "
                    f"'{name}' undefined (got {raw!r})",
                    field=f"boundaries.data_contract.{name}",
                    task_id=task_id,
                )
            values[name] = raw.strip()

        allowed = set(REQUIRED_DATA_CONTRACT_FIELDS) | {"subject_kind"}
        unknown = sorted(k for k in data if k not in allowed)
        if unknown:
            raise InvalidDataContractError(
                f"{_display(task_id)} data contract carries undefined field(s) "
                f"{unknown}; expected only "
                f"{sorted(allowed)}",
                field=f"boundaries.data_contract.{unknown[0]}",
                task_id=task_id,
            )

        subject_kind = data.get("subject_kind", "")
        if subject_kind:
            if subject_kind not in WORK_CONTRACT_SUBJECT_KINDS:
                raise InvalidDataContractError(
                    f"{_display(task_id)} data contract subject_kind "
                    f"{subject_kind!r} is not in the integrated WorkContract "
                    f"vocabulary {list(WORK_CONTRACT_SUBJECT_KINDS)}",
                    field="boundaries.data_contract.subject_kind",
                    task_id=task_id,
                )
            subject_kind = str(subject_kind)

        return cls(subject_kind=subject_kind, **values)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            name: getattr(self, name) for name in REQUIRED_DATA_CONTRACT_FIELDS
        }
        if self.subject_kind:
            payload["subject_kind"] = self.subject_kind
        return payload


def classify_task_boundary(
    task: Mapping[str, Any],
) -> str:
    """Classify one task's data-boundary disposition.

    Returns :data:`NO_DATA_BOUNDARY` for a task that declares no boundary (the
    compatibility path), or :data:`CONTRACT_REQUIRING` for a task whose
    boundaries are all well-formed contract-requiring boundaries. Any boundary
    that is unknown, prose-only, or missing a required field raises before the
    classification is returned.
    """
    if not isinstance(task, Mapping):
        raise InvalidDataContractError(
            f"task must be a mapping, got {type(task).__name__}",
            field="tasks",
        )
    task_id = task.get("id")
    boundaries = task.get("boundaries")
    if boundaries is None or boundaries == []:
        return NO_DATA_BOUNDARY
    if not isinstance(boundaries, list):
        raise InvalidDataContractError(
            f"{_display(task_id)} 'boundaries' must be an array, got "
            f"{type(boundaries).__name__}",
            field="boundaries",
            task_id=task_id,
        )

    saw_contract_requiring = False
    for index, boundary in enumerate(boundaries):
        where = f"boundaries[{index}]"
        if not isinstance(boundary, Mapping):
            raise InvalidDataContractError(
                f"{_display(task_id)} {where} must be a mapping, got "
                f"{type(boundary).__name__}",
                field=where,
                task_id=task_id,
            )
        try:
            kind = normalize_boundary_kind(boundary.get("kind"))
        except UnknownBoundaryKindError as exc:
            raise UnknownBoundaryKindError(
                f"{_display(task_id)} {where}: {exc}",
                field=f"{where}.kind",
                task_id=task_id,
            ) from exc
        if kind not in BOUNDARY_KINDS:
            continue
        saw_contract_requiring = True
        if "data_contract" not in boundary or boundary.get("data_contract") is None:
            raise ProseOnlyBoundaryError(
                f"{_display(task_id)} {where} declares a contract-requiring "
                f"'{kind}' boundary with no data_contract; a prose description "
                f"must define {list(REQUIRED_DATA_CONTRACT_FIELDS)}",
                field=f"{where}.data_contract",
                task_id=task_id,
            )
        DataContract.from_mapping(
            boundary.get("data_contract"), task_id=task_id, kind=kind
        )

    return CONTRACT_REQUIRING if saw_contract_requiring else NO_DATA_BOUNDARY


def validate_prd_data_boundaries(prd: Mapping[str, Any]) -> dict[str, str]:
    """Validate every task's data-boundary declaration, returning dispositions.

    Maps each task id to :data:`CONTRACT_REQUIRING` or :data:`NO_DATA_BOUNDARY`.
    Fails closed on the first violation, naming the task and the field. A PRD
    with no ``tasks``, or a task with no ``boundaries``, is valid and yields
    :data:`NO_DATA_BOUNDARY` — existing PRDs are not retroactively broken.
    """
    if not isinstance(prd, Mapping):
        raise InvalidDataContractError(
            f"PRD must be a mapping, got {type(prd).__name__}", field="prd"
        )
    tasks = prd.get("tasks")
    if tasks is None:
        return {}
    if not isinstance(tasks, list):
        raise InvalidDataContractError(
            f"PRD 'tasks' must be an array, got {type(tasks).__name__}",
            field="tasks",
        )
    return {str(task.get("id")): classify_task_boundary(task) for task in tasks}


__all__ = [
    "CONTRACT_REQUIRING",
    "NO_DATA_BOUNDARY",
    "BOUNDARY_STORAGE",
    "BOUNDARY_PROCESS",
    "BOUNDARY_ADAPTER",
    "BOUNDARY_TELEMETRY",
    "BOUNDARY_PUBLIC_API",
    "BOUNDARY_KINDS",
    "REQUIRED_DATA_CONTRACT_FIELDS",
    "WORK_CONTRACT_SUBJECT_KINDS",
    "DataBoundaryContractError",
    "UnknownBoundaryKindError",
    "ProseOnlyBoundaryError",
    "UndefinedContractFieldError",
    "InvalidDataContractError",
    "DataContract",
    "normalize_boundary_kind",
    "is_contract_requiring",
    "classify_task_boundary",
    "validate_prd_data_boundaries",
]
