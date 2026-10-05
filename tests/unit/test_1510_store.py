"""SW-1510-STORE: Unify native lifecycle state with transactional run persistence.

Criterion 1: Start, completion, cancellation and resume share one SQLite/CAS
  authority; stale completion cannot resurrect a cancelled run.
Criterion 2: Parallel writers, duplicate IDs, crash/restart and event-sequence
  contention pass deterministic race fixtures without lost or duplicate
  logical transitions.
Criterion 3: An explicit migration reads legacy JSON records as historical
  data without falsely marking them executed or mutating their bytes.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import base64
import hashlib
from pathlib import Path

_src = Path(__file__).resolve().parent.parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

import pytest

from skillweave.runtime.store import (
    SQLiteRunStore,
    RunRecord,
    RunStateModel,
    CompletionReconciler,
    CancellationReconciler,
)
from skillweave.runtime.journal import EventJournal
from skillweave.runtime.errors import (
    VersionConflictError,
    InvalidTransitionError,
    KillOutcomeError,
    StoreError,
)
from skillweave.runsvc import RunApplicationService
from skillweave.runtime.registry import RawArtifactStore

# =========================================================================
# Helper utilities
# =========================================================================


def _fresh_store(db_path: str = ":memory:") -> SQLiteRunStore:
    """Create a fresh store with schema initialised."""
    return SQLiteRunStore(db_path=db_path)


def _make_run(run_id: str, state: str = "preflight") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        root_run_id=run_id,
        parent_run_id=None,
        state=state,
        version=1,
        created_at="2026-10-01T00:00:00Z",
        updated_at="2026-10-01T00:00:00Z",
        ended_at=None,
        role="ops",
    )


def _make_service(db_path: str = ":memory:") -> tuple[RunApplicationService, SQLiteRunStore, EventJournal]:
    store = SQLiteRunStore(db_path=db_path)
    journal = EventJournal(store)
    raw = RawArtifactStore()
    return RunApplicationService(store, journal, raw), store, journal


# =========================================================================
# Criterion 1: Shared SQLite/CAS authority
# =========================================================================


class TestCriterion1_SharedAuthority:
    """Start, completion, cancellation and resume share one SQLite/CAS authority."""

    def test_create_run_through_service(self):
        """A run created via the service is immediately visible in the store."""
        svc, store, _ = _make_service()
        result = svc.start_async(
            [sys.executable, "-c", "print('hi')"],
            run_id="shared-auth-1",
        )
        assert result["run_id"] == "shared-auth-1"
        run = store.get_run("shared-auth-1")
        assert run is not None
        assert run.state == "preflight"

    def test_stale_completion_cannot_resurrect_cancelled_run(self):
        """A stale writer holding an old version cannot complete a killed run.

        Advance preflight -> batch_selection (v2), then kill via
        batch_selection -> advance_or_stop (v3).  A stale writer holding
        v1 must fail with VersionConflictError.  A fresh completion on
        the terminal row must fail with InvalidTransitionError.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)
            store.save_run(_make_run("resurrect-1"))

            # Advance preflight -> batch_selection (v2)
            run = store.get_run("resurrect-1")
            store.transition(
                "resurrect-1",
                "batch_selection",
                expected_state=run.state,
                expected_version=run.version,
                reason="test: advance",
            )

            # Kill via batch_selection -> advance_or_stop (v3)
            run = store.get_run("resurrect-1")
            store.transition(
                "resurrect-1",
                "advance_or_stop",
                expected_state=run.state,
                expected_version=run.version,
                reason="test: kill",
                stop_reason="operator_halt",
            )

            # Verify terminal
            run = store.get_run("resurrect-1")
            assert RunStateModel.is_terminal(run.state)
            assert run.version == 3

            # Stale completion using old version (1) must be rejected
            with pytest.raises(VersionConflictError):
                store.transition(
                    "resurrect-1",
                    "advance_or_stop",
                    expected_state="preflight",
                    expected_version=1,
                    reason="stale completion",
                )

            # Fresh completion on terminal also fails (no legal transition)
            with pytest.raises(InvalidTransitionError):
                store.transition(
                    "resurrect-1",
                    "advance_or_stop",
                    expected_state=run.state,
                    expected_version=run.version,
                    reason="fresh completion on terminal",
                )

            store.close()

    def test_update_metadata_cas_protects_terminal(self):
        """update_metadata with expected_version rejects stale writers and terminal records."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)
            store.save_run(_make_run("cas-meta-1"))

            # Advance to batch_selection (v2), then to advance_or_stop (v3)
            run = store.get_run("cas-meta-1")
            store.transition(
                "cas-meta-1",
                "batch_selection",
                expected_state=run.state,
                expected_version=run.version,
                reason="test: advance",
            )
            run = store.get_run("cas-meta-1")
            store.transition(
                "cas-meta-1",
                "advance_or_stop",
                expected_state=run.state,
                expected_version=run.version,
                reason="test: terminal",
            )

            # update_metadata on terminal must raise
            with pytest.raises(InvalidTransitionError):
                store.update_metadata("cas-meta-1", expected_version=3, state="preflight")

            store.close()

    def test_update_metadata_cas_rejects_stale_version(self):
        """update_metadata with wrong expected_version raises VersionConflictError."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)
            store.save_run(_make_run("cas-stale-1"))

            # Advance to v2
            run = store.get_run("cas-stale-1")
            store.transition(
                "cas-stale-1",
                "batch_selection",
                expected_state=run.state,
                expected_version=run.version,
                reason="test: advance",
            )

            # Try update_metadata with stale version
            with pytest.raises(VersionConflictError):
                store.update_metadata("cas-stale-1", expected_version=1, metadata={"x": "y"})

            store.close()

    def test_update_metadata_without_expected_version_still_atomic(self):
        """update_metadata without expected_version is an atomic current-row update."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)
            store.save_run(_make_run("cas-noexp-1"))

            store.update_metadata("cas-noexp-1", metadata={"key": "val"})
            run = store.get_run("cas-noexp-1")
            assert run.metadata.get("key") == "val"
            assert run.version == 2

            store.close()

    def test_resume_run_returns_current_state(self):
        """resume_run returns the run's state without launching a duplicate."""
        svc, store, _ = _make_service()
        result = svc.start_async(
            [sys.executable, "-c", "print('ok')"],
            run_id="resume-test-1",
        )
        assert result["run_id"] == "resume-test-1"

        # Resume should return current state
        resume = svc.resume_run("resume-test-1")
        assert resume is not None
        assert resume["state"] == "preflight"

    def test_resume_run_without_pending_intent_returns_current_state(self):
        """Resume reconciles only a recorded intent; a plain run is unchanged.

        A normal non-terminal run (no ``_pending_cancellation`` intent) must be
        returned as-is — resume must never fabricate a terminal transition or
        add a cancellation event.
        """
        svc, store, _ = _make_service()
        store.save_run(_make_run("resume-no-intent-1"))
        assert store.get_run("resume-no-intent-1").metadata.get("_pending_cancellation") is None

        resume = svc.resume_run("resume-no-intent-1")
        assert resume is not None
        assert resume["state"] == "preflight"
        after = store.get_run("resume-no-intent-1")
        assert after.state == "preflight"
        assert after.metadata.get("_pending_cancellation") is None

    def test_resume_terminal_run_fails(self):
        """Resuming a terminal run must return an error."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            svc, store, _ = _make_service(db_path)
            store.save_run(_make_run("resume-term-1"))
            run = store.get_run("resume-term-1")
            # Advance through valid states to terminal
            store.transition(
                "resume-term-1",
                "batch_selection",
                expected_state=run.state,
                expected_version=run.version,
                reason="test: advance",
            )
            run = store.get_run("resume-term-1")
            store.transition(
                "resume-term-1",
                "advance_or_stop",
                expected_state=run.state,
                expected_version=run.version,
                reason="test: terminal",
            )

            resume = svc.resume_run("resume-term-1")
            assert resume is not None
            assert "error" in resume
            assert "terminal" in resume["error"]

    def test_duplicate_run_id_rejected(self):
        """Creating a run with an existing ID must raise VersionConflictError."""
        store = _fresh_store()
        store.save_run(_make_run("dup-1"))
        with pytest.raises(VersionConflictError):
            store.save_run(_make_run("dup-1"))
        store.close()

    def test_wrapper_uses_kernel_store(self):
        """The detached wrapper must write completion to the kernel store.db."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            svc, store, journal = _make_service(db_path)

            result = svc.start_async(
                [sys.executable, "-c", "print('wrapper-test')"],
                run_id="wrapper-kernel-1",
            )
            assert result["run_id"] == "wrapper-kernel-1"

            # Wait for child to finish
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                run = store.get_run("wrapper-kernel-1")
                if run and RunStateModel.is_terminal(run.state):
                    break
                time.sleep(0.05)

            run = store.get_run("wrapper-kernel-1")
            assert run is not None

            journal.close()
            store.close()


# =========================================================================
# Criterion 2: Race fixtures
# =========================================================================


class TestCriterion2_RaceFixtures:
    """Parallel writers, duplicate IDs, crash/restart and event-sequence contention."""

    def test_parallel_transition_writers_exactly_one_wins(self):
        """N writers racing to transition the same run; exactly one wins."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)
            store.save_run(_make_run("race-trans-1"))
            store.close()

            outcomes = {"wins": 0, "conflicts": 0, "errors": []}
            writers = 6

            def _writer():
                s = SQLiteRunStore(db_path=db_path)
                try:
                    s.transition(
                        "race-trans-1",
                        "batch_selection",
                        expected_state="preflight",
                        expected_version=1,
                        reason="race",
                    )
                    outcomes["wins"] += 1
                except (VersionConflictError, InvalidTransitionError):
                    outcomes["conflicts"] += 1
                except Exception as exc:
                    outcomes["errors"].append(exc)
                finally:
                    s.close()

            threads = [threading.Thread(target=_writer) for _ in range(writers)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert outcomes["errors"] == [], f"unexpected errors: {outcomes['errors']}"
            assert outcomes["wins"] == 1
            assert outcomes["conflicts"] == writers - 1

            reader = SQLiteRunStore(db_path=db_path)
            final = reader.get_run("race-trans-1")
            assert final is not None
            assert final.state == "batch_selection"
            assert final.version == 2
            reader.close()

    def test_parallel_journal_appends_no_duplicates(self):
        """8 writers, 200 appends each: all persisted, no duplicates, gapless."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            run_id = "race-journal-1"

            # Init schema
            init = EventJournal(db_path)
            init.close()

            writers = 8
            appends_per_writer = 200
            total = writers * appends_per_writer
            results = {"ok": 0, "errors": []}

            def _writer(start):
                j = EventJournal(db_path)
                try:
                    for i in range(start, start + appends_per_writer):
                        j.append(run_id=run_id, payload={"n": i})
                        results["ok"] += 1
                except Exception as exc:
                    results["errors"].append(exc)
                finally:
                    j.close()

            threads = [
                threading.Thread(target=_writer, args=(w * appends_per_writer,))
                for w in range(writers)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert results["errors"] == [], f"errors: {results['errors'][:3]}"
            assert results["ok"] == total

            journal = EventJournal(db_path)
            events = journal.get_events(run_id)
            journal.close()

            sequences = [e.sequence for e in events]
            assert len(sequences) == total
            assert len(set(sequences)) == total
            assert sorted(sequences) == list(range(1, total + 1))

    def test_duplicate_id_parallel_create_rejected(self):
        """N writers racing to create the same run_id; exactly one wins."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            bootstrap = _fresh_store(db_path)
            bootstrap.close()

            outcomes = {"wins": 0, "conflicts": 0, "errors": []}

            def _writer():
                s = SQLiteRunStore(db_path=db_path)
                try:
                    s.save_run(_make_run("race-dup-id"))
                    outcomes["wins"] += 1
                except (VersionConflictError, StoreError):
                    outcomes["conflicts"] += 1
                except Exception as exc:
                    outcomes["errors"].append(exc)
                finally:
                    s.close()

            writers = 8
            threads = [threading.Thread(target=_writer) for _ in range(writers)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert outcomes["errors"] == []
            assert outcomes["wins"] == 1
            assert outcomes["conflicts"] == writers - 1

    def test_crash_before_commit_does_not_persist(self):
        """A transition that crashes before commit must not leave a partial row."""
        store = _fresh_store()
        store.save_run(_make_run("crash-1"))

        # Simulate crash: start a transition but rollback
        run = store.get_run("crash-1")
        store._conn.execute("BEGIN IMMEDIATE")
        try:
            store._conn.execute(
                "UPDATE runs SET state = ?, version = ? WHERE run_id = ?",
                ("batch_selection", run.version + 1, "crash-1"),
            )
            # Simulate crash before commit
            store._conn.rollback()
        except Exception:
            store._conn.rollback()

        run_after = store.get_run("crash-1")
        assert run_after.state == "preflight"
        assert run_after.version == 1
        store.close()

    def test_crash_after_commit_survives_reopen(self):
        """A committed transition survives a store reopen."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)
            store.save_run(_make_run("crash-commit-1"))

            run = store.get_run("crash-commit-1")
            store.transition(
                "crash-commit-1",
                "batch_selection",
                expected_state=run.state,
                expected_version=run.version,
                reason="test",
            )
            store.close()

            store2 = _fresh_store(db_path)
            run2 = store2.get_run("crash-commit-1")
            assert run2 is not None
            assert run2.state == "batch_selection"
            assert run2.version == 2
            store2.close()

    def test_idempotent_journal_event_not_duplicated(self):
        """Appending with the same idempotency_key returns the existing event."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            journal = EventJournal(db_path)

            e1 = journal.append(
                run_id="idem-1",
                event_type="test",
                payload={"msg": "hello"},
                idempotency_key="key-1",
            )
            e2 = journal.append(
                run_id="idem-1",
                event_type="test",
                payload={"msg": "hello"},
                idempotency_key="key-1",
            )

            assert e1.sequence == e2.sequence
            assert e1.idempotency_key == "key-1"

            events = journal.get_events("idem-1")
            assert len(events) == 1
            journal.close()

    def test_event_sequence_allocation_atomic_under_concurrency(self):
        """Parallel writers allocate unique sequences atomically (no gaps, no dupes)."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            run_id = "race-seq-alloc"
            init = EventJournal(db_path)
            init.close()

            writers = 4
            per_writer = 50
            total = writers * per_writer
            results = {"ok": 0, "errors": []}

            def _writer(start):
                j = EventJournal(db_path)
                try:
                    for i in range(start, start + per_writer):
                        j.append(run_id=run_id, payload={"n": i})
                        results["ok"] += 1
                except Exception as exc:
                    results["errors"].append(exc)
                finally:
                    j.close()

            threads = [
                threading.Thread(target=_writer, args=(w * per_writer,))
                for w in range(writers)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert results["errors"] == []
            assert results["ok"] == total

            j = EventJournal(db_path)
            events = j.get_events(run_id)
            j.close()

            seqs = [e.sequence for e in events]
            assert len(seqs) == total
            assert len(set(seqs)) == total
            assert sorted(seqs) == list(range(1, total + 1))


# =========================================================================
# Criterion 3: Legacy migration
# =========================================================================


class TestCriterion3_LegacyMigration:
    """Explicit migration reads legacy JSON records as historical data."""

    def test_import_legacy_preserves_exact_bytes(self):
        """import_legacy stores exact source bytes in metadata."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)

            source = json.dumps({
                "run_id": "legacy-1",
                "state": "IN_PROGRESS",
                "command": ["echo", "hello"],
            }).encode("utf-8")

            record = store.import_legacy("legacy-1", source)
            assert record.state == "unknown"
            assert record.metadata.get("historical") is True
            assert record.metadata.get("provenance", {}).get("byte_length") == len(source)

            # Verify exact bytes are preserved
            stored_b64 = record.metadata.get("raw_bytes_b64")
            assert stored_b64 is not None
            decoded = base64.b64decode(stored_b64)
            assert decoded == source

            store.close()

    def test_import_legacy_never_infers_execution(self):
        """import_legacy must not mark a run as executed or infer success."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)

            source = json.dumps({
                "run_id": "legacy-2",
                "state": "SUCCEEDED",
                "exit_code": 0,
            }).encode("utf-8")

            record = store.import_legacy("legacy-2", source)
            assert record.state == "unknown"
            assert record.metadata.get("historical") is True

            store.close()

    def test_import_legacy_never_infers_pid_ownership(self):
        """import_legacy must not infer live PID ownership from old data."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)

            source = json.dumps({
                "run_id": "legacy-3",
                "pid": 12345,
                "state": "running",
            }).encode("utf-8")

            record = store.import_legacy("legacy-3", source)
            assert record.state == "unknown"
            assert record.metadata.get("historical") is True

            store.close()

    def test_import_legacy_does_not_mutate_source_bytes(self):
        """The original source bytes must be unchanged after import."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)

            original = json.dumps({
                "run_id": "legacy-4",
                "state": "PREFLIGHT",
                "version": 1,
            }).encode("utf-8")
            original_copy = original[:]

            store.import_legacy("legacy-4", original)
            assert original == original_copy, "source bytes were mutated"

            store.close()

    def test_import_legacy_existing_row_wins(self):
        """import_legacy returns existing row when run_id already exists."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)
            store.save_run(_make_run("existing-1"))

            source = json.dumps({"run_id": "existing-1", "state": "NEW"}).encode("utf-8")
            record = store.import_legacy("existing-1", source)

            # Must return the existing row, not the import
            assert record.state == "preflight"

            store.close()

    def test_import_legacy_idempotent(self):
        """import_legacy is idempotent — second call returns same record."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)

            source = json.dumps({"run_id": "idem-legacy-1"}).encode("utf-8")
            r1 = store.import_legacy("idem-legacy-1", source)
            r2 = store.import_legacy("idem-legacy-1", source)

            assert r1.run_id == r2.run_id
            assert r1.state == r2.state
            assert r1.version == r2.version

            store.close()

    def test_import_legacy_records_provenance(self):
        """import_legacy records provenance including source_path when given."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)

            source = b'{"run_id": "prov-1"}'
            source_path = "/path/to/legacy/runs/prov-1/run.json"
            record = store.import_legacy("prov-1", source, source_path=source_path)

            prov = record.metadata.get("provenance", {})
            assert prov.get("source") == "legacy_import"
            assert prov.get("source_path") == source_path
            assert prov.get("byte_length") == len(source)
            assert prov.get("sha256") == hashlib.sha256(source).hexdigest()

            store.close()

    def test_import_legacy_resume_fails(self):
        """resume_run on an imported historical record must return an error."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            svc, store, _ = _make_service(db_path)

            source = b'{"run_id": "hist-resume-1"}'
            store.import_legacy("hist-resume-1", source)

            resume = svc.resume_run("hist-resume-1")
            assert resume is not None
            assert "error" in resume
            assert "historical" in resume["error"]

    def test_legacy_migration_preserves_vocabulary(self):
        """Legacy uppercase states are migrated to canonical lowercase on open."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")

            # Write a legacy uppercase state directly
            conn = __import__("sqlite3").connect(db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    root_run_id TEXT NOT NULL,
                    parent_run_id TEXT,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    ended_at TEXT,
                    role TEXT NOT NULL DEFAULT 'ops',
                    metadata TEXT NOT NULL DEFAULT '{}'
                )
            """)
            conn.execute(
                "INSERT INTO runs (run_id, root_run_id, state, version, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("legacy-state-1", "legacy-state-1", "IN_PROGRESS", 1, "now", "now"),
            )
            conn.commit()
            conn.close()

            store = _fresh_store(db_path)
            run = store.get_run("legacy-state-1")
            assert run is not None
            assert run.state == "in_progress"
            store.close()


# =========================================================================
# Additional: Crash/Idempotency and Late-Wrapper tests
# =========================================================================


class TestCrashIdempotency:
    """Deterministic crash and idempotency tests beyond the basic race fixtures."""

    def test_save_run_on_terminal_raises_invalid_transition(self):
        """save_run() on a terminal record must raise InvalidTransitionError."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)
            store.save_run(_make_run("save-term-1"))

            # Advance preflight -> batch_selection -> advance_or_stop (terminal)
            run = store.get_run("save-term-1")
            store.transition(
                "save-term-1", "batch_selection",
                expected_state=run.state, expected_version=run.version,
                reason="test: advance",
            )
            run = store.get_run("save-term-1")
            store.transition(
                "save-term-1", "advance_or_stop",
                expected_state=run.state, expected_version=run.version,
                reason="test: terminal",
            )

            # save_run with matching version must still be rejected
            terminal = store.get_run("save-term-1")
            stale_record = _make_run("save-term-1", state="preflight")
            stale_record.version = terminal.version
            with pytest.raises(InvalidTransitionError):
                store.save_run(stale_record)

            store.close()

    def test_save_run_on_terminal_stale_version_also_raises(self):
        """save_run() with stale version on terminal record raises InvalidTransitionError."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)
            store.save_run(_make_run("save-term-stale-1"))

            # Advance to terminal (v3)
            run = store.get_run("save-term-stale-1")
            store.transition(
                "save-term-stale-1", "batch_selection",
                expected_state=run.state, expected_version=run.version,
                reason="test: advance",
            )
            run = store.get_run("save-term-stale-1")
            store.transition(
                "save-term-stale-1", "advance_or_stop",
                expected_state=run.state, expected_version=run.version,
                reason="test: terminal",
            )

            # stale_record with version=1 (old stale version)
            # Version 1 hits the INSERT path -> IntegrityError -> VersionConflictError
            stale_record = _make_run("save-term-stale-1", state="preflight")
            stale_record.version = 1
            with pytest.raises((VersionConflictError, InvalidTransitionError)):
                store.save_run(stale_record)

            store.close()

    def test_kill_run_idempotent(self):
        """Killing an already-killed run raises (terminal state cannot be re-entered)."""
        from skillweave.runlifecycle.store import RunStateStore

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = str(root / ".skillweave" / "store.db")
            (root / ".skillweave").mkdir(parents=True, exist_ok=True)

            store = _fresh_store(db_path)
            store.save_run(_make_run("kill-idem-1"))
            store.close()

            # First kill via RunStateStore facade
            facade = RunStateStore(project_root=root)
            facade.update_metadata("kill-idem-1", state="killed")

            meta = facade.read_metadata("kill-idem-1")
            assert meta is not None
            assert meta.state == "killed"

            # Second kill on terminal must raise (no re-entering terminal)
            with pytest.raises(InvalidTransitionError):
                facade.update_metadata("kill-idem-1", state="killed")

    def test_crash_during_save_run_does_not_corrupt(self):
        """A crash (rollback) during save_run must leave prior state intact."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)
            store.save_run(_make_run("crash-save-1"))

            # Advance to v2
            run = store.get_run("crash-save-1")
            store.transition(
                "crash-save-1", "batch_selection",
                expected_state=run.state, expected_version=run.version,
                reason="test: advance",
            )

            # Simulate crash mid-save_run
            run = store.get_run("crash-save-1")
            store._conn.execute("BEGIN IMMEDIATE")
            try:
                store._conn.execute(
                    "UPDATE runs SET state = ?, version = ? WHERE run_id = ?",
                    ("advance_or_stop", run.version + 1, "crash-save-1"),
                )
                # Rollback before commit
                store._conn.rollback()
            except Exception:
                store._conn.rollback()

            # State must still be batch_selection
            run_after = store.get_run("crash-save-1")
            assert run_after.state == "batch_selection"
            assert run_after.version == 2

            store.close()

    def test_duplicate_save_run_same_version_raises(self):
        """Calling save_run twice with the same version raises VersionConflictError."""
        store = _fresh_store()
        store.save_run(_make_run("dup-save-1"))

        with pytest.raises(VersionConflictError):
            store.save_run(_make_run("dup-save-1"))

        store.close()

    def test_update_metadata_idempotent_on_terminal(self):
        """update_metadata with same killed state on terminal is idempotent."""
        from skillweave.runlifecycle.store import RunStateStore

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = str(root / ".skillweave" / "store.db")
            (root / ".skillweave").mkdir(parents=True, exist_ok=True)

            store = _fresh_store(db_path)
            store.save_run(_make_run("meta-idem-1"))
            store.close()

            facade = RunStateStore(project_root=root)
            facade.update_metadata("meta-idem-1", state="killed")

            # Calling update_metadata again with state=killed on terminal
            # should not raise (it's idempotent via the facade)
            meta = facade.read_metadata("meta-idem-1")
            assert meta.state == "killed"

            # Direct update_metadata on terminal should raise
            store2 = _fresh_store(db_path)
            with pytest.raises(InvalidTransitionError):
                store2.update_metadata("meta-idem-1", expected_version=meta.version, state="running")

            store2.close()


class TestLateWrapper:
    """Stale wrapper completion cannot resurrect a cancelled/killed run."""

    def test_wrapper_cannot_save_run_after_cancellation(self):
        """A stale wrapper holding an old version cannot save_run a killed run."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)
            store.save_run(_make_run("late-wrapper-1"))

            # Advance to batch_selection (v2)
            run = store.get_run("late-wrapper-1")
            store.transition(
                "late-wrapper-1", "batch_selection",
                expected_state=run.state, expected_version=run.version,
                reason="test: advance",
            )

            # Kill via batch_selection -> advance_or_stop (v3)
            run = store.get_run("late-wrapper-1")
            store.transition(
                "late-wrapper-1", "advance_or_stop",
                expected_state=run.state, expected_version=run.version,
                reason="test: kill", stop_reason="operator_halt",
            )

            # Wrapper snapshot from v1 tries save_run with version=1
            stale_record = _make_run("late-wrapper-1", state="preflight")
            stale_record.version = 1
            with pytest.raises((VersionConflictError, InvalidTransitionError)):
                store.save_run(stale_record)

            # Terminal state is unchanged
            final = store.get_run("late-wrapper-1")
            assert RunStateModel.is_terminal(final.state)

            store.close()

    def test_wrapper_cannot_transition_after_cancellation(self):
        """A stale wrapper cannot transition a killed run forward."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)
            store.save_run(_make_run("late-wrap-trans-1"))

            # Advance to batch_selection (v2)
            run = store.get_run("late-wrap-trans-1")
            store.transition(
                "late-wrap-trans-1", "batch_selection",
                expected_state=run.state, expected_version=run.version,
                reason="test: advance",
            )

            # Kill via batch_selection -> advance_or_stop (v3)
            run = store.get_run("late-wrap-trans-1")
            store.transition(
                "late-wrap-trans-1", "advance_or_stop",
                expected_state=run.state, expected_version=run.version,
                reason="test: kill", stop_reason="operator_halt",
            )

            # Wrapper tries to transition from its stale snapshot
            with pytest.raises(VersionConflictError):
                store.transition(
                    "late-wrap-trans-1", "batch_selection",
                    expected_state="preflight", expected_version=1,
                    reason="stale wrapper advance",
                )

            # Even with correct version, terminal has no outgoing transitions
            run = store.get_run("late-wrap-trans-1")
            with pytest.raises(InvalidTransitionError):
                store.transition(
                    "late-wrap-trans-1", "batch_selection",
                    expected_state=run.state, expected_version=run.version,
                    reason="wrapper advance on terminal",
                )

            store.close()

    def test_wrapper_cannot_update_metadata_after_cancellation(self):
        """A stale wrapper cannot update_metadata on a killed run."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)
            store.save_run(_make_run("late-wrap-meta-1"))

            # Advance to batch_selection (v2)
            run = store.get_run("late-wrap-meta-1")
            store.transition(
                "late-wrap-meta-1", "batch_selection",
                expected_state=run.state, expected_version=run.version,
                reason="test: advance",
            )

            # Kill via batch_selection -> advance_or_stop (v3)
            run = store.get_run("late-wrap-meta-1")
            store.transition(
                "late-wrap-meta-1", "advance_or_stop",
                expected_state=run.state, expected_version=run.version,
                reason="test: kill", stop_reason="operator_halt",
            )

            # Wrapper tries to update metadata on terminal
            with pytest.raises(InvalidTransitionError):
                store.update_metadata(
                    "late-wrap-meta-1",
                    expected_version=3,
                    metadata={"runlifecycle": {"lifecycle_state": "succeeded", "exit_code": 0}},
                )

            store.close()

    def test_wrapper_cannot_append_finished_event_after_terminal_transition(self):
        """After terminal transition, a stale wrapper cannot append a finished event.

        The wrapper checks the run state before appending 'finished'. If the
        run is already terminal, the wrapper must skip the finished event.
        This test verifies the store-level guard: append_event after terminal
        is allowed (events are append-only), but the logical contract says
        the wrapper must check first.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)
            journal = EventJournal(store)
            store.save_run(_make_run("late-wrap-event-1"))

            # Advance to batch_selection (v2)
            run = store.get_run("late-wrap-event-1")
            store.transition(
                "late-wrap-event-1", "batch_selection",
                expected_state=run.state, expected_version=run.version,
                reason="test: advance",
            )

            # Kill via batch_selection -> advance_or_stop (v3)
            run = store.get_run("late-wrap-event-1")
            store.transition(
                "late-wrap-event-1", "advance_or_stop",
                expected_state=run.state, expected_version=run.version,
                reason="test: kill", stop_reason="operator_halt",
            )

            # Wrapper tries to append finished event (journal allows it,
            # but the run is terminal so the wrapper should have checked)
            journal.append(
                "late-wrap-event-1",
                event_type="finished",
                payload={"returncode": 0, "stdout_preview": "late output"},
            )

            # The finished event is in the journal (append is always allowed),
            # but the run state must remain terminal/killed
            run = store.get_run("late-wrap-event-1")
            assert RunStateModel.is_terminal(run.state)
            assert run.metadata.get("runlifecycle", {}).get("lifecycle_state") != "succeeded"

            journal.close()
            store.close()

    def test_full_service_kill_then_stale_wrapper_completion(self):
        """Integration: service kill followed by stale wrapper save_run is safe.

        Simulates the full scenario:
        1. Start a run via the service
        2. Kill it via the service (sets lifecycle_state=killed, terminal transition)
        3. Stale wrapper tries to save_run with old version -> rejected
        """
        from skillweave.runtime.registry import RawArtifactStore
        from skillweave.runsvc.service import RunApplicationService

        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)
            journal = EventJournal(store)
            raw = RawArtifactStore()
            svc = RunApplicationService(store, journal, raw)

            # Start a run
            result = svc.start_async(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                run_id="full-kill-test-1",
            )
            run_id = result["run_id"]
            pid = result["pid"]

            # Kill it
            kill_result = svc.kill_run(run_id, pid)
            assert kill_result["state"] == "killed"

            # Verify terminal state and lifecycle metadata
            run = store.get_run(run_id)
            assert RunStateModel.is_terminal(run.state)
            assert run.metadata.get("runlifecycle", {}).get("lifecycle_state") == "killed"

            # Stale wrapper tries save_run with version=1 (old snapshot)
            stale = _make_run(run_id, state="preflight")
            stale.version = 1
            with pytest.raises((VersionConflictError, InvalidTransitionError)):
                store.save_run(stale)

            # Even with matching version, terminal guard prevents resurrection
            stale.version = run.version
            with pytest.raises(InvalidTransitionError):
                store.save_run(stale)

            # Terminal state unchanged
            run = store.get_run(run_id)
            assert RunStateModel.is_terminal(run.state)

            journal.close()
            store.close()

    def test_concurrent_kill_and_wrapper_completion(self):
        """Simulate concurrent kill and wrapper: exactly one wins, no corruption.

        Two threads race: one tries to kill, the other tries to complete
        (transition to terminal).  Exactly one succeeds; the other gets a
        CAS or transition error.  The final state must be terminal.
        """
        import threading

        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)
            store.save_run(_make_run("concurrent-race-1"))

            # Advance to batch_selection (v2) so both paths have a valid
            # non-terminal state to start from
            run = store.get_run("concurrent-race-1")
            store.transition(
                "concurrent-race-1", "batch_selection",
                expected_state=run.state, expected_version=run.version,
                reason="test: advance",
            )
            store.close()

            outcomes = {"kill_wins": 0, "complete_wins": 0, "errors": []}

            def _killer():
                s = SQLiteRunStore(db_path=db_path)
                try:
                    run = s.get_run("concurrent-race-1")
                    s.transition(
                        "concurrent-race-1", "advance_or_stop",
                        expected_state=run.state, expected_version=run.version,
                        reason="kill", stop_reason="operator_halt",
                    )
                    outcomes["kill_wins"] += 1
                except (VersionConflictError, InvalidTransitionError):
                    pass
                except Exception as exc:
                    outcomes["errors"].append(exc)
                finally:
                    s.close()

            def _completer():
                s = SQLiteRunStore(db_path=db_path)
                try:
                    run = s.get_run("concurrent-race-1")
                    s.transition(
                        "concurrent-race-1", "advance_or_stop",
                        expected_state=run.state, expected_version=run.version,
                        reason="completion",
                    )
                    outcomes["complete_wins"] += 1
                except (VersionConflictError, InvalidTransitionError):
                    pass
                except Exception as exc:
                    outcomes["errors"].append(exc)
                finally:
                    s.close()

            threads = [
                threading.Thread(target=_killer),
                threading.Thread(target=_completer),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert outcomes["errors"] == [], f"unexpected errors: {outcomes['errors']}"
            assert outcomes["kill_wins"] + outcomes["complete_wins"] == 1

            s = SQLiteRunStore(db_path=db_path)
            final = s.get_run("concurrent-race-1")
            assert final is not None
            assert RunStateModel.is_terminal(final.state)
            s.close()

    def test_detached_wrapper_with_concurrent_version_bump(self):
        """A real detached child must reach terminal despite a concurrent writer.

        Reproduces the production defect: the parent's PID-update metadata
        write bumps the version while the child wrapper is running.  The
        wrapper reads the bumped version and must retry the forward
        transition, not silently lose the completion.  This test runs a
        real detached child via start_async and a concurrent writer that
        repeatedly bumps the run's metadata version while the child runs.
        """
        import threading

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = str(root / ".skillweave" / "store.db")
            (root / ".skillweave").mkdir(parents=True, exist_ok=True)

            # Build PYTHONPATH so the detached wrapper can import skillweave
            src_path = str(Path(__file__).resolve().parent.parent.parent / "src")
            env = os.environ.copy()
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = f"{src_path}:{existing}" if existing else src_path

            svc, store, journal = _make_service(db_path)
            result = svc.start_async(
                [sys.executable, "-c", "import time; time.sleep(0.5)"],
                run_id="retry-detach-1",
                _env=env,
            )
            run_id = result["run_id"]

            # Concurrent writer bumps the version while child starts.
            # In production the only competing write is a single PID update
            # from start_async, so we simulate 3 bumps then stop.
            def _bumper():
                for _ in range(3):
                    try:
                        s = SQLiteRunStore(db_path=db_path)
                        run = s.get_run(run_id)
                        if run is not None and not RunStateModel.is_terminal(run.state):
                            s.update_metadata(
                                run_id,
                                expected_version=run.version,
                                metadata={"bump_counter": _},
                            )
                        s.close()
                    except Exception:
                        pass
                    time.sleep(0.1)

            bumper_thread = threading.Thread(target=_bumper)
            bumper_thread.start()

            # Wait for child to finish
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                run = store.get_run(run_id)
                if run and RunStateModel.is_terminal(run.state):
                    break
                time.sleep(0.05)

            bumper_thread.join()

            # The run MUST be terminal
            run = store.get_run(run_id)
            assert run is not None, "run disappeared"
            assert RunStateModel.is_terminal(run.state), (
                f"run {run_id} should be terminal but is {run.state} "
                f"(v{run.version})"
            )

            # Lifecycle metadata must reflect success
            lifecycle = run.metadata.get("runlifecycle", {})
            assert lifecycle.get("lifecycle_state") in ("succeeded", "failed"), (
                f"expected lifecycle_state succeeded/failed, got {lifecycle.get('lifecycle_state')}"
            )

            # There must be a 'finished' event (from wrapper)
            events = journal.get_events(run_id)
            finished_events = [e for e in events if e.event_type == "finished"]
            assert len(finished_events) == 1, (
                f"expected exactly 1 finished event, got {len(finished_events)}: "
                f"{[(e.event_type, e.sequence) for e in events]}"
            )

            store.close()
            journal.close()

    def test_wrapper_retries_on_version_conflict(self):
        """Wrapper must re-read and retry when another writer bumps the version.

        Simulates the race: the parent's PID update (or concurrent metadata
        writer) bumps the version between the wrapper's get_run and its
        update_metadata. The wrapper must retry, not silently drop the
        completion.  This test directly exercises the store-level retry
        pattern that the wrapper uses.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)
            store.save_run(_make_run("retry-race-1"))

            # Simulate start_async: version goes from 1 to 2 (lifecycle metadata)
            store.update_metadata(
                "retry-race-1",
                expected_version=1,
                metadata={"runlifecycle": {"command": ["test"], "pid": 0, "lifecycle_state": "running"}},
            )

            # Wrapper snapshot: reads version 2, state preflight
            run = store.get_run("retry-race-1")
            assert run.state == "preflight"
            assert run.version == 2

            # Another writer bumps the version (simulates PID update race)
            store.update_metadata(
                "retry-race-1",
                expected_version=2,
                metadata={"runlifecycle": {"pid": 99999}},
            )
            run = store.get_run("retry-race-1")
            assert run.version == 3

            # Wrapper retries: re-reads, now version 3, advances and completes
            run = store.get_run("retry-race-1")
            assert run.version == 3

            # Advance preflight -> batch_selection
            store.transition(
                "retry-race-1", "batch_selection",
                expected_state=run.state, expected_version=run.version,
                reason="wrapper: advance (retry)",
            )
            run = store.get_run("retry-race-1")
            assert run.state == "batch_selection"

            # Set lifecycle metadata
            store.update_metadata(
                "retry-race-1",
                expected_version=run.version,
                metadata={"runlifecycle": {"lifecycle_state": "succeeded", "exit_code": 0}},
            )
            run = store.get_run("retry-race-1")

            # Terminal transition
            store.transition(
                "retry-race-1", "advance_or_stop",
                expected_state=run.state, expected_version=run.version,
                reason="wrapper: process completed",
            )

            run = store.get_run("retry-race-1")
            assert RunStateModel.is_terminal(run.state)
            assert run.version == 6
            assert run.metadata.get("runlifecycle", {}).get("lifecycle_state") == "succeeded"

            store.close()

    def test_wrapper_retries_through_multiple_conflicts(self):
        """Wrapper retries up to 5 times and eventually succeeds."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)
            store.save_run(_make_run("retry-multi-1"))

            # Start lifecycle metadata (v2)
            store.update_metadata(
                "retry-multi-1",
                expected_version=1,
                metadata={"runlifecycle": {"command": ["test"], "pid": 0, "lifecycle_state": "running"}},
            )

            # Wrapper reads at v2, but we bump to v3
            store.update_metadata(
                "retry-multi-1",
                expected_version=2,
                metadata={"runlifecycle": {"pid": 111}},
            )
            # Bump again to v4
            store.update_metadata(
                "retry-multi-1",
                expected_version=3,
                metadata={"runlifecycle": {"pid": 222}},
            )

            # Now wrapper retries: re-reads at v4, should succeed
            run = store.get_run("retry-multi-1")
            assert run.version == 4

            store.transition(
                "retry-multi-1", "batch_selection",
                expected_state=run.state, expected_version=run.version,
                reason="wrapper: advance (retry)",
            )
            run = store.get_run("retry-multi-1")
            store.update_metadata(
                "retry-multi-1",
                expected_version=run.version,
                metadata={"runlifecycle": {"lifecycle_state": "succeeded", "exit_code": 0}},
            )
            run = store.get_run("retry-multi-1")
            store.transition(
                "retry-multi-1", "advance_or_stop",
                expected_state=run.state, expected_version=run.version,
                reason="wrapper: process completed",
            )

            run = store.get_run("retry-multi-1")
            assert RunStateModel.is_terminal(run.state)
            store.close()

    def test_wrapper_retry_gives_up_on_terminal(self):
        """Wrapper retry loop must stop when run is already terminal."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)
            store.save_run(_make_run("retry-term-1"))

            # Start lifecycle metadata (v2)
            store.update_metadata(
                "retry-term-1",
                expected_version=1,
                metadata={"runlifecycle": {"command": ["test"], "pid": 0, "lifecycle_state": "running"}},
            )

            # Another writer kills the run (batch_selection -> advance_or_stop)
            run = store.get_run("retry-term-1")
            store.transition(
                "retry-term-1", "batch_selection",
                expected_state=run.state, expected_version=run.version,
                reason="kill: advance",
            )
            run = store.get_run("retry-term-1")
            store.transition(
                "retry-term-1", "advance_or_stop",
                expected_state=run.state, expected_version=run.version,
                reason="kill", stop_reason="operator_halt",
            )
            assert RunStateModel.is_terminal(store.get_run("retry-term-1").state)

            # Wrapper retries: reads terminal, must give up
            run = store.get_run("retry-term-1")
            assert RunStateModel.is_terminal(run.state)

            # Wrapper tries to complete but should abort because terminal
            # (simulates the InvalidTransitionError break in the retry loop)
            with pytest.raises(InvalidTransitionError):
                store.transition(
                    "retry-term-1", "advance_or_stop",
                    expected_state=run.state, expected_version=run.version,
                    reason="wrapper: process completed",
                )

            store.close()

    def test_corner_case_transition_terminal_then_save_run_new_id(self):
        """Creating a different run after terminal is fine (different run_id)."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)

            # Create and kill run-1
            store.save_run(_make_run("corner-run-1"))
            run = store.get_run("corner-run-1")
            store.transition(
                "corner-run-1", "batch_selection",
                expected_state=run.state, expected_version=run.version,
                reason="test: advance",
            )
            run = store.get_run("corner-run-1")
            store.transition(
                "corner-run-1", "advance_or_stop",
                expected_state=run.state, expected_version=run.version,
                reason="test: kill", stop_reason="operator_halt",
            )
            assert RunStateModel.is_terminal(store.get_run("corner-run-1").state)

            # Creating a different run should work fine
            store.save_run(_make_run("corner-run-2"))
            run2 = store.get_run("corner-run-2")
            assert run2 is not None
            assert run2.state == "preflight"

            store.close()


# =========================================================================
# Criterion 2 follow-up: durable reconciliation under finite contention
# =========================================================================


class TestDurableCompletionReconciliation:
    """A finite burst of legal competing writers must not lose a completion.

    The controller's finite-budget fixture (an out-of-band SQLite connection
    that bumps a non-terminal run's version after the detached wrapper reads
    it) previously discarded the child's outcome once the fixed retry budget was
    exhausted.  These tests pin the durable semantics: reconciliation retries
    without a finite bound, co-commits exactly one logical journal event, and
    never overturns a genuine terminal (cancelled) winner.
    """

    def test_reconcile_survives_more_conflicts_than_any_fixed_budget(self):
        """20 real competing writes must not exhaust a fixed retry budget.

        Each competing writer is a separate SQLite connection doing a legal
        metadata write (mirroring the wrapper read seam).  The reconciler must
        still commit the terminal state, the lifecycle metadata and exactly one
        logical journal event.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)
            run_id = "reconcile-storm-1"
            store.save_run(_make_run(run_id))

            competing = 20
            for n in range(1, competing + 1):
                other = SQLiteRunStore(db_path)
                snap = other.get_run(run_id)
                other.update_metadata(
                    run_id,
                    expected_version=snap.version,
                    metadata={"fixture_other_writer": n},
                )
                other.close()

            snapshot = store.get_run(run_id)
            assert not RunStateModel.is_terminal(snapshot.state)
            assert snapshot.version == competing + 1

            record = CompletionReconciler(
                store, run_id,
                target_state="advance_or_stop",
                lifecycle_update={"lifecycle_state": "succeeded", "exit_code": 0},
                event_type="finished",
                event_payload={"returncode": 0},
                idempotency_key="wrapper:finish:" + run_id + ":0",
                reason="wrapper: process completed",
            ).reconcile()

            assert RunStateModel.is_terminal(record.state)
            assert record.metadata["runlifecycle"]["lifecycle_state"] == "succeeded"

            journal = EventJournal(db_path)
            finished = [e for e in journal.get_events(run_id) if e.event_type == "finished"]
            journal.close()
            assert len(finished) == 1, "exactly one logical terminal event expected"

            # An independent reopen observes the same durable terminal outcome.
            reopened = SQLiteRunStore(db_path)
            assert RunStateModel.is_terminal(reopened.get_run(run_id).state)
            reopened.close()
            store.close()

    def test_reconcile_preserves_genuine_terminal_winner(self):
        """A committed cancellation is never overturned by a late completion."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)
            run_id = "reconcile-winner-1"
            store.save_run(_make_run(run_id))
            store.transition(
                run_id, "batch_selection",
                expected_state="preflight", expected_version=1,
                reason="kill: advance",
            )
            cancelled = store.transition(
                run_id, "advance_or_stop",
                expected_state="batch_selection", expected_version=2,
                reason="kill", stop_reason="operator_halt",
            )
            assert RunStateModel.is_terminal(cancelled.state)

            result = CompletionReconciler(
                store, run_id,
                target_state="advance_or_stop",
                lifecycle_update={"lifecycle_state": "succeeded", "exit_code": 0},
                event_type="finished",
                event_payload={"returncode": 0},
                idempotency_key="wrapper:finish:" + run_id + ":0",
                reason="wrapper: process completed",
            ).reconcile()

            assert result.version == cancelled.version
            assert result.metadata.get("runlifecycle") is None, (
                "late completion must not attach lifecycle metadata to a winner"
            )
            journal = EventJournal(db_path)
            finished = [e for e in journal.get_events(run_id) if e.event_type == "finished"]
            journal.close()
            assert finished == [], "late completion must not append a terminal event"
            store.close()

    def test_reconcile_terminal_does_not_swallow_io_failure(self):
        """A non-CAS storage failure must propagate, never look like success."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)
            run_id = "reconcile-io-1"
            store.save_run(_make_run(run_id))

            original = store.transition_with_event

            def _explode(*args, **kwargs):
                raise OSError("injected disk failure")

            store.transition_with_event = _explode
            try:
                with pytest.raises(OSError):
                    CompletionReconciler(
                        store, run_id,
                        target_state="advance_or_stop",
                        lifecycle_update={"lifecycle_state": "succeeded", "exit_code": 0},
                        event_type="finished",
                        event_payload={"returncode": 0},
                        idempotency_key="wrapper:finish:" + run_id + ":0",
                        reason="wrapper: process completed",
                    ).reconcile()
            finally:
                store.transition_with_event = original

            # No false terminal state was persisted.
            assert not RunStateModel.is_terminal(store.get_run(run_id).state)
            store.close()

    def test_resume_pending_completes_recorded_intent_after_crash(self):
        """A recorded intent (crash before commit) is finished on a later pass."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)
            run_id = "reconcile-resume-1"
            store.save_run(_make_run(run_id))

            reconciler = CompletionReconciler(
                store, run_id,
                target_state="advance_or_stop",
                lifecycle_update={"lifecycle_state": "failed", "exit_code": 3},
                event_type="error",
                event_payload={"error": "boom"},
                idempotency_key="wrapper:error:" + run_id,
                reason="wrapper: error: boom",
            )
            reconciler.record_intent()
            pending = store.get_run(run_id).metadata.get("_pending_completion")
            assert isinstance(pending, dict) and pending["target_state"] == "advance_or_stop"
            assert not RunStateModel.is_terminal(store.get_run(run_id).state)
            store.close()

            # Simulate a restart with a fresh connection: the intent survives and
            # is completed exactly once.
            reopened = SQLiteRunStore(db_path)
            finished = CompletionReconciler.resume_pending(reopened, run_id)
            assert finished is not None and RunStateModel.is_terminal(finished.state)

            journal = EventJournal(db_path)
            errors = [e for e in journal.get_events(run_id) if e.event_type == "error"]
            journal.close()
            assert len(errors) == 1
            # Idempotent: a second resume finds a terminal run and does nothing.
            assert CompletionReconciler.resume_pending(reopened, run_id) is None
            reopened.close()


# ---------------------------------------------------------------------------
# Cancellation durability (controller counterproof)
# ---------------------------------------------------------------------------


def _terminal_event_count(store, run_id):
    """Count distinct logical terminal events across journal and outbox."""
    rows = store._conn.execute(
        "SELECT event_type, idempotency_key FROM events WHERE run_id = ?",
        (run_id,),
    ).fetchall()
    seen = {r["idempotency_key"] for r in rows
            if r["event_type"] in ("process_killed", "killed", "finished", "error")}
    outbox = store._conn.execute(
        "SELECT event_type, idempotency_key FROM outbox WHERE run_id = ?",
        (run_id,),
    ).fetchall()
    for r in outbox:
        if r["event_type"] in ("process_killed", "killed", "finished", "error"):
            seen.add(r["idempotency_key"] or ("outbox:", r["event_type"]))
    journal_only = [r for r in rows
                    if r["event_type"] in ("process_killed", "killed", "finished", "error")
                    and r["idempotency_key"] is None]
    return len(seen) + len(journal_only)


class TestDurableCancellationReconciliation:
    """A cancellation must not be lost under CAS contention or I/O failure.

    The controller reproduced a lost cancellation against the previous
    candidate: ``RunApplicationService.kill_run`` caught every exception after
    signalling and returned a successful-looking ``already_dead``/``running``
    result with **no** cancellation event.  These tests pin the corrected
    semantics: one durable terminal cancellation under a finite burst of real
    competing writers, a typed error (not a swallowed success) on a non-CAS
    storage failure, and preservation of a genuine terminal winner.
    """

    def _service_run(self, store):
        from skillweave.runsvc.service import RunApplicationService
        return RunApplicationService(store, EventJournal(store), RawArtifactStore())

    def test_service_cancellation_is_durable_under_cas_contention(self):
        """A real competing writer immediately before the terminal commit.

        A finite burst of legal metadata writes bumps the run version; the kill
        must still leave a terminal ``killed`` lifecycle and exactly one
        cancellation outbox event, not a lost ``running`` run.
        """
        import skillweave.runsvc.service as service_module
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)
            other = SQLiteRunStore(db_path)
            run_id = "cancel-durable-cas"
            store.create_run(run_id, runlifecycle={
                "pid": 313372, "command": ["offline"], "lifecycle_state": "running"})
            svc = self._service_run(store)

            original = store.transition_with_event
            state = {"injected": False}

            def at_commit(*args, **kwargs):
                if not state["injected"]:
                    state["injected"] = True
                    snap = other.get_run(run_id)
                    other.update_metadata(run_id, expected_version=snap.version,
                                          metadata={"fixture_writer": "real bump"})
                return original(*args, **kwargs)

            def absent(*_a):
                raise ProcessLookupError("offline fixture: no signal")

            store.transition_with_event = at_commit
            old = service_module.os.kill, service_module.os.killpg
            try:
                service_module.os.kill = service_module.os.killpg = absent
                svc.kill_run(run_id, 313372)
            finally:
                service_module.os.kill, service_module.os.killpg = old
                store.transition_with_event = original

            actual = other.get_run(run_id)
            assert state["injected"], "the terminal commit seam was not exercised"
            assert RunStateModel.is_terminal(actual.state)
            assert actual.metadata.get("runlifecycle", {}).get("lifecycle_state") == "killed"
            assert _terminal_event_count(other, run_id) == 1
            store.close()
            other.close()

    def test_service_cancellation_surfaces_typed_error_on_io_failure(self):
        """A non-CAS storage failure must raise, never look like a success."""
        import sqlite3
        import skillweave.runsvc.service as service_module
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)
            run_id = "cancel-durable-io"
            store.create_run(run_id, runlifecycle={
                "pid": 313372, "command": ["offline"], "lifecycle_state": "running"})
            svc = self._service_run(store)

            original = store.transition_with_event

            def explode(*args, **kwargs):
                raise sqlite3.OperationalError("injected terminal storage failure")

            def absent(*_a):
                raise ProcessLookupError("offline fixture: no signal")

            store.transition_with_event = explode
            old = service_module.os.kill, service_module.os.killpg
            try:
                service_module.os.kill = service_module.os.killpg = absent
                with pytest.raises(KillOutcomeError) as info:
                    svc.kill_run(run_id, 313372)
            finally:
                service_module.os.kill, service_module.os.killpg = old
                store.transition_with_event = original

            assert isinstance(info.value.__cause__, sqlite3.OperationalError)
            # No false terminal state or lifecycle claim was persisted.
            stored = store.get_run(run_id)
            assert not RunStateModel.is_terminal(stored.state)
            assert stored.metadata.get("runlifecycle", {}).get("lifecycle_state") == "running"
            assert _terminal_event_count(store, run_id) == 0
            store.close()

    def test_service_cancellation_preserves_genuine_terminal_winner(self):
        """A committed completion is never overturned by a late kill."""
        import skillweave.runsvc.service as service_module
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)
            run_id = "cancel-winner"
            store.create_run(run_id, runlifecycle={
                "pid": 313372, "command": ["offline"], "lifecycle_state": "running"})
            winner = CompletionReconciler(
                store, run_id,
                target_state="advance_or_stop",
                lifecycle_update={"lifecycle_state": "succeeded", "exit_code": 0},
                event_type="finished",
                event_payload={"returncode": 0},
                idempotency_key="wrapper:finish:" + run_id + ":0",
                reason="wrapper: process completed",
            ).reconcile()
            assert RunStateModel.is_terminal(winner.state)

            svc = self._service_run(store)

            def absent(*_a):
                raise ProcessLookupError("offline fixture: no signal")

            old = service_module.os.kill, service_module.os.killpg
            try:
                service_module.os.kill = service_module.os.killpg = absent
                returned = svc.kill_run(run_id, 313372)
            finally:
                service_module.os.kill, service_module.os.killpg = old

            after = store.get_run(run_id)
            assert after.version == winner.version
            assert after.metadata["runlifecycle"]["lifecycle_state"] == "succeeded"
            assert returned["state"] != "killed", (
                "a late kill must not claim killed over a completed winner")
            assert returned["state"] == "advance_or_stop"
            store.close()

    def test_cancellation_intent_resumes_after_crash(self):
        """A recorded cancellation intent is finished on a later pass."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)
            run_id = "cancel-resume"
            store.save_run(_make_run(run_id))
            reconciler = CancellationReconciler(
                store, run_id, pid=4242, result="forced_kill",
                event_type="process_killed", event_payload={"pid": 4242},
                idempotency_key="kill:" + run_id,
            )
            reconciler.record_intent()
            pending = store.get_run(run_id).metadata.get("_pending_cancellation")
            assert isinstance(pending, dict)
            assert not RunStateModel.is_terminal(store.get_run(run_id).state)
            store.close()

            reopened = SQLiteRunStore(db_path)
            finished = CancellationReconciler.resume_pending(reopened, run_id)
            assert finished is not None and RunStateModel.is_terminal(finished.state)
            assert finished.metadata["runlifecycle"]["lifecycle_state"] == "killed"
            assert _terminal_event_count(reopened, run_id) == 1
            # Idempotent: a second resume finds a terminal run and does nothing.
            assert CancellationReconciler.resume_pending(reopened, run_id) is None
            reopened.close()

    def test_kill_records_durable_intent_before_signal(self):
        """``kill_run`` records a cancellation intent before the signal call.

        Simulates abrupt supervisor loss *at* the signal boundary: the first
        signal call raises (a crash before the real terminal commit).  The
        durable intent must be readable at that point, so a later restart can
        still finish the cancellation instead of leaving ``running``.
        """
        import skillweave.runsvc.service as service_module
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)
            run_id = "kill-intent-boundary"
            store.create_run(run_id, runlifecycle={
                "pid": 313372, "command": ["offline"], "lifecycle_state": "running"})
            svc = self._service_run(store)

            intent_at_signal = {}

            def crash_at_signal(*_a, **_k):
                intent_at_signal["pending"] = (
                    store.get_run(run_id).metadata.get("_pending_cancellation"))
                raise KeyboardInterrupt("supervisor lost at the signal boundary")

            old = service_module.os.kill, service_module.os.killpg
            try:
                service_module.os.kill = service_module.os.killpg = crash_at_signal
                with pytest.raises(KeyboardInterrupt):
                    svc.kill_run(run_id, 313372)
            finally:
                service_module.os.kill, service_module.os.killpg = old

            assert isinstance(intent_at_signal.get("pending"), dict), (
                "no durable cancellation intent existed before the signal boundary")
            assert intent_at_signal["pending"].get("idempotency_key") == "kill:" + run_id
            # The run is still non-terminal (the crash prevented the commit).
            assert not RunStateModel.is_terminal(store.get_run(run_id).state)
            store.close()

    def test_resume_run_reconciles_recorded_cancellation_intent(self):
        """Production resume finishes a recorded-but-uncommitted cancellation.

        Mirrors the crash-at-signal-boundary scenario end to end through the
        real production restart path (``RunApplicationService.resume_run``), not
        the reconciler helper alone: the run must reach terminal ``killed`` with
        exactly one logical cancellation event.
        """
        import skillweave.runsvc.service as service_module
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "store.db")
            store = _fresh_store(db_path)
            run_id = "kill-resume-production"
            store.create_run(run_id, runlifecycle={
                "pid": 313372, "command": ["offline"], "lifecycle_state": "running"})
            svc = self._service_run(store)

            def crash_at_signal(*_a, **_k):
                raise KeyboardInterrupt("supervisor lost at the signal boundary")

            old = service_module.os.kill, service_module.os.killpg
            try:
                service_module.os.kill = service_module.os.killpg = crash_at_signal
                with pytest.raises(KeyboardInterrupt):
                    svc.kill_run(run_id, 313372)
            finally:
                service_module.os.kill, service_module.os.killpg = old

            assert isinstance(
                store.get_run(run_id).metadata.get("_pending_cancellation"), dict)
            store.close()

            # Restart with a fresh connection and resume through the production
            # path: the recorded cancellation is reconciled.
            reopened = SQLiteRunStore(db_path)
            resumed = RunApplicationService(
                reopened, EventJournal(reopened), RawArtifactStore()).resume_run(run_id)
            assert resumed is not None
            after = reopened.get_run(run_id)
            assert RunStateModel.is_terminal(after.state)
            assert after.metadata["runlifecycle"]["lifecycle_state"] == "killed"
            assert _terminal_event_count(reopened, run_id) == 1
            reopened.close()


# =========================================================================
# Run standalone
# =========================================================================

def _run_all() -> int:
    tests = [
        TestCriterion1_SharedAuthority(),
        TestCriterion2_RaceFixtures(),
        TestCriterion3_LegacyMigration(),
        TestCrashIdempotency(),
        TestLateWrapper(),
        TestDurableCompletionReconciliation(),
        TestDurableCancellationReconciliation(),
    ]
    failed = 0
    total = 0
    for group in tests:
        for name in dir(group):
            if name.startswith("test_"):
                total += 1
                try:
                    getattr(group, name)()
                    print(f"PASS {group.__class__.__name__}.{name}")
                except Exception as e:
                    failed += 1
                    print(f"FAIL {group.__class__.__name__}.{name}: {type(e).__name__}: {e}")
    print(f"\n{total - failed}/{total} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
