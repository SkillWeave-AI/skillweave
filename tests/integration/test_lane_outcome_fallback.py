"""Integration tests for evidence-backed lane outcomes (SW-159P-OUTCOME-001).

Six criteria are discharged behaviourally over
:mod:`skillweave.selfhost.outcome`:

1. A versioned ``LaneOutcome`` contract distinguishes ``sentinel_confirmed``,
   ``state_confirmed``, ``failed`` and ``inconclusive`` and records every
   evidence source used.
2. A valid sentinel requires the named full SHA to resolve on the declared
   remote branch, descend from the pinned base, and satisfy repository/
   write-scope identity.
3. When the sentinel is absent, ``state_confirmed`` requires exit 0, no
   terminal failure evidence, a freshly fetched remote tip different from and
   descending from the pinned base, allowed write scope, and every lane-required
   verification receipt.
4. A changed local-only commit, an unpushed branch, a wrong repository SHA, a
   forbidden diff, a failed test, a nonzero exit, or an arbitrary ``PASS``
   substring cannot produce success.
5. Read-only review lanes can never pass from a changed commit and still
   require their explicit binary review verdict plus zero reviewed-surface
   changes.
6. Missing-sentinel recovery emits a warning event and preserves stdout/stderr
   digests for later inspection.

The "old false negative" is the regex-only ``OPS_READY`` parse: a lane that
pushed a valid, verified tip but printed no sentinel had no outcome at all. The
first fallback test proves that scenario now turns green (``state_confirmed``),
while every false-positive counterexample stays red.

Self-contained sys.path handling, following the sibling-test convention.
"""

import io
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from skillweave.dispatch.events import DispatchEventStream  # noqa: E402
from skillweave.dispatch.work_contract import WriteScope, sha256_digest  # noqa: E402
from skillweave.selfhost.outcome import (  # noqa: E402
    FAILED,
    INCONCLUSIVE,
    LANE_OUTCOME_CONTRACT_VERSION,
    LANE_OUTCOME_STATES,
    REVIEW_FAIL,
    REVIEW_PASS,
    SENTINEL_CONFIRMED,
    STATE_CONFIRMED,
    WARNING_MISSING_SENTINEL_RECOVERY,
    DiffEvidence,
    LaneEvidence,
    LaneOutcome,
    LaneOutcomeError,
    LaneOutcomeResolver,
    RemoteState,
    VerificationReceipt,
    emit_lane_outcome_warning,
    parse_sentinel,
)

BASE = "a" * 40
TIP = "b" * 40
LOCAL_ONLY = "c" * 40

REPO = "skillweave"
BRANCH = "ops/SW-159P-outcome-verification"

WRITE_SCOPE = WriteScope(
    allow=["src/skillweave/selfhost", "tests/integration"],
)


def _resolver(**over) -> LaneOutcomeResolver:
    kwargs = dict(
        repo=REPO,
        remote_branch=BRANCH,
        pinned_base=BASE,
        write_scope=WRITE_SCOPE,
        required_receipts=["pytest"],
    )
    kwargs.update(over)
    return LaneOutcomeResolver(**kwargs)


def _remote(**over) -> RemoteState:
    kwargs = dict(
        repo=REPO,
        branch=BRANCH,
        tip_sha=TIP,
        freshly_fetched=True,
        descends_from_base=True,
    )
    kwargs.update(over)
    return RemoteState(**kwargs)


def _receipts(*, passed: bool = True):
    return (VerificationReceipt(receipt_type="pytest", passed=passed),)


def _evidence(**over) -> LaneEvidence:
    kwargs = dict(
        exit_code=0,
        termination="exited",
        signal=None,
        stdout=b"",
        stderr=b"",
        remote=_remote(),
        diff=DiffEvidence(changed_paths=("src/skillweave/selfhost/outcome.py",)),
        receipts=_receipts(),
    )
    kwargs.update(over)
    return LaneEvidence(**kwargs)


# ── Criterion 1: versioned contract distinguishes the four states ────────────


def test_contract_is_versioned_and_distinguishes_four_states():
    assert LANE_OUTCOME_CONTRACT_VERSION == "1"
    assert set(LANE_OUTCOME_STATES) == {
        SENTINEL_CONFIRMED,
        STATE_CONFIRMED,
        FAILED,
        INCONCLUSIVE,
    }
    # Every outcome records the contract version it was produced under.
    outcome = LaneOutcome(state=SENTINEL_CONFIRMED)
    assert outcome.version == LANE_OUTCOME_CONTRACT_VERSION
    assert outcome.to_dict()["version"] == LANE_OUTCOME_CONTRACT_VERSION


def test_unknown_state_is_refused():
    try:
        LaneOutcome(state="god_mode")
    except LaneOutcomeError as exc:
        assert exc.field == "outcome.state"
    else:
        raise AssertionError("an unknown lane outcome state must be refused")


def test_outcome_records_every_evidence_source_used():
    evidence = _evidence(stdout=f"OPS_READY {TIP}\n".encode())
    outcome = _resolver().resolve(evidence)
    assert outcome.state == SENTINEL_CONFIRMED
    for source in (
        "process.exit_code",
        "process.termination",
        "process.stdout",
        "sentinel",
        "remote",
        "remote.resolution",
        "ancestry",
        "write_scope",
        "verification_receipts",
    ):
        assert source in outcome.evidence_sources, source


def test_sentinel_parse_requires_full_forty_hex_sha():
    assert parse_sentinel(b"OPS_READY " + TIP.encode()) == TIP
    # A short ref, a branch name, or an ambiguous prefix is not a sentinel.
    assert parse_sentinel(b"OPS_READY abc123") is None
    assert parse_sentinel(b"OPS_READY " + (TIP + "00").encode()) is None
    assert parse_sentinel(b"ALL TESTS PASS") is None


def test_sentinel_parse_ignores_review_token():
    # The review lane's own token must never read as the ops sentinel.
    review_token = f"OPS_READY_FOR_REVIEW SW-159P-OUTCOME-001 {TIP} LANE_OUTCOME_PASS"
    assert parse_sentinel(review_token.encode()) is None


# ── Criterion 2: a valid sentinel is validated against repository state ──────


def test_valid_sentinel_resolves_on_declared_branch_and_descends():
    evidence = _evidence(stdout=f"OPS_READY {TIP}\n".encode())
    outcome = _resolver().resolve(evidence)
    assert outcome.state == SENTINEL_CONFIRMED
    assert outcome.sha == TIP
    assert outcome.confirmed is True
    assert outcome.warning is None


def test_sentinel_naming_sha_not_on_remote_branch_fails():
    # The sentinel names a commit that does not resolve on the declared branch:
    # a local-only, unpushed commit. This can never confirm.
    evidence = _evidence(stdout=f"OPS_READY {LOCAL_ONLY}\n".encode())
    outcome = _resolver().resolve(evidence)
    assert outcome.state == FAILED
    assert outcome.confirmed is False


def test_sentinel_wrong_repository_identity_fails():
    evidence = _evidence(
        stdout=f"OPS_READY {TIP}\n".encode(),
        remote=_remote(repo="some-other-repo"),
    )
    outcome = _resolver().resolve(evidence)
    assert outcome.state == FAILED


def test_sentinel_unfresh_branch_is_unverifiable():
    evidence = _evidence(
        stdout=f"OPS_READY {TIP}\n".encode(),
        remote=_remote(freshly_fetched=False),
    )
    outcome = _resolver().resolve(evidence)
    assert outcome.state == FAILED
    assert "remote.freshness" in outcome.evidence_sources


def test_sentinel_not_descending_from_pinned_base_fails():
    evidence = _evidence(
        stdout=f"OPS_READY {TIP}\n".encode(),
        remote=_remote(descends_from_base=False),
    )
    outcome = _resolver().resolve(evidence)
    assert outcome.state == FAILED


def test_non_tip_sentinel_ancestry_uses_oracle_or_is_inconclusive():
    # A sentinel naming an older pushed commit resolves on the branch only when
    # the oracle proves ancestry from the pinned base.
    older = "d" * 40
    evidence = _evidence(
        stdout=f"OPS_READY {older}\n".encode(),
        remote=_remote(contains=(older,)),
    )
    without_oracle = _resolver().resolve(evidence)
    assert without_oracle.state == INCONCLUSIVE

    with_oracle = _resolver(is_ancestor=lambda a, b: b == older).resolve(evidence)
    assert with_oracle.state == SENTINEL_CONFIRMED
    assert with_oracle.sha == older


# ── Criterion 3: missing-sentinel recovery requires every state fact ─────────


def test_missing_sentinel_old_false_negative_turns_green():
    # THE old false negative: the lane pushed a valid, verified tip but printed
    # no OPS_READY sentinel. The regex-only parse found nothing (no outcome at
    # all); the resolver now confirms the lane from independent state facts.
    stdout = b"implemented lane outcome resolver; all checks green\n"
    assert parse_sentinel(stdout) is None  # the old parse yields nothing

    outcome = _resolver().resolve(_evidence(stdout=stdout))
    assert outcome.state == STATE_CONFIRMED
    assert outcome.sha == TIP
    assert outcome.confirmed is True
    for source in ("fallback.missing_sentinel", "remote.tip", "ancestry", "verification_receipts"):
        assert source in outcome.evidence_sources, source


def test_state_confirmed_requires_freshly_fetched_tip():
    outcome = _resolver().resolve(_evidence(remote=_remote(freshly_fetched=False)))
    assert outcome.state == INCONCLUSIVE
    assert outcome.confirmed is False


def test_state_confirmed_requires_tip_different_from_pinned_base():
    # Nothing pushed: the remote tip is still the pinned base. Not a candidate.
    outcome = _resolver().resolve(_evidence(remote=_remote(tip_sha=BASE)))
    assert outcome.state == INCONCLUSIVE


def test_state_confirmed_requires_descending_tip():
    outcome = _resolver().resolve(_evidence(remote=_remote(descends_from_base=False)))
    assert outcome.state == INCONCLUSIVE


def test_state_confirmed_requires_no_terminal_failure_evidence():
    outcome = _resolver().resolve(_evidence(failure_evidence=("pytest exited 1",)))
    assert outcome.state == FAILED


def test_state_confirmed_requires_all_required_receipts():
    outcome = _resolver().resolve(_evidence(receipts=()))
    assert outcome.state == INCONCLUSIVE


# ── Criterion 4: false positives can never produce success ───────────────────


def test_nonzero_exit_cannot_succeed():
    outcome = _resolver().resolve(_evidence(exit_code=1))
    assert outcome.state == FAILED
    assert outcome.confirmed is False


def test_signalled_process_cannot_succeed():
    outcome = _resolver().resolve(_evidence(signal=9, termination="signaled"))
    assert outcome.state == FAILED


def test_arbitrary_pass_substring_cannot_succeed():
    # Nothing was pushed and no sentinel was printed: a bare PASS string is not
    # evidence. The lane is inconclusive, never confirmed.
    outcome = _resolver().resolve(
        _evidence(stdout=b"ALL TESTS PASS\n", remote=_remote(tip_sha=BASE))
    )
    assert outcome.state == INCONCLUSIVE
    assert outcome.confirmed is False


def test_unpushed_branch_cannot_succeed():
    # A sentinel for a commit that is not on the fetched remote branch: local
    # only, unpushed.
    outcome = _resolver().resolve(_evidence(stdout=f"OPS_READY {LOCAL_ONLY}\n".encode()))
    assert outcome.state == FAILED


def test_forbidden_diff_cannot_succeed():
    outcome = _resolver().resolve(
        _evidence(diff=DiffEvidence(changed_paths=("src/skillweave/cli/main.py",)))
    )
    assert outcome.state == FAILED
    assert outcome.confirmed is False


def test_failed_verification_receipt_cannot_succeed():
    outcome = _resolver().resolve(_evidence(receipts=_receipts(passed=False)))
    assert outcome.state == FAILED
    assert outcome.confirmed is False


def test_declared_receipt_absent_is_never_silently_passing():
    resolver = _resolver(required_receipts=["pytest", "integration"])
    outcome = resolver.resolve(
        _evidence(receipts=(VerificationReceipt(receipt_type="pytest", passed=True),))
    )
    assert outcome.state == INCONCLUSIVE


def test_evidence_objects_refuse_malformed_values():
    try:
        RemoteState(repo=REPO, branch=BRANCH, tip_sha="short")
    except LaneOutcomeError as exc:
        assert exc.field == "remote.tip_sha"
    else:
        raise AssertionError("a non-full remote tip SHA must be refused")

    try:
        _resolver(pinned_base="main")
    except LaneOutcomeError as exc:
        assert exc.field == "pinned_base"
    else:
        raise AssertionError("a branch name as pinned base must be refused")


# ── Criterion 5: read-only reviews need an explicit verdict, zero changes ────


def _review_evidence(**over) -> LaneEvidence:
    kwargs = dict(exit_code=0, termination="exited", signal=None)
    kwargs.update(over)
    return LaneEvidence(**kwargs)


def test_review_changed_surface_never_passes_even_with_pass_verdict():
    outcome = _resolver().resolve_review(
        _review_evidence(stdout=b"REVIEW_PASS\n"),
        verdict=REVIEW_PASS,
        changed_paths=("src/skillweave/selfhost/runner.py",),
    )
    assert outcome.state == FAILED
    assert outcome.confirmed is False


def test_review_pass_requires_explicit_verdict_and_zero_changes():
    outcome = _resolver().resolve_review(
        _review_evidence(stdout=b"looks good\n"),
        verdict=REVIEW_PASS,
        changed_paths=(),
    )
    assert outcome.state == SENTINEL_CONFIRMED
    assert outcome.confirmed is True
    assert outcome.review_verdict == REVIEW_PASS


def test_review_verdict_is_never_inferred_from_stdout():
    # A REVIEW_PASS substring in stdout is not a verdict.
    outcome = _resolver().resolve_review(
        _review_evidence(stdout=b"the report says REVIEW_PASS\n"),
        verdict=None,
        changed_paths=(),
    )
    assert outcome.state == INCONCLUSIVE
    assert outcome.confirmed is False


def test_review_unknown_verdict_is_inconclusive():
    outcome = _resolver().resolve_review(
        _review_evidence(), verdict="APPROVED", changed_paths=()
    )
    assert outcome.state == INCONCLUSIVE


def test_review_explicit_fail_verdict_fails():
    outcome = _resolver().resolve_review(
        _review_evidence(), verdict=REVIEW_FAIL, changed_paths=()
    )
    assert outcome.state == FAILED


# ── Criterion 6: fallback emits a warning and preserves digests ──────────────


def test_missing_sentinel_recovery_emits_warning_with_digests():
    stdout = b"implemented; no sentinel\n"
    stderr = b"a warning line\n"
    outcome = _resolver().resolve(
        _evidence(stdout=stdout, stderr=stderr, remote=_remote())
    )
    assert outcome.state == STATE_CONFIRMED
    assert outcome.warning == WARNING_MISSING_SENTINEL_RECOVERY
    assert outcome.stdout_digest == sha256_digest(stdout)
    assert outcome.stderr_digest == sha256_digest(stderr)

    sink = io.StringIO()
    stream = DispatchEventStream("run-outcome-test", sink)
    event = emit_lane_outcome_warning(
        stream,
        wave="W3",
        lane_id="L1",
        dispatch_id="L1-0",
        outcome=outcome,
    )
    assert event is not None
    typed = stream.typed_events_since()
    assert len(typed) == 1
    payload = typed[0]
    assert payload["warning"] == WARNING_MISSING_SENTINEL_RECOVERY
    assert payload["lane_outcome"] == STATE_CONFIRMED
    assert payload["lane_outcome_contract_version"] == LANE_OUTCOME_CONTRACT_VERSION
    assert payload["stdout_digest"] == sha256_digest(stdout)
    assert payload["stderr_digest"] == sha256_digest(stderr)
    # Metadata-only: no raw stdout/stderr bytes anywhere in the emitted event.
    assert "stdout" not in payload and "stderr" not in payload


def test_sentinel_confirmed_outcome_emits_no_warning():
    outcome = _resolver().resolve(_evidence(stdout=f"OPS_READY {TIP}\n".encode()))
    assert outcome.warning is None
    assert outcome.warning_event() is None

    sink = io.StringIO()
    stream = DispatchEventStream("run-outcome-test-2", sink)
    emitted = emit_lane_outcome_warning(
        stream, wave="W3", lane_id="L1", dispatch_id="L1-0", outcome=outcome
    )
    assert emitted is None
    assert stream.typed_events_since() == []


def _run_all() -> int:
    tests = [
        test_contract_is_versioned_and_distinguishes_four_states,
        test_unknown_state_is_refused,
        test_outcome_records_every_evidence_source_used,
        test_sentinel_parse_requires_full_forty_hex_sha,
        test_sentinel_parse_ignores_review_token,
        test_valid_sentinel_resolves_on_declared_branch_and_descends,
        test_sentinel_naming_sha_not_on_remote_branch_fails,
        test_sentinel_wrong_repository_identity_fails,
        test_sentinel_unfresh_branch_is_unverifiable,
        test_sentinel_not_descending_from_pinned_base_fails,
        test_non_tip_sentinel_ancestry_uses_oracle_or_is_inconclusive,
        test_missing_sentinel_old_false_negative_turns_green,
        test_state_confirmed_requires_freshly_fetched_tip,
        test_state_confirmed_requires_tip_different_from_pinned_base,
        test_state_confirmed_requires_descending_tip,
        test_state_confirmed_requires_no_terminal_failure_evidence,
        test_state_confirmed_requires_all_required_receipts,
        test_nonzero_exit_cannot_succeed,
        test_signalled_process_cannot_succeed,
        test_arbitrary_pass_substring_cannot_succeed,
        test_unpushed_branch_cannot_succeed,
        test_forbidden_diff_cannot_succeed,
        test_failed_verification_receipt_cannot_succeed,
        test_declared_receipt_absent_is_never_silently_passing,
        test_evidence_objects_refuse_malformed_values,
        test_review_changed_surface_never_passes_even_with_pass_verdict,
        test_review_pass_requires_explicit_verdict_and_zero_changes,
        test_review_verdict_is_never_inferred_from_stdout,
        test_review_unknown_verdict_is_inconclusive,
        test_review_explicit_fail_verdict_fails,
        test_missing_sentinel_recovery_emits_warning_with_digests,
        test_sentinel_confirmed_outcome_emits_no_warning,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
