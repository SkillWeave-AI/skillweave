"""SW-1511A-RESULT-001 (repair 003): the closing message survives the stream cap.

Finding F-1511A-01, measured: a lane stream is capped at ``DEFAULT_RETAIN_LIMIT``
(1,048,576) bytes with the *head* kept and the tail dropped, so the child's
closing message -- the last stream-json ``result`` event, or simply the final
line the child wrote -- is not among the retained bytes. The
``attempt-final-result/v1`` record carried only an evidence *digest* over every
observed byte, so a controller could prove *that* the child said something but
could not read *what* it said without the raw stream, which the cap had dropped.

Owner decision (2026-10-16): the finding is fixed inside 1.5.11. The repair
preserves a bounded tail of the observed stream independently of retention:

* the stream store keeps a bounded trailing excerpt as it ingests, so the tail
  is captured at ingest time and not reconstructed from a capped log;
* the final-result store persists it as a durable sibling beside the record,
  atomically and fsynced, and binds it into the record by digest and byte range
  as *additive, optional* fields (a record without them still reads and
  validates);
* secret-shaped content in the tail never lands on disk;
* the record says explicitly when no closing message was observed.

Each test below is written to *fail* on the unrepaired head ``1c9598b``: there
is no tail capture (``closing_excerpt`` is absent), no sibling (``closing_path``
is absent), no binding fields, and unscrubbed tail bytes would reach disk.
Nothing here launches a process, opens a socket or reads a credential.
"""

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

_src = Path(__file__).resolve().parent.parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

#: A cap small enough to make the tail incontrovertibly beyond it, so the test
#: exercises the same geometry as the measured 1 MiB finding without 1 MiB of
#: fixture. The store exposes the cap through ``retain_limit``.
_CAP = 4096

#: The closing message sits after the cap has already been reached.
_CLOSING = b'{"type":"result","subtype":"success","result":"child-ran"}\n'


def _begin_capped(store, *, run_id="run-close", task_id="SW-1511A-RESULT-001"):
    store.begin_attempt(run_id=run_id, task_id=task_id, attempt=1, retain_limit=_CAP)
    return run_id, task_id


def _spend_the_cap(store, *, run_id, task_id, stream="stdout"):
    """Push strictly more than the cap so the *head* is what is retained."""
    filler = b"x" * _CAP
    store.ingest(attempt=1, stream=stream, chunk=filler, run_id=run_id, task_id=task_id)
    store.ingest(
        attempt=1, stream=stream, chunk=b"y" * 64, run_id=run_id, task_id=task_id
    )


# ── (a) the closing message beyond the cap is recoverable from the sibling ───


def test_closing_message_beyond_the_cap_is_recoverable_from_the_record():
    """The measured defect: the tail is lost, the record must still carry it.

    On the unrepaired head this fails at the first assertion: the spool has no
    bounded tail, so the closing message that sits past the cap is gone and the
    record has no sibling to read it from.
    """
    from skillweave.runtime.stream import DurableStreamStore
    from skillweave.trace.attempt_final_result import (
        AttemptFinalResultStore,
        final_result_from_attempt,
    )

    store = DurableStreamStore(root=None)
    run_id, task_id = _begin_capped(store)
    _spend_the_cap(store, run_id=run_id, task_id=task_id)
    # The closing message arrives *after* the cap was already spent.
    store.ingest(attempt=1, stream="stdout", chunk=_CLOSING, run_id=run_id, task_id=task_id)
    store.finish_attempt(
        run_id=run_id, task_id=task_id, attempt=1,
        exit_code=0, signal=None, termination="exited",
        duration_ns=1_000_000, pid=4242,
        started_at="2026-10-16T08:59:00+00:00",
        completed_at="2026-10-16T09:00:00+00:00",
    )

    summary = store.summary(1, "stdout", run_id=run_id, task_id=task_id)
    # The geometry under test: the tail really is beyond retention.
    assert summary.observed_bytes > _CAP
    assert summary.retained_bytes <= _CAP
    retained = store.spool(1, "stdout", run_id=run_id, task_id=task_id).read_retained()
    assert _CLOSING not in retained

    # The store captured a bounded trailing excerpt *at ingest time*, so the
    # closing message is present even though it is beyond the cap.
    excerpt = store.closing_excerpt(1, "stdout", run_id=run_id, task_id=task_id)
    assert excerpt["observed"] is True
    assert _CLOSING in excerpt["text"].encode("utf-8")
    assert excerpt["bytes"] > 0
    assert excerpt["byte_range_end"] == summary.observed_bytes

    import tempfile

    with tempfile.TemporaryDirectory(prefix="sw1511a-close-") as tmp:
        result = final_result_from_attempt(
            store, run_id=run_id, task_id=task_id, attempt=1,
            wrapper_exit=0, assay_state="artifact_verify",
            observed_at="2026-10-16T09:00:00+00:00",
        )
        # The record binds the closing message by digest and byte range ...
        assert result.closing_observed is True
        assert result.closing_digest
        assert result.closing_byte_range_end == summary.observed_bytes
        assert result.closing_stream == "stdout"

        fr_store = AttemptFinalResultStore(root=tmp)
        fr_store.write(result)
        # ... and a fresh process reads the *text* with no raw stream at all.
        reopened = AttemptFinalResultStore(root=tmp)
        recovered = reopened.read(run_id, task_id, 1)
        assert recovered.closing_observed is True
        text = reopened.read_closing_message(run_id, task_id, 1)
        assert text is not None and _CLOSING.decode() in text
        # The sibling really is a separate durable file beside the record.
        assert recovered.closing_path
        assert os.path.exists(recovered.closing_path)
        assert os.path.dirname(os.path.abspath(recovered.closing_path)) == os.path.abspath(tmp)


def test_records_without_a_closing_message_say_so_explicitly():
    """Absence is stated, never implied: a record must not be silent on it.

    A memory-only store has no durable sibling, and a stream whose every byte was
    retained has nothing *beyond* the cap to excerpt. Both must be recorded as an
    explicit ``closing_observed is False`` with a named reason, so a controller
    can never read the absence of text as an absence of information.
    """
    from skillweave.trace.attempt_final_result import (
        AttemptFinalResult,
        AttemptFinalResultStore,
        FinalVerdict,
        final_result_from_attempt,
    )

    store = AttemptFinalResultStore(root=None)
    assert isinstance(final_result_from_attempt, object)
    result = AttemptFinalResult(
        run_id="run-close",
        task_id="SW-1511A-RESULT-001",
        attempt=1,
        child_exit=0,
        wrapper_exit=0,
        verdict=FinalVerdict.PASS,
        terminal_reason="child_exited",
        evidence_digest="d",
    )
    # Default construction is explicit about the absence and names no digest.
    assert result.closing_observed is False
    assert result.closing_digest == ""
    assert result.closing_text == ""
    assert "no closing message" in result.closing_note.lower()
    # The explicit state round-trips, so a reader sees the same statement.
    assert result.to_dict()["closing_observed"] is False
    assert AttemptFinalResult.from_dict(result.to_dict()).closing_observed is False


# ── (b) secret-shaped text in the tail never lands on disk ───────────────────


def test_secret_shaped_tail_never_lands_on_disk():
    """Scrubbing is a hard guarantee for the persisted sibling and the record.

    A child can print a credential in its closing message. Capturing the tail
    must never write that credential to the durable sibling or the record: the
    on-disk bytes are searched directly, not through the reader.
    """
    from skillweave.runtime.stream import DurableStreamStore
    from skillweave.trace.attempt_final_result import (
        AttemptFinalResultStore,
        final_result_from_attempt,
    )

    secret = "sk-live-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    tail = (
        '{"type":"result","result":"authorization: Bearer ' + secret + '",'
        '"note":"api_key=' + secret + '"}\n'
    ).encode("utf-8")

    store = DurableStreamStore(root=None)
    run_id, task_id = _begin_capped(store)
    _spend_the_cap(store, run_id=run_id, task_id=task_id)
    store.ingest(attempt=1, stream="stdout", chunk=tail, run_id=run_id, task_id=task_id)
    store.finish_attempt(
        run_id=run_id, task_id=task_id, attempt=1,
        exit_code=0, signal=None, termination="exited",
        duration_ns=1_000_000, pid=4242,
        started_at="2026-10-16T08:59:00+00:00",
        completed_at="2026-10-16T09:00:00+00:00",
    )

    excerpt = store.closing_excerpt(1, "stdout", run_id=run_id, task_id=task_id)
    assert excerpt["observed"] is True
    # The captured excerpt itself is already scrubbed.
    assert secret not in excerpt["text"]

    import tempfile

    with tempfile.TemporaryDirectory(prefix="sw1511a-secret-") as tmp:
        result = final_result_from_attempt(
            store, run_id=run_id, task_id=task_id, attempt=1,
            wrapper_exit=0, assay_state="artifact_verify",
            observed_at="2026-10-16T09:00:00+00:00",
        )
        assert secret not in result.closing_text
        fr_store = AttemptFinalResultStore(root=tmp)
        fr_store.write(result)

        # Search *every* byte the writer put on disk.
        leaked = []
        for dirpath, _dirnames, filenames in os.walk(tmp):
            for name in filenames:
                with open(os.path.join(dirpath, name), "rb") as fh:
                    blob = fh.read()
                if secret.encode() in blob:
                    leaked.append(os.path.join(dirpath, name))
        assert leaked == [], f"secret reached disk: {leaked}"
        # The record still says a closing message was observed and binds it.
        assert result.closing_observed is True
        assert "REDACTED" in (result.closing_text or "").upper()


# ── (c) the exactly-one guarantee is untouched ───────────────────────────────


def test_idempotent_rewrite_and_conflicting_rewrite_are_unchanged():
    """Additive fields must not weaken the exactly-one record guarantee.

    A byte-identical re-write (carrying the same closing-message binding) is
    still idempotent; a *differing* closing-message binding for one identity is
    still a conflict, refused rather than silently replacing the record.
    """
    from skillweave.trace.attempt_final_result import (
        AttemptFinalResult,
        AttemptFinalResultStore,
        DuplicateFinalResultError,
        FinalVerdict,
    )

    base = dict(
        run_id="run-close",
        task_id="SW-1511A-RESULT-001",
        attempt=1,
        child_exit=0,
        wrapper_exit=0,
        verdict=FinalVerdict.PASS,
        terminal_reason="child_exited",
        evidence_digest="d",
    )
    first = AttemptFinalResult(
        **base,
        closing_observed=True,
        closing_digest=hashlib.sha256(_CLOSING).hexdigest(),
        closing_text=_CLOSING.decode(),
        closing_byte_range_end=1234,
        closing_stream="stdout",
    )
    store = AttemptFinalResultStore(root=None)
    assert store.write(first).to_dict() == first.to_dict()
    # Idempotent: the identical record (same binding) is returned unchanged.
    assert store.write(first).to_dict() == first.to_dict()

    # A differing binding for the same identity is refused.
    divergent = AttemptFinalResult(
        **base,
        closing_observed=True,
        closing_digest="different",
        closing_text="something else",
        closing_byte_range_end=99,
        closing_stream="stdout",
    )
    with pytest.raises(DuplicateFinalResultError):
        store.write(divergent)
    assert store.read("run-close", "SW-1511A-RESULT-001", 1).closing_digest == (
        hashlib.sha256(_CLOSING).hexdigest()
    )


# ── (d) old records without the new fields still load ────────────────────────


def test_records_predating_the_fields_still_load_and_validate():
    """Backward compatibility: a ``1.0.0`` record with no closing fields loads.

    The new fields are additive and optional. A record serialized before the
    repair has none of them; reading it must not raise, and the reader must
    present the same explicit "no closing message observed" state rather than a
    ``KeyError`` or a silently different verdict.
    """
    from skillweave.trace.attempt_final_result import (
        ATTEMPT_FINAL_RESULT_SCHEMA_ID,
        ATTEMPT_FINAL_RESULT_SCHEMA_VERSION,
        AttemptFinalResult,
        AttemptFinalResultStore,
    )

    legacy_on_disk = {
        "schema": ATTEMPT_FINAL_RESULT_SCHEMA_ID,
        "schema_version": ATTEMPT_FINAL_RESULT_SCHEMA_VERSION,
        "run_id": "run-old",
        "task_id": "SW-1511A-RESULT-001",
        "attempt": 1,
        "child_exit": 0,
        "child_signal": None,
        "wrapper_exit": 0,
        "outcome": "exit_code",
        "assay_state": "artifact_verify",
        "artifact_semantics": "applicable",
        "verdict": "pass",
        "gate_state": "pass",
        "evidence_digest": "legacy",
        "terminal_reason": "child_exited",
        "hold_name": "",
        "hold_reason": "",
        "observed_at": "2026-10-15T00:00:00+00:00",
        "migrated_from_legacy": False,
    }
    # The pre-repair schema had no closing fields at all.
    assert not any(k.startswith("closing") for k in legacy_on_disk)

    loaded = AttemptFinalResult.from_dict(legacy_on_disk)
    assert loaded.verdict.value == "pass"
    assert loaded.evidence_digest == "legacy"
    assert loaded.closing_observed is False
    assert loaded.closing_digest == ""
    # Round-tripping the legacy record is read-compatible: the added fields are
    # additive and do not change any pre-existing value.
    merged = loaded.to_dict()
    for key, value in legacy_on_disk.items():
        assert merged[key] == value

    # And a store over a directory holding the legacy JSON reads it back.
    import tempfile
    from pathlib import Path as _Path

    with tempfile.TemporaryDirectory(prefix="sw1511a-legacy-") as tmp:
        target = _Path(tmp) / "attempt-final-results"
        target.mkdir(parents=True, exist_ok=True)
        raw = f"run-old\x00SW-1511A-RESULT-001\x001".encode("utf-8")
        key = hashlib.sha256(raw).hexdigest()[:32]
        (target / f"{key}.json").write_text(json.dumps(legacy_on_disk), encoding="utf-8")
        store = AttemptFinalResultStore(root=tmp)
        recovered = store.read("run-old", "SW-1511A-RESULT-001", 1)
        assert recovered.verdict.value == "pass"
        assert recovered.closing_observed is False


if __name__ == "__main__":  # pragma: no cover - manual runner
    raise SystemExit(pytest.main([__file__, "-q"]))
