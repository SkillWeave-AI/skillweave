"""Provider-neutral harness adapter contract and strict-controller binding (SW1311-HARNESS-001).

This module owns two things and nothing else:

1. **The adapter capability profile** as declarable data. A profile declares which
   of the eight dispatch capabilities an adapter supports (native tool launch,
   external child process, in-place execution, stdin transport, status, cancel,
   state namespace, and installed-skill digests), which authority a role holds,
   how it may delegate, and its four independently evidenced run-statuses
   (documented, installed, dispatch-proven, production) as separate facts.

   The module is deliberately provider-neutral: it contains **no literal harness
   name and no branch on any concrete adapter**. A concrete harness name belongs
   in the adapter's own data file key, which this module reads as opaque data.
   Adding a harness edits data; it never adds a branch here.

2. **The experimental strict-controller binding.** Strict mode refuses a dispatch
   unless the validated sequence, the resolved profile, the exact task brief and
   the installed skill digests are all bound. A missing, stale, or mismatched
   skill/capability digest fails closed *before* worker launch and names the
   mismatched asset. A harness-native delegation or direct-shell bypass is
   recorded and fails closed when strict mode requires SkillWeave dispatch.

   Strict mode is experimental and fail-closed: nothing is defaulted into a
   binding, and a refusal always names the asset that failed, never a bare NO.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Sequence

# ── The eight dispatch capabilities ────────────────────────────────────────

#: The provider-neutral capability vocabulary. Core dispatch branches on the
#: *capability*, never on the adapter; an adapter's data file maps itself to a
#: subset of these. A missing capability falls closed to ``False``.
#:
#: SW-1511-RECOVERY criterion 2 adds ``session-resume``: an adapter attests it
#: *only* when it can resume an actual provider session across a controller
#: restart. A recovered controller uses actual session resume only against an
#: attested adapter and otherwise reports a named checkpoint continuation --
#: never fabricated session-UUID continuity.
CAPABILITIES: tuple[str, ...] = (
    "native-tool",
    "external-process",
    "in-place",
    "stdin",
    "status",
    "cancel",
    "state-namespace",
    "installed-skill-digest",
    "session-resume",
)


# ── The four separated run-statuses ────────────────────────────────────────

#: ``documented``: a doc names the adapter and how its operator sets the seam.
#: Supports the *declaration mechanism* only — never a run record.
STATUS_DOCUMENTED = "documented"

#: ``installed``: the installer has a target path and copies skills there. A
#: write destination, not a run record.
STATUS_INSTALLED = "installed"

#: ``dispatch-proven``: a real run originated from this adapter and terminated
#: cleanly, recorded by the dispatch seam. Per-adapter, per-machine.
STATUS_DISPATCH_PROVEN = "dispatch-proven"

#: ``production``: a deployment profile an operator actually runs. Ship nothing
#: that claims it; it is operator-owned.
STATUS_PRODUCTION = "production"

#: Every statuses file may name these four; they stay mechanically separate so a
#: documented name can never read as a proven run.
STATUS_KEYS: tuple[str, ...] = (
    STATUS_DOCUMENTED,
    STATUS_INSTALLED,
    STATUS_DISPATCH_PROVEN,
    STATUS_PRODUCTION,
)

#: The distinct authority roles. One role, one authority; controller, ops,
#: reviewer, observer and integrator are never conflated.
AUTHORITY_ROLES: tuple[str, ...] = (
    "controller",
    "ops",
    "reviewer",
    "observer",
    "integrator",
)


class HarnessContractError(ValueError):
    """An adapter capability profile or strict binding is invalid.

    Raised before any worker launch. The offending field (an asset name, a
    role, a capability, a status) is named via ``asset`` so the failure is
    attributable, never a bare refusal.
    """

    def __init__(self, message: str, *, asset: Optional[str] = None):
        super().__init__(message)
        self.asset = asset


class StrictControllerError(HarnessContractError):
    """Strict-controller mode refused a dispatch because a required binding is
    missing, stale, mismatched, or bypassed.

    The missing or mismatched asset is named via ``asset`` (criterion 4).
    """


class DigestMismatchError(StrictControllerError):
    """An expected skill/capability digest does not match what the adapter
    actually reports. Raised before launch, naming the mismatched asset."""


class BypassNotRecordedError(StrictControllerError):
    """A harness-native delegation or direct-shell bypass was attempted while
    strict mode requires SkillWeave dispatch. Recorded and fail-closed."""


# ── The adapter capability profile ─────────────────────────────────────────

@dataclass
class HarnessAdapterProfile:
    """One adapter's capability and authority profile, loaded from data.

    ``name`` is the concrete harness name (opaque here). ``capabilities`` maps
    the eight capability names to a truthiness; ``authority`` is the single role
    this adapter holds; ``delegation`` records whether SkillWeave dispatch is
    allowed and whether the two bypasses (native delegation, direct shell) are.
    ``statuses`` holds the four run-statuses as separate facts.

    Nothing in this dataclass names a concrete harness as a branch: every value
    is read from the data a caller supplies.
    """

    name: str
    capabilities: dict[str, bool] = field(default_factory=dict)
    authority: dict[str, Any] = field(default_factory=dict)
    delegation: dict[str, bool] = field(default_factory=dict)
    statuses: dict[str, bool] = field(default_factory=dict)
    skill_digests: dict[str, str] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        *,
        adapter_name: str = "",
        statuses: Optional[Mapping[str, bool]] = None,
        skill_digests: Optional[Mapping[str, str]] = None,
    ) -> "HarnessAdapterProfile":
        """Build a profile from a data mapping (fail-closed on structure).

        ``adapter_name`` supplies the concrete name (from the data key or an
        explicit caller). ``statuses`` and ``skill_digests`` may be passed in so
        hermetic fixtures can separate the adapter's *declared* capability
        surface from its *observed* status and digest facts.
        """
        if not isinstance(data, Mapping):
            raise HarnessContractError(
                "adapter profile must be a mapping", asset=adapter_name or None
            )
        raw_caps = dict(data.get("capabilities", {}) or {})
        for key in raw_caps:
            if key not in CAPABILITIES:
                raise HarnessContractError(
                    f"unknown capability '{key}' (expected one of "
                    f"{list(CAPABILITIES)})",
                    asset=adapter_name or None,
                )
        capabilities = {c: bool(raw_caps.get(c, False)) for c in CAPABILITIES}

        authority = dict(data.get("authority", {}) or {})
        role = authority.get("role")
        if role is not None and role not in AUTHORITY_ROLES:
            raise HarnessContractError(
                f"unknown authority role '{role}' (expected one of "
                f"{list(AUTHORITY_ROLES)})",
                asset=adapter_name or None,
            )

        delegation = dict(data.get("delegation", {}) or {})
        delegation = {k: bool(delegation.get(k, False)) for k in delegation}

        if statuses is not None:
            resolved_statuses = {s: bool(statuses.get(s, False)) for s in STATUS_KEYS}
        else:
            raw_statuses = dict(data.get("statuses", {}) or {})
            resolved_statuses = {s: bool(raw_statuses.get(s, False)) for s in STATUS_KEYS}

        if skill_digests is not None:
            resolved_digests = dict(skill_digests)
        else:
            raw_digests = dict(data.get("skill_digests", {}) or {})
            resolved_digests = {
                str(k): str(v) for k, v in raw_digests.items()
            }

        return cls(
            name=str(adapter_name or data.get("adapter", "")),
            capabilities=capabilities,
            authority=authority,
            delegation=delegation,
            statuses=resolved_statuses,
            skill_digests=resolved_digests,
            evidence=dict(data.get("evidence", {}) or {}),
        )

    def capability(self, name: str) -> bool:
        """Whether this adapter supports ``name``. Falls closed to ``False``."""
        return bool(self.capabilities.get(name, False))

    def authority_role(self) -> Optional[str]:
        """The single authority this adapter holds, or ``None`` when undeclared."""
        return self.authority.get("role")

    def has_authority(self, role: str) -> bool:
        """Whether this adapter holds exactly ``role``'s authority."""
        return self.authority.get("role") == role

    def status(self, key: str) -> bool:
        """The separated status fact for ``key`` (falls closed to ``False``)."""
        return bool(self.statuses.get(key, False))

    def skillweave_dispatch_allowed(self) -> bool:
        """Whether this adapter may dispatch work through SkillWeave."""
        return bool(self.delegation.get("skillweave-dispatch", False))

    def bypass_flags(self) -> list[str]:
        """The delegation bypasses (native delegation / direct shell) declared.
        A bypass present here surfaces as a foreign attempt to record."""
        return [
            key
            for key in ("native-delegation", "direct-shell")
            if self.delegation.get(key, False)
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "capabilities": dict(self.capabilities),
            "authority": dict(self.authority),
            "delegation": dict(self.delegation),
            "statuses": dict(self.statuses),
            "skill_digests": dict(self.skill_digests),
            "evidence": dict(self.evidence),
        }


# ── Status honesty helper ──────────────────────────────────────────────────

def assert_statuses_honest(profile: HarnessAdapterProfile) -> None:
    """Refuse a statuses surface where a weaker fact claims a stronger one.

    The four statuses are separate axes. ``documented`` never implies
    ``dispatch-proven`` nor ``production``; ``installed`` is a write destination,
    not a run record. A profile that reports ``production`` must also report
    ``dispatch-proven`` (a production profile is meaningless without a proven
    run), and ``dispatch-proven`` without ``documented``/``installed`` is a
    recorded run on a host the repository has no installer or doc target for —
    which the fixtures keep honest by separating the facts, never conflating.
    """
    if profile.status(STATUS_PRODUCTION) and not profile.status(STATUS_DISPATCH_PROVEN):
        raise HarnessContractError(
            f"adapter '{profile.name}' claims '{STATUS_PRODUCTION}' without a "
            f"'{STATUS_DISPATCH_PROVEN}' run; the statuses are separate and "
            "production must be grounded in a proven run",
            asset=profile.name,
        )


# ── Strict-controller binding ──────────────────────────────────────────────

@dataclass
class BoundDispatch:
    """A dispatcher request bound for strict-controller execution.

    Strict mode refuses a dispatch unless all four are bound: the validated
    sequence, the resolved profile, the exact task brief, and the installed
    skill digests. ``adapter`` is the resolved adapter capability profile and
    ``bound_at`` is the run identity the bind happened under.
    """

    sequence: Any
    profile: Any
    task_brief: bytes
    skill_digests: dict[str, str]
    adapter: Optional[HarnessAdapterProfile] = None
    bound_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence_bound": self.sequence is not None,
            "profile_bound": self.profile is not None,
            "task_brief_bound": self.task_brief is not None,
            "skill_digests": dict(self.skill_digests),
            "adapter": self.adapter.to_dict() if self.adapter is not None else None,
            "bound_at": self.bound_at,
        }


class StrictController:
    """The experimental strict-adherence gate between the validated declaration
    and the first worker launch.

    ``bind`` requires the validated sequence, the resolved profile, the exact
    task brief and the installed skill digests all to be present; any missing one
    is refused by name. ``observe_actual_digests`` reconciles the adapter's
    declared expected digests against what the adapter actually reports; a
    missing or stale skill/capability digest fails *before* launch, naming the
    mismatched asset. ``record_attempt`` records a foreign (native-delegation or
    direct-shell) dispatch attempt and, when strict mode requires SkillWeave
    dispatch, fails closed.

    All of this is data-driven: the gate reads adapter capability/status/digest
    data and never branches on a concrete harness name.
    """

    def __init__(self, *, require_skillweave_dispatch: bool = True):
        self._require_skillweave_dispatch = bool(require_skillweave_dispatch)
        self._attempts: list[dict[str, Any]] = []

    @property
    def requires_skillweave_dispatch(self) -> bool:
        """Whether skip/allowed-only SkillWeave dispatch, forbidding bypasses."""
        return self._require_skillweave_dispatch

    @property
    def attempts(self) -> list[dict[str, Any]]:
        """Recorded foreign / bypass dispatch attempts, in arrival order."""
        return list(self._attempts)

    def bind(
        self,
        *,
        sequence: Any,
        profile: Any,
        task_brief: bytes,
        skill_digests: Mapping[str, str],
        adapter: Optional[HarnessAdapterProfile] = None,
        bound_at: str = "",
    ) -> BoundDispatch:
        """Bind the four required facts, refusing any that is missing by name.

        A dispatch may not reach a worker launch in strict mode unless the
        validated sequence, the resolved profile, the exact task brief and the
        installed skill digests are all bound (criterion 3).
        """
        missing: list[str] = []
        if sequence is None:
            missing.append("validated sequence")
        if profile is None:
            missing.append("resolved profile")
        if task_brief is None or not task_brief:
            missing.append("exact task brief")
        if skill_digests is None or not skill_digests:
            missing.append("installed skill digests")
        if missing:
            raise StrictControllerError(
                "strict-controller dispatch refused: missing required binding(s): "
                + ", ".join(missing),
                asset=(adapter.name if adapter is not None else None),
            )
        return BoundDispatch(
            sequence=sequence,
            profile=profile,
            task_brief=task_brief,
            skill_digests=dict(skill_digests),
            adapter=adapter,
            bound_at=bound_at,
        )

    def observe_actual_digests(
        self,
        adapter: HarnessAdapterProfile,
        actual: Mapping[str, str],
    ) -> dict[str, str]:
        """Reconcile the adapter's expected skill digests against the observed.

        For every expected skill digest the adapter declares, the observed value
        must be present and equal; otherwise a ``DigestMismatchError`` names the
        mismatched asset before any worker launch (criterion 4). Returns the
        reconciled per-skill digest mapping on success.
        """
        if not adapter.skill_digests:
            return {}
        reconciled: dict[str, str] = {}
        for skill, expected in adapter.skill_digests.items():
            observed = actual.get(skill)
            if observed is None:
                raise DigestMismatchError(
                    f"skill '{skill}' expected digest is not reported by adapter "
                    f"'{adapter.name}'",
                    asset=skill,
                )
            if observed != expected:
                raise DigestMismatchError(
                    f"skill '{skill}' digest mismatch: expected {expected!r}, "
                    f"adapter reports {observed!r}",
                    asset=skill,
                )
            reconciled[skill] = observed
        return reconciled

    def record_attempt(
        self,
        *,
        kind: str,
        detail: str,
        adapter: Optional[HarnessAdapterProfile] = None,
    ) -> None:
        """Record a dispatch attempt, failing closed on a forbidden bypass.

        ``kind`` is one of ``skillweave``, ``native-delegation`` or
        ``direct-shell``. A SkillWeave dispatch is allowed. In strict mode a
        harness-native delegation or a direct-shell bypass is recorded and then
        refused (``BypassNotRecordedError``) whenever this controller requires
        SkillWeave dispatch and the adapter's profile does not explicitly allow
        the bypass.
        """
        entry: dict[str, Any] = {
            "kind": kind,
            "detail": detail,
            "adapter": adapter.name if adapter is not None else None,
        }
        self._attempts.append(entry)

        # In strict mode a harness-native delegation or a direct-shell bypass is
        # always recorded AND refused: the adapter's delegation *declaration*
        # records that the capability exists (a risk to surface), it never grants
        # permission to bypass SkillWeave dispatch. If the controller does not
        # require SkillWeave dispatch, the bypass is recorded but allowed.
        if kind in ("native-delegation", "direct-shell"):
            if self._require_skillweave_dispatch:
                raise BypassNotRecordedError(
                    f"{kind} dispatch attempt by adapter "
                    f"'{adapter.name if adapter is not None else '<unbound>'}' is "
                    "not allowed under strict SkillWeave dispatch; refusing "
                    "worker launch and recording the attempt",
                    asset=adapter.name if adapter is not None else None,
                )

    def reconcile_authority(
        self,
        adapter: HarnessAdapterProfile,
    ) -> None:
        """Refuse an adapter whose declared authority is not a distinct role.

        Every adapter holds exactly one of the five distinct authorities
        (controller, ops, reviewer, observer, integrator). A profile that
        declares an unknown or no role, or that claims two roles at once, is
        refused; core dispatch never guesses a role.
        """
        role = adapter.authority.get("role")
        if role is None or role not in AUTHORITY_ROLES:
            raise HarnessContractError(
                f"adapter '{adapter.name}' declares no distinct authority role; "
                f"got {adapter.authority!r}",
                asset=adapter.name,
            )
        # A single-role surface is the contract: the authority mapping may hold
        # exactly one role name plus the required-flag field, never two roles.
        role_fields = [
            k for k in adapter.authority if k not in ("role", "skillweave-dispatch-required")
        ]
        if role_fields:
            raise HarnessContractError(
                f"adapter '{adapter.name}' authority claims multiple roles "
                f"{role_fields!r}; one adapter holds exactly one authority",
                asset=adapter.name,
            )


# ── Assembly / loading helpers (data-driven) ───────────────────────────────

def load_adapter_profiles(
    data: Mapping[str, Any],
    *,
    statuses: Optional[Mapping[str, Mapping[str, bool]]] = None,
    skill_digests: Optional[Mapping[str, Mapping[str, str]]] = None,
) -> dict[str, HarnessAdapterProfile]:
    """Load a set of adapter capability profiles from one data mapping.

    ``data`` maps a concrete harness name to its capability/authority/delegation
    declaration. ``statuses`` and ``skill_digests`` may supply the observed,
    hermetic status and digest facts separately from the declared capability
    surface, so an adapter's *capabilities* read as DATA while its
    *statuses/digests* stay provable from the fixtures.
    """
    if not isinstance(data, Mapping):
        raise HarnessContractError("adapter profiles must be a mapping")
    loaded: dict[str, HarnessAdapterProfile] = {}
    for adapter_name, raw in data.items():
        profile = HarnessAdapterProfile.from_dict(
            raw,
            adapter_name=str(adapter_name),
            statuses=(statuses or {}).get(str(adapter_name)),
            skill_digests=(skill_digests or {}).get(str(adapter_name)),
        )
        assert_statuses_honest(profile)
        loaded[profile.name] = profile
    return loaded


__all__ = [
    "CAPABILITIES",
    "STATUS_DOCUMENTED",
    "STATUS_INSTALLED",
    "STATUS_DISPATCH_PROVEN",
    "STATUS_PRODUCTION",
    "STATUS_KEYS",
    "AUTHORITY_ROLES",
    "HarnessContractError",
    "StrictControllerError",
    "DigestMismatchError",
    "BypassNotRecordedError",
    "HarnessAdapterProfile",
    "BoundDispatch",
    "StrictController",
    "assert_statuses_honest",
    "load_adapter_profiles",
]
