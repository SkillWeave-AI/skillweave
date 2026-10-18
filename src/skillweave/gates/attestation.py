"""Strict dual-review attestation (SW1312-ATTESTATION-STRICT-001).

A reusable, fail-closed validator that canonicalizes the 1.3.11
controller-attested dual-review shape into a strict contract. It:

1. **Requires** ``candidate_sha``, exactly two reviewer records, an explicit
   ``contradiction`` boolean, and per-reviewer identity, class, verdict and
   subject SHA.
2. **Rejects unknown keys** at the top level and inside each reviewer record,
   so a misspelled field such as ``contradiction_recorded`` or ``contradiction``
   mis-typed as ``contradiction_flg`` cannot be silently ignored.
3. **Canonicalizes every full 40-hex SHA to lowercase** at ingestion and
   serialization, so mixed-case equivalents (``ABC...`` vs ``abc...``) collapse
   to one lowercase identity before candidate/subject comparison.
4. **Requires reviewer diversity** — one Pro-class and one Flash-class
   reviewer — inspecting the identical canonical candidate and returning the
   exact ``REVIEW_PASS`` verdict while ``contradiction`` is ``False``.
5. **Fails closed** on a missing or non-boolean contradiction, differing
   subjects, a malformed SHA, a duplicate reviewer identity or class, or a
   non-``REVIEW_PASS`` verdict.

The :class:`DualReviewAttestation` object is the serialized canonical form; a
caller that only needs the acceptance decision can use :func:`validate` or
:func:`canonicalize`. This module names no concrete harness, provider or model
(``pro``/``flash`` are the provider-neutral capability tiers already defined in
``skillweave.dispatch.model_policy`` and ``skillweave.transfer.catalog``).

It is the single shared contract that SW1312 gate receipts validate against,
and the released ``tests/gate_1311`` controller-attested behavior is migrated
onto it rather than keeping a second local interpretation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

#: Full 40-hex SHA, lowercase only (canonical). Mixed-case input is lowered
#: before matching, never accepted as a distinct identity.
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")

#: The two provider-neutral capability tiers a dual review must span.
_REVIEWER_CLASSES: frozenset[str] = frozenset({"pro", "flash"})

#: The exact dual-review verdict that alone authorizes a gate.
REVIEW_PASS = "REVIEW_PASS"

#: Top-level keys a dual-review attestation may carry. Anything else is rejected.
_TOP_LEVEL_KEYS: frozenset[str] = frozenset({"candidate_sha", "reviewers", "contradiction"})

#: Keys a reviewer record may carry. Anything else is rejected.
_REVIEWER_KEYS: frozenset[str] = frozenset(
    {"reviewer_id", "class", "verdict", "subject_sha"}
)

def _schema_path() -> Path:
    """Resolve the shipped attestation schema from installed package data."""
    from ..assets import packaged_schema_path

    return packaged_schema_path("dual-review-attestation.schema.json")


class AttestationError(ValueError):
    """A dual-review attestation is missing, malformed or self-contradictory."""


def _normalize_sha(sha: Any, *, field: str) -> str:
    """Canonicalize a full SHA to lowercase, refusing anything malformed."""
    if not isinstance(sha, str):
        raise AttestationError(f"{field} must be a string, got {sha!r}")
    canonical = sha.strip().lower()
    if not _FULL_SHA.match(canonical):
        raise AttestationError(f"{field} is not a full 40-hex SHA: {sha!r}")
    return canonical


def _check_unknown_keys(mapping: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = [k for k in mapping if k not in allowed]
    if unknown:
        raise AttestationError(
            f"{label} carries unknown key(s) {unknown}; only {sorted(allowed)} are allowed"
        )


@dataclass(frozen=True)
class DualReviewAttestation:
    """The canonicalized, validated dual-review attestation.

    ``candidate_sha`` and each ``subject_sha`` are lowercase canonical; the two
    ``reviewers`` are the validated records in preserved order. All fields are
    the strict-contract values that passed validation, never the raw input.
    """

    candidate_sha: str
    reviewers: tuple[Mapping[str, Any], ...]
    contradiction: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_sha": self.candidate_sha,
            "reviewers": [
                {k: r[k] for k in sorted(_REVIEWER_KEYS)}
                for r in self.reviewers
            ],
            "contradiction": self.contradiction,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)


def canonicalize(evidence: Mapping[str, Any]) -> DualReviewAttestation:
    """Validate and canonicalize a dual-review attestation, fail-closed.

    Raises :class:`AttestationError` on any missing, unknown, malformed or
    self-contradictory field. Returns the canonical :class:`DualReviewAttestation`
    on success — the single lowercase identity the rest of the gate consumes.
    """
    if not isinstance(evidence, Mapping):
        raise AttestationError(f"attestation must be a mapping, got {evidence!r}")

    _check_unknown_keys(evidence, _TOP_LEVEL_KEYS, "attestation")

    if "candidate_sha" not in evidence:
        raise AttestationError("attestation is missing 'candidate_sha'")
    candidate_sha = _normalize_sha(evidence["candidate_sha"], field="candidate_sha")

    if "reviewers" not in evidence:
        raise AttestationError("attestation is missing 'reviewers'")
    reviewers = evidence["reviewers"]
    if not isinstance(reviewers, list) or len(reviewers) != 2:
        raise AttestationError(
            f"dual review requires exactly two reviewers, got {reviewers!r}"
        )

    if "contradiction" not in evidence:
        raise AttestationError("attestation is missing 'contradiction'")
    contradiction = evidence["contradiction"]
    if not isinstance(contradiction, bool):
        raise AttestationError(
            f"contradiction must be a boolean, got {contradiction!r}"
        )

    canonical_reviewers: list[Mapping[str, Any]] = []
    seen_ids: set[str] = set()
    seen_classes: set[str] = set()
    for idx, reviewer in enumerate(reviewers):
        if not isinstance(reviewer, Mapping):
            raise AttestationError(f"reviewer entry is not a mapping: {reviewer!r}")
        _check_unknown_keys(reviewer, _REVIEWER_KEYS, f"reviewers[{idx}]")

        for required in ("reviewer_id", "class", "verdict", "subject_sha"):
            if required not in reviewer:
                raise AttestationError(f"reviewers[{idx}] is missing '{required}'")

        reviewer_id = reviewer["reviewer_id"]
        if not isinstance(reviewer_id, str) or not reviewer_id:
            raise AttestationError(f"reviewers[{idx}] reviewer_id must be non-empty: {reviewer_id!r}")
        if reviewer_id in seen_ids:
            raise AttestationError(f"duplicate reviewer identity: {reviewer_id!r}")
        seen_ids.add(reviewer_id)

        reviewer_class = reviewer["class"]
        if reviewer_class not in _REVIEWER_CLASSES:
            raise AttestationError(
                f"reviewer class must be one of {sorted(_REVIEWER_CLASSES)}, got {reviewer_class!r}"
            )
        if reviewer_class in seen_classes:
            raise AttestationError(
                f"duplicate reviewer class: {reviewer_class!r} (dual review needs one pro and one flash)"
            )
        seen_classes.add(reviewer_class)

        verdict = reviewer["verdict"]
        if verdict != REVIEW_PASS:
            raise AttestationError(
                f"reviewer verdict must be exactly '{REVIEW_PASS}', got {verdict!r}"
            )

        subject_sha = _normalize_sha(reviewer["subject_sha"], field=f"reviewers[{idx}].subject_sha")
        if subject_sha != candidate_sha:
            raise AttestationError(
                f"reviewer attested subject {subject_sha} but the candidate "
                f"under attestation is {candidate_sha}"
            )

        canonical_reviewers.append(
            {
                "reviewer_id": reviewer_id,
                "class": reviewer_class,
                "verdict": verdict,
                "subject_sha": subject_sha,
            }
        )

    # Reviewer diversity: exactly one pro and one flash.
    if seen_classes != _REVIEWER_CLASSES:
        raise AttestationError(
            f"dual review must be one pro and one flash reviewer, got {sorted(seen_classes)}"
        )

    # A dual-review attestation that records a material contradiction can never
    # authorize a gate.
    if contradiction:
        raise AttestationError("dual review records a material contradiction")

    return DualReviewAttestation(
        candidate_sha=candidate_sha,
        reviewers=tuple(canonical_reviewers),
        contradiction=contradiction,
    )


def validate(evidence: Mapping[str, Any]) -> DualReviewAttestation:
    """Validate the attestation (alias of :func:`canonicalize`).

    Both the canonical identity (lowercase SHAs) and the acceptance decision
    (diversity, verdict, contradiction) are enforced; nothing is separated out
    and defaulted true.
    """
    return canonicalize(evidence)


def load_schema() -> dict[str, Any]:
    """Return the shipped dual-review attestation JSON schema, verbatim."""
    return json.loads(_schema_path().read_text(encoding="utf-8"))


__all__ = [
    "REVIEW_PASS",
    "AttestationError",
    "DualReviewAttestation",
    "canonicalize",
    "validate",
    "load_schema",
]
