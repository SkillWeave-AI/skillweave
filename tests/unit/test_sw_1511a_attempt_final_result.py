"""SW-1511A-RESULT-001: the unambiguous attempt final result.

This file is the adversarial proof for the three lane criteria. Each criterion
has a dedicated ``test_criterionN_*`` function written to *fail* on the frozen
base: the base has no ``skillweave.trace.attempt_final_result`` module at all,
so every criterion test reaches the contract through an explicit
``_import_final_result()`` that raises (never skips, never xfails).

* criterion 1 -- every launched attempt writes exactly one
  ``attempt-final-result/v1`` containing child exit, wrapper exit, Assay state,
  verdict, evidence digest and terminal reason;
* criterion 2 -- a successful child with intentionally absent artifact
  semantics is a named hold or a non-applicable gate, never an undifferentiated
  process failure;
* criterion 3 -- verdict and receipt remain available when stream output
  exceeds the retention limit, and no resume is needed solely to recover a
  final message.

Criterion 3 is proved against the *real* :class:`DurableStreamStore` with a tiny
retain limit, so the retention-independence is exercised on production code and
not on a mock. Nothing here launches a process, reads a wall clock, opens a
socket or touches a provider credential.
"""

import hashlib
import importlib
import json
import sys
from pathlib import Path

import pytest

_src = Path(__file__).resolve().parent.parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))


def _import_final_result():
    """Import the contract, or fail the test on the frozen base.

    The base SHA this task is measured against has no attempt-final-result
    module: the resulting :class:`ModuleNotFoundError` is the red proof. It is
    deliberately *not* converted to a skip or an xfail.
    """
    from skillweave.trace import attempt_final_result

    return attempt_final_result


# ── criterion 1 ──────────────────────────────────────────────────────────────


def test_criterion1_exactly_one_result_per_attempt_with_all_dimensions():
    """Criterion 1: exactly one v1 per launched attempt, every dimension present.

    Red on the base: there is no contract module, so no result can be written.
    """
    fr = _import_final_result()

    result = fr.AttemptFinalResult(
        run_id="run-1511a",
        task_id="SW-1511A-RESULT-001",
        attempt=1,
        child_exit=0,
        wrapper_exit=0,
        verdict=fr.FinalVerdict.PASS,
        terminal_reason="child_exited",
        evidence_digest=fr.evidence_digest(["d-stdout", "d-stderr"]),
        assay_state="artifact_verify",
        observed_at="2026-10-16T09:00:00+00:00",
    )

    # The six required dimensions are *separate* fields, none derived from
    # another: child exit, wrapper exit, assay state, verdict, evidence digest
    # and terminal reason.
    payload = result.to_dict()
    assert payload["schema"] == fr.ATTEMPT_FINAL_RESULT_SCHEMA_ID
    assert payload["schema_version"] == fr.ATTEMPT_FINAL_RESULT_SCHEMA_VERSION
    assert fr.ATTEMPT_FINAL_RESULT_SCHEMA_VERSION in (
        fr.SUPPORTED_ATTEMPT_FINAL_RESULT_VERSIONS
    )
    assert payload["child_exit"] == 0
    assert payload["wrapper_exit"] == 0
    assert payload["assay_state"] == "artifact_verify"
    assert payload["verdict"] == "pass"
    assert payload["evidence_digest"] == fr.evidence_digest(["d-stdout", "d-stderr"])
    assert payload["terminal_reason"] == "child_exited"
    assert payload["outcome"] == fr.ChildOutcome.EXIT_CODE.value

    # The record round-trips through JSON with its identity intact.
    assert fr.AttemptFinalResult.from_dict(json.loads(json.dumps(payload))).to_dict() == payload
    assert result.identity == ("run-1511a", "SW-1511A-RESULT-001", 1)

    # A child signal and a child exit are mutually exclusive machine outcomes,
    # never one ambiguous blob.
    with pytest.raises(fr.AttemptFinalResultError):
        fr.AttemptFinalResult(
            run_id="run-1511a",
            task_id="SW-1511A-RESULT-001",
            attempt=2,
            child_exit=0,
            child_signal=9,
            wrapper_exit=137,
            verdict=fr.FinalVerdict.FAIL,
            terminal_reason="child_exited",
            evidence_digest="d",
        )
    # A child that never ran is a technical terminal reason with its own name.
    launch = fr.AttemptFinalResult(
        run_id="run-1511a",
        task_id="SW-1511A-RESULT-001",
        attempt=3,
        child_exit=None,
        wrapper_exit=127,
        verdict=fr.FinalVerdict.INCONCLUSIVE,
        terminal_reason="launch_failed",
        evidence_digest="d",
    )
    assert launch.outcome == fr.ChildOutcome.LAUNCH_FAILED.value
    assert launch.terminal_reason in fr.TECHNICAL_TERMINAL_REASONS

    # An unknown schema version is refused, not coerced: backward
    # compatibility is explicit.
    with pytest.raises(fr.UnknownFinalResultVersionError):
        fr.AttemptFinalResult(
            run_id="run-1511a",
            task_id="SW-1511A-RESULT-001",
            attempt=4,
            child_exit=0,
            wrapper_exit=0,
            verdict=fr.FinalVerdict.PASS,
            terminal_reason="child_exited",
            evidence_digest="d",
            schema_version="2.0.0",
        )

    # Exactly one result per attempt identity: an identical re-write is
    # idempotent, a differing one is refused.
    store = fr.AttemptFinalResultStore()
    assert store.write(result).identity == result.identity
    assert store.write(result).to_dict() == payload
    with pytest.raises(fr.DuplicateFinalResultError):
        store.write(fr.AttemptFinalResult(
            run_id="run-1511a",
            task_id="SW-1511A-RESULT-001",
            attempt=1,
            child_exit=0,
            wrapper_exit=0,
            verdict=fr.FinalVerdict.FAIL,
            terminal_reason="child_exited",
            evidence_digest="other",
        ))
    assert store.read("run-1511a", "SW-1511A-RESULT-001", 1).verdict is fr.FinalVerdict.PASS

    # The evidence digest is order-independent over the stream set, so it is a
    # pure function of the observed bytes and not of ingestion order.
    assert fr.evidence_digest(["a", "b"]) == fr.evidence_digest(["b", "a"])

    # The barrel is the documented consumer path and must name the same class.
    barrel = importlib.import_module("skillweave.trace")
    assert barrel.AttemptFinalResult is fr.AttemptFinalResult
    assert barrel.ATTEMPT_FINAL_RESULT_SCHEMA_ID == fr.ATTEMPT_FINAL_RESULT_SCHEMA_ID


# ── criterion 2 ──────────────────────────────────────────────────────────────


def test_criterion2_intentional_absence_is_named_never_a_process_failure():
    """Criterion 2: absent-artifact success is a named hold / non-applicable gate.

    Red on the base: no contract exists to distinguish the two.
    """
    fr = _import_final_result()

    # (a) A named hold: the child succeeded, its artifacts are deliberately
    # absent, and the result *names* the hold.
    hold = fr.AttemptFinalResult(
        run_id="run-1511a",
        task_id="SW-1511A-RESULT-001",
        attempt=1,
        child_exit=0,
        wrapper_exit=0,
        verdict=fr.FinalVerdict.HOLD,
        terminal_reason="child_exited",
        evidence_digest="d",
        assay_state="hold",
        artifact_semantics=fr.ArtifactSemantics.NAMED_HOLD,
        hold_name="cms-publication-prereq",
        hold_reason="CMS publication prerequisites unavailable",
    )
    assert hold.verdict is fr.FinalVerdict.HOLD
    assert hold.gate_state == "hold"
    assert hold.hold_name and "prerequisites" in hold.hold_reason
    assert hold.to_dict()["artifact_semantics"] == "named_hold"

    # (b) A non-applicable gate: same successful child, a different *named*
    # decision, still never an undifferentiated failure.
    na = fr.AttemptFinalResult(
        run_id="run-1511a",
        task_id="SW-1511A-RESULT-001",
        attempt=2,
        child_exit=0,
        wrapper_exit=0,
        verdict=fr.FinalVerdict.NOT_APPLICABLE,
        terminal_reason="child_exited",
        evidence_digest="d",
        artifact_semantics=fr.ArtifactSemantics.NOT_APPLICABLE,
        hold_name="live-qualification",
        hold_reason="not applicable to this slice",
    )
    assert na.verdict is fr.FinalVerdict.NOT_APPLICABLE
    assert na.gate_state == "not_applicable"

    # (c) The flattened case is *refused*: a successful child with
    # intentionally absent artifacts recorded as a bare process failure.
    with pytest.raises(fr.UndifferentiatedProcessFailureError):
        fr.AttemptFinalResult(
            run_id="run-1511a",
            task_id="SW-1511A-RESULT-001",
            attempt=3,
            child_exit=0,
            wrapper_exit=0,
            verdict=fr.FinalVerdict.FAIL,
            terminal_reason="child_exited",
            evidence_digest="d",
            artifact_semantics=fr.ArtifactSemantics.NAMED_HOLD,
            hold_name="cms-publication-prereq",
        )
    # A mixed-up semantics/verdict pair is likewise refused rather than
    # silently normalised.
    with pytest.raises(fr.UndifferentiatedProcessFailureError):
        fr.AttemptFinalResult(
            run_id="run-1511a",
            task_id="SW-1511A-RESULT-001",
            attempt=4,
            child_exit=0,
            wrapper_exit=0,
            verdict=fr.FinalVerdict.HOLD,
            terminal_reason="child_exited",
            evidence_digest="d",
            artifact_semantics=fr.ArtifactSemantics.NOT_APPLICABLE,
            hold_reason="named",
        )

    # The derivation agrees: intentional absence on a successful child yields a
    # hold / non-applicable verdict and never `fail`.
    assert fr.derive_final_verdict(
        child_exit=0,
        child_signal=None,
        terminal_reason="child_exited",
        assay_state="hold",
        artifact_semantics=fr.ArtifactSemantics.NAMED_HOLD,
    ) is fr.FinalVerdict.HOLD
    assert fr.derive_final_verdict(
        child_exit=0,
        child_signal=None,
        terminal_reason="child_exited",
        artifact_semantics=fr.ArtifactSemantics.NOT_APPLICABLE,
    ) is fr.FinalVerdict.NOT_APPLICABLE
    # A technical end is inconclusive, never a fabricated task failure.
    assert fr.derive_final_verdict(
        child_exit=None,
        child_signal=None,
        terminal_reason="launch_failed",
    ) is fr.FinalVerdict.INCONCLUSIVE
    # A genuine child failure is still a failure.
    assert fr.derive_final_verdict(
        child_exit=1,
        child_signal=None,
        terminal_reason="child_exited",
    ) is fr.FinalVerdict.FAIL

    # The vocabulary aligns with the accepted ledger's hold / live axis states,
    # so the two contracts do not drift apart.
    from skillweave.trace import task_ledger as tl

    assert "not_applicable" in tl.AXIS_STATES[tl.AXIS_LIVE_QUALIFICATION]
    assert "named_open" in tl.AXIS_STATES[tl.AXIS_HOLD]
    assert "hold" in fr.ASSAY_STATES


# ── criterion 3 ──────────────────────────────────────────────────────────────


def test_criterion3_verdict_and_receipt_survive_exceeded_retention():
    """Criterion 3: retention-capped stream still yields the verdict and receipt.

    Red on the base: no writer/reader exists, so nothing survives anything.
    """
    fr = _import_final_result()
    from skillweave.runtime.assay import AssayState
    from skillweave.runtime.stream import DEFAULT_RETAIN_LIMIT, DurableStreamStore

    retain_limit = 64
    assert retain_limit < DEFAULT_RETAIN_LIMIT

    store = DurableStreamStore(retain_limit=retain_limit)
    store.begin_attempt(run_id="run-1511a", task_id="SW-1511A-RESULT-001", attempt=1)
    chunk = b"x" * 256
    for _ in range(8):
        store.ingest(
            attempt=1,
            stream="stdout",
            chunk=chunk,
            run_id="run-1511a",
            task_id="SW-1511A-RESULT-001",
        )
    store.ingest(
        attempt=1,
        stream="stderr",
        chunk=b"e" * 256,
        run_id="run-1511a",
        task_id="SW-1511A-RESULT-001",
    )
    store.finish_attempt(
        run_id="run-1511a",
        task_id="SW-1511A-RESULT-001",
        attempt=1,
        exit_code=0,
        signal=None,
        termination="exited",
        duration_ns=1_000_000,
        pid=4242,
        started_at="2026-10-16T08:59:00+00:00",
        completed_at="2026-10-16T09:00:00+00:00",
    )

    summary = store.summary(1, "stdout", run_id="run-1511a", task_id="SW-1511A-RESULT-001")
    # The stream genuinely exceeded retention: only a bounded prefix was kept
    # while every observed byte was still hashed and counted.
    assert summary.observed_bytes == 8 * 256
    assert summary.retained_bytes <= retain_limit
    assert summary.retained_bytes < summary.observed_bytes
    assert summary.digest != summary.retained_digest

    # The evidence digest is taken from the whole-observed digest, never the
    # retained prefix, so it is the exact digest of everything that arrived.
    digest = fr.evidence_digest_from_store(
        store, run_id="run-1511a", task_id="SW-1511A-RESULT-001", attempt=1
    )
    expected = fr.evidence_digest([
        store.summary(1, "stdout", run_id="run-1511a", task_id="SW-1511A-RESULT-001").digest,
        store.summary(1, "stderr", run_id="run-1511a", task_id="SW-1511A-RESULT-001").digest,
    ])
    assert digest == expected
    # A store that never observed the attempt yields the digest of nothing
    # rather than crashing: the evidence digest degrades honestly.
    empty = DurableStreamStore(retain_limit=DEFAULT_RETAIN_LIMIT)
    assert fr.evidence_digest_from_store(
        empty, run_id="run-1511a", task_id="SW-1511A-RESULT-001", attempt=1
    ) == fr.evidence_digest([])

    # A full record can be reconciled from the retention-capped store.
    result = fr.final_result_from_attempt(
        store,
        run_id="run-1511a",
        task_id="SW-1511A-RESULT-001",
        attempt=1,
        wrapper_exit=0,
        assay_state=AssayState.ARTIFACT_VERIFY,
        observed_at="2026-10-16T09:00:00+00:00",
    )
    assert result.evidence_digest == digest
    assert result.assay_state == "artifact_verify"
    assert result.verdict is fr.FinalVerdict.PASS

    # The verdict and its receipt are written durably *outside* the stream log,
    # so they are readable without any resume solely to recover them.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="sw1511a-fr-") as tmp:
        durable = fr.AttemptFinalResultStore(root=tmp)
        durable.write(result)
        # A brand-new store over the same root (a fresh process) recovers it.
        reopened = fr.AttemptFinalResultStore(root=tmp)
        recovered = reopened.read("run-1511a", "SW-1511A-RESULT-001", 1)
        assert recovered.to_dict() == result.to_dict()
        assert recovered.evidence_digest == digest
        assert recovered.gate_state == "pass"
        # The persisted bytes are the contract's own envelope, not a preview.
        on_disk = json.loads(
            next(Path(tmp).rglob("*.json")).read_text(encoding="utf-8")
        )
        assert on_disk["schema"] == fr.ATTEMPT_FINAL_RESULT_SCHEMA_ID
        assert on_disk["evidence_digest"] == digest

    # The trace-layer digest is honestly derived from the store, not a
    # fabricated constant: it changes when the observed bytes change.
    other = DurableStreamStore(retain_limit=retain_limit)
    other.begin_attempt(run_id="run-1511a", task_id="SW-1511A-RESULT-001", attempt=1)
    other.ingest(
        attempt=1,
        stream="stdout",
        chunk=b"y" * 256,
        run_id="run-1511a",
        task_id="SW-1511A-RESULT-001",
    )
    assert fr.evidence_digest_from_store(
        other, run_id="run-1511a", task_id="SW-1511A-RESULT-001", attempt=1
    ) != digest
    assert fr.evidence_digest_from_store(
        other, run_id="run-1511a", task_id="SW-1511A-RESULT-001", attempt=1
    ) == hashlib.sha256(
        json.dumps(
            sorted(
                other.summary(
                    attempt=1,
                    stream=name,
                    run_id="run-1511a",
                    task_id="SW-1511A-RESULT-001",
                ).digest
                for name in ("stdout", "stderr")
            ),
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


# ── compatibility adapter (the migration clause) ─────────────────────────────


def test_legacy_exit_raw_stream_attempts_remain_readable_via_adapter():
    """Legacy exit/raw-stream attempts migrate through an explicit adapter."""
    fr = _import_final_result()

    migrated = fr.adapt_legacy_attempt(
        run_id="run-1511a",
        task_id="SW-1511A-RESULT-001",
        attempt=1,
        exit_code=0,
        raw_stream_digest="legacy-digest",
        observed_at="2026-10-16T09:00:00+00:00",
    )
    assert migrated.migrated_from_legacy is True
    assert migrated.verdict is fr.FinalVerdict.PASS
    assert migrated.to_dict()["migrated_from_legacy"] is True
    # A legacy signalled attempt is a technical end, not a fabricated failure.
    signalled = fr.adapt_legacy_attempt(
        run_id="run-1511a",
        task_id="SW-1511A-RESULT-001",
        attempt=2,
        exit_code=None,
        signal=9,
        termination="signaled",
    )
    assert signalled.terminal_reason == "child_signaled"
    assert signalled.verdict is fr.FinalVerdict.INCONCLUSIVE
    assert signalled.outcome == fr.ChildOutcome.SIGNAL.value


if __name__ == "__main__":  # pragma: no cover - manual runner
    raise SystemExit(pytest.main([__file__, "-q"]))
