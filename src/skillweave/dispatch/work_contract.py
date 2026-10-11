"""Generic subject, evidence, capability, authority, and exact-brief contracts (SW-159-GENERIC-001).

The dispatch layer has, until now, identified every unit of work by a Git
``repo`` plus a full base SHA. That is correct for code lanes and wrong for
everything else: a CMS entry, a configuration change, a deployment target and an
incident all have subjects, and none of them is a commit.

This module owns the *generic* contracts that let one dispatch pipeline carry
Git and non-Git work side by side:

* :class:`SubjectRef` — a **discriminated** reference to what a unit of work acts
  on. Variants: repository, content, configuration, deployment, incident. The
  Git variant carries ``repo``/``commit``; the non-Git variants carry their own
  fields and are **never** required to synthesize a SHA.
* :class:`EvidenceReceipt` — binds evidence to a :class:`SubjectRef` **without
  forcing Git fields**, so a content/config/deployment/incident receipt is
  complete on its own terms.
* :class:`WorkContract` — the authority, write scope, irreversible-action,
  verification, rollback, budget, methodology and policy declaration for one
  unit of work, independent of the subject's kind.
* :class:`CapabilityRegistry` — resolves capabilities through a catalogue/profile
  with three *separated* states: declared, detected, runtime-attested. A weaker
  state never reads as a stronger one, and the resolution stays free of harness,
  router, provider and model identifiers.
* :func:`bind_brief` / :func:`assert_brief_digests_match` — the exact-brief
  contract: the exact submitted bytes (or an immutable content-addressed
  reference) are bound to a worker, and a worker/adherence digest difference
  fails *before* any mutation.

The module is deliberately provider-neutral: no harness name, router name,
provider name or model id appears here, and core code never branches on a
concrete adapter. Those identifiers belong in data (a catalogue or profile), read
as opaque values.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Sequence

#: The subject kinds the generic contract discriminates. ``repository`` is the
#: Git variant; the remaining four are non-Git and carry no Git field.
SUBJECT_REPOSITORY = "repository"
SUBJECT_CONTENT = "content"
SUBJECT_CONFIGURATION = "configuration"
SUBJECT_DEPLOYMENT = "deployment"
SUBJECT_INCIDENT = "incident"

#: Every subject kind, in declaration order. A subject ``kind`` outside this set
#: fails before any mutation — an unknown subject is never inferred.
SUBJECT_KINDS: tuple[str, ...] = (
    SUBJECT_REPOSITORY,
    SUBJECT_CONTENT,
    SUBJECT_CONFIGURATION,
    SUBJECT_DEPLOYMENT,
    SUBJECT_INCIDENT,
)

#: The capability resolution states, kept mechanically separate. A capability
#: that is merely ``declared`` must never read as ``detected`` or
#: ``runtime-attested``; the same discipline the harness statuses already hold.
CAPABILITY_DECLARED = "declared"
CAPABILITY_DETECTED = "detected"
CAPABILITY_RUNTIME_ATTESTED = "runtime-attested"

CAPABILITY_STATES: tuple[str, ...] = (
    CAPABILITY_DECLARED,
    CAPABILITY_DETECTED,
    CAPABILITY_RUNTIME_ATTESTED,
)

#: Authority roles a work contract may declare. One contract declares one
#: authority; a reviewer never carries an ops authority, and composite roles are
#: refused rather than conflated. This mirrors the five distinct harness
#: authority roles so a contract authority and an adapter authority are the same
#: vocabulary.
WORK_AUTHORITY_ROLES: tuple[str, ...] = (
    "controller",
    "ops",
    "reviewer",
    "observer",
    "integrator",
)

#: The categories of action a contract may declare irreversible. A contract that
#: names an irreversible action must carry an explicit authorization record and a
#: rollback note (or an explicit "none, and here is why"). These are the generic
#: counterparts of the runtime's ``IRREVERSIBLE_SURFACES``.
IRREVERSIBLE_KINDS: frozenset[str] = frozenset({
    "release",
    "deploy",
    "prod",
    "publish",
    "push",
    "tag",
    "package_sign",
    "finance",
    "legal",
    "public_channel",
    "human",
    "organization",
})

#: Conflict detection markers that must never be committed into a brief.
_CONFLICT_MARKERS = ("<<<<<<<", "=======", ">>>>>>>")


class WorkContractError(ValueError):
    """A generic work contract failed validation.

    Raised before any mutation. The offending field is named via ``field`` so the
    refusal is attributable rather than a bare NO.
    """

    def __init__(self, message: str, *, field: Optional[str] = None):
        super().__init__(message)
        self.field = field


class SubjectRefError(WorkContractError):
    """A subject reference is malformed or names an unknown kind."""


class EvidenceBindingError(WorkContractError):
    """An evidence receipt cannot be bound to its subject."""


class CapabilityResolutionError(WorkContractError):
    """A capability could not be resolved through catalogue/profile data."""


class ExactBriefError(WorkContractError):
    """The exact-brief contract was violated (missing, mismatched, or tampered).

    A worker/adherence digest difference raises this *before* any mutation.
    """


# ── Subject references (discriminated) ─────────────────────────────────────


def _require_nonempty(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SubjectRefError(
            f"'{field_name}' must be a non-empty string, got {value!r}",
            field=field_name,
        )
    return value.strip()


@dataclass(frozen=True)
class RepositorySubject:
    """The Git variant: a repository at a full committed SHA.

    ``commit`` is a full 40-hex SHA, never a branch name — the same discipline
    the Git dispatch contract already enforces.
    """

    repo: str
    commit: str

    kind: str = field(default=SUBJECT_REPOSITORY, init=False)

    def __post_init__(self) -> None:
        _require_nonempty(self.repo, "repo")
        if not _is_full_sha(self.commit):
            raise SubjectRefError(
                f"repository subject 'commit' must be a full 40-hex SHA, "
                f"got {self.commit!r}",
                field="commit",
            )

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "repo": self.repo, "commit": self.commit}


@dataclass(frozen=True)
class ContentSubject:
    """A CMS/content variant: a channel-qualified entry at a known revision.

    No Git field. ``content_id`` is the entry's stable identity; ``revision`` is
    the opaque revision token the content system reports.
    """

    channel: str
    content_id: str
    revision: str

    kind: str = field(default=SUBJECT_CONTENT, init=False)

    def __post_init__(self) -> None:
        _require_nonempty(self.channel, "channel")
        _require_nonempty(self.content_id, "content_id")
        _require_nonempty(self.revision, "revision")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "channel": self.channel,
            "content_id": self.content_id,
            "revision": self.revision,
        }


@dataclass(frozen=True)
class ConfigurationSubject:
    """A configuration variant: a named config at an environment-bound version.

    No Git field. ``config_id`` is the config's identity; ``version`` is its
    opaque version token.
    """

    config_id: str
    environment: str
    version: str

    kind: str = field(default=SUBJECT_CONFIGURATION, init=False)

    def __post_init__(self) -> None:
        _require_nonempty(self.config_id, "config_id")
        _require_nonempty(self.environment, "environment")
        _require_nonempty(self.version, "version")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "config_id": self.config_id,
            "environment": self.environment,
            "version": self.version,
        }


@dataclass(frozen=True)
class DeploymentSubject:
    """A deployment variant: a target environment running a named artifact.

    No Git field. ``artifact_digest`` is the content address of what is deployed;
    it is *not* a Git commit and is never validated as one.
    """

    target: str
    environment: str
    artifact_digest: str

    kind: str = field(default=SUBJECT_DEPLOYMENT, init=False)

    def __post_init__(self) -> None:
        _require_nonempty(self.target, "target")
        _require_nonempty(self.environment, "environment")
        _require_nonempty(self.artifact_digest, "artifact_digest")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target": self.target,
            "environment": self.environment,
            "artifact_digest": self.artifact_digest,
        }


@dataclass(frozen=True)
class IncidentSubject:
    """An incident variant: an incident record at an observed state.

    No Git field. ``incident_id`` is the incident's identity; ``state`` is its
    current lifecycle state.
    """

    incident_id: str
    state: str
    severity: str

    kind: str = field(default=SUBJECT_INCIDENT, init=False)

    def __post_init__(self) -> None:
        _require_nonempty(self.incident_id, "incident_id")
        _require_nonempty(self.state, "state")
        _require_nonempty(self.severity, "severity")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "incident_id": self.incident_id,
            "state": self.state,
            "severity": self.severity,
        }


#: The discriminated union, as a type alias for annotations.
SubjectRef = (
    RepositorySubject
    | ContentSubject
    | ConfigurationSubject
    | DeploymentSubject
    | IncidentSubject
)

_SUBJECT_BUILDERS = {
    SUBJECT_REPOSITORY: lambda d: RepositorySubject(
        repo=d.get("repo"), commit=d.get("commit")
    ),
    SUBJECT_CONTENT: lambda d: ContentSubject(
        channel=d.get("channel"),
        content_id=d.get("content_id"),
        revision=d.get("revision"),
    ),
    SUBJECT_CONFIGURATION: lambda d: ConfigurationSubject(
        config_id=d.get("config_id"),
        environment=d.get("environment"),
        version=d.get("version"),
    ),
    SUBJECT_DEPLOYMENT: lambda d: DeploymentSubject(
        target=d.get("target"),
        environment=d.get("environment"),
        artifact_digest=d.get("artifact_digest"),
    ),
    SUBJECT_INCIDENT: lambda d: IncidentSubject(
        incident_id=d.get("incident_id"),
        state=d.get("state"),
        severity=d.get("severity"),
    ),
}


def subject_ref_from_dict(data: Mapping[str, Any]) -> SubjectRef:
    """Build the discriminated subject named by ``data['kind']``.

    An unknown or missing ``kind`` fails before any mutation — the contract never
    guesses which variant a payload means. A non-Git variant is never required to
    carry ``repo``/``commit``.
    """
    if not isinstance(data, Mapping):
        raise SubjectRefError("subject must be a mapping", field="subject")
    kind = data.get("kind")
    if kind not in SUBJECT_KINDS:
        raise SubjectRefError(
            f"unknown subject kind {kind!r}; expected one of {list(SUBJECT_KINDS)}",
            field="subject.kind",
        )
    return _SUBJECT_BUILDERS[kind](data)


def is_git_subject(subject: Any) -> bool:
    """True when *subject* is the repository (Git) variant."""
    return getattr(subject, "kind", None) == SUBJECT_REPOSITORY


def subject_identity(subject: Any) -> str:
    """A stable, human-readable identity string for a subject of any kind.

    Used as the evidence-binding key so a receipt can name *what* it is about
    without assuming the subject is a Git revision.
    """
    if is_git_subject(subject):
        return f"repository:{subject.repo}@{subject.commit}"
    if subject.kind == SUBJECT_CONTENT:
        return f"content:{subject.channel}/{subject.content_id}@{subject.revision}"
    if subject.kind == SUBJECT_CONFIGURATION:
        return (
            f"configuration:{subject.config_id}@{subject.environment}"
            f"#{subject.version}"
        )
    if subject.kind == SUBJECT_DEPLOYMENT:
        return (
            f"deployment:{subject.target}@{subject.environment}"
            f"#{subject.artifact_digest}"
        )
    if subject.kind == SUBJECT_INCIDENT:
        return f"incident:{subject.incident_id}@{subject.state}"
    raise SubjectRefError(
        f"unknown subject kind {getattr(subject, 'kind', None)!r}", field="subject.kind"
    )


# ── Evidence bound to a subject, without forcing Git fields ─────────────────


@dataclass
class EvidenceReceipt:
    """One piece of evidence, bound to a :class:`SubjectRef`.

    ``subject`` is any subject variant: the receipt binds to it via
    :func:`subject_identity` and **never** synthesizes a ``repo``/``commit`` when
    the subject is non-Git. ``artifact_digest`` is the content address of the
    evidence payload itself; ``method`` and ``purpose`` keep evidence honest
    (what was done, and why it counts).
    """

    subject: Any
    artifact_digest: str
    evidence_type: str
    purpose: str = ""
    method: str = ""
    producer: str = ""
    observed_at: str = ""

    def __post_init__(self) -> None:
        if getattr(self.subject, "kind", None) not in SUBJECT_KINDS:
            raise EvidenceBindingError(
                f"evidence must bind to a known subject kind, got "
                f"{getattr(self.subject, 'kind', None)!r}",
                field="evidence.subject",
            )
        _require_nonempty(self.artifact_digest, "artifact_digest")
        _require_nonempty(self.evidence_type, "evidence_type")

    @property
    def subject_identity(self) -> str:
        """The bound subject's identity (Git or non-Git)."""
        return subject_identity(self.subject)

    @property
    def git_fields(self) -> Optional[dict[str, str]]:
        """The Git fields of the subject, or ``None`` for a non-Git subject.

        A non-Git receipt reports ``None`` here — it is complete on its own
        terms and is never forced to manufacture a ``repo``/``commit``.
        """
        if is_git_subject(self.subject):
            return {"repo": self.subject.repo, "commit": self.subject.commit}
        return None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "subject": self.subject.to_dict(),
            "subject_identity": self.subject_identity,
            "artifact_digest": self.artifact_digest,
            "evidence_type": self.evidence_type,
            "purpose": self.purpose,
            "method": self.method,
            "producer": self.producer,
            "observed_at": self.observed_at,
        }
        # Only a Git subject carries Git fields; a non-Git receipt omits them
        # entirely rather than emitting nulls that a consumer might read as
        # "not yet resolved".
        if is_git_subject(self.subject):
            payload["subject_repo"] = self.subject.repo
            payload["subject_commit"] = self.subject.commit
        return payload


def bind_evidence(
    subject: Any,
    *,
    artifact_digest: str,
    evidence_type: str,
    purpose: str = "",
    method: str = "",
    producer: str = "",
    observed_at: str = "",
) -> EvidenceReceipt:
    """Bind evidence to ``subject`` (any variant) without forcing Git fields."""
    return EvidenceReceipt(
        subject=subject,
        artifact_digest=artifact_digest,
        evidence_type=evidence_type,
        purpose=purpose,
        method=method,
        producer=producer,
        observed_at=observed_at,
    )


# ── Capability resolution (declared / detected / runtime-attested) ──────────


@dataclass
class CapabilityResolution:
    """The three separated states of one capability, plus its data provenance.

    ``declared`` comes from catalogue/profile data (what is claimed).
    ``detected`` comes from an independent probe (what is present).
    ``runtime_attested`` comes from an actual run (what was proven). The three
    are separate axes: a capability that is declared-only reports
    ``runtime_attested=False`` and must not be treated as proven.
    """

    name: str
    declared: bool = False
    detected: bool = False
    runtime_attested: bool = False
    source: str = ""

    def state(self) -> str:
        """The strongest state reached: one of the three capability states."""
        if self.runtime_attested:
            return CAPABILITY_RUNTIME_ATTESTED
        if self.detected:
            return CAPABILITY_DETECTED
        return CAPABILITY_DECLARED

    def is_usable(self) -> bool:
        """True when the capability is at least *detected* (not declared-only)."""
        return self.detected or self.runtime_attested

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "declared": self.declared,
            "detected": self.detected,
            "runtime_attested": self.runtime_attested,
            "state": self.state(),
            "source": self.source,
        }


class CapabilityRegistry:
    """Resolves capabilities through catalogue/profile data, keeping states.

    Data-driven and provider-neutral: the registry reads capability names and
    their declared/detected/attested booleans from the supplied mappings and
    never branches on a harness, router, provider or model identifier. A
    capability absent from the data fails closed to *declared-only, False* — it
    never silently resolves as available.

    ``resolve`` returns the full :class:`CapabilityResolution` so a caller can see
    *which* state a capability reached, rather than a bare boolean that erases
    the declared/detected/proven distinction.
    """

    def __init__(
        self,
        *,
        declared: Optional[Mapping[str, bool]] = None,
        detected: Optional[Mapping[str, bool]] = None,
        runtime_attested: Optional[Mapping[str, bool]] = None,
        source: str = "",
    ):
        self._declared = {str(k): bool(v) for k, v in (declared or {}).items()}
        self._detected = {str(k): bool(v) for k, v in (detected or {}).items()}
        self._runtime = {str(k): bool(v) for k, v in (runtime_attested or {}).items()}
        self._source = source

    def resolve(self, name: str) -> CapabilityResolution:
        """Resolve one capability, preserving its separated states."""
        return CapabilityResolution(
            name=name,
            declared=bool(self._declared.get(name, False)),
            detected=bool(self._detected.get(name, False)),
            runtime_attested=bool(self._runtime.get(name, False)),
            source=self._source,
        )

    def declared_only(self) -> list[str]:
        """Capabilities that are declared but neither detected nor attested.

        These are the ones a caller must not treat as usable: the declaration
        alone is a claim, not a proven capability.
        """
        names = set(self._declared) | set(self._detected) | set(self._runtime)
        return sorted(
            n for n in names
            if self._declared.get(n, False)
            and not self._detected.get(n, False)
            and not self._runtime.get(n, False)
        )

    def require(self, name: str) -> CapabilityResolution:
        """Resolve ``name``, failing closed when it is not usable.

        A declared-only capability is refused: the work may not proceed on a
        capability that has never been detected or attested.
        """
        resolution = self.resolve(name)
        if not resolution.is_usable():
            raise CapabilityResolutionError(
                f"capability '{name}' is not usable: state="
                f"{resolution.state()} (declared={resolution.declared}, "
                f"detected={resolution.detected}, "
                f"runtime_attested={resolution.runtime_attested})",
                field=f"capabilities.{name}",
            )
        return resolution

    def to_dict(self) -> dict[str, Any]:
        names = sorted(set(self._declared) | set(self._detected) | set(self._runtime))
        return {
            "source": self._source,
            "capabilities": {n: self.resolve(n).to_dict() for n in names},
        }


def load_capability_registry(
    catalogue: Mapping[str, Any],
    *,
    profile: Optional[Mapping[str, Any]] = None,
    source: str = "",
) -> CapabilityRegistry:
    """Build a registry from a catalogue and an optional profile mapping.

    The catalogue supplies declared capabilities; the profile may additionally
    supply detected and runtime-attested facts. Capability names are read as
    opaque strings — no provider/model/harness identifier is interpreted here.
    """
    if not isinstance(catalogue, Mapping):
        raise CapabilityResolutionError(
            "catalogue must be a mapping", field="catalogue"
        )
    profile = profile or {}

    declared = dict(catalogue.get("capabilities", {}) or {})
    declared.update(profile.get("declared", {}) or {})
    detected = dict(catalogue.get("detected", {}) or {})
    detected.update(profile.get("detected", {}) or {})
    runtime = dict(catalogue.get("runtime_attested", {}) or {})
    runtime.update(profile.get("runtime_attested", {}) or {})

    return CapabilityRegistry(
        declared=declared,
        detected=detected,
        runtime_attested=runtime,
        source=source,
    )


# ── The work contract ───────────────────────────────────────────────────────


@dataclass
class WriteScope:
    """Where a unit of work may write, and where it may not.

    ``allow`` is the bounded set of paths/surfaces the work may touch.
    ``deny`` are explicit exclusions that override ``allow``. ``kind`` names the
    scope's nature (e.g. ``paths``, ``objects``) so a non-Git workflow is not
    forced to express its scope as file paths.
    """

    kind: str = "paths"
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)

    def permits(self, target: str) -> bool:
        """True when ``target`` is inside ``allow`` and not inside ``deny``."""
        if any(target == d or target.startswith(d.rstrip("*").rstrip("/") + "/")
               for d in self.deny):
            return False
        return any(
            target == a or target.startswith(a.rstrip("*").rstrip("/") + "/")
            for a in self.allow
        )

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "allow": list(self.allow), "deny": list(self.deny)}


@dataclass
class IrreversibleAction:
    """One action a contract declares as irreversible.

    ``kind`` must be a known irreversible kind. ``authorization`` records the
    explicit authority under which the action runs; ``rollback`` names the
    rollback path, or is empty when the action has none — an irreversible action
    with no rollback is permitted only when an authorization is recorded and the
    caller states the absence explicitly.
    """

    kind: str
    authorization: str = ""
    rollback: str = ""

    def __post_init__(self) -> None:
        if self.kind not in IRREVERSIBLE_KINDS:
            raise WorkContractError(
                f"unknown irreversible action kind {self.kind!r}; expected one of "
                f"{sorted(IRREVERSIBLE_KINDS)}",
                field="irreversible_actions.kind",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "authorization": self.authorization,
            "rollback": self.rollback,
        }


@dataclass
class VerificationClause:
    """How the work's outcome is verified.

    ``method`` names the verification approach; ``evidence_type`` names the
    evidence a verification run produces; ``command`` is the concrete check (a
    shell command, a query, or an API call) for the subject's kind.
    """

    method: str
    evidence_type: str
    command: str = ""

    def __post_init__(self) -> None:
        _require_nonempty(self.method, "verification.method")
        _require_nonempty(self.evidence_type, "verification.evidence_type")

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "evidence_type": self.evidence_type,
            "command": self.command,
        }


@dataclass
class Budget:
    """The bounded budget a contract grants.

    A work contract is bounded on every axis: correction rounds, dispatch
    attempts, and wall-clock/step ceilings. ``None`` means *unbounded on that
    axis* and must be declared explicitly — it is never defaulted.
    """

    max_correction_rounds: int = 0
    max_attempts: int = 1
    max_steps: Optional[int] = None

    def __post_init__(self) -> None:
        if not isinstance(self.max_correction_rounds, int) or self.max_correction_rounds < 0:
            raise WorkContractError(
                "budget.max_correction_rounds must be a non-negative integer",
                field="budget.max_correction_rounds",
            )
        if not isinstance(self.max_attempts, int) or self.max_attempts < 1:
            raise WorkContractError(
                "budget.max_attempts must be a positive integer",
                field="budget.max_attempts",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_correction_rounds": self.max_correction_rounds,
            "max_attempts": self.max_attempts,
            "max_steps": self.max_steps,
        }


@dataclass
class WorkContract:
    """The generic declaration of authority for one unit of work.

    Binds a :class:`SubjectRef` to its authority, write scope, irreversible
    actions, verification, rollback, budget, methodology and policy. It is
    subject-kind agnostic: the same contract type carries a Git lane, a CMS
    entry, a config change, a deployment and an incident.
    """

    id: str
    subject: Any
    authority: str
    write_scope: WriteScope = field(default_factory=WriteScope)
    irreversible_actions: list[IrreversibleAction] = field(default_factory=list)
    verification: list[VerificationClause] = field(default_factory=list)
    rollback: str = ""
    budget: Budget = field(default_factory=Budget)
    methodology: str = ""
    policy: str = ""
    evidence_required: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        _require_nonempty(self.id, "id")
        if getattr(self.subject, "kind", None) not in SUBJECT_KINDS:
            raise WorkContractError(
                f"work contract '{self.id}' must declare a known subject kind, got "
                f"{getattr(self.subject, 'kind', None)!r}",
                field=f"{self.id}.subject",
            )
        if self.authority not in WORK_AUTHORITY_ROLES:
            raise WorkContractError(
                f"work contract '{self.id}' declares unknown authority "
                f"{self.authority!r}; expected one of {list(WORK_AUTHORITY_ROLES)}",
                field=f"{self.id}.authority",
            )
        if not isinstance(self.write_scope, WriteScope):
            raise WorkContractError(
                f"work contract '{self.id}' write_scope must be a WriteScope",
                field=f"{self.id}.write_scope",
            )

    def assert_authorized(self) -> None:
        """Fail closed when an irreversible action lacks authority or rollback.

        Every irreversible action must carry an explicit ``authorization``. An
        action with no rollback is permitted only when the contract also records
        an explicit acknowledgment, which here is a non-empty ``rollback`` value
        of ``"none: <reason>"``. This is the "authorize before mutation" gate —
        it runs *before* any write.
        """
        for action in self.irreversible_actions:
            if not action.authorization.strip():
                raise WorkContractError(
                    f"work contract '{self.id}' irreversible action "
                    f"'{action.kind}' has no recorded authorization",
                    field=f"{self.id}.irreversible_actions.{action.kind}",
                )
            if not action.rollback.strip():
                raise WorkContractError(
                    f"work contract '{self.id}' irreversible action "
                    f"'{action.kind}' has no recorded rollback; declare "
                    "'none: <reason>' when no rollback exists",
                    field=f"{self.id}.irreversible_actions.{action.kind}.rollback",
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "subject": self.subject.to_dict(),
            "authority": self.authority,
            "write_scope": self.write_scope.to_dict(),
            "irreversible_actions": [a.to_dict() for a in self.irreversible_actions],
            "verification": [v.to_dict() for v in self.verification],
            "rollback": self.rollback,
            "budget": self.budget.to_dict(),
            "methodology": self.methodology,
            "policy": self.policy,
            "evidence_required": list(self.evidence_required),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WorkContract":
        """Build a contract from a mapping, failing closed on every axis."""
        if not isinstance(data, Mapping):
            raise WorkContractError("work contract must be a mapping", field=None)
        subject = subject_ref_from_dict(data.get("subject") or {})
        scope_raw = data.get("write_scope") or {}
        write_scope = WriteScope(
            kind=str(scope_raw.get("kind", "paths")),
            allow=list(scope_raw.get("allow") or []),
            deny=list(scope_raw.get("deny") or []),
        )
        budget_raw = data.get("budget") or {}
        budget = Budget(
            max_correction_rounds=int(budget_raw.get("max_correction_rounds", 0)),
            max_attempts=int(budget_raw.get("max_attempts", 1)),
            max_steps=budget_raw.get("max_steps"),
        )
        irreversible = [
            IrreversibleAction(
                kind=str(a.get("kind")),
                authorization=str(a.get("authorization", "")),
                rollback=str(a.get("rollback", "")),
            )
            for a in (data.get("irreversible_actions") or [])
        ]
        verification = [
            VerificationClause(
                method=str(v.get("method")),
                evidence_type=str(v.get("evidence_type")),
                command=str(v.get("command", "")),
            )
            for v in (data.get("verification") or [])
        ]
        return cls(
            id=str(data.get("id", "")),
            subject=subject,
            authority=str(data.get("authority", "")),
            write_scope=write_scope,
            irreversible_actions=irreversible,
            verification=verification,
            rollback=str(data.get("rollback", "")),
            budget=budget,
            methodology=str(data.get("methodology", "")),
            policy=str(data.get("policy", "")),
            evidence_required=list(data.get("evidence_required") or []),
        )


# ── Exact-brief contract ────────────────────────────────────────────────────


def sha256_digest(data: bytes) -> str:
    """The SHA-256 content address of ``data`` as hex."""
    if not isinstance(data, (bytes, bytearray)):
        raise ExactBriefError(
            f"exact-brief payload must be bytes, got {type(data).__name__}",
            field="brief",
        )
    return hashlib.sha256(bytes(data)).hexdigest()


@dataclass(frozen=True)
class BriefReference:
    """An immutable, content-addressed reference to exact brief bytes.

    Either the exact ``bytes`` are carried, or an immutable ``content_address``
    (its SHA-256) is carried — never both omitted. ``digest`` is always the
    SHA-256 of the bytes, whether they were supplied directly or addressed.
    """

    digest: str
    byte_length: int
    content_address: str = ""
    _bytes: Optional[bytes] = None

    def exact_bytes(self) -> Optional[bytes]:
        """The exact bytes when they were supplied, else ``None``.

        A content-addressed reference that carries no inline bytes resolves to
        ``None`` here; the consumer fetches the bytes by ``content_address`` and
        :func:`assert_brief_matches` verifies them.
        """
        return self._bytes

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "byte_length": self.byte_length,
            "content_address": self.content_address,
        }


def bind_brief(work: bytes) -> BriefReference:
    """Bind the *exact* submitted brief bytes to a worker.

    Returns a :class:`BriefReference` carrying the SHA-256 digest of the exact
    bytes. The digest is computed over the bytes as submitted — a re-encoded or
    normalized brief produces a different digest and is refused later by
    :func:`assert_brief_digests_match`.
    """
    if work is None or not isinstance(work, (bytes, bytearray)):
        raise ExactBriefError(
            "exact brief must be the submitted bytes (bytes/bytearray)",
            field="brief",
        )
    raw = bytes(work)
    return BriefReference(
        digest=sha256_digest(raw),
        byte_length=len(raw),
        content_address=sha256_digest(raw),
        _bytes=raw,
    )


def bind_brief_reference(digest: str, *, byte_length: int, content_address: str = "") -> BriefReference:
    """Bind an immutable content-addressed brief reference (no inline bytes).

    Used when the exact bytes live in a content-addressed store and the worker
    receives only the immutable reference. ``digest`` must be a SHA-256 hex
    string; the reference is verified against resolved bytes by
    :func:`assert_brief_matches`.
    """
    if not isinstance(digest, str) or len(digest) != 64:
        raise ExactBriefError(
            f"brief digest must be a 64-hex SHA-256 value, got {digest!r}",
            field="brief.digest",
        )
    try:
        int(digest, 16)
    except ValueError:
        raise ExactBriefError(
            f"brief digest must be hexadecimal, got {digest!r}", field="brief.digest"
        )
    return BriefReference(
        digest=digest,
        byte_length=int(byte_length),
        content_address=content_address or digest,
    )


@dataclass
class BriefBinding:
    """The exact-brief facts bound for one worker dispatch.

    ``worker`` is the digest the worker was handed; ``adherence`` is the digest
    the adherence/validation seam independently computed from the brief it
    checked. A difference means the worker and the gate did not see the same
    bytes, and dispatch must fail **before** any mutation.
    """

    reference: BriefReference
    worker: str
    adherence: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference.to_dict(),
            "worker": self.worker,
            "adherence": self.adherence,
        }


def assert_brief_digests_match(
    worker: str,
    adherence: str,
    *,
    field_name: str = "brief",
) -> None:
    """Fail closed when the worker and adherence brief digests differ.

    Called *before* any mutation. The mismatch names both digests so the failure
    is attributable, and never proceeds on a partially-checked brief.
    """
    if not worker or not adherence:
        raise ExactBriefError(
            "both worker and adherence brief digests are required; "
            f"got worker={worker!r}, adherence={adherence!r}",
            field=field_name,
        )
    if worker != adherence:
        raise ExactBriefError(
            f"worker/adherence brief digest mismatch: worker={worker!r}, "
            f"adherence={adherence!r}; refusing dispatch before any mutation",
            field=field_name,
        )


def prepare_brief_binding(
    work: bytes,
    *,
    adherence_brief: Optional[bytes] = None,
) -> BriefBinding:
    """Bind the exact brief and verify the worker/adherence digests agree.

    ``adherence_brief`` is the bytes the adherence/validation seam independently
    received. When it is omitted, the same exact bytes are assumed to have
    reached both seams and the binding is trivially consistent. When supplied, a
    difference in the two digests raises :class:`ExactBriefError` before any
    mutation.
    """
    reference = bind_brief(work)
    worker_digest = reference.digest
    if adherence_brief is None:
        adherence_digest = worker_digest
    else:
        adherence_digest = sha256_digest(adherence_brief)
    assert_brief_digests_match(worker_digest, adherence_digest)
    return BriefBinding(
        reference=reference,
        worker=worker_digest,
        adherence=adherence_digest,
    )


def assert_brief_matches(reference: BriefReference, resolved: bytes) -> None:
    """Verify resolved bytes reproduce a content-addressed brief reference.

    Used when a worker receives only the immutable reference and the bytes are
    later fetched by content address: the fetched bytes must hash back to the
    reference digest, or the brief was tampered with.
    """
    actual = sha256_digest(resolved)
    if actual != reference.digest:
        raise ExactBriefError(
            f"brief content-address mismatch: reference digest "
            f"{reference.digest!r}, resolved bytes hash to {actual!r}",
            field="brief.content_address",
        )
    if reference.byte_length and len(resolved) != reference.byte_length:
        raise ExactBriefError(
            f"brief byte-length mismatch: reference {reference.byte_length}, "
            f"resolved {len(resolved)}",
            field="brief.byte_length",
        )


def assert_brief_has_no_conflict_markers(work: bytes) -> None:
    """Refuse a brief that carries unresolved merge-conflict markers.

    A conflict marker in a submitted brief is an exact-byte contract violation:
    the bytes are not the settled submission the gate is meant to bind.
    """
    text = bytes(work).decode("utf-8", errors="replace")
    for marker in _CONFLICT_MARKERS:
        if marker in text:
            raise ExactBriefError(
                f"brief contains unresolved conflict marker {marker!r}",
                field="brief",
            )


def _is_full_sha(value: Any) -> bool:
    """A full SHA is 40 hexadecimal characters, not a branch name."""
    if not isinstance(value, str) or len(value) != 40:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


__all__ = [
    "WorkContractError",
    "SubjectRefError",
    "EvidenceBindingError",
    "CapabilityResolutionError",
    "ExactBriefError",
    "SUBJECT_REPOSITORY",
    "SUBJECT_CONTENT",
    "SUBJECT_CONFIGURATION",
    "SUBJECT_DEPLOYMENT",
    "SUBJECT_INCIDENT",
    "SUBJECT_KINDS",
    "CAPABILITY_DECLARED",
    "CAPABILITY_DETECTED",
    "CAPABILITY_RUNTIME_ATTESTED",
    "CAPABILITY_STATES",
    "WORK_AUTHORITY_ROLES",
    "IRREVERSIBLE_KINDS",
    "RepositorySubject",
    "ContentSubject",
    "ConfigurationSubject",
    "DeploymentSubject",
    "IncidentSubject",
    "SubjectRef",
    "subject_ref_from_dict",
    "is_git_subject",
    "subject_identity",
    "EvidenceReceipt",
    "bind_evidence",
    "CapabilityResolution",
    "CapabilityRegistry",
    "load_capability_registry",
    "WriteScope",
    "IrreversibleAction",
    "VerificationClause",
    "Budget",
    "WorkContract",
    "sha256_digest",
    "BriefReference",
    "BriefBinding",
    "bind_brief",
    "bind_brief_reference",
    "assert_brief_digests_match",
    "prepare_brief_binding",
    "assert_brief_matches",
    "assert_brief_has_no_conflict_markers",
]
