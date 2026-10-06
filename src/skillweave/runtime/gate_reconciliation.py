"""Gate reconciliation with mandatory artifact-specific semantics (SW-1510-VERIFY).

A gate is reconciled only when the evidence backing it is real. Process
completion (a clean exit), artifact integrity (bytes hash to the claimed
digest) and semantic verification (the artifact actually *analyzed* the source
it claims) are three separate facts; this module refuses to reconcile a gate
whose artifact evidence fails semantic verification at the real consumer
(:class:`skillweave.runtime.verify.ArtifactEvidenceVerifier`).

Backward compatibility: the legacy call signature
``reconcile_gate(gate_name, registry, observer, authority)`` with no artifact
evidence carried is preserved through an explicit legacy adapter -- it is the
pre-SW-1510 flow that never asserted analyzed-source semantics. As soon as an
artifact proof is supplied (``artifact_evidence``) or a source binding is
requested (``source_resolver``), artifact-semantic verification becomes
mandatory: a missing, empty, phantom, README-only, unsupported or substituted
proof can no longer reconcile to ``True`` by schema repair or placeholders.

``ReconciliationResult`` deliberately carries no override/force field: a caller
cannot flip a refusal, and the artifact refusal is folded into
``authority_statement`` (raw diagnostics preserved) plus the
``evidence_weight`` label.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Sequence


@dataclass
class ReconciliationResult:
    reconciled: bool
    evidence_weight: str
    observer_verdict: str
    authority_statement: str
    gate_name: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "reconciled": self.reconciled,
            "evidence_weight": self.evidence_weight,
            "observer_verdict": self.observer_verdict,
            "authority_statement": self.authority_statement,
            "gate_name": self.gate_name,
            "timestamp": self.timestamp,
        }


def _artifact_semantics(
    artifacts: Sequence[Any],
    source_resolver: Any,
    analysis_reader: Optional[Callable[[str], Optional[bytes]]],
) -> tuple[bool, str]:
    """Verify each artifact's analysis proof; return ``(all_admitted, reason)``.

    The verifier is the real consumer (:class:`ArtifactEvidenceVerifier`); this
    function only wires it. Every refusal reason is returned verbatim so the
    gate's ``authority_statement`` preserves the raw diagnostic.
    """
    from .verify import ArtifactEvidenceVerifier

    if not artifacts:
        return False, "artifact-semantic proof required but none supplied"

    verifier = ArtifactEvidenceVerifier()
    failures: list[str] = []
    admitted = 0
    for artifact in artifacts:
        result = verifier.verify(
            artifact,
            resolver=source_resolver,
            analysis_reader=analysis_reader,
        )
        if result.admitted:
            admitted += 1
        else:
            label = (
                artifact.get("artifact_id", "?")
                if isinstance(artifact, dict)
                else "?"
            )
            failures.append(f"{label}: {'; '.join(result.reasons) or 'refused'}")
    if failures:
        return False, (
            f"{len(failures)} artifact(s) refused artifact-semantic verification: "
            + " | ".join(failures)
        )
    return True, f"{admitted} artifact(s) verified against bound source"


def reconcile_gate(
    gate_name: str,
    evidence_registry,
    observer_runtime,
    authority_guard,
    *,
    artifact_evidence: Optional[Sequence[Any]] = None,
    source_resolver: Any = None,
    analysis_reader: Optional[Callable[[str], Optional[bytes]]] = None,
) -> ReconciliationResult:
    """Reconcile a gate, requiring artifact-specific semantics before a PASS.

    ``artifact_evidence`` is the collection of evidence receipts whose analysis
    proof must be genuine; ``source_resolver`` binds coverage to real files and
    ``analysis_reader`` resolves each claimed analysis artifact's bytes. When
    either is supplied, a gate cannot reconcile to ``True`` unless *every*
    artifact admits artifact-semantic verification.
    """
    counts = evidence_registry.count_by_type()
    total_evidence = sum(counts.values())
    findings = evidence_registry.get_findings()
    observer_outputs = observer_runtime.state().outputs

    if total_evidence < 3:
        return ReconciliationResult(
            reconciled=False,
            evidence_weight="insufficient",
            observer_verdict="neutral",
            authority_statement=f"Gate {gate_name}: insufficient evidence ({total_evidence} artifacts)",
            gate_name=gate_name,
        )

    has_alerts = any(o.output_type == "alert" for o in observer_outputs)
    has_critical_findings = any(f.severity == "critical" for f in findings)

    # Artifact-specific semantics are mandatory as soon as evidence is carried.
    # A caller that supplies no artifact proof at all keeps the explicit legacy
    # path (pre-SW-1510 flows never asserted analyzed-source semantics).
    requires_artifact_semantics = artifact_evidence is not None or source_resolver is not None
    artifact_ok = True
    artifact_reason = ""
    if requires_artifact_semantics:
        artifact_ok, artifact_reason = _artifact_semantics(
            list(artifact_evidence or []), source_resolver, analysis_reader
        )

    if has_critical_findings:
        evidence_weight = "high_evidence_blocked"
        reconciled = False
    elif requires_artifact_semantics and not artifact_ok:
        # A falsified/empty/README-only/missing proof can never reconcile a
        # gate to True -- no schema repair, no placeholder coverage.
        evidence_weight = "artifact_evidence_refused"
        reconciled = False
    elif has_alerts:
        evidence_weight = "medium_evidence_warning"
        reconciled = False
    else:
        evidence_weight = "sufficient"
        reconciled = True

    observer_verdict = "blocked" if not reconciled else "clear"

    has_ops_approval = False
    for o in observer_outputs:
        if o.output_type == "recommendation" and o.authority_verified:
            has_ops_approval = True

    authority_statement = (
        f"Gate {gate_name}: {evidence_weight}, observer={observer_verdict}, "
        f"authority={'verified' if has_ops_approval else 'pending'}"
    )
    if requires_artifact_semantics:
        authority_statement = f"{authority_statement}; {artifact_reason}"

    return ReconciliationResult(
        reconciled=reconciled,
        evidence_weight=evidence_weight,
        observer_verdict=observer_verdict,
        authority_statement=authority_statement,
        gate_name=gate_name,
    )
