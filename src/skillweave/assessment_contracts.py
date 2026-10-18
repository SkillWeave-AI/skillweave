"""Assessment receipt contract (SW-157-ASSESS-001).

A structured, tamper-evident record of one assessment. It owns the *contract*,
not the mechanics: it defines what a receipt must represent, validates it
fail-closed, and refuses any receipt whose content digest does not match its
payload.

A valid receipt represents:

* a ``result`` that is either ``available`` or ``unavailable``;
* the ``subject`` under assessment as a canonical lowercase full 40-hex SHA;
* the ordered ``sources`` inspected (path plus sha256);
* the ordered ``commands`` run and their integer exit codes;
* the ordered ``findings`` (id, severity, summary);
* the explicit ``limits`` of the assessment;
* the ``provenance`` of the assessor (assessor, run id, produced-at, model);
* a ``digest`` — sha256 over the canonical receipt with this field excluded.

Tamper rejection is the point: every digested field is covered, list order is
preserved and digested, unknown keys are refused at every level, and the digest
is recomputed and compared on ingestion. A post-hoc mutation of any digested
field (a flipped status, a changed SHA, an altered exit code, a dropped or
added finding, a reordered source) fails closed with
:class:`AssessmentTamperError`.

The module is deliberately dependency-light: it imports only the standard
library (``hashlib``, ``json``, ``re``, ``dataclasses``, ``pathlib``, ``typing``)
so the contract can be imported and enforced without importing the full
``skillweave`` runtime (see ``tests/contract/test_assessment_receipt.py``).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

#: The only receipt schema version that exists.
SCHEMA_VERSION = 1

#: The two permitted assessment result statuses.
RESULT_AVAILABLE = "available"
RESULT_UNAVAILABLE = "unavailable"
_RESULT_STATUSES = frozenset({RESULT_AVAILABLE, RESULT_UNAVAILABLE})

#: The permitted finding severities, ordered least to most severe.
SEVERITIES = ("info", "low", "medium", "high", "critical")
_SEVERITY_SET = frozenset(SEVERITIES)

#: Full 40-hex SHA, lowercase only (canonical). A mixed-case or whitespace-padded
#: variant is refused, never lowered into an identity, so the module and the
#: shipped schema agree on exactly what a canonical SHA is.
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")

#: Canonical lowercase sha256 hex.
_SHA256 = re.compile(r"^[a-f0-9]{64}$")

#: Core keys a receipt must carry (``digest`` is validated separately, since it
#: is derived from these).
_CORE_KEYS = (
    "schema_version",
    "result",
    "subject",
    "sources",
    "commands",
    "findings",
    "limits",
    "provenance",
)

#: Every key a receipt may carry. Anything else is rejected.
_TOP_LEVEL_KEYS = frozenset(_CORE_KEYS) | {"digest"}

def _schema_path() -> Path:
    """Resolve the shipped assessment-receipt schema from installed package data."""
    from .assets import packaged_schema_path

    return packaged_schema_path("assessment-receipt.schema.json")


class AssessmentError(ValueError):
    """An assessment receipt is missing, malformed or self-contradictory."""


class AssessmentTamperError(AssessmentError):
    """A receipt's recomputed digest does not match the digest it carries."""


def _canonical_json(payload: Mapping[str, Any]) -> str:
    """Canonical JSON: sorted keys, no insignificant whitespace, ASCII-safe.

    List order is preserved, so the order of ``sources``, ``commands`` and
    ``findings`` is part of the digested identity.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_digest(receipt: Mapping[str, Any]) -> str:
    """Return the sha256 digest of ``receipt`` with any ``digest`` key excluded.

    The digest is computed over the receipt exactly as supplied (canonical JSON),
    so a caller that mutates a digested field changes the digest. Use
    :func:`seal` to obtain a receipt whose digest is guaranteed to match.
    """
    payload = {k: v for k, v in receipt.items() if k != "digest"}
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AssessmentError(f"{label} must be a mapping, got {value!r}")
    return value


def _check_unknown_keys(mapping: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted((k for k in mapping if k not in allowed), key=repr)
    if unknown:
        raise AssessmentError(
            f"{label} carries unknown key(s) {unknown}; only {sorted(allowed)} are allowed"
        )


def _require_nonempty_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise AssessmentError(f"{label} must be a non-empty string, got {value!r}")
    return value


def _normalize_sha(value: Any, *, field: str) -> str:
    """Accept a canonical lowercase full 40-hex SHA, exactly as written.

    A mixed-case or whitespace-padded variant is refused rather than lowered, so
    the module and the shipped schema agree on exactly one canonical form.
    """
    if not isinstance(value, str) or not _FULL_SHA.match(value):
        raise AssessmentError(f"{field} is not a canonical lowercase full 40-hex SHA: {value!r}")
    return value


def _normalize_sha256(value: Any, *, field: str) -> str:
    """Accept a canonical lowercase sha256 hex digest, exactly as written."""
    if not isinstance(value, str) or not _SHA256.match(value):
        raise AssessmentError(f"{field} is not a canonical lowercase sha256 hex digest: {value!r}")
    return value


def _normalize_result(value: Any) -> dict[str, Any]:
    result = _require_mapping(value, "result")
    _check_unknown_keys(result, frozenset({"status", "summary"}), "result")
    if "status" not in result:
        raise AssessmentError("result is missing 'status'")
    status = result["status"]
    if not isinstance(status, str) or status not in _RESULT_STATUSES:
        raise AssessmentError(
            f"result.status must be one of {sorted(_RESULT_STATUSES)}, got {status!r}"
        )
    normalized: dict[str, Any] = {"status": status}
    if "summary" in result:
        normalized["summary"] = _require_nonempty_str(result["summary"], "result.summary")
    return normalized


def _normalize_subject(value: Any) -> dict[str, Any]:
    subject = _require_mapping(value, "subject")
    _check_unknown_keys(subject, frozenset({"full_sha", "repo", "ref"}), "subject")
    if "full_sha" not in subject:
        raise AssessmentError("subject is missing 'full_sha'")
    normalized: dict[str, Any] = {
        "full_sha": _normalize_sha(subject["full_sha"], field="subject.full_sha")
    }
    for optional in ("repo", "ref"):
        if optional in subject:
            normalized[optional] = _require_nonempty_str(subject[optional], f"subject.{optional}")
    return normalized


def _normalize_sources(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise AssessmentError(f"sources must be a list, got {value!r}")
    normalized: list[dict[str, Any]] = []
    for idx, source in enumerate(value):
        entry = _require_mapping(source, f"sources[{idx}]")
        _check_unknown_keys(entry, frozenset({"path", "sha256"}), f"sources[{idx}]")
        for required in ("path", "sha256"):
            if required not in entry:
                raise AssessmentError(f"sources[{idx}] is missing '{required}'")
        normalized.append(
            {
                "path": _require_nonempty_str(entry["path"], f"sources[{idx}].path"),
                "sha256": _normalize_sha256(entry["sha256"], field=f"sources[{idx}].sha256"),
            }
        )
    return normalized


def _normalize_commands(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise AssessmentError(f"commands must be a list, got {value!r}")
    normalized: list[dict[str, Any]] = []
    for idx, command in enumerate(value):
        entry = _require_mapping(command, f"commands[{idx}]")
        _check_unknown_keys(entry, frozenset({"command", "exit"}), f"commands[{idx}]")
        for required in ("command", "exit"):
            if required not in entry:
                raise AssessmentError(f"commands[{idx}] is missing '{required}'")
        exit_code = entry["exit"]
        # bool is an int subclass; an exit code is never a boolean.
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            raise AssessmentError(f"commands[{idx}].exit must be an integer, got {exit_code!r}")
        normalized.append(
            {
                "command": _require_nonempty_str(entry["command"], f"commands[{idx}].command"),
                "exit": exit_code,
            }
        )
    return normalized


def _normalize_findings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise AssessmentError(f"findings must be a list, got {value!r}")
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for idx, finding in enumerate(value):
        entry = _require_mapping(finding, f"findings[{idx}]")
        _check_unknown_keys(entry, frozenset({"id", "severity", "summary"}), f"findings[{idx}]")
        for required in ("id", "severity", "summary"):
            if required not in entry:
                raise AssessmentError(f"findings[{idx}] is missing '{required}'")
        finding_id = _require_nonempty_str(entry["id"], f"findings[{idx}].id")
        if finding_id in seen_ids:
            raise AssessmentError(f"duplicate finding id: {finding_id!r}")
        seen_ids.add(finding_id)
        severity = entry["severity"]
        if not isinstance(severity, str) or severity not in _SEVERITY_SET:
            raise AssessmentError(
                f"findings[{idx}].severity must be one of {list(SEVERITIES)}, got {severity!r}"
            )
        normalized.append(
            {
                "id": finding_id,
                "severity": severity,
                "summary": _require_nonempty_str(entry["summary"], f"findings[{idx}].summary"),
            }
        )
    return normalized


def _normalize_limits(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise AssessmentError(f"limits must be a list, got {value!r}")
    return [
        _require_nonempty_str(limit, f"limits[{idx}]") for idx, limit in enumerate(value)
    ]


def _normalize_provenance(value: Any) -> dict[str, Any]:
    provenance = _require_mapping(value, "provenance")
    _check_unknown_keys(
        provenance, frozenset({"assessor", "run_id", "produced_at", "model"}), "provenance"
    )
    normalized: dict[str, Any] = {}
    for required in ("assessor", "run_id", "produced_at"):
        if required not in provenance:
            raise AssessmentError(f"provenance is missing '{required}'")
        normalized[required] = _require_nonempty_str(
            provenance[required], f"provenance.{required}"
        )
    if "model" in provenance:
        normalized["model"] = _require_nonempty_str(provenance["model"], "provenance.model")
    return normalized


def _normalize_core(receipt: Any) -> dict[str, Any]:
    """Validate and canonicalize every digested field, excluding ``digest``."""
    if not isinstance(receipt, Mapping):
        raise AssessmentError(f"receipt must be a mapping, got {receipt!r}")
    _check_unknown_keys(receipt, _TOP_LEVEL_KEYS, "receipt")
    for required in _CORE_KEYS:
        if required not in receipt:
            raise AssessmentError(f"receipt is missing '{required}'")

    version = receipt["schema_version"]
    # bool is an int subclass, and 1.0 == 1; neither is the integer version.
    if not isinstance(version, int) or isinstance(version, bool) or version != SCHEMA_VERSION:
        raise AssessmentError(f"schema_version must be {SCHEMA_VERSION}, got {version!r}")

    result = _normalize_result(receipt["result"])
    sources = _normalize_sources(receipt["sources"])
    commands = _normalize_commands(receipt["commands"])
    findings = _normalize_findings(receipt["findings"])
    limits = _normalize_limits(receipt["limits"])
    provenance = _normalize_provenance(receipt["provenance"])

    # An unavailable assessment attests nothing it did not run, so it must state
    # its limits; an available one must actually name the sources it read and the
    # commands it ran.
    if result["status"] == RESULT_UNAVAILABLE:
        if not limits:
            raise AssessmentError("an unavailable receipt must declare at least one limit")
    else:
        if not sources:
            raise AssessmentError("an available receipt must declare at least one source")
        if not commands:
            raise AssessmentError("an available receipt must declare at least one command")

    return {
        "schema_version": SCHEMA_VERSION,
        "result": result,
        "subject": _normalize_subject(receipt["subject"]),
        "sources": sources,
        "commands": commands,
        "findings": findings,
        "limits": limits,
        "provenance": provenance,
    }


@dataclass(frozen=True)
class AssessmentReceipt:
    """The canonicalized, validated, digest-bearing assessment receipt.

    ``payload`` is the canonical digested form (the ``digest`` field excluded);
    ``digest`` is the sha256 that was recomputed from and matched against it.
    """

    payload: Mapping[str, Any]
    digest: str

    def to_dict(self) -> dict[str, Any]:
        document = dict(self.payload)
        document["digest"] = self.digest
        return document

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)


def seal(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Validate ``receipt`` and return it sealed with a matching digest.

    Any ``digest`` on the input is ignored and replaced, so the returned
    document always satisfies :func:`canonicalize`.
    """
    canonical = _normalize_core(receipt)
    canonical["digest"] = compute_digest(canonical)
    return canonical


def canonicalize(receipt: Mapping[str, Any]) -> AssessmentReceipt:
    """Validate a sealed receipt, fail-closed, and verify its digest.

    Raises :class:`AssessmentError` on any missing, unknown, malformed or
    self-contradictory field, and :class:`AssessmentTamperError` when the
    recomputed digest does not match the carried ``digest``. Returns the
    canonical :class:`AssessmentReceipt` on success.
    """
    canonical = _normalize_core(receipt)

    if "digest" not in receipt:
        raise AssessmentError("receipt is missing 'digest'")
    provided = _normalize_sha256(receipt["digest"], field="digest")
    expected = compute_digest(canonical)
    if provided != expected:
        raise AssessmentTamperError(
            f"digest mismatch: receipt carries {provided} but its content hashes to {expected}"
        )

    return AssessmentReceipt(payload=canonical, digest=expected)


def validate(receipt: Mapping[str, Any]) -> AssessmentReceipt:
    """Validate the receipt (alias of :func:`canonicalize`)."""
    return canonicalize(receipt)


def load_schema() -> dict[str, Any]:
    """Return the shipped assessment-receipt JSON schema, verbatim."""
    return json.loads(_schema_path().read_text(encoding="utf-8"))


__all__ = [
    "SCHEMA_VERSION",
    "RESULT_AVAILABLE",
    "RESULT_UNAVAILABLE",
    "SEVERITIES",
    "AssessmentError",
    "AssessmentTamperError",
    "AssessmentReceipt",
    "compute_digest",
    "seal",
    "canonicalize",
    "validate",
    "load_schema",
]
