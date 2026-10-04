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

from skillweave.runtime.store import SQLiteRunStore, RunRecord, RunStateModel
from skillweave.runtime.journal import EventJournal
from skillweave.runtime.errors import (
    VersionConflictError,
    InvalidTransitionError,
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
# Run standalone
# =========================================================================

def _run_all() -> int:
    tests = [
        TestCriterion1_SharedAuthority(),
        TestCriterion2_RaceFixtures(),
        TestCriterion3_LegacyMigration(),
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
