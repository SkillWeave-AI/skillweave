"""Atomic cumulative admission for parent and nested request scopes (SW-1510-BUDGET).

A :class:`ReservationEnvelope` carries the owner-authorized bounds for one
request scope (run/task/attempt/nested). Every provider or tool execution must
obtain an atomic reservation *before* it runs — not as an unused helper called
after the fact.  The :class:`ReservationStore` backs reservations durably in
SQLite, using ``BEGIN IMMEDIATE`` transactions and version-based CAS so
concurrent writers cannot overspend.

Key design decisions
--------------------

* **Atomic admission before execution.**  The caller obtains a reservation
  slot inside the same transaction that creates the run record, so no provider
  or tool can run without an occupied slot.  A reservation is *acquired* before
  the child is spawned and *released* (freed) when the child reaches a terminal
  transport outcome — never before.

* **Unknown usage stays reserved.**  When a transport completes without
  observable byte or token counts, the reserved unknown slot remains occupied
  until an explicit reconciliation.  This prevents a runaway task from silently
  consuming unbounded resources.

* **Retry consumes its own allocation.**  A retry is not a free redo of the
  original admission.  Each retry acquires its own explicit reservation slot
  from the envelope's request budget (if finite), and its completion frees only
  that slot — never double-charges or double-releases the parent.

* **Idempotency keys are scoped.**  An idempotency key used for one request
  scope cannot release a reservation from a different scope.  The key is
  combined with the scope identity (run_id + task_id + attempt) before any
  store operation.

* **Explicit unlimited mode.**  ``None`` for ``remaining_budget``,
  ``max_cost``, ``max_tokens``, ``max_bytes``, or ``max_deadline`` means "no
  hard cumulative limit" — the explicit owner-authorized no-limit envelope.
  No hidden default (e.g. 150 USD / 1000 requests / 24h job deadline) is
  substituted.  Positive per-request bounds (in-flight slots, per-request byte
  limits) still apply.

* **Finite limits cannot overspend.**  A reservation that would exceed any
  finite bound is atomically refused.  Negative, boolean, or otherwise
  malformed limits are refused at envelope construction time.

Schema digest (frozen before implementation)
--------------------------------------------

Reservation envelope schema (proposed v1):
  ``catalog.json`` entry ``reservation-v1`` — see
  ``contracts/catalog.json`` for the exact URI id and version policy.

Execution envelope schema (proposed v1):
  ``catalog.json`` entry ``execution-envelope-v1`` — the boundary artifact
  consumed by Faigate at provider ingress.  The schema is NOT re-invented
  here; the existing ``work-contract.schema.json`` budget definition is the
  authority.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


# ── Error types ─────────────────────────────────────────────────────────────


class ReservationError(Exception):
    """Base for all reservation/admission errors."""


class AdmissionRefused(ReservationError):
    """A reservation was refused because a bound would be exceeded."""

    def __init__(self, axis: str, reason: str, *,
                 run_id: str = "",
                 envelope: Optional["ReservationEnvelope"] = None,
                 detail: Optional[dict] = None):
        self.axis = axis
        self.reason = reason
        self.run_id = run_id
        self.envelope = envelope
        self.detail = detail or {}
        super().__init__(f"admission refused ({axis}): {reason}")


class ReservationConflict(ReservationError):
    """Concurrent reservation CAS conflict (version mismatch)."""


class ScopeMismatch(ReservationError):
    """An idempotency key or release targeted the wrong scope."""


class EnvelopeRejected(ReservationError):
    """The envelope itself is malformed, negative or otherwise invalid."""


class QuotaRefused(ReservationError):
    """A reservation was refused because of a quota or route restriction."""

    def __init__(self, axis: str, reason: str, *,
                 run_id: str = "",
                 detail: Optional[dict] = None):
        self.axis = axis
        self.reason = reason
        self.run_id = run_id
        self.detail = detail or {}
        super().__init__(f"quota refused ({axis}): {reason}")


# ── Reservation state ───────────────────────────────────────────────────────


class ReservationSlotState(str, Enum):
    """Lifecycle of one reservation slot.

    Values are lowercase for consistency with ``RunStateModel``.
    """

    RESERVED = "reserved"          # acquired, child not yet running
    OCCUPIED = "occupied"          # child is running (in-flight)
    COMPLETED = "completed"        # terminal transport outcome received
    RECONCILED = "reconciled"      # unknown usage reconciled
    RELEASED = "released"          # slot freed for reuse
    CANCELLED = "cancelled"        # slot cancelled before child ran

    @classmethod
    def terminal_values(cls) -> frozenset[str]:
        return frozenset({cls.RELEASED.value, cls.CANCELLED.value})

    @classmethod
    def active_values(cls) -> frozenset[str]:
        return frozenset({cls.RESERVED.value, cls.OCCUPIED.value})


# ── ReservationEnvelope: owner-authorized bounds ────────────────────────────


@dataclass(frozen=True)
class ReservationEnvelope:
    """Owner-authorized bounds for one request scope.

    Every field defaults to ``None``, meaning *no hard cumulative limit* for
    that axis — the explicit owner-authorized unlimited mode.  No hidden default
    value (150 USD, 1000 requests, 24h deadline) is ever substituted.

    Finite limits:
    * ``max_cost`` — cumulative monetary cost ceiling (float, non-negative).
    * ``max_tokens`` — cumulative token upper bound (int, non-negative).
    * ``max_bytes`` — cumulative byte upper bound (int, non-negative).
    * ``max_requests`` — cumulative request count (int, non-negative).
    * ``max_deadline`` — ISO instant deadline; admissions refused at or after.
    * ``max_in_flight`` — simultaneous in-flight slot limit per-run (int, positive).
    * ``max_per_request_bytes`` — per-request byte ceiling (int, non-negative).
    * ``max_per_request_tokens`` — per-request token ceiling (int, non-negative).
    * ``allowed_routes`` — optional list of permitted provider route names;
      if set, only these routes may be used (``None`` means all routes permitted).
    """

    max_cost: Optional[float] = None
    max_tokens: Optional[int] = None
    max_bytes: Optional[int] = None
    max_requests: Optional[int] = None
    max_deadline: Optional[str] = None
    max_in_flight: Optional[int] = 3  # default: 3 simultaneous slots
    max_per_request_bytes: Optional[int] = None
    max_per_request_tokens: Optional[int] = None
    allowed_routes: Optional[list[str]] = None

    def __post_init__(self) -> None:
        _validate_finite_limits(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_cost": self.max_cost,
            "max_tokens": self.max_tokens,
            "max_bytes": self.max_bytes,
            "max_requests": self.max_requests,
            "max_deadline": self.max_deadline,
            "max_in_flight": self.max_in_flight,
            "max_per_request_bytes": self.max_per_request_bytes,
            "max_per_request_tokens": self.max_per_request_tokens,
            "allowed_routes": self.allowed_routes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReservationEnvelope":
        return cls(
            max_cost=_safe_float(data, "max_cost"),
            max_tokens=_safe_int(data, "max_tokens"),
            max_bytes=_safe_int(data, "max_bytes"),
            max_requests=_safe_int(data, "max_requests"),
            max_deadline=str(data["max_deadline"]) if data.get("max_deadline") is not None else None,
            max_in_flight=_safe_int(data, "max_in_flight", default=3),
            max_per_request_bytes=_safe_int(data, "max_per_request_bytes"),
            max_per_request_tokens=_safe_int(data, "max_per_request_tokens"),
            allowed_routes=data.get("allowed_routes"),
        )


def _safe_float(data: dict, key: str) -> Optional[float]:
    v = data.get(key)
    if v is None:
        return None
    return float(v)


def _safe_int(data: dict, key: str, default: Any = None) -> Optional[int]:
    v = data.get(key)
    if v is None:
        return default
    return int(v)


def _validate_finite_limits(env: ReservationEnvelope) -> None:
    """Validate that finite limits are non-negative and well-formed.

    ``None`` is the explicit unlimited mode and is always valid.  Negative
    values, booleans, or non-integer types where integers are expected are
    rejected.
    """
    axes: list[tuple[str, Any, str]] = [
        ("max_cost", env.max_cost, "float"),
        ("max_tokens", env.max_tokens, "int"),
        ("max_bytes", env.max_bytes, "int"),
        ("max_requests", env.max_requests, "int"),
        ("max_in_flight", env.max_in_flight, "int"),
        ("max_per_request_bytes", env.max_per_request_bytes, "int"),
        ("max_per_request_tokens", env.max_per_request_tokens, "int"),
    ]
    for name, value, expected_type in axes:
        if value is None:
            continue
        if isinstance(value, bool):
            raise EnvelopeRejected(
                f"{name} must not be a boolean; got {value!r}"
            )
        if expected_type == "int":
            if not isinstance(value, int):
                raise EnvelopeRejected(
                    f"{name} must be an integer or None; got {type(value).__name__}"
                )
            if value < 0:
                raise EnvelopeRejected(
                    f"{name} must not be negative; got {value}"
                )
        elif expected_type == "float":
            if not isinstance(value, (int, float)):
                raise EnvelopeRejected(
                    f"{name} must be a number or None; got {type(value).__name__}"
                )
            if value < 0:
                raise EnvelopeRejected(
                    f"{name} must not be negative; got {value}"
                )

    # max_in_flight must be at least 1 when set
    if env.max_in_flight is not None and env.max_in_flight < 1:
        raise EnvelopeRejected(
            f"max_in_flight must be >= 1 or None; got {env.max_in_flight}"
        )

    # Validate deadline if set
    if env.max_deadline is not None:
        try:
            dt = datetime.fromisoformat(env.max_deadline)
            if dt.tzinfo is None:
                # Treat naive as UTC
                dt = dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError) as exc:
            raise EnvelopeRejected(
                f"max_deadline must be an ISO timestamp; got {env.max_deadline!r}: {exc}"
            )


def _validate_identity(
    *,
    run_id: str = "",
    task_id: str = "",
    attempt: int = 1,
    scope_id: str = "",
    parent_scope_id: Optional[str] = None,
) -> None:
    """Validate identity fields for reservation admission.

    Refuses bool or otherwise malformed values that could indicate
    a bypass attempt or data corruption.
    """
    for name, value in [("run_id", run_id), ("task_id", task_id),
                        ("scope_id", scope_id)]:
        if isinstance(value, bool):
            raise EnvelopeRejected(
                f"{name} must not be a boolean; got {value!r}"
            )
        if not isinstance(value, str) or not value:
            raise EnvelopeRejected(
                f"{name} must be a non-empty string; got {value!r}"
            )
    if isinstance(attempt, bool) or not isinstance(attempt, int):
        raise EnvelopeRejected(
            f"attempt must be an int; got {attempt!r}"
        )
    if parent_scope_id is not None:
        if isinstance(parent_scope_id, bool):
            raise EnvelopeRejected(
                f"parent_scope_id must not be a boolean; got {parent_scope_id!r}"
            )
        if not isinstance(parent_scope_id, str) or not parent_scope_id:
            raise EnvelopeRejected(
                f"parent_scope_id must be a non-empty string or None; got {parent_scope_id!r}"
            )


# ── Reservation record ──────────────────────────────────────────────────────


@dataclass
class ReservationRecord:
    """One persisted reservation slot.

    ``scope_id`` identifies the request scope (e.g. ``run_id/task_id/attempt``
    or a nested request path).  ``parent_scope_id`` links nested reservations
    to their parent.

    ``observed_bytes`` and ``observed_tokens`` are set by the transport on
    completion.  ``reserved_bytes`` / ``reserved_tokens`` are the conservative
    upper bounds reserved at admission time.
    """

    reservation_id: str
    run_id: str
    task_id: str
    attempt: int
    scope_id: str
    parent_scope_id: Optional[str]
    state: str
    version: int
    reserved_bytes: Optional[int]
    reserved_tokens: Optional[float]
    observed_bytes: Optional[int]
    observed_tokens: Optional[float]
    cost: Optional[float]
    created_at: str
    updated_at: str
    completed_at: Optional[str]
    idempotency_key: Optional[str]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "scope_id": self.scope_id,
            "parent_scope_id": self.parent_scope_id,
            "state": self.state,
            "version": self.version,
            "reserved_bytes": self.reserved_bytes,
            "reserved_tokens": self.reserved_tokens,
            "observed_bytes": self.observed_bytes,
            "observed_tokens": self.observed_tokens,
            "cost": self.cost,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "idempotency_key": self.idempotency_key,
            "metadata": dict(self.metadata),
        }


# ── ReservationStore (SQLite-backed, CAS concurrency) ───────────────────────


class ReservationStore:
    """Durable SQLite-backed store for atomic reservation slots.

    Uses ``BEGIN IMMEDIATE`` for all writes, so concurrent connections see a
    write lock rather than corrupting each other's CAS guard.  The schema
    mirrors ``SQLiteRunStore`` conventions (WAL mode, busy timeout, ISO
    timestamps).

    The store is independent of ``SQLiteRunStore`` — it may share the same
    database file or use its own.  When sharing, both stores use their own
    table namespaces.

    Two usage tables:
    * ``reservation_usage`` — per-scope cumulative counters (bytes, tokens,
      cost, requests).
    * ``reservation_run_usage`` — per-run in-flight slot count.
    """

    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS reservations (
                reservation_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 1,
                scope_id TEXT NOT NULL,
                parent_scope_id TEXT,
                state TEXT NOT NULL DEFAULT 'reserved',
                version INTEGER NOT NULL DEFAULT 1,
                reserved_bytes INTEGER,
                reserved_tokens REAL,
                observed_bytes INTEGER,
                observed_tokens REAL,
                cost REAL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                idempotency_key TEXT,
                metadata TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_reservations_run
                ON reservations(run_id, scope_id);
            CREATE INDEX IF NOT EXISTS idx_reservations_parent
                ON reservations(parent_scope_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_reservations_idempotency
                ON reservations(run_id, scope_id, idempotency_key)
                WHERE idempotency_key IS NOT NULL;

            CREATE TABLE IF NOT EXISTS reservation_envelopes (
                run_id TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                envelope_json TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (run_id, scope_id)
            );

            CREATE TABLE IF NOT EXISTS reservation_usage (
                run_id TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                cumulative_cost REAL NOT NULL DEFAULT 0.0,
                cumulative_tokens INTEGER NOT NULL DEFAULT 0,
                cumulative_bytes INTEGER NOT NULL DEFAULT 0,
                cumulative_requests INTEGER NOT NULL DEFAULT 0,
                version INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (run_id, scope_id)
            );

            CREATE TABLE IF NOT EXISTS reservation_run_usage (
                run_id TEXT PRIMARY KEY,
                in_flight INTEGER NOT NULL DEFAULT 0,
                cumulative_requests INTEGER NOT NULL DEFAULT 0,
                cumulative_bytes INTEGER NOT NULL DEFAULT 0,
                cumulative_tokens REAL NOT NULL DEFAULT 0,
                cumulative_cost REAL NOT NULL DEFAULT 0.0,
                version INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reservation_task_usage (
                task_id TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                cumulative_requests INTEGER NOT NULL DEFAULT 0,
                cumulative_bytes INTEGER NOT NULL DEFAULT 0,
                cumulative_tokens REAL NOT NULL DEFAULT 0,
                cumulative_cost REAL NOT NULL DEFAULT 0.0,
                version INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (task_id, scope_id)
            );

            CREATE TABLE IF NOT EXISTS reservation_task_aggregate (
                task_id TEXT PRIMARY KEY,
                cumulative_requests INTEGER NOT NULL DEFAULT 0,
                cumulative_bytes INTEGER NOT NULL DEFAULT 0,
                cumulative_tokens REAL NOT NULL DEFAULT 0,
                cumulative_cost REAL NOT NULL DEFAULT 0.0,
                version INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            );
        """)
        self._conn.commit()

    # ── Envelope management ──────────────────────────────────────────

    @staticmethod
    def _envelope_widens(existing: ReservationEnvelope, proposed: ReservationEnvelope) -> Optional[str]:
        """Return the dimension name if *proposed* widens a finite owner value.

        A caller-supplied envelope must never relax an owner-set finite bound.
        ``None`` (unlimited) is always wider than any finite value.  Only the
        owner (or an explicitly authorized caller) may tighten a bound.
        Returns ``None`` when the proposed envelope is not wider.
        """
        dimensions: list[tuple[str, Any, Any]] = [
            ("max_cost", existing.max_cost, proposed.max_cost),
            ("max_tokens", existing.max_tokens, proposed.max_tokens),
            ("max_bytes", existing.max_bytes, proposed.max_bytes),
            ("max_requests", existing.max_requests, proposed.max_requests),
            ("max_in_flight", existing.max_in_flight, proposed.max_in_flight),
            ("max_per_request_bytes", existing.max_per_request_bytes, proposed.max_per_request_bytes),
            ("max_per_request_tokens", existing.max_per_request_tokens, proposed.max_per_request_tokens),
            ("allowed_routes", existing.allowed_routes, proposed.allowed_routes),
        ]
        for name, owner_val, caller_val in dimensions:
            if owner_val is None:
                continue  # owner explicitly unlimited — caller cannot tighten either, but widening is irrelevant
            if caller_val is None:
                return name  # caller sets unlimited over owner's finite value = widening
            if isinstance(owner_val, list) and isinstance(caller_val, list):
                # For allowed_routes, widening means adding routes not in owner's list
                extra = [r for r in caller_val if r not in owner_val]
                if extra:
                    return name
            elif caller_val > owner_val:
                return name  # caller relaxes owner's bound
        # Deadline check
        if existing.max_deadline is not None:
            if proposed.max_deadline is None:
                return "max_deadline"
            try:
                existing_dt = datetime.fromisoformat(existing.max_deadline)
                proposed_dt = datetime.fromisoformat(proposed.max_deadline)
                if proposed_dt > existing_dt:
                    return "max_deadline"
            except (TypeError, ValueError):
                return "max_deadline"
        return None

    def set_envelope(
        self,
        run_id: str,
        scope_id: str,
        envelope: ReservationEnvelope,
        *,
        force: bool = False,
    ) -> None:
        """Persist an envelope for a (run_id, scope_id) pair.

        When a persisted envelope already exists and ``force`` is ``False``,
        the update is **refused** if the new envelope would *widen* any
        finite owner-set dimension (``None`` is unlimited and always wider
        than a finite value).  This prevents a caller or retry from
        enlarging the approved budget.

        Pass ``force=True`` (owner-authorized) to override this guard.
        For initial creation, version is 1.
        """
        now = datetime.now(timezone.utc).isoformat()
        existing_row = self._conn.execute(
            "SELECT envelope_json, version FROM reservation_envelopes "
            "WHERE run_id = ? AND scope_id = ?",
            (run_id, scope_id),
        ).fetchone()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            if existing_row is None:
                self._conn.execute(
                    """INSERT INTO reservation_envelopes
                       (run_id, scope_id, envelope_json, version, created_at, updated_at)
                       VALUES (?, ?, ?, 1, ?, ?)""",
                    (run_id, scope_id, json.dumps(envelope.to_dict()), now, now),
                )
            else:
                if not force:
                    existing_env = ReservationEnvelope.from_dict(
                        json.loads(existing_row["envelope_json"])
                    )
                    widening = self._envelope_widens(existing_env, envelope)
                    if widening is not None:
                        raise EnvelopeRejected(
                            f"caller envelope widens owner-set '{widening}'; "
                            f"persisted finite owner envelope is authoritative"
                        )
                new_version = existing_row["version"] + 1
                cur = self._conn.execute(
                    """UPDATE reservation_envelopes
                       SET envelope_json = ?, version = ?, updated_at = ?
                       WHERE run_id = ? AND scope_id = ? AND version = ?""",
                    (json.dumps(envelope.to_dict()), new_version, now,
                     run_id, scope_id, existing_row["version"]),
                )
                if cur.rowcount == 0:
                    raise ReservationConflict(
                        f"envelope CAS conflict for ({run_id}, {scope_id})"
                    )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def get_envelope(
        self,
        run_id: str,
        scope_id: str,
        *,
        fallback_task_scope: Optional[str] = None,
    ) -> Optional[ReservationEnvelope]:
        """Return the envelope for (run_id, scope_id) or None.

        When ``fallback_task_scope`` is provided and no exact match is found,
        search for any envelope (across ALL run_ids) whose scope_id starts
        with ``{fallback_task_scope}/attempt-``.  This ensures that a finite
        owner envelope set on ``attempt-1`` is also authoritative for a retry
        at ``attempt-2`` — a retry cannot escape the owner's budget by moving
        to a new run_id or an unbound scope.
        """
        row = self._conn.execute(
            "SELECT envelope_json FROM reservation_envelopes "
            "WHERE run_id = ? AND scope_id = ?",
            (run_id, scope_id),
        ).fetchone()
        if row is not None:
            data = json.loads(row["envelope_json"])
            return ReservationEnvelope.from_dict(data)

        # Fall back: any envelope across ALL runs whose scope starts with
        # the task scope prefix (covers retry attempts with new run_ids).
        if fallback_task_scope is not None:
            prefix = f"{fallback_task_scope}/attempt-"
            row = self._conn.execute(
                "SELECT envelope_json FROM reservation_envelopes "
                "WHERE scope_id LIKE ?",
                (prefix + "%",),
            ).fetchone()
            if row is not None:
                data = json.loads(row["envelope_json"])
                return ReservationEnvelope.from_dict(data)

        return None

    def get_envelope_for_task_scope(
        self,
        task_id: str,
    ) -> Optional[ReservationEnvelope]:
        """Return the first persisted envelope whose scope matches a task scope.

        Searches for any envelope whose scope_id starts with
        ``{task_id}/attempt-`` across ALL run_ids.  This is used by the
        installed ingress (cli/run.py, mcp/server.py) to honour an existing
        owner-set finite envelope before creating a new unlimited one.
        """
        prefix = f"{task_id}/attempt-"
        row = self._conn.execute(
            "SELECT envelope_json FROM reservation_envelopes "
            "WHERE scope_id LIKE ?",
            (prefix + "%",),
        ).fetchone()
        if row is not None:
            data = json.loads(row["envelope_json"])
            return ReservationEnvelope.from_dict(data)
        return None

    def check_route_allowed(
        self,
        route: str,
        envelope: ReservationEnvelope,
    ) -> None:
        """Raise :class:`QuotaRefused` if *route* is not in the envelope's
        ``allowed_routes`` (when set).  An empty or ``None`` list permits all.
        """
        if envelope.allowed_routes is not None:
            if route not in envelope.allowed_routes:
                raise QuotaRefused(
                    "route",
                    f"route {route!r} is not in allowed_routes {envelope.allowed_routes}",
                    detail={"route": route, "allowed_routes": envelope.allowed_routes},
                )

    def _check_parent_nested_budget(
        self,
        env: ReservationEnvelope,
        parent_scope_id: str,
        reserved_bytes: Optional[int],
        reserved_tokens: Optional[float],
        cost: Optional[float],
        run_id: str,
        now: str,
    ) -> None:
        """Enforce nested-scope budget constraints against the parent scope.

        When ``parent_scope_id`` is set, the child reservation's cumulative
        usage is charged against the parent's envelope and cumulative usage.
        This prevents nested requests from escaping the owner's aggregate
        budget.

        The parent's envelope (not the child's) is used for budget checks,
        because the child typically has no explicit envelope and defaults to
        unlimited.
        """
        if not parent_scope_id:
            return
        # Look up the parent's reservation to find the parent's task_id and run_id
        parent_row = self._conn.execute(
            "SELECT task_id, run_id FROM reservations WHERE scope_id = ? AND run_id = ?",
            (parent_scope_id, run_id),
        ).fetchone()
        if parent_row is None:
            return  # no parent found -- skip nested enforcement
        parent_task_id = parent_row["task_id"]
        parent_run_id = parent_row["run_id"]

        # Look up the parent's envelope to get the authoritative budget limits
        parent_env = self.get_envelope(parent_run_id, parent_scope_id)
        if parent_env is None:
            return  # no parent envelope -- no budget to enforce

        # Check parent task-scoped cumulative usage against PARENT envelope
        parent_usage = self._get_or_create_task_usage(parent_task_id, parent_scope_id, now)
        if parent_env.max_requests is not None:
            if parent_usage["cumulative_requests"] >= parent_env.max_requests:
                self._conn.rollback()
                raise AdmissionRefused(
                    "requests",
                    f"nested-scope cumulative requests {parent_usage['cumulative_requests']} >= max {parent_env.max_requests}",
                    run_id=run_id, envelope=parent_env,
                    detail={"cumulative": parent_usage["cumulative_requests"],
                            "max": parent_env.max_requests},
                )
        if parent_env.max_bytes is not None:
            projected = parent_usage["cumulative_bytes"] + (reserved_bytes or 0)
            if projected > parent_env.max_bytes:
                self._conn.rollback()
                raise AdmissionRefused(
                    "bytes",
                    f"nested-scope cumulative bytes {projected} would exceed max {parent_env.max_bytes}",
                    run_id=run_id, envelope=parent_env,
                )
        if parent_env.max_tokens is not None:
            projected_tokens = parent_usage["cumulative_tokens"] + int(reserved_tokens or 0)
            if projected_tokens > parent_env.max_tokens:
                self._conn.rollback()
                raise AdmissionRefused(
                    "tokens",
                    f"nested-scope cumulative tokens {projected_tokens} would exceed max {parent_env.max_tokens}",
                    run_id=run_id, envelope=parent_env,
                )
        if parent_env.max_cost is not None:
            projected_cost = parent_usage["cumulative_cost"] + (cost or 0.0)
            if projected_cost > parent_env.max_cost:
                self._conn.rollback()
                raise AdmissionRefused(
                    "cost",
                    f"nested-scope cumulative cost {projected_cost} would exceed max {parent_env.max_cost}",
                    run_id=run_id, envelope=parent_env,
                )

    def _get_or_create_task_aggregate(self, task_id: str, now: str) -> dict[str, Any]:
        """Get existing task aggregate row or create one (inside active transaction)."""
        row = self._conn.execute(
            "SELECT * FROM reservation_task_aggregate WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is not None:
            return dict(row)
        self._conn.execute(
            """INSERT INTO reservation_task_aggregate
               (task_id, cumulative_requests, cumulative_bytes,
                cumulative_tokens, cumulative_cost, version, updated_at)
               VALUES (?, 0, 0, 0, 0.0, 1, ?)""",
            (task_id, now),
        )
        return {
            "task_id": task_id,
            "cumulative_requests": 0,
            "cumulative_bytes": 0,
            "cumulative_tokens": 0,
            "cumulative_cost": 0.0,
            "version": 1,
            "updated_at": now,
        }

    def get_task_aggregate(
        self,
        task_id: str,
    ) -> Optional[dict[str, Any]]:
        """Return cumulative task aggregate (across ALL attempts) or None."""
        row = self._conn.execute(
            "SELECT * FROM reservation_task_aggregate WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    # ── Reservation operations ───────────────────────────────────────

    def reserve(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        scope_id: str,
        parent_scope_id: Optional[str] = None,
        reserved_bytes: Optional[int] = None,
        reserved_tokens: Optional[float] = None,
        cost: Optional[float] = None,
        idempotency_key: Optional[str] = None,
        envelope: Optional[ReservationEnvelope] = None,
    ) -> ReservationRecord:
        """Atomically acquire a reservation slot.

        This is the central admission gate.  It checks every finite bound in
        the envelope *before* creating the slot and refuses the whole
        transaction if any would be exceeded.

        The operation is atomic: either the reservation is created and all
        cumulative counters are updated, or nothing changes.

        **Envelope authority**: a persisted envelope (set via
        :meth:`set_envelope`) is always authoritative over any caller-supplied
        envelope.  If a persisted envelope exists for ``(run_id, scope_id)``,
        it is used regardless of what the caller passes as ``envelope``.  A
        caller-supplied envelope is only used when no persisted envelope
        exists.  This prevents a caller from bypassing owner-set bounds
        (e.g. ``max_requests=0``) by passing an unlimited envelope.

        Returns the created ``ReservationRecord`` or raises
        :class:`AdmissionRefused`.
        """
        # Validate identity fields — refuse bool or malformed values
        _validate_identity(run_id=run_id, task_id=task_id, attempt=attempt,
                           scope_id=scope_id, parent_scope_id=parent_scope_id)

        # 1. Resolve envelope: persisted owner envelope is ALWAYS authoritative.
        #    When the exact scope has no envelope, fall back to any envelope for
        #    the same task (e.g. attempt-1's envelope covers attempt-2 retries).
        persisted = self.get_envelope(
            run_id, scope_id,
            fallback_task_scope=task_id,
        )
        if persisted is not None:
            env = persisted
        elif envelope is not None:
            env = envelope
        else:
            env = ReservationEnvelope()  # unlimited defaults

        # SW-1510-BUDGET: auto-derive reserved_bytes/reserved_tokens from the
        # resolved envelope's per-request limits when not explicitly provided.
        # This ensures the token/byte dimensions are always exercised through
        # the product ingress, not only at the helper level.  When the resolved
        # envelope has no per-request limit for a dimension, the caller's
        # explicit value (or None) is preserved.
        if reserved_bytes is None and env.max_per_request_bytes is not None:
            reserved_bytes = env.max_per_request_bytes
        if reserved_tokens is None and env.max_per_request_tokens is not None:
            reserved_tokens = float(env.max_per_request_tokens)

        now = datetime.now(timezone.utc).isoformat()
        reservation_id = str(uuid.uuid4())

        # 2. Scope-scoped idempotency: key is combined with scope
        effective_key = idempotency_key
        if effective_key is not None:
            effective_key = f"{run_id}:{scope_id}:{effective_key}"
            existing = self._conn.execute(
                "SELECT * FROM reservations WHERE run_id = ? AND scope_id = ? AND idempotency_key = ?",
                (run_id, scope_id, effective_key),
            ).fetchone()
            if existing is not None:
                return self._row_to_record(existing)

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            # 3. Check deadline
            self._check_deadline(env, now)

            # 4. Get or create per-run in-flight counter
            run_usage = self._get_or_create_run_usage(run_id, now)

            # 5. Check per-run in-flight slots
            if env.max_in_flight is not None:
                if run_usage["in_flight"] >= env.max_in_flight:
                    self._conn.rollback()
                    raise AdmissionRefused(
                        "in_flight",
                        f"in-flight slots {run_usage['in_flight']} >= max {env.max_in_flight}",
                        run_id=run_id, envelope=env,
                        detail={"in_flight": run_usage["in_flight"],
                                "max_in_flight": env.max_in_flight},
                    )

            # 6. Get or create per-scope usage row (for nested tracking)
            usage = self._get_or_create_usage(run_id, scope_id, now)

            # 7. Check cumulative request budget (if finite) — per-run
            if env.max_requests is not None:
                if run_usage["cumulative_requests"] >= env.max_requests:
                    self._conn.rollback()
                    raise AdmissionRefused(
                        "requests",
                        f"cumulative requests {run_usage['cumulative_requests']} >= max {env.max_requests}",
                        run_id=run_id, envelope=env,
                        detail={"cumulative": run_usage["cumulative_requests"],
                                "max": env.max_requests},
                    )

            # 8. Check cumulative bytes (if finite) — per-run
            if env.max_bytes is not None:
                projected = run_usage["cumulative_bytes"] + (reserved_bytes or 0)
                if projected > env.max_bytes:
                    self._conn.rollback()
                    raise AdmissionRefused(
                        "bytes",
                        f"cumulative bytes {projected} would exceed max {env.max_bytes}",
                        run_id=run_id, envelope=env,
                        detail={"cumulative": run_usage["cumulative_bytes"],
                                "reserved": reserved_bytes,
                                "max": env.max_bytes},
                    )

            # 9. Check cumulative tokens (if finite) — per-run
            if env.max_tokens is not None:
                projected_tokens = run_usage["cumulative_tokens"] + int(reserved_tokens or 0)
                if projected_tokens > env.max_tokens:
                    self._conn.rollback()
                    raise AdmissionRefused(
                        "tokens",
                        f"cumulative tokens {projected_tokens} would exceed max {env.max_tokens}",
                        run_id=run_id, envelope=env,
                        detail={"cumulative": run_usage["cumulative_tokens"],
                                "reserved": reserved_tokens,
                                "max": env.max_tokens},
                    )

            # 10. Check cumulative cost (if finite) — per-run
            if env.max_cost is not None:
                projected_cost = run_usage["cumulative_cost"] + (cost or 0.0)
                if projected_cost > env.max_cost:
                    self._conn.rollback()
                    raise AdmissionRefused(
                        "cost",
                        f"cumulative cost {projected_cost} would exceed max {env.max_cost}",
                        run_id=run_id, envelope=env,
                        detail={"cumulative": run_usage["cumulative_cost"],
                                "reserved": cost,
                                "max": env.max_cost},
                    )

            # 10b. Check cumulative budgets against task-scoped usage too.
            # This enforces aggregate budgets across ALL runs for the same
            # task scope (not just per-run), so a retry with a new run_id
            # cannot escape the owner's cumulative budget.
            task_usage = self._get_or_create_task_usage(task_id, scope_id, now)
            if env.max_requests is not None:
                if task_usage["cumulative_requests"] >= env.max_requests:
                    self._conn.rollback()
                    raise AdmissionRefused(
                        "requests",
                        f"task-scoped cumulative requests {task_usage['cumulative_requests']} >= max {env.max_requests}",
                        run_id=run_id, envelope=env,
                        detail={"cumulative": task_usage["cumulative_requests"],
                                "max": env.max_requests},
                    )
            if env.max_bytes is not None:
                projected = task_usage["cumulative_bytes"] + (reserved_bytes or 0)
                if projected > env.max_bytes:
                    self._conn.rollback()
                    raise AdmissionRefused(
                        "bytes",
                        f"task-scoped cumulative bytes {projected} would exceed max {env.max_bytes}",
                        run_id=run_id, envelope=env,
                        detail={"cumulative": task_usage["cumulative_bytes"],
                                "reserved": reserved_bytes,
                                "max": env.max_bytes},
                    )
            if env.max_tokens is not None:
                projected_tokens = task_usage["cumulative_tokens"] + int(reserved_tokens or 0)
                if projected_tokens > env.max_tokens:
                    self._conn.rollback()
                    raise AdmissionRefused(
                        "tokens",
                        f"task-scoped cumulative tokens {projected_tokens} would exceed max {env.max_tokens}",
                        run_id=run_id, envelope=env,
                        detail={"cumulative": task_usage["cumulative_tokens"],
                                "reserved": reserved_tokens,
                                "max": env.max_tokens},
                    )
            if env.max_cost is not None:
                projected_cost = task_usage["cumulative_cost"] + (cost or 0.0)
                if projected_cost > env.max_cost:
                    self._conn.rollback()
                    raise AdmissionRefused(
                        "cost",
                        f"task-scoped cumulative cost {projected_cost} would exceed max {env.max_cost}",
                        run_id=run_id, envelope=env,
                        detail={"cumulative": task_usage["cumulative_cost"],
                                "reserved": cost,
                                "max": env.max_cost},
                    )

            # 10c. Check nested-scope cumulative budgets when parent_scope_id is set.
            # This ensures a nested request is constrained by its parent's budget.
            if parent_scope_id is not None:
                self._check_parent_nested_budget(
                    env, parent_scope_id, reserved_bytes, reserved_tokens,
                    cost, run_id, now,
                )

            # 10d. Check cumulative budgets against task aggregate (across ALL
            # attempts for this task).  This ensures the total across all attempts
            # does not exceed the owner's envelope.
            task_agg = self._get_or_create_task_aggregate(task_id, now)
            if env.max_requests is not None:
                if task_agg["cumulative_requests"] >= env.max_requests:
                    self._conn.rollback()
                    raise AdmissionRefused(
                        "requests",
                        f"task aggregate cumulative requests {task_agg['cumulative_requests']} >= max {env.max_requests}",
                        run_id=run_id, envelope=env,
                        detail={"cumulative": task_agg["cumulative_requests"],
                                "max": env.max_requests},
                    )
            if env.max_bytes is not None:
                projected = task_agg["cumulative_bytes"] + (reserved_bytes or 0)
                if projected > env.max_bytes:
                    self._conn.rollback()
                    raise AdmissionRefused(
                        "bytes",
                        f"task aggregate cumulative bytes {projected} would exceed max {env.max_bytes}",
                        run_id=run_id, envelope=env,
                    )
            if env.max_tokens is not None:
                projected_tokens = task_agg["cumulative_tokens"] + int(reserved_tokens or 0)
                if projected_tokens > env.max_tokens:
                    self._conn.rollback()
                    raise AdmissionRefused(
                        "tokens",
                        f"task aggregate cumulative tokens {projected_tokens} would exceed max {env.max_tokens}",
                        run_id=run_id, envelope=env,
                    )
            if env.max_cost is not None:
                projected_cost = task_agg["cumulative_cost"] + (cost or 0.0)
                if projected_cost > env.max_cost:
                    self._conn.rollback()
                    raise AdmissionRefused(
                        "cost",
                        f"task aggregate cumulative cost {projected_cost} would exceed max {env.max_cost}",
                        run_id=run_id, envelope=env,
                    )

            # 11. Check per-request bounds (if finite)
            if env.max_per_request_bytes is not None:
                if (reserved_bytes or 0) > env.max_per_request_bytes:
                    self._conn.rollback()
                    raise AdmissionRefused(
                        "per_request_bytes",
                        f"request bytes {reserved_bytes} exceeds max {env.max_per_request_bytes}",
                        run_id=run_id, envelope=env,
                    )
            if env.max_per_request_tokens is not None:
                if int(reserved_tokens or 0) > env.max_per_request_tokens:
                    self._conn.rollback()
                    raise AdmissionRefused(
                        "per_request_tokens",
                        f"request tokens {int(reserved_tokens or 0)} exceeds max {env.max_per_request_tokens}",
                        run_id=run_id, envelope=env,
                    )

            # 12. Create the reservation record
            self._conn.execute(
                """INSERT INTO reservations
                   (reservation_id, run_id, task_id, attempt, scope_id, parent_scope_id,
                    state, version, reserved_bytes, reserved_tokens, cost,
                    created_at, updated_at, completed_at, idempotency_key, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, NULL, ?, '{}')""",
                (reservation_id, run_id, task_id, attempt, scope_id, parent_scope_id,
                 ReservationSlotState.RESERVED.value,
                 reserved_bytes, reserved_tokens, cost,
                 now, now, effective_key),
            )

            # 13. Update per-run counters (in_flight + cumulative)
            new_rv = run_usage["version"] + 1
            cur = self._conn.execute(
                """UPDATE reservation_run_usage
                   SET in_flight = in_flight + 1,
                       cumulative_requests = cumulative_requests + 1,
                       cumulative_bytes = cumulative_bytes + ?,
                       cumulative_tokens = cumulative_tokens + ?,
                       cumulative_cost = cumulative_cost + ?,
                       version = ?,
                       updated_at = ?
                   WHERE run_id = ? AND version = ?""",
                (reserved_bytes or 0, int(reserved_tokens or 0), cost or 0.0,
                 new_rv, now, run_id, run_usage["version"]),
            )
            if cur.rowcount == 0:
                self._conn.rollback()
                raise ReservationConflict(
                    f"run usage CAS conflict for run {run_id}"
                )

            # 14. Update per-scope cumulative usage (increment request count)
            new_version = usage["version"] + 1
            cur2 = self._conn.execute(
                """UPDATE reservation_usage
                   SET cumulative_requests = cumulative_requests + 1,
                       cumulative_bytes = cumulative_bytes + ?,
                       cumulative_tokens = cumulative_tokens + ?,
                       cumulative_cost = cumulative_cost + ?,
                       version = ?,
                       updated_at = ?
                   WHERE run_id = ? AND scope_id = ? AND version = ?""",
                (reserved_bytes or 0, int(reserved_tokens or 0), cost or 0.0,
                 new_version, now, run_id, scope_id, usage["version"]),
            )
            if cur2.rowcount == 0:
                self._conn.rollback()
                raise ReservationConflict(
                    f"usage CAS conflict for ({run_id}, {scope_id})"
                )

            # 15. Update task-scoped cumulative usage (aggregate across runs)
            new_tv = task_usage["version"] + 1
            cur3 = self._conn.execute(
                """UPDATE reservation_task_usage
                   SET cumulative_requests = cumulative_requests + 1,
                       cumulative_bytes = cumulative_bytes + ?,
                       cumulative_tokens = cumulative_tokens + ?,
                       cumulative_cost = cumulative_cost + ?,
                       version = ?,
                       updated_at = ?
                   WHERE task_id = ? AND scope_id = ? AND version = ?""",
                (reserved_bytes or 0, int(reserved_tokens or 0), cost or 0.0,
                 new_tv, now, task_id, scope_id, task_usage["version"]),
            )
            if cur3.rowcount == 0:
                self._conn.rollback()
                raise ReservationConflict(
                    f"task usage CAS conflict for ({task_id}, {scope_id})"
                )

            # 16. Update task aggregate cumulative usage (across ALL attempts)
            new_av = task_agg["version"] + 1
            cur4 = self._conn.execute(
                """UPDATE reservation_task_aggregate
                   SET cumulative_requests = cumulative_requests + 1,
                       cumulative_bytes = cumulative_bytes + ?,
                       cumulative_tokens = cumulative_tokens + ?,
                       cumulative_cost = cumulative_cost + ?,
                       version = ?,
                       updated_at = ?
                   WHERE task_id = ? AND version = ?""",
                (reserved_bytes or 0, int(reserved_tokens or 0), cost or 0.0,
                 new_av, now, task_id, task_agg["version"]),
            )
            if cur4.rowcount == 0:
                self._conn.rollback()
                raise ReservationConflict(
                    f"task aggregate CAS conflict for {task_id}"
                )

            self._conn.commit()
        except (AdmissionRefused, ReservationConflict):
            self._conn.rollback()
            raise
        except Exception:
            self._conn.rollback()
            raise

        return self._get_reservation(reservation_id)

    def occupy(self, reservation_id: str) -> ReservationRecord:
        """Transition a reservation from RESERVED to OCCUPIED (child started).

        CAS-guarded: only transitions from RESERVED.
        """
        return self._transition_state(
            reservation_id,
            ReservationSlotState.RESERVED.value,
            ReservationSlotState.OCCUPIED.value,
        )

    def complete(
        self,
        reservation_id: str,
        *,
        observed_bytes: Optional[int] = None,
        observed_tokens: Optional[float] = None,
        cost: Optional[float] = None,
    ) -> ReservationRecord:
        """Record terminal transport outcome.

        Frees the in-flight slot but does NOT erase the unknown/reserved
        accounting for dimensions that were NOT observed — only the
        explicitly provided observed dimensions are reconciled against
        reserved amounts.  Unknown usage stays reserved until an explicit
        :meth:`reconcile` call.  This prevents a runaway task from silently
        consuming unbounded resources.

        Mixed observation (e.g. observed_bytes=80, observed_tokens=None)
        is handled per-dimension: bytes are reconciled, tokens are preserved
        as reserved until reconcile.
        """
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            rec = self._get_reservation(reservation_id)
            if rec is None:
                self._conn.rollback()
                raise ReservationError(f"reservation {reservation_id} not found")

            allowed = {ReservationSlotState.RESERVED.value,
                       ReservationSlotState.OCCUPIED.value}
            if rec.state not in allowed:
                self._conn.rollback()
                raise ReservationError(
                    f"cannot complete reservation {reservation_id} "
                    f"in state {rec.state!r}; expected one of {allowed}"
                )

            new_version = rec.version + 1
            cur = self._conn.execute(
                """UPDATE reservations
                   SET state = ?, version = ?, updated_at = ?, completed_at = ?,
                       observed_bytes = COALESCE(?, observed_bytes),
                       observed_tokens = COALESCE(?, observed_tokens),
                       cost = COALESCE(?, cost)
                   WHERE reservation_id = ? AND version = ?""",
                (ReservationSlotState.COMPLETED.value, new_version, now, now,
                 observed_bytes, observed_tokens, cost,
                 reservation_id, rec.version),
            )
            if cur.rowcount == 0:
                self._conn.rollback()
                raise ReservationConflict(
                    f"CAS conflict completing {reservation_id}"
                )

            # Decrement per-run in_flight
            run_usage = self._get_or_create_run_usage(rec.run_id, now)
            new_rv = run_usage["version"] + 1

            # Build per-dimension delta set.  Only dimensions that were
            # explicitly observed get a delta; unobserved (None) dimensions
            # keep their reserved amounts in cumulative totals until an
            # explicit reconcile() call.
            delta_bytes = 0
            delta_tokens = 0
            delta_cost = 0.0
            if observed_bytes is not None:
                delta_bytes = observed_bytes - (rec.reserved_bytes or 0)
            if observed_tokens is not None:
                delta_tokens = int(observed_tokens) - int(rec.reserved_tokens or 0)
            if cost is not None:
                delta_cost = cost - (rec.cost or 0.0)

            any_observed = (observed_bytes is not None
                            or observed_tokens is not None
                            or cost is not None)
            if any_observed:
                cur_r = self._conn.execute(
                    """UPDATE reservation_run_usage
                       SET in_flight = MAX(0, in_flight - 1),
                           cumulative_bytes = MAX(0, cumulative_bytes + ?),
                           cumulative_tokens = MAX(0, cumulative_tokens + ?),
                           cumulative_cost = MAX(0, cumulative_cost + ?),
                           version = ?,
                           updated_at = ?
                       WHERE run_id = ? AND version = ?""",
                    (delta_bytes, delta_tokens, delta_cost,
                     new_rv, now, rec.run_id, run_usage["version"]),
                )
            else:
                cur_r = self._conn.execute(
                    """UPDATE reservation_run_usage
                       SET in_flight = MAX(0, in_flight - 1),
                           version = ?,
                           updated_at = ?
                       WHERE run_id = ? AND version = ?""",
                    (new_rv, now, rec.run_id, run_usage["version"]),
                )
            if cur_r.rowcount == 0:
                self._conn.rollback()
                raise ReservationConflict(
                    f"run usage CAS conflict during complete for {rec.run_id}"
                )

            # Update per-scope cumulative counters — same per-dimension logic
            usage = self._get_or_create_usage(rec.run_id, rec.scope_id, now)
            new_uv = usage["version"] + 1
            if any_observed:
                cur2 = self._conn.execute(
                    """UPDATE reservation_usage
                       SET cumulative_bytes = cumulative_bytes + ?,
                           cumulative_tokens = cumulative_tokens + ?,
                           cumulative_cost = cumulative_cost + ?,
                           version = ?,
                           updated_at = ?
                       WHERE run_id = ? AND scope_id = ? AND version = ?""",
                    (delta_bytes, delta_tokens, delta_cost,
                     new_uv, now, rec.run_id, rec.scope_id, usage["version"]),
                )
            else:
                cur2 = self._conn.execute(
                    """UPDATE reservation_usage
                       SET cumulative_requests = cumulative_requests,
                           version = ?,
                           updated_at = ?
                       WHERE run_id = ? AND scope_id = ? AND version = ?""",
                    (new_uv, now, rec.run_id, rec.scope_id, usage["version"]),
                )
            if cur2.rowcount == 0:
                self._conn.rollback()
                raise ReservationConflict(
                    f"usage CAS conflict during complete for ({rec.run_id}, {rec.scope_id})"
                )

            # Update task-scoped cumulative counters — same per-dimension logic
            task_usage = self._get_or_create_task_usage(rec.task_id, rec.scope_id, now)
            new_tv = task_usage["version"] + 1
            if any_observed:
                cur3 = self._conn.execute(
                    """UPDATE reservation_task_usage
                       SET cumulative_bytes = cumulative_bytes + ?,
                           cumulative_tokens = cumulative_tokens + ?,
                           cumulative_cost = cumulative_cost + ?,
                           version = ?,
                           updated_at = ?
                       WHERE task_id = ? AND scope_id = ? AND version = ?""",
                    (delta_bytes, delta_tokens, delta_cost,
                     new_tv, now, rec.task_id, rec.scope_id, task_usage["version"]),
                )
            else:
                cur3 = self._conn.execute(
                    """UPDATE reservation_task_usage
                       SET cumulative_requests = cumulative_requests,
                           version = ?,
                           updated_at = ?
                       WHERE task_id = ? AND scope_id = ? AND version = ?""",
                    (new_tv, now, rec.task_id, rec.scope_id, task_usage["version"]),
                )
            if cur3.rowcount == 0:
                self._conn.rollback()
                raise ReservationConflict(
                    f"task usage CAS conflict during complete for ({rec.task_id}, {rec.scope_id})"
                )

            # Update task aggregate cumulative counters — across ALL attempts
            task_agg = self._get_or_create_task_aggregate(rec.task_id, now)
            new_av = task_agg["version"] + 1
            if any_observed:
                cur4 = self._conn.execute(
                    """UPDATE reservation_task_aggregate
                       SET cumulative_bytes = MAX(0, cumulative_bytes + ?),
                           cumulative_tokens = MAX(0, cumulative_tokens + ?),
                           cumulative_cost = MAX(0, cumulative_cost + ?),
                           version = ?,
                           updated_at = ?
                       WHERE task_id = ? AND version = ?""",
                    (delta_bytes, delta_tokens, delta_cost,
                     new_av, now, rec.task_id, task_agg["version"]),
                )
            else:
                cur4 = self._conn.execute(
                    """UPDATE reservation_task_aggregate
                       SET cumulative_requests = cumulative_requests,
                           version = ?,
                           updated_at = ?
                       WHERE task_id = ? AND version = ?""",
                    (new_av, now, rec.task_id, task_agg["version"]),
                )
            if cur4.rowcount == 0:
                self._conn.rollback()
                raise ReservationConflict(
                    f"task aggregate CAS conflict during complete for {rec.task_id}"
                )

            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

        return self._get_reservation(reservation_id)

    def release(self, reservation_id: str) -> ReservationRecord:
        """Free a reservation slot entirely (terminal release).

        Only transitions COMPLETED or CANCELLED -> RELEASED.
        Does NOT erase cumulative accounting — unknown usage stays reserved.
        """
        return self._transition_state(
            reservation_id,
            (ReservationSlotState.COMPLETED.value,
             ReservationSlotState.CANCELLED.value),
            ReservationSlotState.RELEASED.value,
        )

    def cancel(self, reservation_id: str) -> ReservationRecord:
        """Cancel a reservation before it was occupied (e.g. pre-flight failure).

        Transitions RESERVED -> CANCELLED and frees the in-flight slot.
        """
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            rec = self._get_reservation(reservation_id)
            if rec is None:
                self._conn.rollback()
                raise ReservationError(f"reservation {reservation_id} not found")

            if rec.state != ReservationSlotState.RESERVED.value:
                self._conn.rollback()
                raise ReservationError(
                    f"cannot cancel reservation {reservation_id} "
                    f"in state {rec.state!r}; expected 'reserved'"
                )

            new_version = rec.version + 1
            cur = self._conn.execute(
                """UPDATE reservations
                   SET state = ?, version = ?, updated_at = ?, completed_at = ?
                   WHERE reservation_id = ? AND version = ?""",
                (ReservationSlotState.CANCELLED.value, new_version, now, now,
                 reservation_id, rec.version),
            )
            if cur.rowcount == 0:
                self._conn.rollback()
                raise ReservationConflict(
                    f"CAS conflict cancelling {reservation_id}"
                )

            # Decrement per-run in_flight and reverse cumulative counters (single UPDATE)
            run_usage = self._get_or_create_run_usage(rec.run_id, now)
            new_rv = run_usage["version"] + 1
            cur_r = self._conn.execute(
                """UPDATE reservation_run_usage
                   SET in_flight = MAX(0, in_flight - 1),
                       cumulative_requests = MAX(0, cumulative_requests - 1),
                       cumulative_bytes = MAX(0, cumulative_bytes - ?),
                       cumulative_tokens = MAX(0, cumulative_tokens - ?),
                       cumulative_cost = MAX(0, cumulative_cost - ?),
                       version = ?,
                       updated_at = ?
                   WHERE run_id = ? AND version = ?""",
                (rec.reserved_bytes or 0, int(rec.reserved_tokens or 0),
                 rec.cost or 0.0, new_rv, now,
                 rec.run_id, run_usage["version"]),
            )
            if cur_r.rowcount == 0:
                self._conn.rollback()
                raise ReservationConflict(
                    f"run usage CAS conflict during cancel for {rec.run_id}"
                )

            # Reverse per-scope cumulative counters (for nested tracking)
            usage = self._get_or_create_usage(rec.run_id, rec.scope_id, now)
            new_uv = usage["version"] + 1
            cur2 = self._conn.execute(
                """UPDATE reservation_usage
                   SET cumulative_requests = MAX(0, cumulative_requests - 1),
                       cumulative_bytes = MAX(0, cumulative_bytes - ?),
                       cumulative_tokens = MAX(0, cumulative_tokens - ?),
                       cumulative_cost = MAX(0, cumulative_cost - ?),
                       version = ?,
                       updated_at = ?
                   WHERE run_id = ? AND scope_id = ? AND version = ?""",
                (rec.reserved_bytes or 0, int(rec.reserved_tokens or 0),
                 rec.cost or 0.0, new_uv, now,
                 rec.run_id, rec.scope_id, usage["version"]),
            )
            if cur2.rowcount == 0:
                self._conn.rollback()
                raise ReservationConflict(
                    f"usage CAS conflict during cancel for ({rec.run_id}, {rec.scope_id})"
                )

            # Reverse task-scoped cumulative counters (aggregate across runs)
            task_usage = self._get_or_create_task_usage(rec.task_id, rec.scope_id, now)
            new_tv = task_usage["version"] + 1
            cur3 = self._conn.execute(
                """UPDATE reservation_task_usage
                   SET cumulative_requests = MAX(0, cumulative_requests - 1),
                       cumulative_bytes = MAX(0, cumulative_bytes - ?),
                       cumulative_tokens = MAX(0, cumulative_tokens - ?),
                       cumulative_cost = MAX(0, cumulative_cost - ?),
                       version = ?,
                       updated_at = ?
                   WHERE task_id = ? AND scope_id = ? AND version = ?""",
                (rec.reserved_bytes or 0, int(rec.reserved_tokens or 0),
                 rec.cost or 0.0, new_tv, now,
                 rec.task_id, rec.scope_id, task_usage["version"]),
            )
            if cur3.rowcount == 0:
                self._conn.rollback()
                raise ReservationConflict(
                    f"task usage CAS conflict during cancel for ({rec.task_id}, {rec.scope_id})"
                )

            # Reverse task aggregate cumulative counters (across ALL attempts)
            task_agg = self._get_or_create_task_aggregate(rec.task_id, now)
            new_av = task_agg["version"] + 1
            cur4 = self._conn.execute(
                """UPDATE reservation_task_aggregate
                   SET cumulative_requests = MAX(0, cumulative_requests - 1),
                       cumulative_bytes = MAX(0, cumulative_bytes - ?),
                       cumulative_tokens = MAX(0, cumulative_tokens - ?),
                       cumulative_cost = MAX(0, cumulative_cost - ?),
                       version = ?,
                       updated_at = ?
                   WHERE task_id = ? AND version = ?""",
                (rec.reserved_bytes or 0, int(rec.reserved_tokens or 0),
                 rec.cost or 0.0, new_av, now,
                 rec.task_id, task_agg["version"]),
            )
            if cur4.rowcount == 0:
                self._conn.rollback()
                raise ReservationConflict(
                    f"task aggregate CAS conflict during cancel for {rec.task_id}"
                )

            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

        return self._get_reservation(reservation_id)

    def reconcile(
        self,
        reservation_id: str,
        *,
        actual_bytes: Optional[int] = None,
        actual_tokens: Optional[float] = None,
        actual_cost: Optional[float] = None,
    ) -> ReservationRecord:
        """Reconcile unknown usage: replace reserved amounts with actuals.

        Transitions COMPLETED -> RECONCILED.  Only dimensions that are
        explicitly provided are reconciled; dimensions left as None keep
        their reserved amounts in cumulative totals.  Only callable once
        per reservation (state must be COMPLETED).

        Per-dimension delta is computed from the *original reserved*
        amount, not from previously observed values, so a prior
        ``complete()`` with partial observation does not cause
        double-application.
        """
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            rec = self._get_reservation(reservation_id)
            if rec is None:
                self._conn.rollback()
                raise ReservationError(f"reservation {reservation_id} not found")

            if rec.state != ReservationSlotState.COMPLETED.value:
                self._conn.rollback()
                raise ReservationError(
                    f"cannot reconcile reservation {reservation_id} "
                    f"in state {rec.state!r}; expected 'completed'"
                )

            new_version = rec.version + 1
            # Compute per-dimension deltas from the CURRENT best-known value
            # on the reservation record.  If complete() already observed a
            # dimension, that observed value is the baseline; otherwise the
            # original reserved amount is the baseline.  This prevents
            # double-application when complete() already reconciled a
            # dimension and reconcile() now adjusts a different dimension.
            current_bytes = rec.observed_bytes if rec.observed_bytes is not None else (rec.reserved_bytes or 0)
            current_tokens = int(rec.observed_tokens) if rec.observed_tokens is not None else int(rec.reserved_tokens or 0)
            current_cost = rec.cost if rec.cost is not None else 0.0
            delta_bytes = 0
            delta_tokens = 0
            delta_cost = 0.0
            if actual_bytes is not None:
                delta_bytes = actual_bytes - current_bytes
            if actual_tokens is not None:
                delta_tokens = int(actual_tokens) - current_tokens
            if actual_cost is not None:
                delta_cost = actual_cost - (rec.cost or 0.0)

            any_actual = (actual_bytes is not None
                          or actual_tokens is not None
                          or actual_cost is not None)

            cur = self._conn.execute(
                """UPDATE reservations
                   SET state = ?, version = ?, updated_at = ?,
                       observed_bytes = COALESCE(?, observed_bytes),
                       observed_tokens = COALESCE(?, observed_tokens),
                       cost = COALESCE(?, cost)
                   WHERE reservation_id = ? AND version = ?""",
                (ReservationSlotState.RECONCILED.value, new_version, now,
                 actual_bytes, actual_tokens, actual_cost,
                 reservation_id, rec.version),
            )
            if cur.rowcount == 0:
                self._conn.rollback()
                raise ReservationConflict(
                    f"CAS conflict reconciling {reservation_id}"
                )

            # Apply delta to per-scope cumulative totals — per-dimension
            usage = self._get_or_create_usage(rec.run_id, rec.scope_id, now)
            new_uv = usage["version"] + 1
            cur2 = self._conn.execute(
                """UPDATE reservation_usage
                   SET cumulative_bytes = MAX(0, cumulative_bytes + ?),
                       cumulative_tokens = MAX(0, cumulative_tokens + ?),
                       cumulative_cost = MAX(0, cumulative_cost + ?),
                       version = ?,
                       updated_at = ?
                   WHERE run_id = ? AND scope_id = ? AND version = ?""",
                (delta_bytes, delta_tokens, delta_cost,
                 new_uv, now, rec.run_id, rec.scope_id, usage["version"]),
            )
            if cur2.rowcount == 0:
                self._conn.rollback()
                raise ReservationConflict(
                    f"usage CAS conflict during reconcile for ({rec.run_id}, {rec.scope_id})"
                )

            # Apply delta to per-run cumulative totals — per-dimension
            run_usage = self._get_or_create_run_usage(rec.run_id, now)
            new_rv = run_usage["version"] + 1
            cur_r = self._conn.execute(
                """UPDATE reservation_run_usage
                   SET cumulative_bytes = MAX(0, cumulative_bytes + ?),
                       cumulative_tokens = MAX(0, cumulative_tokens + ?),
                       cumulative_cost = MAX(0, cumulative_cost + ?),
                       version = ?,
                       updated_at = ?
                   WHERE run_id = ? AND version = ?""",
                (delta_bytes, delta_tokens, delta_cost,
                 new_rv, now, rec.run_id, run_usage["version"]),
            )
            if cur_r.rowcount == 0:
                self._conn.rollback()
                raise ReservationConflict(
                    f"run usage CAS conflict during reconcile for {rec.run_id}"
                )

            # Apply delta to task-scoped cumulative totals — per-dimension
            task_usage = self._get_or_create_task_usage(rec.task_id, rec.scope_id, now)
            new_tv = task_usage["version"] + 1
            if any_actual:
                cur_t = self._conn.execute(
                    """UPDATE reservation_task_usage
                       SET cumulative_bytes = MAX(0, cumulative_bytes + ?),
                           cumulative_tokens = MAX(0, cumulative_tokens + ?),
                           cumulative_cost = MAX(0, cumulative_cost + ?),
                           version = ?,
                           updated_at = ?
                       WHERE task_id = ? AND scope_id = ? AND version = ?""",
                    (delta_bytes, delta_tokens, delta_cost,
                     new_tv, now, rec.task_id, rec.scope_id, task_usage["version"]),
                )
            else:
                cur_t = self._conn.execute(
                    """UPDATE reservation_task_usage
                       SET cumulative_requests = cumulative_requests,
                           version = ?,
                           updated_at = ?
                       WHERE task_id = ? AND scope_id = ? AND version = ?""",
                    (new_tv, now, rec.task_id, rec.scope_id, task_usage["version"]),
                )
            if cur_t.rowcount == 0:
                self._conn.rollback()
                raise ReservationConflict(
                    f"task usage CAS conflict during reconcile for ({rec.task_id}, {rec.scope_id})"
                )

            # Apply delta to task aggregate cumulative counters (across ALL attempts)
            task_agg = self._get_or_create_task_aggregate(rec.task_id, now)
            new_av = task_agg["version"] + 1
            if any_actual:
                cur_a = self._conn.execute(
                    """UPDATE reservation_task_aggregate
                       SET cumulative_bytes = MAX(0, cumulative_bytes + ?),
                           cumulative_tokens = MAX(0, cumulative_tokens + ?),
                           cumulative_cost = MAX(0, cumulative_cost + ?),
                           version = ?,
                           updated_at = ?
                       WHERE task_id = ? AND version = ?""",
                    (delta_bytes, delta_tokens, delta_cost,
                     new_av, now, rec.task_id, task_agg["version"]),
                )
            else:
                cur_a = self._conn.execute(
                    """UPDATE reservation_task_aggregate
                       SET cumulative_requests = cumulative_requests,
                           version = ?,
                           updated_at = ?
                       WHERE task_id = ? AND version = ?""",
                    (new_av, now, rec.task_id, task_agg["version"]),
                )
            if cur_a.rowcount == 0:
                self._conn.rollback()
                raise ReservationConflict(
                    f"task aggregate CAS conflict during reconcile for {rec.task_id}"
                )

            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

        return self._get_reservation(reservation_id)

    # ── Query ────────────────────────────────────────────────────────

    def get_reservation(self, reservation_id: str) -> Optional[ReservationRecord]:
        return self._get_reservation(reservation_id)

    def list_reservations(
        self,
        run_id: str,
        scope_id: Optional[str] = None,
        state: Optional[str] = None,
    ) -> list[ReservationRecord]:
        """List reservations for a run, optionally filtered by scope and state."""
        clauses = ["run_id = ?"]
        params: list[Any] = [run_id]
        if scope_id is not None:
            clauses.append("scope_id = ?")
            params.append(scope_id)
        if state is not None:
            clauses.append("state = ?")
            params.append(state)
        sql = "SELECT * FROM reservations WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at ASC"
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_record(r) for r in rows]

    def get_usage(
        self,
        run_id: str,
        scope_id: str,
    ) -> Optional[dict[str, Any]]:
        """Return cumulative usage for (run_id, scope_id) or None."""
        row = self._conn.execute(
            "SELECT * FROM reservation_usage WHERE run_id = ? AND scope_id = ?",
            (run_id, scope_id),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def get_run_usage(self, run_id: str) -> Optional[dict[str, Any]]:
        """Return per-run usage for *run_id* or None."""
        row = self._conn.execute(
            "SELECT * FROM reservation_run_usage WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def count_in_flight(self, run_id: str) -> int:
        """Return the number of currently in-flight slots for a run."""
        usage = self.get_run_usage(run_id)
        if usage is None:
            return 0
        return usage["in_flight"]

    # ── Restart recovery ─────────────────────────────────────────────

    def recover_orphaned(self) -> list[ReservationRecord]:
        """Reconcile reservations that were left in RESERVED/OCCUPIED on restart.

        These are slots that were acquired but never completed because the
        process crashed.  They are transitioned to COMPLETED with no observed
        usage, and their in-flight slots are freed.

        Returns the list of recovered records.
        """
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            orphans = self._conn.execute(
                "SELECT * FROM reservations WHERE state IN ('reserved', 'occupied')"
            ).fetchall()
            recovered: list[ReservationRecord] = []
            for row in orphans:
                rec = self._row_to_record(row)
                new_version = rec.version + 1
                cur = self._conn.execute(
                    """UPDATE reservations
                       SET state = ?, version = ?, updated_at = ?, completed_at = ?
                       WHERE reservation_id = ? AND version = ?""",
                    (ReservationSlotState.COMPLETED.value, new_version, now, now,
                     rec.reservation_id, rec.version),
                )
                if cur.rowcount == 0:
                    continue  # another writer already handled this one
                # Free per-run in-flight
                run_usage = self._get_or_create_run_usage(rec.run_id, now)
                new_rv = run_usage["version"] + 1
                self._conn.execute(
                    """UPDATE reservation_run_usage
                       SET in_flight = MAX(0, in_flight - 1), version = ?, updated_at = ?
                       WHERE run_id = ? AND version = ?""",
                    (new_rv, now, rec.run_id, run_usage["version"]),
                )
                rec.state = ReservationSlotState.COMPLETED.value
                rec.version = new_version
                rec.updated_at = now
                rec.completed_at = now
                recovered.append(rec)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return recovered

    # ── Internal helpers ─────────────────────────────────────────────

    def _get_reservation(self, reservation_id: str) -> Optional[ReservationRecord]:
        row = self._conn.execute(
            "SELECT * FROM reservations WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
        return self._row_to_record(row) if row else None

    def _row_to_record(self, row) -> ReservationRecord:
        meta = {}
        if row["metadata"]:
            try:
                meta = json.loads(row["metadata"])
            except (json.JSONDecodeError, TypeError):
                meta = {}
        return ReservationRecord(
            reservation_id=row["reservation_id"],
            run_id=row["run_id"],
            task_id=row["task_id"],
            attempt=row["attempt"],
            scope_id=row["scope_id"],
            parent_scope_id=row["parent_scope_id"],
            state=row["state"],
            version=row["version"],
            reserved_bytes=row["reserved_bytes"],
            reserved_tokens=row["reserved_tokens"],
            observed_bytes=row["observed_bytes"],
            observed_tokens=row["observed_tokens"],
            cost=row["cost"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            idempotency_key=row["idempotency_key"],
            metadata=meta,
        )

    def _get_or_create_usage(
        self, run_id: str, scope_id: str, now: str
    ) -> dict[str, Any]:
        """Get existing usage row or create one (inside an active transaction)."""
        row = self._conn.execute(
            "SELECT * FROM reservation_usage WHERE run_id = ? AND scope_id = ?",
            (run_id, scope_id),
        ).fetchone()
        if row is not None:
            return dict(row)
        self._conn.execute(
            """INSERT INTO reservation_usage
               (run_id, scope_id, cumulative_cost, cumulative_tokens,
                cumulative_bytes, cumulative_requests, version, updated_at)
               VALUES (?, ?, 0.0, 0, 0, 0, 1, ?)""",
            (run_id, scope_id, now),
        )
        return {
            "run_id": run_id,
            "scope_id": scope_id,
            "cumulative_cost": 0.0,
            "cumulative_tokens": 0,
            "cumulative_bytes": 0,
            "cumulative_requests": 0,
            "version": 1,
            "updated_at": now,
        }

    def _get_or_create_run_usage(self, run_id: str, now: str) -> dict[str, Any]:
        """Get existing run usage row or create one (inside active transaction)."""
        row = self._conn.execute(
            "SELECT * FROM reservation_run_usage WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is not None:
            return dict(row)
        self._conn.execute(
            """INSERT INTO reservation_run_usage
               (run_id, in_flight, cumulative_requests, cumulative_bytes,
                cumulative_tokens, cumulative_cost, version, updated_at)
               VALUES (?, 0, 0, 0, 0, 0.0, 1, ?)""",
            (run_id, now),
        )
        return {
            "run_id": run_id,
            "in_flight": 0,
            "cumulative_requests": 0,
            "cumulative_bytes": 0,
            "cumulative_tokens": 0.0,
            "cumulative_cost": 0.0,
            "version": 1,
            "updated_at": now,
        }

    def _get_or_create_task_usage(
        self, task_id: str, scope_id: str, now: str
    ) -> dict[str, Any]:
        """Get existing task-scoped usage row or create one (inside active transaction)."""
        row = self._conn.execute(
            "SELECT * FROM reservation_task_usage WHERE task_id = ? AND scope_id = ?",
            (task_id, scope_id),
        ).fetchone()
        if row is not None:
            return dict(row)
        self._conn.execute(
            """INSERT INTO reservation_task_usage
               (task_id, scope_id, cumulative_requests, cumulative_bytes,
                cumulative_tokens, cumulative_cost, version, updated_at)
               VALUES (?, ?, 0, 0, 0, 0.0, 1, ?)""",
            (task_id, scope_id, now),
        )
        return {
            "task_id": task_id,
            "scope_id": scope_id,
            "cumulative_requests": 0,
            "cumulative_bytes": 0,
            "cumulative_tokens": 0,
            "cumulative_cost": 0.0,
            "version": 1,
            "updated_at": now,
        }

    def get_task_usage(
        self,
        task_id: str,
        scope_id: str,
    ) -> Optional[dict[str, Any]]:
        """Return cumulative task-scoped usage for (task_id, scope_id) or None."""
        row = self._conn.execute(
            "SELECT * FROM reservation_task_usage WHERE task_id = ? AND scope_id = ?",
            (task_id, scope_id),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def _check_deadline(self, env: ReservationEnvelope, now_str: str) -> None:
        """Refuse admission if the envelope's deadline has passed."""
        if env.max_deadline is None:
            return
        try:
            deadline = datetime.fromisoformat(env.max_deadline)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return  # malformed deadline treated as absent (envelope validated at creation)
        now = datetime.now(timezone.utc)
        if now >= deadline:
            raise AdmissionRefused(
                "deadline",
                f"deadline {env.max_deadline} has passed (now: {now_str})",
                envelope=env,
                detail={"deadline": env.max_deadline, "now": now_str},
            )

    def _transition_state(
        self,
        reservation_id: str,
        from_states: str | tuple[str, ...],
        to_state: str,
    ) -> ReservationRecord:
        """CAS-guarded state transition."""
        if isinstance(from_states, str):
            from_states = (from_states,)

        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            rec = self._get_reservation(reservation_id)
            if rec is None:
                self._conn.rollback()
                raise ReservationError(f"reservation {reservation_id} not found")

            if rec.state not in from_states:
                self._conn.rollback()
                raise ReservationError(
                    f"cannot transition reservation {reservation_id} "
                    f"from state {rec.state!r}; expected one of {from_states}"
                )

            new_version = rec.version + 1
            cur = self._conn.execute(
                """UPDATE reservations
                   SET state = ?, version = ?, updated_at = ?
                   WHERE reservation_id = ? AND version = ?""",
                (to_state, new_version, now, reservation_id, rec.version),
            )
            if cur.rowcount == 0:
                self._conn.rollback()
                raise ReservationConflict(
                    f"CAS conflict transitioning {reservation_id}"
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

        return self._get_reservation(reservation_id)

    def close(self) -> None:
        self._conn.close()
