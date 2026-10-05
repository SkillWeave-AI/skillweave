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
| `correlation_id`  | TEXT | Optional correlation id                         |
| `causation_id`    | TEXT | Optional causation id                           |
| `idempotency_key` | TEXT | Optional deduplication key                      |
| `timestamp`       | TEXT | ISO-8601 timestamp                              |
| `version`         | INT  | Run version at which the event was written      |

The unique index `idx_events_idempotency` on
`(run_id, idempotency_key)` (where the key is not NULL) makes a logical
event with a given key exactly-once per run.

**`outbox`** — transactional outbox for atomic transitions.

| Column         | Type | Description                                     |
|----------------|------|-------------------------------------------------|
| `id`           | INT  | Auto-increment delivery id                      |
| `run_id`       | TEXT | FK to `runs.run_id`                             |
| `event_type`   | TEXT | Logical event type                              |
| `payload`      | TEXT | JSON payload                                    |
| `idempotency_key`| TEXT | Optional deduplication key                    |
| `created_at`   | TEXT | ISO-8601 timestamp                              |
| `delivered_at` | TEXT | Delivery timestamp, or NULL while undelivered   |

`runs`, `events` and `outbox` live in the same `.skillweave/store.db`
database.  `SQLiteRunStore` and `EventJournal` open their own connections to
that database; because the `events` schema is created idempotently
(`CREATE TABLE IF NOT EXISTS`), the two classes share the one physical
`events` table rather than maintaining separate journals.

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

`transition_with_event()` commits the run-row state change and the outbox
event in **one** `BEGIN IMMEDIATE` transaction, so the terminal state and its
intent to emit one logical event can never diverge.  By default the logical
event is written **only** to the `outbox` table; it becomes visible in the
readable `events` journal once a relay calls `mark_outbox_delivered()`.

```python
# default: outbox only
store.transition_with_event(run_id, target_state, ..., event_type="finished")

# journal_event=True: also co-insert the logical event into `events`
# inside the SAME transaction (sequence = MAX(sequence)+1 under the
# same write lock).  Used by reconcile_terminal so a reader cannot
# observe a terminal state with no logical record.
```

The wrapper completion path and `reconcile_terminal()` pass
`journal_event=True`, so a finished/error completion is atomic with a single
logical journal event.  Direct `journal.append()` also writes to the `events`
table and is immediately readable via `get_events()`.

**Downstream limit:** there is currently **no outbox relay/drain** in the
source tree — nothing moves undelivered `outbox` rows into `events` and calls
`mark_outbox_delivered()`.  Events written only to the outbox (i.e. a bare
`transition_with_event()` call without `journal_event=True`) are durable and
readable via `list_outbox()`, but they are **not** visible through the
`events` journal (`read_events()` / `get_events()`) until a relay exists.
Downstream consumers that read only the journal will therefore not see
outbox-only events.

The kill path in `RunApplicationService` drives the lane to terminal through
the canonical durable reconciliation primitive, `reconcile_terminal()`, which
passes `journal_event=True`.  The terminal lane transition,
`lifecycle_state="killed"` and the `process_killed` event are therefore
co-committed — into **both** the outbox and the readable `events` journal — in
one `BEGIN IMMEDIATE` transaction.  The same is true of the façade kill path
(`RunStateStore.update_metadata(state="killed")`).  A re-kill of an
already-terminal run only appends an idempotent journal event via
`journal.append()`; it does not fabricate a second terminal transition.

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

`kill_run` in `RunApplicationService` drives the run to terminal through the
canonical durable reconciliation primitive, `reconcile_terminal()`.  That
primitive walks the legal forward-only lane transitions (resolved from
`RunStateModel`, not a hand-maintained table) and then commits the **terminal
transition, `lifecycle_state="killed"` and the `process_killed` event in a
single transaction** — so there is no crash window in which lifecycle says
killed while the lane is still non-terminal or the outbox is empty.  Once the
run is terminal, a stale wrapper cannot resurrect it or append a second
terminal event.

### Durable cancellation under contention

A single CAS attempt is not enough.  Any competing metadata writer bumps the
run version, so a **fixed** retry budget can be exhausted and a cancellation
silently lost — the run would remain `running` with no cancellation event.
`reconcile_terminal()` therefore retries **without a finite bound** on each
`VersionConflictError`: it re-reads the current non-terminal snapshot and
tries again.  A finite burst of legal competing writes (for example 20 version
bumps from an independent SQLite connection) can therefore never discard the
cancellation.  The loop terminates as soon as a terminal state is observed, so
exactly one cancellation event is written however many writers race.

A genuine pre-existing terminal winner (a committed completion, or an earlier
cancellation) is returned unchanged and **never** overwritten: `kill_run`
reports the winner's real state, and does not fabricate `killed`.

### Typed failure, never a swallowed success

Only `VersionConflictError` is retried.  Any other storage failure (for
example a SQLite I/O error during the terminal commit) is **not** swallowed:
`kill_run` raises `KillOutcomeError` (a `StoreError`) with the original
exception chained as `__cause__`, so a caller can tell a durable cancellation
from a storage failure.  A kill is never reported as an apparent success
(``already_dead``/``running``) when its terminal unit of work did not commit.

### Cancellation intent recovery

`CancellationReconciler` (returned by
`SQLiteRunStore.reconcile_cancellation_intent()`) records the cancellation
intent (`_pending_cancellation` metadata) **before** the terminal commit.  A
crash between the intent and the commit leaves the intent readable, so
`CancellationReconciler.resume_pending()` finishes the recorded cancellation
on a later pass instead of leaving a non-terminal `running` run behind.

#### Durable intent before the irreversible signal boundary

The signal is irreversible: once `os.killpg(pid, SIGTERM/SIGKILL)` fires, the
process may be gone and there is no way to un-send it.  Both kill paths
therefore record the durable cancellation intent **before** the signal block,
not only before the terminal commit:

* `RunApplicationService.kill_run` calls
  `reconcile_cancellation_intent(run_id, ...).record_intent()` immediately
  after the terminal-winner early-return and before the first `os.killpg`.
  The real signal outcome is still committed afterwards through
  `reconcile_terminal()`; the intent payload (`result="pending"`) is the
  recoverable fallback.  If the intent cannot be durably recorded, `kill_run`
  raises `KillOutcomeError` rather than proceeding to the irreversible signal
  as if it were safe.
* The compatibility façade `runlifecycle.handle.kill_run` records the same
  intent through `RunStateStore._kernel.reconcile_cancellation_intent()` before
  its signal block.

A crash *at* the signal call (supervisor lost between the signal and the
terminal commit) therefore still leaves the intent readable, and the
production resume path finishes the cancellation: `RunApplicationService.resume_run`
calls `CancellationReconciler.resume_pending()` for a non-terminal,
non-historical run before returning its state.  A recorded-but-uncommitted
cancellation therefore reaches terminal `killed` with exactly one logical
event across a real restart, instead of leaving the run non-terminal
`running`.

`RunApplicationService.kill_run` additionally commits through
`reconcile_terminal()`, which retries CAS contention without a finite bound, so
a finite burst of competing writers cannot drop the cancellation even when the
service survives to the commit.  `RunStateStore.update_metadata(state="killed")`
uses the same canonical reconciliation, so a façade-level cancellation cannot
swallow a CAS conflict either.

## Detached wrapper completion

The child process runs a wrapper script that:

1. Captures stdout/stderr from the real command.
2. On success/failure, records a durable completion **intent** and then
   reconciles it (`CompletionReconciler.reconcile()`).
3. The reconciler re-reads the run and, while it is non-terminal, walks the
   legal lane transitions and commits the terminal state, the lifecycle
   metadata (`lifecycle_state`, `exit_code`) and **exactly one** logical
   `finished`/`error` journal event together in one `BEGIN IMMEDIATE`
   transaction.
4. If the run has already been killed (terminal), reconciliation returns the
   existing winner unchanged — it does **not** attach lifecycle metadata or
   append a terminal event after cancellation.

### Durable reconciliation under contention

A single CAS attempt is not enough for the out-of-band wrapper: any competing
metadata writer bumps the run `version`, so a **fixed** retry budget can be
exhausted and a completed child's outcome silently lost.
`reconcile_terminal()` therefore retries **without a finite bound**: on each
`VersionConflictError` it re-reads the current non-terminal snapshot and tries
again.  This is what lets a finite burst of legal competing writes (e.g. 20
version bumps from an independent SQLite connection) never discard a
completion.  The loop terminates as soon as a terminal state is observed, so
exactly one terminal event is written however many writers race.

The intent (`_pending_completion` metadata) is written *before* the terminal
commit.  A crash between the intent and the commit leaves the intent
readable.  The production restart path reconciles it:
`RunApplicationService.resume_run` calls
`CompletionReconciler.resume_pending()` after the terminal/historical checks,
so a completed child whose wrapper was lost before the commit is finished —
terminal `advance_or_stop` with the recorded lifecycle and exactly one logical
`finished`/`error` event — instead of being left as a stuck non-terminal run.
A genuine terminal winner (including a cancellation committed first) is
preserved unchanged, so the resume reconcile is idempotent and never writes a
second terminal event.

Only `VersionConflictError` is retried.  Any other storage failure (for
example a SQLite I/O error) propagates to the caller, so an I/O failure is
never swallowed and never reported as an apparent successful completion.

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
- Reconciles a recorded-but-uncommitted `_pending_cancellation` intent (a
  crash at the signal boundary) through `CancellationReconciler.resume_pending()`
  before returning, so a lost cancellation becomes a terminal `killed` run.
- Reconciles a recorded-but-uncommitted `_pending_completion` intent (a crash
  between the wrapper's intent write and its terminal commit) through
  `CompletionReconciler.resume_pending()`, so a completed child is not left as
  a stuck non-terminal run.  Ordering (cancellation first, then completion)
  and terminal-winner preservation mean exactly one terminal event is ever
  written.
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
sequences.  Sequences are per-run, starting at 1.  A co-committed
(`journal_event=True`) logical event allocates its sequence with
`MAX(sequence)+1` for the run under the same write lock as the state change,
so it cannot race a concurrent `journal.append()`.

### Journal vs outbox visibility

There is no drain/relay from `outbox` into `events`.  `read_events()` /
`get_events()` see only the `events` table; events committed solely to the
outbox (the default, e.g. `process_killed`) are durable but visible only via
`list_outbox()`.  Completion events (wrapper terminal `finished`/`error`) are
co-committed with `journal_event=True` and are therefore immediately visible
in the journal.  Downstream consumers that need every logical event must read
both the journal and the undelivered outbox, or a relay must be added.
