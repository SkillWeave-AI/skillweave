# Execution Store — SQLite/CAS run persistence

## Architecture

The execution store replaces the old file-based `run.json` + `events.jsonl`
vocabulary with a single SQLite database at `.skillweave/store.db`.  The
database is the **CAS authority** — every write uses version-based
compare-and-swap so that concurrent launcher, wrapper, and kill paths
cannot silently clobber each other.

### Tables

**`runs`** — one row per run (also called a "lane" in the kernel).

| Column       | Type   | Description                                       |
|--------------|--------|---------------------------------------------------|
| `run_id`     | TEXT   | Primary key, UUID hex                             |
| `root_run_id`| TEXT   | Root run ID (same as `run_id` for top-level)      |
| `parent_run_id`| TEXT | Parent run ID, or NULL                            |
| `state`      | TEXT   | Current lane state machine state                  |
| `version`    | INT    | Monotonically increasing CAS counter              |
| `created_at` | TEXT   | ISO-8601 creation timestamp                       |
| `updated_at` | TEXT   | ISO-8601 last-update timestamp                    |
| `ended_at`   | TEXT   | ISO-8601 terminal timestamp, or NULL              |
| `role`       | TEXT   | Lane role (e.g. `"ops"`)                          |
| `metadata`   | TEXT   | JSON blob for lifecycle + extensible data         |

**`events`** — ordered event journal for each run.

| Column            | Type | Description                                     |
|-------------------|------|-------------------------------------------------|
| `sequence`        | INT  | Monotonically increasing per-run (auto-assigned)|
| `run_id`          | TEXT | FK to `runs.run_id`                             |
| `event_type`      | TEXT | Logical event type                              |
| `payload`         | TEXT | JSON payload                                    |
| `idempotency_key` | TEXT | Optional deduplication key                      |
| `created_at`      | TEXT | ISO-8601 timestamp                              |

### State machine

Legal lane transitions (defined in `RunStateModel`):

```
preflight ──→ batch_selection ──→ lane_plan ──→ implement ──→ verify ──→ review_gate ──→ integrate ──→ advance_or_stop
                     │                │             │            │           │               │
                     └────────────────┴─────────────┴────────────┴───────────┴───────────────┴── fix_retry ──→ implement
```

Terminal states: **`advance_or_stop`**, **`failed`**

No outgoing transitions from terminal states.

### Version-based CAS

Every `runs` row carries a `version` integer.  Writers must supply the
version they last observed; the UPDATE statement includes
`WHERE version = ?`.  If the persisted version has changed, the UPDATE
affects zero rows and the writer raises `VersionConflictError`.

```sql
UPDATE runs SET state = ?, version = version + 1, ...
WHERE run_id = ? AND version = ?;
-- rowcount == 0 → VersionConflictError
```

## save_run terminal guard

`save_run()` is the general-purpose upsert used by the compatibility facade
(`RunStateStore.write_metadata`).  It accepts any `RunRecord` and writes it
atomically under `BEGIN IMMEDIATE`.  The overwrite path (version >= 2) adds an
**additional guard**: before entering the transaction, it reads the existing
row and checks `RunStateModel.is_terminal(existing.state)`.  If the record is
terminal, `save_run` raises `InvalidTransitionError` with reason
`"terminal record cannot be resurrected"`.

This prevents a stale wrapper completion (or any caller holding a matching
version) from overwriting a cancelled run's terminal state.

## Outbox / unit-of-work contract

`transition_with_event()` writes to the `outbox` table, not the `events`
table.  Outbox events must be explicitly dispatched/delivered to the events
table by an outbox relay process.  Direct `journal.append()` writes to the
`events` table and is immediately readable via `get_events()`.

The kill path in `RunApplicationService` uses `store.transition()` (runs
table) + `journal.append()` (events table) rather than
`transition_with_event()` (outbox table), so the kill event is immediately
visible in the event stream.

## Lifecycle facade

The compatibility layer (`RunStateStore` in `runlifecycle/store.py`) maps
between lifecycle states (`running`, `succeeded`, `failed`, `killed`,
`unknown`) and lane states (`preflight`, `advance_or_stop`, `failed`,
`unknown`).

| Lifecycle state | Lane state       |
|-----------------|------------------|
| `running`       | `preflight`      |
| `succeeded`     | `advance_or_stop`|
| `failed`        | `failed`         |
| `killed`        | `advance_or_stop`|
| `unknown`       | `unknown`        |

Lifecycle-specific data (`command`, `pid`, `lifecycle_state`, `exit_code`,
`finished_at`) is stored under `metadata.runlifecycle` in the runs table.

## Pre-launch lifecycle metadata

Before forking a child process, `start_async()` writes a preliminary
`RunRecord` with `state=preflight` and metadata containing `runlifecycle`
(`command`, `pid`, `lifecycle_state="running"`).  This ensures the run is
visible in the store before the child starts, preventing a race where a fast
child completes and writes terminal state before the parent finishes its
startup bookkeeping.

## Cancellation atomicity

`kill_run` in `RunApplicationService` performs three steps atomically:

1. **Advance lane state** through legal intermediate states (preflight →
   batch_selection → advance_or_stop).
2. **Set lifecycle metadata** (`lifecycle_state="killed"`) **before** the
   terminal transition.  `update_metadata()` rejects terminal records, so the
   lifecycle state must be set while the run is still non-terminal.
3. **Append kill event** to the journal **after** the terminal transition.
   Once the run is terminal, a stale wrapper cannot resurrect it or append a
   second terminal event.

If any step fails (CAS conflict), the entire operation is abandoned —
partial state is not committed.

## Detached wrapper completion

The child process runs a wrapper script that:

1. Captures stdout/stderr from the real command.
2. On success/failure, reads the current run record.
3. If the run is still non-terminal, advances the lane state through legal
   transitions (preflight → batch_selection → advance_or_stop), sets
   lifecycle metadata (`lifecycle_state`, `exit_code`), and appends a
   `finished` or `error` event.
4. If the run has been killed (terminal), the wrapper's transition attempt
   fails with `InvalidTransitionError` or `VersionConflictError`, which the
   wrapper catches silently — it does **not** append a finished event after
   cancellation.

The wrapper uses its own `SQLiteRunStore` connection (separate from the
launcher), so the parent can exit without affecting the child's ability to
write completion.

## Legacy migration

`import_legacy()` reads a legacy JSON run record and stores it as a
historical row:

- State is set to `"unknown"` (never inferred from legacy strings like
  `"SUCCEEDED"` or `"IN_PROGRESS"`).
- `metadata.historical` is set to `true`.
- Exact source bytes are base64-encoded and stored in
  `metadata.raw_bytes_b64`.
- Provenance (`source_path`, `byte_length`, `sha256`) is recorded in
  `metadata.provenance`.
- Idempotent: re-importing the same `run_id` returns the existing row.
- Existing rows always win: if `run_id` already exists, the import is a
  no-op.
- `resume_run` on an imported historical record returns an error.

### Auto-discovery

`RunStateStore.import_legacy` with no arguments auto-discovers the legacy
file at `.skillweave/runs/{run_id}/run.json`.

## Resume

`resume_run()` reconnects to a stored run and returns its current state:

- Returns `None` if the run does not exist.
- Returns an error for terminal or historical runs.
- Returns the current run state for active runs.
- Does **not** launch a duplicate process or revive a terminal/historical run.

## Downstream security limits

### Process group isolation

The child process is started with `start_new_session=True`, placing it in its
own process group.  `kill_run` uses `os.killpg(pid, SIGTERM/SIGKILL)` to
signal the entire group, ensuring all child processes are killed.

### PID staleness check

`verify_pid()` compares the provided PID against the PID recorded at start
time.  If they do not match, `StalePidError` is raised, preventing the kill
path from targeting a stale/reused PID.

### No direct SQLite access

Downstream consumers must go through `RunStateStore`,
`RunApplicationService`, or the `RunHandle` facade.  Direct SQLite access
is not supported and may bypass CAS guards.

### File permissions

The `.skillweave/store.db` file inherits the project directory's
permissions.  The database is not encrypted; sensitive data should not be
stored in metadata.

### WAL mode

The SQLite store uses WAL (Write-Ahead Logging) mode for better concurrent
read/write performance.  WAL files (`.skillweave/store.db-wal`,
`.skillweave/store.db-shm`) are transient and can be safely deleted when the
store is closed.

### Event journal sequence integrity

Event sequences are allocated atomically within `BEGIN IMMEDIATE`
transactions.  Parallel writers never receive duplicate or out-of-order
sequences.  Sequences are per-run, starting at 1.
