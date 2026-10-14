from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from .errors import InvalidTransitionError, VersionConflictError
from .checkpoint import Checkpoint, EnvironmentFingerprint
from .registry import ArtifactReceipt, EvidenceQuality
from .handoff import ColdStartBundle, HandoffOffer
from .native_process import (
    LEASE_KEY,
    INTENT_CLAIMING_STATES,
    INTENT_FAILED,
    INTENT_LAUNCHED,
    INTENT_STATES,
    ControllerLease,
    DispatchIntent,
    DuplicateDispatchError,
    LeaseNotHeldError,
    LeaseRefusedError,
    StaleFencingTokenError,
)

import sqlite3
import json


class RunStateModel(str, Enum):
    """Canonical lowercase state vocabulary shared by code and schema.

    Every persisted state value is lowercase. The enum member NAMES are
    irrelevant to the vocabulary (they never persist); the VALUES are the
    canonical form. Formerly the vocabulary mixed snake_case lane states with
    UPPERCASE sandbox sub-states (``SANDBOX_PREFLIGHT``, ``IN_PROGRESS``, ...)
    and carried a legacy placeholder ``STOPPED_BEFORE_B06``. Both defects are
    resolved here: values are lowercase-only, and ``STOPPED_BEFORE_B06`` is
    gone — a run that halts before a gate records the *reason* in
    ``metadata["stop_reason"]`` while entering a terminal state, never a
    bespoke vocabulary value.

    Terminal states are explicit via :meth:`terminal_values` and carry no
    outgoing transitions. A run reaches a terminal state either by completing
    the lane loop (``advance_or_stop``) or by failing (``failed``).
    """

    PREFLIGHT = "preflight"
    BATCH_SELECTION = "batch_selection"
    LANE_PLAN = "lane_plan"
    IMPLEMENT = "implement"
    VERIFY = "verify"
    REVIEW_GATE = "review_gate"
    FIX_RETRY = "fix_retry"
    INTEGRATE = "integrate"
    ADVANCE_OR_STOP = "advance_or_stop"
    SANDBOX_PREFLIGHT = "sandbox_preflight"
    IN_PROGRESS = "in_progress"
    PREFLIGHT_COMPLETE = "preflight_complete"
    BLOCKED_WAITING_FOR_GATE = "blocked_waiting_for_gate"
    REVIEW_REQUIRED = "review_required"
    FAILED = "failed"

    @classmethod
    def terminal_values(cls) -> frozenset[str]:
        """Return the canonical values of every terminal (non-forward) state."""
        return frozenset({cls.ADVANCE_OR_STOP.value, cls.FAILED.value})

    @classmethod
    def is_terminal(cls, value: str) -> bool:
        return value in cls.terminal_values()

    @classmethod
    def legal_transitions(cls, from_state):
        transition_map = {
            cls.PREFLIGHT: [cls.BATCH_SELECTION],
            cls.BATCH_SELECTION: [cls.LANE_PLAN, cls.ADVANCE_OR_STOP],
            cls.LANE_PLAN: [cls.IMPLEMENT],
            cls.IMPLEMENT: [cls.VERIFY],
            cls.VERIFY: [cls.REVIEW_GATE, cls.FIX_RETRY],
            cls.REVIEW_GATE: [cls.INTEGRATE, cls.FIX_RETRY, cls.ADVANCE_OR_STOP],
            cls.FIX_RETRY: [cls.IMPLEMENT, cls.REVIEW_GATE, cls.ADVANCE_OR_STOP],
            cls.INTEGRATE: [cls.VERIFY, cls.ADVANCE_OR_STOP],
            cls.ADVANCE_OR_STOP: [],
            cls.SANDBOX_PREFLIGHT: [cls.IN_PROGRESS, cls.FAILED],
            cls.IN_PROGRESS: [cls.PREFLIGHT_COMPLETE, cls.BLOCKED_WAITING_FOR_GATE, cls.REVIEW_REQUIRED, cls.FAILED],
            cls.PREFLIGHT_COMPLETE: [cls.IN_PROGRESS, cls.REVIEW_REQUIRED, cls.FAILED],
            cls.BLOCKED_WAITING_FOR_GATE: [cls.IN_PROGRESS, cls.REVIEW_REQUIRED, cls.FAILED],
            cls.REVIEW_REQUIRED: [cls.IN_PROGRESS, cls.FAILED],
            cls.FAILED: [],
        }
        from_state = cls(from_state) if isinstance(from_state, str) else from_state
        return transition_map.get(from_state, [])

    @classmethod
    def canonical_path_to_terminal(cls, from_state, target_state: str = "advance_or_stop"):
        """Return forward-only lane states to advance through so that
        *target_state* becomes a legal outgoing transition.

        Returns an empty list when *target_state* is already legal from
        *from_state*, or ``None`` when no forward path reaches it.  The path is
        resolved from the state machine rather than a hand-maintained table, so
        it cannot drift from :meth:`legal_transitions`.
        """
        def _values(states):
            return [s.value if isinstance(s, RunStateModel) else s for s in states]

        start = from_state.value if isinstance(from_state, RunStateModel) else from_state
        if target_state in _values(cls.legal_transitions(start)):
            return []
        prev: dict = {start: None}
        queue = [start]
        found = None
        while queue:
            node = queue.pop(0)
            if target_state in _values(cls.legal_transitions(node)):
                found = node
                break
            for nxt in cls.legal_transitions(node):
                nv = nxt.value if isinstance(nxt, RunStateModel) else nxt
                if nv not in prev:
                    prev[nv] = node
                    queue.append(nv)
        if found is None:
            return None
        path = []
        node = found
        while node is not None and node != start:
            path.append(node)
            node = prev.get(node)
        path.reverse()
        return path


#: Persistent stop reasons (recorded in ``metadata["stop_reason"]``, never as
#: a dedicated state) that explain why a run entered a terminal state.
STOP_REASONS: tuple[str, ...] = ("before_gate", "operator_halt", "dependency_failed")

#: Legacy uppercase values that used to be persisted as states. Accepted during
#: migration only, never produced as new vocabulary.
LEGACY_STATE_ALIASES: dict[str, str] = {
    "SANDBOX_PREFLIGHT": "sandbox_preflight",
    "IN_PROGRESS": "in_progress",
    "PREFLIGHT_COMPLETE": "preflight_complete",
    "BLOCKED_WAITING_FOR_GATE": "blocked_waiting_for_gate",
    "REVIEW_REQUIRED": "review_required",
    "FAILED": "failed",
    "STOPPED_BEFORE_B06": "advance_or_stop",
}


@dataclass
class RunRecord:
    run_id: str
    root_run_id: str
    parent_run_id: Optional[str]
    state: str
    version: int
    created_at: str
    updated_at: str
    ended_at: Optional[str]
    role: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


class RunStore(ABC):
    @abstractmethod
    def get_run(self, run_id: str) -> Optional[RunRecord]:
        ...

    @abstractmethod
    def save_run(self, record: RunRecord) -> RunRecord:
        ...

    @abstractmethod
    def transition(
        self,
        run_id: str,
        target_state: str,
        expected_state: str,
        expected_version: int,
        reason: str = "",
        role: Optional[str] = None,
        stop_reason: Optional[str] = None,
    ) -> RunRecord:
        ...

    @abstractmethod
    def list_runs(self, state: Optional[str] = None, limit: int = 100) -> list[RunRecord]:
        ...


class SQLiteRunStore(RunStore):
    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        #: The live host authority service this store is bound to for protected
        #: writes (SW-1510-IDENTITY). ``None`` means the protected boundaries
        #: (:meth:`save_protected_evidence`,
        #: :meth:`transition_with_gate_admission`) fail closed — a store reached
        #: by a delegated worker is never bound to the service.
        self._authority_service: Any = None
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        # 5000 is sqlite3.connect()'s built-in default. Stating it explicitly makes the
        # write-lock behavior visible and independent of the library default, so a caller
        # holding its own connection can no longer silently override it. Same value, no
        # behavior change.
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()

    def _init_schema(self):
        self._conn.executescript("""
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
            );
            CREATE TABLE IF NOT EXISTS transitions_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'ops'
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
                run_id TEXT PRIMARY KEY,
                root_run_id TEXT NOT NULL,
                parent_run_id TEXT,
                journal_offset INTEGER NOT NULL DEFAULT 0,
                environment TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS evidence (
                artifact_id TEXT NOT NULL UNIQUE,
                sha256 TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                producer_command TEXT NOT NULL,
                subject_repo TEXT NOT NULL,
                subject_commit TEXT NOT NULL,
                created_at TEXT NOT NULL,
                evidence_type TEXT NOT NULL,
                purpose TEXT NOT NULL,
                method TEXT NOT NULL DEFAULT '',
                system_source TEXT NOT NULL DEFAULT '',
                sensitivity TEXT NOT NULL DEFAULT 'internal',
                retention TEXT NOT NULL DEFAULT 'permanent',
                transformation_history TEXT NOT NULL DEFAULT '[]',
                quality TEXT NOT NULL DEFAULT '{}',
                supersedes TEXT,
                metadata TEXT NOT NULL DEFAULT '{}',
                idempotency_key TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_idempotency
                ON evidence(idempotency_key)
                WHERE idempotency_key IS NOT NULL;
            CREATE TABLE IF NOT EXISTS handoffs (
                handoff_id TEXT PRIMARY KEY,
                from_role TEXT NOT NULL,
                to_role TEXT NOT NULL,
                scope TEXT NOT NULL,
                cold_start_bundle TEXT NOT NULL,
                allowed_actions TEXT NOT NULL DEFAULT '[]',
                input_digests TEXT NOT NULL DEFAULT '{}',
                state TEXT NOT NULL,
                owner TEXT,
                offered_at TEXT NOT NULL,
                accepted_at TEXT,
                completed_at TEXT,
                metadata TEXT NOT NULL DEFAULT '{}',
                idempotency_key TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_handoffs_idempotency
                ON handoffs(idempotency_key)
                WHERE idempotency_key IS NOT NULL;
            CREATE TABLE IF NOT EXISTS write_scope_claims (
                claim_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                resolved_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                released_at TEXT,
                lease_until TEXT,
                heartbeat_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_write_scope_claims_run
                ON write_scope_claims(run_id);
            CREATE INDEX IF NOT EXISTS idx_write_scope_claims_path
                ON write_scope_claims(resolved_path);
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                idempotency_key TEXT,
                created_at TEXT NOT NULL,
                delivered_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_outbox_run
                ON outbox(run_id, id);
            -- The journal event table lives in the same SQLite database so a
            -- terminal state, its outbox intent, and its logical journal event
            -- can commit in ONE transaction (see ``_transition_impl``).  The
            -- DDL is identical to ``EventJournal._init_schema``; both use
            -- CREATE TABLE IF NOT EXISTS so the shared table is defined once.
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER NOT NULL,
                run_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                correlation_id TEXT,
                causation_id TEXT,
                idempotency_key TEXT,
                timestamp TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (run_id, sequence)
            );
            CREATE INDEX IF NOT EXISTS idx_events_run
                ON events(run_id, sequence);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_events_idempotency
                ON events(run_id, idempotency_key)
                WHERE idempotency_key IS NOT NULL;
            -- SW-ORCH-001 criterion 2: the SINGLE controller lease with a
            -- monotonically increasing fencing token.  ``lease_key`` is the
            -- primary key so at most one row per lease lane exists; the token
            -- strictly increases on every acquisition, so a recovering
            -- controller reads a higher token than its predecessor and any
            -- write presented with a superseded token is refused.
            CREATE TABLE IF NOT EXISTS controller_lease (
                lease_key TEXT PRIMARY KEY,
                holder TEXT NOT NULL,
                fencing_token INTEGER NOT NULL,
                acquired_at TEXT NOT NULL,
                lease_until TEXT,
                released_at TEXT
            );
            -- SW-ORCH-001 criterion 2: durable dispatch intents.  One row per
            -- owned attempt; the partial unique index below makes a SECOND
            -- claiming intent for the same execution scope impossible at the
            -- storage layer, so a duplicate launch cannot start a second
            -- worker even across processes.
            CREATE TABLE IF NOT EXISTS dispatch_intents (
                intent_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                execution_scope TEXT NOT NULL,
                step TEXT NOT NULL,
                state TEXT NOT NULL,
                fencing_token INTEGER NOT NULL,
                route TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                process_identity TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_dispatch_intents_run
                ON dispatch_intents(run_id);
            CREATE INDEX IF NOT EXISTS idx_dispatch_intents_scope
                ON dispatch_intents(execution_scope);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_dispatch_intents_claiming_scope
                ON dispatch_intents(execution_scope)
                WHERE state IN ('launched', 'reviewing', 'repairing', 'integrating');
            -- SW-ORCH-001 criterion 4: the last compact changed-state view
            -- served for a run.  Persisting it lets a reattached controller
            -- resume from the durable cursor instead of re-reading the
            -- transcript: the newest row per run is the view an unchanged poll
            -- returns.  Additive and opt-in; no existing table changes.
            CREATE TABLE IF NOT EXISTS evidence_deltas (
                delta_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                journal_cursor INTEGER NOT NULL,
                accepted_gates TEXT NOT NULL DEFAULT '[]',
                required_gates TEXT NOT NULL DEFAULT '[]',
                new_evidence TEXT NOT NULL DEFAULT '[]',
                observed_at TEXT NOT NULL,
                record TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_evidence_deltas_run
                ON evidence_deltas(run_id, delta_id);
            -- SW-ORCH-001 criterion 5: a controlled content transaction (CMS
            -- integration) for a non-repository subject.  Keyed by
            -- (run_id, txn_id) so a reattached controller reads the applied /
            -- verified transaction rather than re-applying it.  No Git merge
            -- participates.  Additive and opt-in.
            CREATE TABLE IF NOT EXISTS content_transactions (
                run_id TEXT NOT NULL,
                txn_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                subject_kind TEXT NOT NULL,
                baseline TEXT NOT NULL,
                applied_digest TEXT NOT NULL,
                readback_digest TEXT NOT NULL DEFAULT '',
                applied INTEGER NOT NULL DEFAULT 0,
                verified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                record TEXT NOT NULL,
                PRIMARY KEY (run_id, txn_id)
            );
        """)
        self._conn.commit()
        self._migrate_legacy_states()

    def _migrate_legacy_states(self) -> None:
        """Migrate persisted legacy uppercase state values to canonical lowercase.

        Idempotent and destructive in exactly one direction: any row whose
        ``state`` is a legacy uppercase value (or which carries the removed
        ``STOPPED_BEFORE_B06`` placeholder) is rewritten to the canonical
        lowercase value. ``STOPPED_BEFORE_B06`` maps to the terminal
        ``advance_or_stop`` state with ``metadata["stop_reason"] =
        "before_gate"`` recorded, so the stop reason survives as data rather
        than a bespoke vocabulary value. Runs already on canonical values are
        left untouched. Called once per store open, before any caller reads.
        """
        import json
        aliases = LEGACY_STATE_ALIASES
        rows = self._conn.execute("SELECT run_id, state, metadata FROM runs").fetchall()
        for row in rows:
            legacy = row["state"]
            if legacy not in aliases:
                continue
            canonical = aliases[legacy]
            meta = json.loads(row["metadata"]) if row["metadata"] else {}
            if legacy == "STOPPED_BEFORE_B06":
                meta["stop_reason"] = "before_gate"
            self._conn.execute(
                "UPDATE runs SET state = ?, metadata = ? WHERE run_id = ?",
                (canonical, json.dumps(meta), row["run_id"]),
            )
        if rows:
            self._conn.commit()

    def _row_to_record(self, row) -> RunRecord:
        import json
        meta = json.loads(row["metadata"]) if row["metadata"] else {}
        return RunRecord(
            run_id=row["run_id"],
            root_run_id=row["root_run_id"],
            parent_run_id=row["parent_run_id"],
            state=row["state"],
            version=row["version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            ended_at=row["ended_at"],
            role=row["role"],
            metadata=meta,
        )

    def get_run(self, run_id: str) -> Optional[RunRecord]:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return self._row_to_record(row) if row else None

    def save_run(self, record: RunRecord) -> RunRecord:
        import json
        now = datetime.now(timezone.utc).isoformat()
        if record.created_at is None:
            record.created_at = now
        record.updated_at = now
        if record.version < 1:
            record.version = 1

        # CAS-safe write with the version guard enforced in SQL, not in a
        # Python check-then-act. Mirrors ``transition()`` (BEGIN IMMEDIATE +
        # guarded write + rowcount == 0 -> VersionConflictError), generalized to
        # cover both the bootstrap create and the overwrite.
        #
        # ``record.version`` is the writer's authoritative snapshot:
        #
        # * version 1 is the bootstrap CREATE. It is exclusive: the ``run_id``
        #   PRIMARY KEY means two concurrent first-creates let exactly one
        #   INSERT succeed and the loser raises via IntegrityError. A second
        #   writer that finds the run already at version 1 is a duplicate
        #   bootstrap, not a stale/valid overwrite, so it conflicts.
        # * version >= 2 is an OVERWRITE. It atomically bumps to
        #   ``record.version + 1`` under ``WHERE version = ?``; rowcount == 0
        #   means the persisted version no longer equals the writer's snapshot,
        #   so exactly one of N concurrent same-snapshot writers wins and the
        #   other N-1 raise VersionConflictError.
        # Read the existing row BEFORE BEGIN IMMEDIATE so we can check
        # terminal state.  The version CAS inside the transaction is still
        # the real authority; the terminal check is an additional guard.
        existing = self.get_run(record.run_id)

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            if record.version <= 1:
                # Bootstrap create. ``run_id`` is the PRIMARY KEY, so a
                # concurrent first-create collides here and exactly one wins.
                try:
                    self._conn.execute(
                        """INSERT INTO runs
                           (run_id, root_run_id, parent_run_id, state, version,
                            created_at, updated_at, ended_at, role, metadata)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            record.run_id, record.root_run_id, record.parent_run_id,
                            record.state, 1,
                            record.created_at, record.updated_at, record.ended_at,
                            record.role, json.dumps(record.metadata),
                        ),
                    )
                except sqlite3.IntegrityError:
                    winner = self._conn.execute(
                        "SELECT version FROM runs WHERE run_id = ?", (record.run_id,)
                    ).fetchone()
                    actual = winner["version"] if winner is not None else 1
                    raise VersionConflictError(record.run_id, 1, actual)
                new_version = 1
            else:
                # Terminal records cannot be resurrected by any caller,
                # even one holding the matching version.
                if existing is not None and RunStateModel.is_terminal(existing.state):
                    self._conn.rollback()
                    raise InvalidTransitionError(
                        existing.state, record.state, record.run_id,
                        extra={"reason": "terminal record cannot be resurrected"},
                    )
                new_version = record.version + 1
                cur = self._conn.execute(
                    """UPDATE runs SET
                       root_run_id = ?, parent_run_id = ?, state = ?, version = ?,
                       updated_at = ?, ended_at = ?, role = ?, metadata = ?
                       WHERE run_id = ? AND version = ?""",
                    (
                        record.root_run_id, record.parent_run_id, record.state,
                        new_version, record.updated_at, record.ended_at,
                        record.role, json.dumps(record.metadata),
                        record.run_id, record.version,
                    ),
                )
                if cur.rowcount == 0:
                    persisted = self._conn.execute(
                        "SELECT version FROM runs WHERE run_id = ?", (record.run_id,)
                    ).fetchone()
                    actual = persisted["version"] if persisted is not None else record.version
                    raise VersionConflictError(record.run_id, record.version, actual)

            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

        record.version = new_version
        return record

    def set_authority_guard(self, guard) -> None:
        self._authority_guard = guard

    def transition(
        self,
        run_id: str,
        target_state: str,
        expected_state: str,
        expected_version: int,
        reason: str = "",
        role: Optional[str] = None,
        stop_reason: Optional[str] = None,
        metadata_update: Optional[dict] = None,
    ) -> RunRecord:
        return self._transition_impl(
            run_id=run_id,
            target_state=target_state,
            expected_state=expected_state,
            expected_version=expected_version,
            reason=reason,
            role=role,
            stop_reason=stop_reason,
            metadata_update=metadata_update,
            event_type=None,
            event_payload=None,
            idempotency_key=None,
        )

    def transition_with_event(
        self,
        run_id: str,
        target_state: str,
        expected_state: str,
        expected_version: int,
        event_type: str,
        event_payload: Optional[dict] = None,
        reason: str = "",
        role: Optional[str] = None,
        stop_reason: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        metadata_update: Optional[dict] = None,
        journal_event: bool = False,
    ) -> RunRecord:
        """Atomically transition a run AND enqueue one journal outbox event.

        The run-state update, its ``transitions_log`` row, and the pending
        journal event (written to the ``outbox`` table) commit in a single
        ``BEGIN IMMEDIATE`` transaction. A crash before commit rolls back all
        three; after commit all three are durably present. This is the
        unit-of-work/outbox contract: state and event never diverge.

        When *metadata_update* is provided, its key-value pairs are merged
        into the run's metadata inside the same transaction (deep-merged for
        the ``runlifecycle`` key). This allows lifecycle state changes and
        the terminal transition to commit atomically.

        When *journal_event* is true the logical event is ALSO inserted into
        the shared ``events`` journal table in the same transaction, so the
        terminal state, its outbox intent, and its logical journal record all
        commit atomically.  It defaults to ``False`` so existing
        outbox-only callers keep their exact behaviour.
        """
        return self._transition_impl(
            run_id=run_id,
            target_state=target_state,
            expected_state=expected_state,
            expected_version=expected_version,
            reason=reason,
            role=role,
            stop_reason=stop_reason,
            event_type=event_type,
            event_payload=event_payload,
            idempotency_key=idempotency_key,
            metadata_update=metadata_update,
            journal_event=journal_event,
        )

    def _transition_impl(
        self,
        run_id: str,
        target_state: str,
        expected_state: Optional[str],
        expected_version: Optional[int],
        reason: str = "",
        role: Optional[str] = None,
        stop_reason: Optional[str] = None,
        event_type: Optional[str] = None,
        event_payload: Optional[dict] = None,
        idempotency_key: Optional[str] = None,
        metadata_update: Optional[dict] = None,
        journal_event: bool = False,
    ) -> RunRecord:
        existing = self.get_run(run_id)
        if existing is None:
            raise InvalidTransitionError(
                "nonexistent", target_state, run_id,
                extra={"reason": "run does not exist"},
            )

        # Version is the CAS authority and is checked BEFORE state so that a
        # stale writer (one whose read-snapshot has been superseded) always
        # fails with a VersionConflictError, never a misleading transition.
        if expected_version is not None and existing.version != expected_version:
            raise VersionConflictError(run_id, expected_version, existing.version)

        if expected_state is not None and existing.state != expected_state:
            raise InvalidTransitionError(
                existing.state, target_state, run_id,
                extra={
                    "expected": expected_state,
                    "actual": existing.state,
                    "reason": "run is not in the expected state",
                },
            )

        if role and hasattr(self, '_authority_guard') and self._authority_guard is not None:
            from skillweave.runtime.authority import can_mutate_run_state
            if not can_mutate_run_state(role):
                from skillweave.runtime.authority import AuthorityError
                raise AuthorityError(role, "transition", f"Role '{role}' lacks mutate_run_state")

        allowed = RunStateModel.legal_transitions(existing.state)
        allowed_values = [s.value if isinstance(s, RunStateModel) else s for s in allowed]
        if target_state not in allowed_values:
            raise InvalidTransitionError(
                existing.state, target_state, run_id,
                extra={"allowed": allowed_values},
            )

        now = datetime.now(timezone.utc).isoformat()
        new_version = existing.version + 1
        ended_at = existing.ended_at
        if RunStateModel.is_terminal(target_state):
            ended_at = now

        # A terminal transition may carry a persistent stop reason. Record it
        # in metadata (the vocabulary itself stays flat; the reason is data).
        # When *metadata_update* is provided, merge its values into metadata
        # inside the same transaction (deep-merge runlifecycle).
        import json
        merged_metadata = dict(existing.metadata or {})
        if stop_reason is not None:
            merged_metadata["stop_reason"] = stop_reason
        if metadata_update is not None:
            for k, v in metadata_update.items():
                if k == "runlifecycle" and isinstance(v, dict):
                    existing_lifecycle = dict(merged_metadata.get("runlifecycle", {}))
                    existing_lifecycle.update(v)
                    merged_metadata["runlifecycle"] = existing_lifecycle
                else:
                    merged_metadata[k] = v

        # One transaction: the run-state UPDATE, its transitions_log row, and
        # (when requested) a pending outbox event commit together. A crash at
        # any point rolls all of them back, so state and journal event never
        # diverge (SW-UOW-001).
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self._conn.execute(
                """UPDATE runs SET state = ?, version = ?, updated_at = ?, ended_at = ?,
                   role = COALESCE(?, role), metadata = ?
                   WHERE run_id = ? AND version = ?""",
                (
                    target_state, new_version, now, ended_at, role,
                    json.dumps(merged_metadata), run_id, expected_version,
                ),
            )
            if cur.rowcount == 0:
                raise VersionConflictError(run_id, expected_version, existing.version)

            self._conn.execute(
                """INSERT INTO transitions_log
                   (run_id, from_state, to_state, reason, version, timestamp, role)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (run_id, existing.state, target_state, reason, new_version, now, role or existing.role),
            )

            if event_type is not None:
                self._conn.execute(
                    """INSERT INTO outbox
                       (run_id, event_type, payload, idempotency_key, created_at, delivered_at)
                       VALUES (?, ?, ?, ?, ?, NULL)""",
                    (run_id, event_type, json.dumps(event_payload or {}), idempotency_key, now),
                )

            # When requested, also write the logical journal event into the
            # shared ``events`` table INSIDE this same transaction.  Previously
            # the wrapper appended the terminal journal event separately AFTER
            # the atomic transition, which is not crash-atomic: a crash between
            # the committed state and the journal append could leave a terminal
            # state with no logical record (and a reader could observe the
            # state/event divergence).  Co-committing both removes that window.
            # Sequence allocation is MAX(sequence)+1 under the same write lock,
            # so it cannot race a concurrent journal append.
            if journal_event and event_type is not None:
                seq = self._conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE run_id = ?",
                    (run_id,),
                ).fetchone()[0]
                self._conn.execute(
                    """INSERT INTO events
                       (sequence, run_id, event_type, payload, correlation_id,
                        causation_id, idempotency_key, timestamp, version)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (seq, run_id, event_type, json.dumps(event_payload or {}),
                     None, None, idempotency_key, now, new_version),
                )

            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

        return self.get_run(run_id)

    # ---- Outbox (unit-of-work delivery) ----

    def list_outbox(self, run_id: Optional[str] = None, include_delivered: bool = False) -> list[dict]:
        """List outbox events queued by atomic transitions.

        By default returns only UNDELIVERED events (``delivered_at IS NULL``).
        Used by the journal's dispatcher to consume what the UOW produced.
        """
        sql = "SELECT * FROM outbox"
        clauses = []
        params: list = []
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if not include_delivered:
            clauses.append("delivered_at IS NULL")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        rows = self._conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if isinstance(d.get("payload"), str):
                d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out

    def mark_outbox_delivered(self, outbox_id: int) -> bool:
        """Mark one outbox event as delivered; idempotent."""
        now = datetime.now(timezone.utc).isoformat()
        cur = self._conn.execute(
            "UPDATE outbox SET delivered_at = ? WHERE id = ?",
            (now, outbox_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def list_runs(self, state: Optional[str] = None, limit: int = 100) -> list[RunRecord]:
        if state:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE state = ? ORDER BY updated_at DESC LIMIT ?",
                (state, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM runs ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    # ---- Checkpoint persistence ----

    def save_checkpoint(self, checkpoint: Checkpoint) -> Checkpoint:
        import json
        if checkpoint.created_at is None:
            checkpoint.created_at = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """INSERT OR REPLACE INTO checkpoints
               (run_id, root_run_id, parent_run_id, journal_offset,
                environment, created_at, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                checkpoint.run_id,
                checkpoint.root_run_id,
                checkpoint.parent_run_id,
                checkpoint.journal_offset,
                json.dumps(checkpoint.environment.to_dict()),
                checkpoint.created_at,
                json.dumps(checkpoint.metadata),
            ),
        )
        self._conn.commit()
        return checkpoint

    def get_checkpoint(self, run_id: str) -> Optional[Checkpoint]:
        import json
        row = self._conn.execute(
            "SELECT * FROM checkpoints WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        env = json.loads(row["environment"]) if row["environment"] else {}
        meta = json.loads(row["metadata"]) if row["metadata"] else {}
        return Checkpoint(
            run_id=row["run_id"],
            root_run_id=row["root_run_id"],
            parent_run_id=row["parent_run_id"],
            journal_offset=row["journal_offset"],
            environment=EnvironmentFingerprint(
                hostname=env.get("hostname", ""),
                os_name=env.get("os_name", ""),
                python_version=env.get("python_version", ""),
                branch=env.get("branch", ""),
                commit_sha=env.get("commit_sha", ""),
                key_hashes=env.get("key_hashes", {}),
                captured_at=env.get("captured_at", ""),
            ),
            created_at=row["created_at"],
            metadata=meta,
        )

    # ---- Write-scope arbitration ----

    def claim_write_scope(
        self,
        run_id: str,
        scope_paths: list[str],
        claim_id: Optional[str] = None,
        ttl_seconds: int = 300,
    ) -> list["WriteScopeClaim"]:
        """Claim a set of write scopes on behalf of a run, atomically, with a lease.

        All scopes are resolved first (same resolution as 004), then every HELD,
        UNEXPIRED claim is checked for overlap. An EXPIRED claim is not a
        conflict: it has been vacated by time and a new claimant may take the
        scope over. If ANY scope overlaps a live claim, the whole operation
        fails and nothing is persisted. On success the claims carry a lease
        deadline (``lease_until``) and an initial heartbeat.
        """
        import uuid

        from .write_scope import (
            WriteScopeClaim,
            ScopeConflictError,
            resolve_scope_path,
            paths_overlap,
            default_lease_until,
        )

        resolved = [resolve_scope_path(p) for p in scope_paths]
        if not resolved:
            return []

        now = datetime.now(timezone.utc).isoformat()
        lease_until = default_lease_until(now, ttl_seconds)

        # Single transaction: check-then-insert is atomic, so two concurrent
        # claimers cannot both succeed on an overlapping scope.
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            held_rows = self._conn.execute(
                "SELECT claim_id, run_id, resolved_path, lease_until FROM write_scope_claims "
                "WHERE released_at IS NULL"
            ).fetchall()

            for new_path in resolved:
                for row in held_rows:
                    # An expired lease does not block a takeover.
                    if row["lease_until"] is not None and row["lease_until"] <= now:
                        continue
                    if paths_overlap(new_path, row["resolved_path"]):
                        raise ScopeConflictError(run_id, row["run_id"], new_path)

            claims: list[WriteScopeClaim] = []
            for i, new_path in enumerate(resolved):
                if len(resolved) == 1 and claim_id:
                    cid = claim_id
                else:
                    cid = claim_id + "::" + str(i) if claim_id else str(uuid.uuid4())
                self._conn.execute(
                    "INSERT INTO write_scope_claims "
                    "(claim_id, run_id, resolved_path, created_at, released_at, "
                    " lease_until, heartbeat_at) "
                    "VALUES (?, ?, ?, ?, NULL, ?, ?)",
                    (cid, run_id, new_path, now, lease_until, now),
                )
                claims.append(WriteScopeClaim(
                    claim_id=cid,
                    run_id=run_id,
                    resolved_path=new_path,
                    created_at=now,
                    released_at=None,
                    lease_until=lease_until,
                    heartbeat_at=now,
                ))

            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

        return claims

    def heartbeat_claim(self, claim_id: str, ttl_seconds: int = 300) -> bool:
        """Renew a held claim's lease from now; fails if the claim is gone/released.

        Returns True on renewal, False when the claim no longer exists or is
        already released (a coordinator that lost its claim cannot renew it).
        """
        from .write_scope import default_lease_until
        now = datetime.now(timezone.utc).isoformat()
        lease_until = default_lease_until(now, ttl_seconds)
        cur = self._conn.execute(
            "UPDATE write_scope_claims SET heartbeat_at = ?, lease_until = ? "
            "WHERE claim_id = ? AND released_at IS NULL",
            (now, lease_until, claim_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def try_takeover(self, claim_id: str, new_run_id: str, ttl_seconds: int = 300) -> bool:
        """Take over a claim whose lease expired or is already released.

        Succeeds only when the claim is void (released) or its lease has
        expired; a live claim is never silently stolen. On success the claim is
        re-bound to ``new_run_id`` with a fresh lease. Returns True on takeover.
        """
        from .write_scope import default_lease_until
        now = datetime.now(timezone.utc).isoformat()
        lease_until = default_lease_until(now, ttl_seconds)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT run_id, lease_until, released_at FROM write_scope_claims "
                "WHERE claim_id = ?", (claim_id,)
            ).fetchone()
            if row is None:
                self._conn.rollback()
                return False
            live = row["released_at"] is None and (
                row["lease_until"] is None or row["lease_until"] > now
            )
            if live:
                self._conn.rollback()
                return False
            self._conn.execute(
                "UPDATE write_scope_claims SET run_id = ?, released_at = NULL, "
                "created_at = ?, lease_until = ?, heartbeat_at = ? "
                "WHERE claim_id = ?",
                (new_run_id, now, lease_until, now, claim_id),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return True

    def release_write_scope(self, claim_id: str) -> bool:
        """Release a held (claim_id, run_id) claim; idempotent.

        Returns True if a held claim was released, False if it was already
        released or did not exist.
        """
        now = datetime.now(timezone.utc).isoformat()
        cur = self._conn.execute(
            "UPDATE write_scope_claims SET released_at = ? "
            "WHERE claim_id = ? AND released_at IS NULL",
            (now, claim_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def list_write_scope_claims(
        self,
        run_id: Optional[str] = None,
        include_released: bool = False,
    ) -> list["WriteScopeClaim"]:
        """List claims. By default only HELD claims and all runs."""
        from .write_scope import WriteScopeClaim

        if run_id is not None:
            rows = self._conn.execute(
                "SELECT * FROM write_scope_claims WHERE run_id = ? "
                + ("" if include_released else "AND released_at IS NULL"),
                (run_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM write_scope_claims "
                + ("" if include_released else "WHERE released_at IS NULL")
            ).fetchall()

        return [
            WriteScopeClaim(
                claim_id=r["claim_id"],
                run_id=r["run_id"],
                resolved_path=r["resolved_path"],
                created_at=r["created_at"],
                released_at=r["released_at"],
                lease_until=r["lease_until"],
                heartbeat_at=r["heartbeat_at"],
            )
            for r in rows
        ]

    def resolve_orphaned_claims(self) -> list[str]:
        """Release held claims whose owning run no longer exists.

        A claim is orphaned when its run_id does not appear in ``runs``. Returns
        the claim_ids that were forcibly released.
        """
        orphaned = self._conn.execute(
            "SELECT c.claim_id FROM write_scope_claims c "
            "LEFT JOIN runs r ON r.run_id = c.run_id "
            "WHERE c.released_at IS NULL AND r.run_id IS NULL"
        ).fetchall()
        claim_ids = [r["claim_id"] for r in orphaned]

        if claim_ids:
            now = datetime.now(timezone.utc).isoformat()
            self._conn.executemany(
                "UPDATE write_scope_claims SET released_at = ? WHERE claim_id = ?",
                [(now, cid) for cid in claim_ids],
            )
            self._conn.commit()
        return claim_ids

    # ---- Controller lease with fencing (SW-ORCH-001 criterion 2) ----

    def _controller_lease_row(self):
        return self._conn.execute(
            "SELECT * FROM controller_lease WHERE lease_key = ?", (LEASE_KEY,)
        ).fetchone()

    def _row_to_controller_lease(self, row) -> ControllerLease:
        return ControllerLease(
            holder=row["holder"],
            fencing_token=row["fencing_token"],
            lease_until=row["lease_until"],
            acquired_at=row["acquired_at"],
            released_at=row["released_at"],
        )

    def get_controller_lease(self) -> Optional[ControllerLease]:
        """Return the single controller lease, or ``None`` if never acquired."""
        row = self._controller_lease_row()
        return None if row is None else self._row_to_controller_lease(row)

    def acquire_controller_lease(
        self, holder: str, ttl_seconds: int = 300
    ) -> ControllerLease:
        """Acquire the single controller lease, or refuse a live one.

        Exactly one controller holds the lease.  The fencing token strictly
        increases on every acquisition (takeover or re-acquire after release),
        so a recovering controller that takes over reads a *higher* token than
        the controller it replaced.  A live (held, unexpired) lease is refused
        with :class:`LeaseRefusedError` -- never silently stolen.

        A takeover of an expired lease trusts a new holder *only* for the
        token; the caller must still re-verify any recorded process identity
        (:func:`process_identity.verify`) before trusting or signalling a live
        worker.  This method arbitrates the token alone.
        """
        from .write_scope import default_lease_until
        now = datetime.now(timezone.utc).isoformat()
        lease_until = default_lease_until(now, ttl_seconds)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._controller_lease_row()
            if row is not None and row["released_at"] is None:
                # A held lease with an unexpired (or undatable) deadline is
                # live: refuse rather than steal.
                deadline = row["lease_until"]
                if deadline is None or deadline > now:
                    self._conn.rollback()
                    raise LeaseRefusedError(row["holder"], deadline)
            current_token = row["fencing_token"] if row is not None else 0
            token = current_token + 1
            if row is None:
                self._conn.execute(
                    "INSERT INTO controller_lease "
                    "(lease_key, holder, fencing_token, acquired_at, lease_until, "
                    " released_at) VALUES (?, ?, ?, ?, ?, NULL)",
                    (LEASE_KEY, holder, token, now, lease_until),
                )
            else:
                self._conn.execute(
                    "UPDATE controller_lease SET holder = ?, fencing_token = ?, "
                    "acquired_at = ?, lease_until = ?, released_at = NULL "
                    "WHERE lease_key = ?",
                    (holder, token, now, lease_until, LEASE_KEY),
                )
            self._conn.commit()
        except Exception:
            if self._conn.in_transaction:
                self._conn.rollback()
            raise
        return ControllerLease(
            holder=holder,
            fencing_token=token,
            lease_until=lease_until,
            acquired_at=now,
            released_at=None,
        )

    def heartbeat_controller_lease(
        self, holder: str, fencing_token: int, ttl_seconds: int = 300
    ) -> bool:
        """Renew the lease from now; refuse a stale token or wrong holder.

        Returns True when the lease was renewed.  A caller presenting a
        superseded token is refused (:class:`StaleFencingTokenError`), as is a
        caller who is not the current holder (:class:`LeaseNotHeldError`): a
        controller that lost the lease cannot renew it.
        """
        from .write_scope import default_lease_until
        now = datetime.now(timezone.utc).isoformat()
        lease_until = default_lease_until(now, ttl_seconds)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._controller_lease_row()
            if row is None or row["released_at"] is not None:
                self._conn.rollback()
                raise LeaseNotHeldError(
                    fencing_token, None if row is None else row["holder"]
                )
            if row["fencing_token"] != fencing_token:
                self._conn.rollback()
                raise StaleFencingTokenError(fencing_token, row["fencing_token"])
            if row["holder"] != holder:
                self._conn.rollback()
                raise LeaseNotHeldError(fencing_token, row["holder"])
            self._conn.execute(
                "UPDATE controller_lease SET lease_until = ? WHERE lease_key = ?",
                (lease_until, LEASE_KEY),
            )
            self._conn.commit()
        except Exception:
            if self._conn.in_transaction:
                self._conn.rollback()
            raise
        return True

    def release_controller_lease(self, fencing_token: int) -> bool:
        """Release the lease; refuse a stale token.

        Returns True when the held lease carrying ``fencing_token`` was
        released.  A superseded token is refused rather than releasing a lease
        the caller no longer owns.
        """
        now = datetime.now(timezone.utc).isoformat()
        row = self._controller_lease_row()
        if row is None or row["released_at"] is not None:
            raise LeaseNotHeldError(fencing_token, None if row is None else row["holder"])
        if row["fencing_token"] != fencing_token:
            raise StaleFencingTokenError(fencing_token, row["fencing_token"])
        cur = self._conn.execute(
            "UPDATE controller_lease SET released_at = ? "
            "WHERE lease_key = ? AND released_at IS NULL AND fencing_token = ?",
            (now, LEASE_KEY, fencing_token),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def current_fencing_token(self) -> int:
        """The current fencing token (``0`` when the lease was never taken)."""
        row = self._controller_lease_row()
        return 0 if row is None else row["fencing_token"]

    def last_journal_sequence(self, run_id: str) -> int:
        """The highest journal ``events.sequence`` for *run_id* (``0`` if none).

        The durable journal cursor a recovering controller resumes from: a
        ``native-process-checkpoint-v1`` records this so a fresh controller
        continues the run's logical journal without replaying or skipping.
        """
        row = self._conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return int(row[0])

    # ---- Read projections for the compact view / content transactions ----

    def list_journal_events(self, run_id: str, after_sequence: int = 0) -> list[dict]:
        """Journal events for *run_id* with ``sequence > after_sequence``.

        A read-only projection of the shared ``events`` table in journal order.
        The compact evidence view consumes exactly the changed window (events
        strictly after its cursor), so an unchanged poll reads nothing new.
        """
        rows = self._conn.execute(
            "SELECT sequence, run_id, event_type, payload, correlation_id, "
            "causation_id, idempotency_key, timestamp, version "
            "FROM events WHERE run_id = ? AND sequence > ? ORDER BY sequence",
            (run_id, after_sequence),
        ).fetchall()
        events: list[dict] = []
        for row in rows:
            data = dict(row)
            payload = data.get("payload")
            if isinstance(payload, str):
                try:
                    data["payload"] = json.loads(payload)
                except (ValueError, TypeError):
                    data["payload"] = {}
            events.append(data)
        return events

    def list_evidence(self, run_id: Optional[str] = None) -> list[ArtifactReceipt]:
        """Evidence receipts, in insertion order (optionally scoped to a run).

        ``ArtifactReceipt`` has no run column, so *run_id* is matched against
        the receipt's ``metadata['run_id']`` when supplied; with no *run_id* the
        full content-addressed set is returned.
        """
        rows = self._conn.execute(
            "SELECT * FROM evidence ORDER BY rowid"
        ).fetchall()
        receipts = [self._row_to_evidence(r) for r in rows]
        if run_id is None:
            return receipts
        return [
            r for r in receipts
            if (r.metadata or {}).get("run_id") == run_id
        ]

    def save_evidence_delta(self, delta: Any) -> Any:
        """Persist one ``evidence-view-v1`` compact view (append, never update).

        The newest row per run is the view a reattached controller resumes from;
        the whole frozen record is retained so a recovering controller resumes
        from a durable cursor rather than re-reading the transcript.
        """
        record = delta.to_dict()
        self._conn.execute(
            """INSERT INTO evidence_deltas
               (run_id, journal_cursor, accepted_gates, required_gates,
                new_evidence, observed_at, record)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                record["run_id"],
                int(record["journal_cursor"]),
                json.dumps(list(record.get("accepted_gates") or [])),
                json.dumps(list(record.get("required_gates") or [])),
                json.dumps(list(record.get("new_evidence") or [])),
                record.get("observed_at", ""),
                json.dumps(record),
            ),
        )
        self._conn.commit()
        return delta

    def get_evidence_delta(self, run_id: str) -> Optional[dict]:
        """The newest persisted compact view for *run_id* (or ``None``)."""
        row = self._conn.execute(
            "SELECT record FROM evidence_deltas WHERE run_id = ? "
            "ORDER BY delta_id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["record"])

    def save_content_transaction(self, txn: Any) -> Any:
        """Persist one controlled content transaction, keyed by (run, txn).

        A re-apply of the same ``(run_id, txn_id)`` is refused, so a controlled
        content transaction is idempotent by identity rather than re-applied.
        """
        record = txn.to_dict()
        self._conn.execute(
            """INSERT INTO content_transactions
               (run_id, txn_id, subject, subject_kind, baseline, applied_digest,
                readback_digest, applied, verified, created_at, record)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record["run_id"],
                record["txn_id"],
                record["subject"],
                record.get("subject_kind", ""),
                record["baseline"],
                record["applied_digest"],
                record.get("readback_digest", ""),
                1 if record.get("applied") else 0,
                1 if bool(record.get("readback_digest"))
                and record.get("readback_digest") == record.get("applied_digest")
                else 0,
                record.get("created_at", ""),
                json.dumps(record),
            ),
        )
        self._conn.commit()
        return txn

    def get_content_transaction(self, run_id: str, txn_id: str) -> Optional[dict]:
        """The persisted content transaction for ``(run_id, txn_id)`` (or None)."""
        row = self._conn.execute(
            "SELECT record FROM content_transactions "
            "WHERE run_id = ? AND txn_id = ?",
            (run_id, txn_id),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["record"])

    def assert_controller(self, fencing_token: int) -> None:
        """Refuse a write whose fencing token is not the current held token.

        This is the fence: a stale controller's write is refused with
        :class:`StaleFencingTokenError` when the presented token is lower than
        the held one, and with :class:`LeaseNotHeldError` when no lease is
        held.  A caller must pass here before its durable effect is applied.
        """
        row = self._controller_lease_row()
        if row is None or row["released_at"] is not None:
            raise LeaseNotHeldError(fencing_token, None if row is None else row["holder"])
        if row["fencing_token"] != fencing_token:
            raise StaleFencingTokenError(fencing_token, row["fencing_token"])

    # ---- Durable dispatch intents (SW-ORCH-001 criterion 2) ----

    def _row_to_dispatch_intent(self, row) -> DispatchIntent:
        return DispatchIntent(
            intent_id=row["intent_id"],
            run_id=row["run_id"],
            task_id=row["task_id"],
            attempt=row["attempt"],
            execution_scope=row["execution_scope"],
            step=row["step"],
            state=row["state"],
            fencing_token=row["fencing_token"],
            route=row["route"],
            session_id=row["session_id"],
            process_identity=(
                json.loads(row["process_identity"])
                if row["process_identity"] else None
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def record_dispatch_intent(
        self,
        run_id: str,
        task_id: str,
        attempt: int,
        execution_scope: str,
        *,
        step: str,
        state: str = INTENT_LAUNCHED,
        fencing_token: int = 0,
        route: str = "",
        session_id: str = "",
        process_identity: Optional[dict] = None,
        intent_id: Optional[str] = None,
    ) -> DispatchIntent:
        """Record one owned attempt's dispatch intent, refusing a duplicate.

        A *claiming* intent (state in :data:`INTENT_CLAIMING_STATES`) for an
        execution scope that already carries a claiming intent is refused with
        :class:`DuplicateDispatchError`; the store's partial unique index
        enforces the same invariant atomically, so two racing processes cannot
        both launch the same scope.
        """
        import uuid

        if state not in INTENT_STATES:
            raise ValueError(
                f"dispatch intent state {state!r} is not one of {sorted(INTENT_STATES)}"
            )
        iid = intent_id or str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        pid_json = json.dumps(process_identity) if process_identity is not None else None

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            if state in INTENT_CLAIMING_STATES:
                existing = self._conn.execute(
                    "SELECT intent_id, run_id FROM dispatch_intents "
                    "WHERE execution_scope = ? AND state IN "
                    f"({','.join('?' for _ in INTENT_CLAIMING_STATES)})",
                    (execution_scope, *sorted(INTENT_CLAIMING_STATES)),
                ).fetchone()
                if existing is not None:
                    self._conn.rollback()
                    raise DuplicateDispatchError(
                        execution_scope, existing["intent_id"], existing["run_id"]
                    )
            self._conn.execute(
                "INSERT INTO dispatch_intents "
                "(intent_id, run_id, task_id, attempt, execution_scope, step, "
                " state, fencing_token, route, session_id, process_identity, "
                " created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (iid, run_id, task_id, attempt, execution_scope, step, state,
                 fencing_token, route, session_id, pid_json, now, now),
            )
            self._conn.commit()
        except DuplicateDispatchError:
            raise
        except Exception:
            if self._conn.in_transaction:
                self._conn.rollback()
            raise
        return DispatchIntent(
            intent_id=iid, run_id=run_id, task_id=task_id, attempt=attempt,
            execution_scope=execution_scope, step=step, state=state,
            fencing_token=fencing_token, route=route, session_id=session_id,
            process_identity=process_identity, created_at=now, updated_at=now,
        )

    def advance_dispatch_intent(
        self, intent_id: str, state: str, *, step: Optional[str] = None
    ) -> DispatchIntent:
        """Advance an intent to ``state``; releases the scope when non-claiming.

        Moving an intent out of a *claiming* state frees its execution scope at
        the storage layer (the partial unique index no longer covers it), so a
        subsequent attempt on the same scope is admitted.
        """
        if state not in INTENT_STATES:
            raise ValueError(
                f"dispatch intent state {state!r} is not one of {sorted(INTENT_STATES)}"
            )
        now = datetime.now(timezone.utc).isoformat()
        if step is None:
            cur = self._conn.execute(
                "UPDATE dispatch_intents SET state = ?, updated_at = ? "
                "WHERE intent_id = ?",
                (state, now, intent_id),
            )
        else:
            cur = self._conn.execute(
                "UPDATE dispatch_intents SET state = ?, step = ?, updated_at = ? "
                "WHERE intent_id = ?",
                (state, step, now, intent_id),
            )
        self._conn.commit()
        if cur.rowcount == 0:
            raise KeyError(f"dispatch intent {intent_id!r} does not exist")
        return self.get_dispatch_intent(intent_id)

    def fail_dispatch_intent(self, intent_id: str) -> DispatchIntent:
        """Mark an intent failed (a resolved, terminal outcome)."""
        return self.advance_dispatch_intent(intent_id, INTENT_FAILED)

    def get_dispatch_intent(self, intent_id: str) -> Optional[DispatchIntent]:
        row = self._conn.execute(
            "SELECT * FROM dispatch_intents WHERE intent_id = ?", (intent_id,)
        ).fetchone()
        return None if row is None else self._row_to_dispatch_intent(row)

    def find_dispatch_intent_by_scope(
        self, execution_scope: str, *, claiming_only: bool = False
    ) -> Optional[DispatchIntent]:
        if claiming_only:
            row = self._conn.execute(
                "SELECT * FROM dispatch_intents WHERE execution_scope = ? AND "
                f"state IN ({','.join('?' for _ in INTENT_CLAIMING_STATES)}) "
                "ORDER BY updated_at DESC LIMIT 1",
                (execution_scope, *sorted(INTENT_CLAIMING_STATES)),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT * FROM dispatch_intents WHERE execution_scope = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (execution_scope,),
            ).fetchone()
        return None if row is None else self._row_to_dispatch_intent(row)

    def list_dispatch_intents(
        self, run_id: Optional[str] = None, *, claiming_only: bool = False
    ) -> list[DispatchIntent]:
        clauses: list[str] = []
        params: list[Any] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if claiming_only:
            clauses.append(
                f"state IN ({','.join('?' for _ in INTENT_CLAIMING_STATES)})"
            )
            params.extend(sorted(INTENT_CLAIMING_STATES))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._conn.execute(
            "SELECT * FROM dispatch_intents" + where + " ORDER BY created_at, intent_id",
            params,
        ).fetchall()
        return [self._row_to_dispatch_intent(r) for r in rows]

    # ---- Evidence persistence ----

    def save_evidence(self, receipt: ArtifactReceipt, idempotency_key: Optional[str] = None) -> ArtifactReceipt:
        import json
        if receipt.created_at is None:
            receipt.created_at = datetime.now(timezone.utc).isoformat()
        # Content-addressing is the default idempotency: identical bytes share
        # one canonical receipt regardless of how many writers raced.
        key = idempotency_key if idempotency_key is not None else f"sha256:{receipt.sha256}"
        try:
            self._conn.execute(
                """INSERT INTO evidence
                   (artifact_id, sha256, schema_version, producer_command,
                    subject_repo, subject_commit, created_at, evidence_type, purpose,
                    method, system_source, sensitivity, retention,
                    transformation_history, quality, supersedes, metadata,
                    idempotency_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    receipt.artifact_id,
                    receipt.sha256,
                    receipt.schema_version,
                    receipt.producer_command,
                    receipt.subject_repo,
                    receipt.subject_commit,
                    receipt.created_at,
                    receipt.evidence_type,
                    receipt.purpose,
                    receipt.method,
                    receipt.system_source,
                    receipt.sensitivity,
                    receipt.retention,
                    json.dumps(receipt.transformation_history),
                    json.dumps(receipt.quality.to_dict()),
                    receipt.supersedes,
                    json.dumps(receipt.metadata),
                    key,
                ),
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            # A concurrent writer recorded the same key; resolve to the winner.
            self._conn.rollback()
            row = self._conn.execute(
                "SELECT * FROM evidence WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if row is None:
                raise
            return self._row_to_evidence(row)
        return receipt

    def get_evidence(self, artifact_id: str) -> Optional[ArtifactReceipt]:
        row = self._conn.execute(
            "SELECT * FROM evidence WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_evidence(row)

    def _row_to_evidence(self, row) -> ArtifactReceipt:
        import json
        history = json.loads(row["transformation_history"]) if row["transformation_history"] else []
        quality = json.loads(row["quality"]) if row["quality"] else {}
        meta = json.loads(row["metadata"]) if row["metadata"] else {}
        return ArtifactReceipt(
            artifact_id=row["artifact_id"],
            sha256=row["sha256"],
            schema_version=row["schema_version"],
            producer_command=row["producer_command"],
            subject_repo=row["subject_repo"],
            subject_commit=row["subject_commit"],
            created_at=row["created_at"],
            evidence_type=row["evidence_type"],
            purpose=row["purpose"],
            method=row["method"],
            system_source=row["system_source"],
            sensitivity=row["sensitivity"],
            retention=row["retention"],
            transformation_history=history,
            quality=EvidenceQuality(
                relevance=quality.get("relevance", "medium"),
                sufficiency=quality.get("sufficiency", "medium"),
                reliability=quality.get("reliability", "medium"),
                currency=quality.get("currency", "medium"),
                integrity=quality.get("integrity", "medium"),
                independence=quality.get("independence", "medium"),
            ),
            supersedes=row["supersedes"],
            metadata=meta,
        )

    # ---- Handoff persistence ----

    def save_handoff(self, offer: HandoffOffer, idempotency_key: Optional[str] = None) -> HandoffOffer:
        import json
        if offer.offered_at is None:
            offer.offered_at = datetime.now(timezone.utc).isoformat()
        key = idempotency_key if idempotency_key is not None else f"handoff:{offer.handoff_id}"
        try:
            self._conn.execute(
                """INSERT INTO handoffs
                   (handoff_id, from_role, to_role, scope, cold_start_bundle,
                    allowed_actions, input_digests, state, owner, offered_at,
                    accepted_at, completed_at, metadata, idempotency_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    offer.handoff_id,
                    offer.from_role,
                    offer.to_role,
                    offer.scope,
                    json.dumps(offer.cold_start_bundle.to_dict()),
                    json.dumps(offer.allowed_actions),
                    json.dumps(offer.input_digests),
                    offer.state,
                    offer.owner,
                    offer.offered_at,
                    offer.accepted_at,
                    offer.completed_at,
                    json.dumps(offer.metadata),
                    key,
                ),
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            self._conn.rollback()
            row = self._conn.execute(
                "SELECT * FROM handoffs WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if row is None:
                raise
            return self._row_to_handoff(row)
        return offer

    def get_handoff(self, handoff_id: str) -> Optional[HandoffOffer]:
        row = self._conn.execute(
            "SELECT * FROM handoffs WHERE handoff_id = ?", (handoff_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_handoff(row)

    def _row_to_handoff(self, row) -> HandoffOffer:
        import json
        bundle = json.loads(row["cold_start_bundle"]) if row["cold_start_bundle"] else {}
        actions = json.loads(row["allowed_actions"]) if row["allowed_actions"] else []
        digests = json.loads(row["input_digests"]) if row["input_digests"] else {}
        meta = json.loads(row["metadata"]) if row["metadata"] else {}
        return HandoffOffer(
            handoff_id=row["handoff_id"],
            from_role=row["from_role"],
            to_role=row["to_role"],
            scope=row["scope"],
            cold_start_bundle=ColdStartBundle(
                prd_uri=bundle.get("prd_uri", ""),
                prd_digest=bundle.get("prd_digest", ""),
                chain_uri=bundle.get("chain_uri", ""),
                chain_digest=bundle.get("chain_digest", ""),
                repo_uri=bundle.get("repo_uri", ""),
                worktree_path=bundle.get("worktree_path", ""),
                branch=bundle.get("branch", ""),
                target_role=bundle.get("target_role", ""),
                sequence_id=bundle.get("sequence_id", ""),
                base_sha=bundle.get("base_sha", ""),
                remote_sha=bundle.get("remote_sha", ""),
                fingerprint=bundle.get("fingerprint"),
            ),
            allowed_actions=actions,
            input_digests=digests,
            state=row["state"],
            owner=row["owner"],
            offered_at=row["offered_at"],
            accepted_at=row["accepted_at"],
            completed_at=row["completed_at"],
            metadata=meta,
        )

    def ensure_storage(self):
        pass

    def with_authority_service(self, authority_service: Any) -> "SQLiteRunStore":
        """Bind this store to the live host authority service (protected writes).

        Only the host process that owns the :class:`HostAuthorityService` binds
        it, and only a genuine service (one that answers :meth:`serve_live`) is
        accepted. The store object reachable by a delegated worker is never
        bound, so its protected boundaries fail closed.
        """
        if authority_service is None or not callable(
            getattr(authority_service, "serve_live", None)
        ):
            from .authority_service import IdentityBoundaryError

            raise IdentityBoundaryError(
                "with_authority_service requires a live HostAuthorityService"
            )
        self._authority_service = authority_service
        return self

    # ---- Protected receipt / gate mutation boundary (SW-1510-IDENTITY) ----
    #
    # These are the *actual* persistence boundaries for a protected receipt and
    # a gate transition. They are separate from ``save_evidence``/``transition``
    # (which stay as general-purpose legacy APIs) so that a worker-shaped call
    # cannot reach a protected write: the boundary requires an
    # ``IngestedReceipt`` that only ``HostAuthorityService`` can produce, and a
    # ``GateAdmission`` that only a controller/integrator credential can obtain.

    def save_protected_evidence(
        self,
        receipt: ArtifactReceipt,
        ingested: Any,
        *,
        idempotency_key: Optional[str] = None,
    ) -> ArtifactReceipt:
        """Persist a receipt that passed the host identity boundary.

        A typed ``IngestedReceipt`` is only a *claim*: the class is importable,
        so a caller can construct one. Authorization is a live, one-shot
        capability that the genuine :class:`HostAuthorityService` mints at
        ingestion and that this boundary *consumes through that same service*
        against the exact attempt and artifact digest. A forged/copied
        ``IngestedReceipt`` (no issued token), a replayed token, a token minted
        for a different attempt/artifact, or a look-alike service object is
        refused before any mutation.
        """
        from .authority_service import IdentityBoundaryError, IngestedReceipt

        if not isinstance(ingested, IngestedReceipt):
            raise IdentityBoundaryError(
                "save_protected_evidence requires an IngestedReceipt produced by "
                "HostAuthorityService.ingest_evidence_receipt"
            )
        service = self._authority_service
        if service is None or not callable(getattr(service, "serve_live", None)):
            raise IdentityBoundaryError(
                "save_protected_evidence requires the store to be bound to the "
                "live authority service; bind with with_authority_service(...)"
            )
        if receipt.sha256 != ingested.artifact_digest:
            raise IdentityBoundaryError(
                "receipt digest does not match the authenticated artifact digest"
            )
        if not service.registry.consume_receipt_ingestion(
            ingested.ingestion_token,
            attempt_key=ingested.attempt_key,
            artifact_digest=ingested.artifact_digest,
        ):
            raise IdentityBoundaryError(
                "ingestion capability was not issued by the live authority service "
                "for this attempt/artifact (forged, replayed or mismatched)"
            )
        return self.save_evidence(receipt, idempotency_key=idempotency_key)

    def transition_with_gate_admission(
        self,
        run_id: str,
        target_state: str,
        expected_state: str,
        expected_version: int,
        admission: Any,
        reason: str = "",
        stop_reason: Optional[str] = None,
    ) -> RunRecord:
        """Transition a run to a gate state, admitted by a live controller decision.

        A typed ``GateAdmission`` is again only a claim. This boundary consumes
        the live, one-shot capability the authority service minted at
        :func:`admit_gate`, bound to the exact run and admitted receipt bytes and
        to a decision the service actually issued. A copied/forged
        ``GateAdmission``, a replay, a token for a different run/receipt, or a
        withdrawn authority decision is refused before any run state changes.
        The admitted principal is recorded as ``role``, so the persisted state
        carries who actually admitted it.
        """
        from .authority_service import GateAdmission, IdentityBoundaryError

        if not isinstance(admission, GateAdmission):
            raise IdentityBoundaryError(
                "transition_with_gate_admission requires a GateAdmission from a "
                "controller/integrator credential"
            )
        service = admission.authority_service
        if service is None or not callable(getattr(service, "serve_live", None)):
            raise IdentityBoundaryError(
                "gate admission is not bound to a live authority service"
            )
        if admission.run_id != run_id:
            raise IdentityBoundaryError("gate admission is for a different run")
        active = service.registry.consume_gate_admission(
            admission.admission_token,
            run_id=run_id,
            receipt_artifact_digest=admission.receipt_artifact_digest,
        )
        if active is None:
            raise IdentityBoundaryError(
                "gate admission capability was not issued by the live authority "
                "service for this run/target/receipt (forged, replayed, expired or "
                "mismatched)"
            )
        return self._transition_impl(
            run_id=run_id,
            target_state=target_state,
            expected_state=expected_state,
            expected_version=expected_version,
            reason=reason,
            role=active["admitted_by"],
            stop_reason=stop_reason,
            event_type=None,
            event_payload=None,
            idempotency_key=None,
        )

    def create_run(self, run_id: str, **kwargs) -> RunRecord:
        from datetime import datetime, timezone
        import uuid
        now = datetime.now(timezone.utc).isoformat()
        record = RunRecord(
            run_id=run_id,
            root_run_id=run_id,
            parent_run_id=None,
            state="preflight",
            version=1,
            created_at=now,
            updated_at=now,
            ended_at=None,
            role="ops",
            metadata=kwargs,
        )
        return self.save_run(record)

    def update_metadata(
        self,
        run_id: str,
        expected_version: Optional[int] = None,
        **changes: Any,
    ) -> RunRecord:
        """Atomic CAS metadata update.

        When *expected_version* is provided, the update succeeds only if the
        persisted version matches (``WHERE version = ?``).  When omitted, the
        update is still an atomic current-row operation — never an unlocked
        read-modify-write.

        Raises
        ------
        VersionConflictError
            When *expected_version* is provided but does not match the
            persisted version.
        InvalidTransitionError
            When the run does not exist or is in a terminal state (terminal
            records cannot be resurrected).
        """
        import json
        from datetime import datetime, timezone

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                self._conn.rollback()
                raise InvalidTransitionError(
                    "nonexistent", "update", run_id,
                    extra={"reason": "run does not exist"},
                )

            if expected_version is not None and row["version"] != expected_version:
                self._conn.rollback()
                raise VersionConflictError(run_id, expected_version, row["version"])

            # Terminal records cannot be resurrected by any caller.
            if RunStateModel.is_terminal(row["state"]):
                self._conn.rollback()
                raise InvalidTransitionError(
                    row["state"], "update", run_id,
                    extra={"reason": "terminal record cannot be resurrected"},
                )

            # Merge changes.  Recognised top-level fields are set directly;
            # everything else is stored in metadata.
            now = datetime.now(timezone.utc).isoformat()
            new_state = changes.pop("state", row["state"])
            new_role = changes.pop("role", row["role"])
            new_ended_at = changes.pop("ended_at", row["ended_at"])
            merged_meta = json.loads(row["metadata"]) if row["metadata"] else {}
            for k, v in changes.items():
                if k == "metadata" and isinstance(v, dict):
                    merged_meta.update(v)
                else:
                    merged_meta[k] = v

            # Determine the version to guard on: the caller's expected_version
            # if given, otherwise the current persisted version (atomic).
            guard_version = expected_version if expected_version is not None else row["version"]
            new_version = row["version"] + 1

            cur = self._conn.execute(
                """UPDATE runs SET state = ?, version = ?, updated_at = ?,
                   ended_at = ?, role = ?, metadata = ?
                   WHERE run_id = ? AND version = ?""",
                (new_state, new_version, now, new_ended_at, new_role,
                 json.dumps(merged_meta), run_id, guard_version),
            )
            if cur.rowcount == 0:
                self._conn.rollback()
                persisted = self._conn.execute(
                    "SELECT version FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                actual = persisted["version"] if persisted else row["version"]
                raise VersionConflictError(run_id, guard_version, actual)

            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

        return self.get_run(run_id)

    def reconcile_terminal(
        self,
        run_id: str,
        *,
        target_state: str,
        lifecycle_update: Optional[dict] = None,
        event_type: Optional[str] = None,
        event_payload: Optional[dict] = None,
        idempotency_key: Optional[str] = None,
        reason: str = "",
        stop_reason: Optional[str] = None,
        extra_metadata: Optional[dict] = None,
    ) -> RunRecord:
        """Durably commit a terminal outcome for *run_id*, retrying past conflicts.

        Unlike :meth:`transition_with_event`, this is the **durable
        reconciliation** primitive for out-of-band writers (the detached
        completion wrapper): a finite sequence of uncoordinated-but-legal
        writers must not be able to discard a real terminal outcome.  A single
        CAS attempt is insufficient because each competing metadata write bumps
        the run version, so any fixed retry budget can be exhausted and lose a
        completed child.

        Semantics
        ---------
        A genuine terminal winner is never overturned:

        * If the run already holds a terminal state, it is returned unchanged
          and nothing is written (the cancelled/terminal winner is preserved).
        * If the run does not exist, ``InvalidTransitionError`` is raised.

        Otherwise this method loops: re-read the current non-terminal snapshot,
        walk the legal lane transitions to a state from which *target_state* is
        reachable, then commit the terminal transition, the lifecycle metadata
        merge, and (when requested) exactly one idempotent outbox event together
        in a single ``BEGIN IMMEDIATE`` transaction.  On ``VersionConflictError``
        it re-reads and tries again, without a finite bound, so a completed
        child is never dropped.  A non-CAS storage error (e.g. a SQLite I/O
        failure) propagates, so an I/O failure is never swallowed and reported
        as an apparent success.

        Idempotency: because a committed terminal state ends the loop on the
        next pass, exactly one terminal event with *idempotency_key* is ever
        written, however many conflicting writers appear.
        """
        while True:
            existing = self.get_run(run_id)
            if existing is None:
                raise InvalidTransitionError(
                    "nonexistent", target_state, run_id,
                    extra={"reason": "run does not exist"},
                )
            if RunStateModel.is_terminal(existing.state):
                # A genuine terminal winner (cancellation or a committed
                # completion) is preserved as-is.
                return existing

            current = existing
            try:
                # Walk to a state from which target_state is a legal outgoing
                # transition.  Each intermediate step is its own transaction
                # and only ever moves forward.
                guard = 0
                while (target_state not in [
                    s.value if isinstance(s, RunStateModel) else s
                    for s in RunStateModel.legal_transitions(current.state)
                ]):
                    guard += 1
                    if guard > len(RunStateModel):
                        raise InvalidTransitionError(
                            current.state, target_state, run_id,
                            extra={"reason": "no legal path to target state"},
                        )
                    next_states = [
                        s.value if isinstance(s, RunStateModel) else s
                        for s in RunStateModel.legal_transitions(current.state)
                    ]
                    # Prefer the canonical forward lane when a choice exists.
                    prefer = ["batch_selection", "lane_plan", "implement",
                              "verify", "fix_retry", "review_gate", "integrate"]
                    step = next(
                        (p for p in prefer if p in next_states),
                        next_states[0] if next_states else None,
                    )
                    if step is None:
                        raise InvalidTransitionError(
                            current.state, target_state, run_id,
                            extra={"reason": "no legal path to target state"},
                        )
                    self.transition(
                        run_id, step,
                        expected_state=current.state,
                        expected_version=current.version,
                        reason=reason or "reconcile: advance",
                    )
                    current = self.get_run(run_id)

                merged: dict = dict(lifecycle_update or {})
                if extra_metadata:
                    merged.update(extra_metadata)
                metadata_update = {"runlifecycle": merged} if merged else None
                return self.transition_with_event(
                    run_id, target_state,
                    expected_state=current.state,
                    expected_version=current.version,
                    event_type=event_type,
                    event_payload=event_payload,
                    idempotency_key=idempotency_key,
                    reason=reason or "reconcile: terminal",
                    stop_reason=stop_reason,
                    metadata_update=metadata_update,
                    journal_event=True,
                )
            except VersionConflictError:
                # A legal unrelated writer moved the run.  Re-read and retry;
                # a completed child's outcome must not be lost.
                continue

    def reconcile_terminal_intent(
        self,
        run_id: str,
        *,
        target_state: str,
        lifecycle_update: Optional[dict] = None,
        event_type: Optional[str] = None,
        event_payload: Optional[dict] = None,
        idempotency_key: Optional[str] = None,
        reason: str = "",
        stop_reason: Optional[str] = None,
        intent_metadata_key: str = "_pending_completion",
    ) -> "CompletionReconciler":
        """Return a reconciler that first durably records the completion intent.

        The intent (the terminal outcome plus its one logical event) is written
        as run metadata **before** the terminal commit.  A crash after the
        intent and before commit leaves the intent readable, so a later
        reconciliation pass can finish the outcome rather than lose it.
        """
        return CompletionReconciler(
            self,
            run_id,
            target_state=target_state,
            lifecycle_update=lifecycle_update,
            event_type=event_type,
            event_payload=event_payload,
            idempotency_key=idempotency_key,
            reason=reason,
            stop_reason=stop_reason,
            intent_metadata_key=intent_metadata_key,
        )

    def reconcile_cancellation_intent(
        self,
        run_id: str,
        *,
        pid: Optional[int] = None,
        result: str = "unknown",
        event_type: str = "process_killed",
        event_payload: Optional[dict] = None,
        idempotency_key: Optional[str] = None,
        stop_reason: str = "operator_halt",
        reason: str = "service: kill",
        intent_metadata_key: str = "_pending_cancellation",
    ) -> "CancellationReconciler":
        """Return a reconciler that durably records a cancellation intent.

        The intent (the terminal outcome plus its one logical event) is stored
        in run metadata, so if the process crashes between the intent and the
        terminal commit a later pass can still finish the cancellation instead
        of leaving a non-terminal ``running`` run behind.
        """
        return CancellationReconciler(
            self,
            run_id,
            pid=pid,
            result=result,
            event_type=event_type,
            event_payload=event_payload,
            idempotency_key=idempotency_key,
            stop_reason=stop_reason,
            reason=reason,
            intent_metadata_key=intent_metadata_key,
        )

    def import_legacy(
        self,
        run_id: str,
        source_bytes: bytes,
        source_path: Optional[str] = None,
    ) -> RunRecord:
        """Import a legacy JSON run record as historical data.

        The imported record is assigned state ``unknown`` and metadata
        ``{"historical": true, "provenance": ...}``.  The exact source bytes
        are preserved in metadata.  This method never infers execution,
        success, review approval, or live PID ownership from the old bytes.

        Idempotent: a second call with the same *run_id* returns the existing
        row unchanged.  Existing SQLite rows always win — if *run_id* already
        exists in the store, this method returns the existing row without
        modifying it.

        Parameters
        ----------
        run_id:
            The run ID to import as.
        source_bytes:
            The raw bytes of the legacy JSON file.
        source_path:
            Optional path to the source file (recorded in provenance).

        Returns
        -------
        RunRecord
            The imported or existing record.
        """
        import json
        from datetime import datetime, timezone

        existing = self.get_run(run_id)
        if existing is not None:
            return existing

        provenance: dict[str, Any] = {
            "source": "legacy_import",
            "byte_length": len(source_bytes),
            "sha256": __import__("hashlib").sha256(source_bytes).hexdigest(),
        }
        if source_path:
            provenance["source_path"] = source_path

        now = datetime.now(timezone.utc).isoformat()
        record = RunRecord(
            run_id=run_id,
            root_run_id=run_id,
            parent_run_id=None,
            state="unknown",
            version=1,
            created_at=now,
            updated_at=now,
            ended_at=None,
            role="ops",
            metadata={
                "historical": True,
                "provenance": provenance,
                "raw_bytes_b64": __import__("base64").b64encode(source_bytes).decode("ascii"),
            },
        )
        return self.save_run(record)

    def close(self):
        self._conn.close()


class CompletionReconciler:
    """Durable completion-intent recorder and reconciler.

    Wraps :meth:`SQLiteRunStore.reconcile_terminal` with an explicit intent
    record.  The intent is stored in run metadata (keyed by
    ``intent_metadata_key``) inside a single recoverable write, then the
    terminal outcome is reconciled.  If the process crashes between the intent
    and the commit, :meth:`resume_pending` finishes the recorded outcome on a
    later pass, so a completed child is never silently lost across a restart.
    """

    def __init__(
        self,
        store: "SQLiteRunStore",
        run_id: str,
        *,
        target_state: str,
        lifecycle_update: Optional[dict] = None,
        event_type: Optional[str] = None,
        event_payload: Optional[dict] = None,
        idempotency_key: Optional[str] = None,
        reason: str = "",
        stop_reason: Optional[str] = None,
        intent_metadata_key: str = "_pending_completion",
    ) -> None:
        self._store = store
        self.run_id = run_id
        self.target_state = target_state
        self.lifecycle_update = dict(lifecycle_update or {})
        self.event_type = event_type
        self.event_payload = event_payload
        self.idempotency_key = idempotency_key
        self.reason = reason
        self.stop_reason = stop_reason
        self.intent_metadata_key = intent_metadata_key

    def _intent(self) -> dict:
        return {
            "target_state": self.target_state,
            "lifecycle_update": self.lifecycle_update,
            "event_type": self.event_type,
            "event_payload": self.event_payload,
            "idempotency_key": self.idempotency_key,
            "reason": self.reason,
            "stop_reason": self.stop_reason,
        }

    def record_intent(self) -> None:
        """Durably write the completion intent, retrying past CAS conflicts.

        A genuine terminal winner is left untouched and no intent is written
        (the winner already stands).
        """
        RunStateModel = globals()["RunStateModel"]
        intent = self._intent()
        while True:
            current = self._store.get_run(self.run_id)
            if current is None or RunStateModel.is_terminal(current.state):
                return
            try:
                self._store.update_metadata(
                    self.run_id,
                    expected_version=current.version,
                    metadata={self.intent_metadata_key: intent},
                )
                return
            except VersionConflictError:
                continue
            except InvalidTransitionError:
                # Terminal winner appeared between read and write.
                return

    def reconcile(self) -> RunRecord:
        self.record_intent()
        return self._store.reconcile_terminal(
            self.run_id,
            target_state=self.target_state,
            lifecycle_update=self.lifecycle_update,
            event_type=self.event_type,
            event_payload=self.event_payload,
            idempotency_key=self.idempotency_key,
            reason=self.reason,
            stop_reason=self.stop_reason,
        )

    @classmethod
    def resume_pending(
        cls,
        store: "SQLiteRunStore",
        run_id: str,
        *,
        intent_metadata_key: str = "_pending_completion",
    ) -> Optional[RunRecord]:
        """Finish a recorded-but-uncommitted completion intent, if any."""
        RunStateModel = globals()["RunStateModel"]
        current = store.get_run(run_id)
        if current is None or RunStateModel.is_terminal(current.state):
            return None
        intent = (current.metadata or {}).get(intent_metadata_key)
        if not isinstance(intent, dict):
            return None
        return store.reconcile_terminal(
            run_id,
            target_state=intent["target_state"],
            lifecycle_update=intent.get("lifecycle_update"),
            event_type=intent.get("event_type"),
            event_payload=intent.get("event_payload"),
            idempotency_key=intent.get("idempotency_key"),
            reason=intent.get("reason", ""),
            stop_reason=intent.get("stop_reason"),
        )


class CancellationReconciler:
    """Durable cancellation-intent recorder and reconciler.

    Mirrors :class:`CompletionReconciler` for the kill path.  A cancellation
    request must leave one durable terminal outcome, or surface a typed error —
    never a successful-looking ``already_dead``/``running`` result with the
    exception swallowed.  A genuine pre-existing terminal winner is preserved
    and never overwritten.
    """

    def __init__(
        self,
        store: "SQLiteRunStore",
        run_id: str,
        *,
        pid: Optional[int] = None,
        result: str = "unknown",
        event_type: str = "process_killed",
        event_payload: Optional[dict] = None,
        idempotency_key: Optional[str] = None,
        stop_reason: str = "operator_halt",
        reason: str = "service: kill",
        intent_metadata_key: str = "_pending_cancellation",
    ) -> None:
        self._store = store
        self.run_id = run_id
        self.pid = pid
        self.result = result
        self.event_type = event_type
        self.event_payload = dict(event_payload or {})
        self.idempotency_key = idempotency_key
        self.stop_reason = stop_reason
        self.reason = reason
        self.intent_metadata_key = intent_metadata_key

    def _intent(self) -> dict:
        return {
            "pid": self.pid,
            "result": self.result,
            "event_type": self.event_type,
            "event_payload": self.event_payload,
            "idempotency_key": self.idempotency_key,
            "stop_reason": self.stop_reason,
            "reason": self.reason,
        }

    def record_intent(self) -> None:
        """Durably write the cancellation intent, retrying past CAS conflicts.

        A genuine terminal winner is left untouched and no intent is written.
        """
        RunStateModel = globals()["RunStateModel"]
        intent = self._intent()
        while True:
            current = self._store.get_run(self.run_id)
            if current is None or RunStateModel.is_terminal(current.state):
                return
            try:
                self._store.update_metadata(
                    self.run_id,
                    expected_version=current.version,
                    metadata={self.intent_metadata_key: intent},
                )
                return
            except VersionConflictError:
                continue
            except InvalidTransitionError:
                # Terminal winner appeared between read and write.
                return

    def reconcile(self) -> RunRecord:
        self.record_intent()
        return self._store.reconcile_terminal(
            self.run_id,
            target_state="advance_or_stop",
            lifecycle_update={"lifecycle_state": "killed"},
            event_type=self.event_type,
            event_payload=self.event_payload,
            idempotency_key=self.idempotency_key,
            reason=self.reason,
            stop_reason=self.stop_reason,
        )

    @classmethod
    def resume_pending(
        cls,
        store: "SQLiteRunStore",
        run_id: str,
        *,
        intent_metadata_key: str = "_pending_cancellation",
    ) -> Optional[RunRecord]:
        """Finish a recorded-but-uncommitted cancellation intent, if any."""
        RunStateModel = globals()["RunStateModel"]
        current = store.get_run(run_id)
        if current is None or RunStateModel.is_terminal(current.state):
            return None
        intent = (current.metadata or {}).get(intent_metadata_key)
        if not isinstance(intent, dict):
            return None
        return store.reconcile_terminal(
            run_id,
            target_state="advance_or_stop",
            lifecycle_update={"lifecycle_state": "killed"},
            event_type=intent.get("event_type", "process_killed"),
            event_payload=intent.get("event_payload"),
            idempotency_key=intent.get("idempotency_key"),
            reason=intent.get("reason", "service: kill"),
            stop_reason=intent.get("stop_reason", "operator_halt"),
        )
