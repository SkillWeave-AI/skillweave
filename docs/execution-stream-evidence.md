# Execution Stream Evidence — durable streaming, immutable facts, replay

SW-1510-STREAM makes a run's execution *observable as it happens* and
*replayable afterwards*, without introducing a second authority next to the
accepted CAS run store, journal, and run service. The stream is evidence: every
chunk, every attempt fact, and every run-state transition carries a monotonic
cursor, an exact content digest, and a typed completeness marker.

Composition (Methodology / Assay and autonomy policy / Unicorn) enters this
layer **as data only**. This module composes snapshot fields by name, derives
no gate decision of its own, and owns no controller. The gate state it records
is produced by the completion contract elsewhere and passed in.

## Architecture

```
core/proc/runner.py            production spawn/collection
        │  run_process_streaming(cmd, run_id=..., task_id=..., store=..., wrap_command=...)
        ▼
runtime/stream.py              DurableStreamStore  (disk-backed durable evidence)
        │  begin_attempt / ingest / seal_attempt / finish_attempt
        │  frames / resolve / summary / fact / preview
        ▼
runtime/snapshot.py            progress_frames / preview_frames / build_run_snapshot
        ▼
core/observer/observer.py      ReadOnlyObserver.read_progress / read_preview
        ▼
runsvc/service.py              execute_streaming_native  (opt-in authoritative integration)
                               stream_read               (narrow collector interface)
```

The layers are separable:

* **`runtime/stream.py`** owns the durable store. It is the single source of
  truth for cursors, digests, retention, and attempt facts.
* **`core/proc/runner.py`** owns the real child process. It spawns the child,
  interleaves its streams under the store's one monotonic cursor, measures the
  duration with `time.monotonic`, and records the observed termination verbatim.
* **`core/observer/observer.py`** is a read-only consumer. It resumes from a
  cursor and returns progress; it never mutates the run or the stream.
* **`runsvc/service.py`** binds the collected evidence and the derived gate
  state into the authoritative store through the single accepted integration
  path (see `docs/execution-store.md`).

The runtime subpackage is optional (GLE-020): `core/proc/runner.py` imports
`skillweave.runtime.stream` lazily, so the foundational module imports without
the runtime present.

## Disk-backed persistence

A durable store is constructed with an explicit private root:

```python
DurableStreamStore(retain_limit=1_048_576, root="/var/lib/skillweave/streams/<run_id>")
```

```python
DurableStreamStore(root=...)      # durable=True; is_durable True, persistence_mode "disk"
DurableStreamStore()              # memory only; is_durable False, persistence_mode "memory"
DurableStreamStore(durable=True)  # WITHOUT root -> StreamPersistenceError (no silent fallback)
```

Layout under `root/`:

| Path | Contents |
|------|----------|
| `meta.json`             | store schema/version and caps |
| `cursor.json`           | the committed monotonic cursor, fsync'd |
| `frames.jsonl`          | one JSON record per observed frame, appended + fsync'd |
| `facts.jsonl`           | one JSON record per immutable attempt fact, appended + fsync'd |
| `streams/<hash>.json`   | a spool's state: digest, observed/retained bytes, incompleteness, caps |
| `streams/<hash>.raw`    | the retained raw bytes for that spool |

`<hash>` is a short sha256 over the compound `(run_id, task_id, attempt)`
identity — plus the stream name for a spool file. Two runs (or two tasks, or two
attempts) that reuse the same attempt *number* therefore get **separate files**.

Every `ingest`, `seal_attempt` and `finish_attempt` appends its frame/fact and
commits the cursor **as the data arrives**, using `tempfile` + `os.replace` +
`fsync` for the spool and cursor files. A reader in a **different process** that
constructs a store on the same `root` reopens the committed cursor, frames,
facts and retained bytes exactly — this holds after a clean exit *and* after a
collector interruption (the restored unsealed spool is re-typed `interrupted`).

## Identity scoping

Storage and replay are keyed by the compound identity `(run_id, task_id,
attempt)`, never by `(attempt)` alone. Supplying the identity on every write and
read is the default; omitting `run_id`/`task_id` resolves against the sole
recorded identity only when that is unambiguous, and otherwise raises
`StreamIdentityError`. A second run that happens to use `attempt=1` can never
read or overwrite another run's spool.

## Cursor model

There is exactly **one** monotonic cursor per store, shared by both streams and
by attempt facts. Ingesting a stdout chunk, a stderr chunk, or recording a
nested event advances the same counter. A consumer that stopped at cursor `N`
resumes with `after_cursor=N` and receives exactly the frames with
`cursor > N` — ordered, gap-free, no duplication across processes.

| Frame `kind` | Meaning |
|--------------|---------|
| `chunk`      | a retained slice of one stream's observed bytes |
| `fact`       | an attempt fact (exit code, signal, duration, subtype) |
| `sealed`     | the stream for that attempt was sealed; incompleteness is final |

Cursor arithmetic is defined directly on the accepted `runcore` journal: the
`event_cursor` recorded in a run snapshot is the store's committed cursor at
the moment the snapshot is built.

## Criterion 1 — durable byte-capped streaming with exact digests

`run_process_streaming` spawns the real child with both pipes open and selects
over them, ingesting each ready chunk into the store in the order `select`
reports it. Per-stream retention caps bound how many bytes are *kept*, but the
digest is computed over **every observed byte**:

* `StreamSummary.digest` — sha256 over the full observed byte sequence.
* `StreamSummary.retained_bytes` / `retained_digest` — what is actually kept.
* `StreamSummary.incompleteness` — one of `complete`, `limit_reached`,
  `interrupted`, `metadata_limit_reached`, `incomplete`.

When a stream reaches its retain cap the child is **still drained** (never
deadlocked) and the pressed point is recorded as a `backpressure_events` count
plus a `limit_reached` incompleteness. A bounded retained prefix therefore can
never be mistaken for the whole stream.

**Metadata is bounded too.** A spool also caps the number of frames it stores
(`DEFAULT_MAX_FRAMES`). Once the frame cap is reached the retained bytes keep
growing (up to the byte cap) but no further per-chunk frame records are added;
the spool is typed `metadata_limit_reached` / `metadata_limited=True` so a
consumer can see the frame list was truncated without confusing it for a
complete per-chunk trace.

Nested-worker events (`{"event": ..., "attempt": ...}` JSON lines) are parsed
into discrete nested events by `DurableStreamStore.nested_events` with its own
`max_bytes`/`max_events` bounds; non-JSON lines stay as raw evidence. The parser
works over the retained bytes, so it never stores an unbounded duplicate blob.

## Criterion 2 — immutable observed facts

`AttemptFact` (frozen) carries the raw result:

| Field | Source |
|-------|--------|
| `exit_code` / `signal` | the child's real return code, split by `_split_returncode` |
| `duration_ns` | `time.monotonic` delta, **never clipped** |
| `result_subtype` | verbatim termination: `exited`, `signaled`, `cancelled`, `timed_out`, `launch_failed`, `storage_failed` |
| `termination` | `exited` / `signaled` / `timed_out` / `launch_failed` / `storage_failed` |

`DurableStreamStore.record_fact` refuses to overwrite an attempt's fact with a
*conflicting* one (`StreamFactConflict`): a second write that would change the
exit code, signal, duration, or subtype is rejected. Coercing an error to
success, or clipping a measured duration, is therefore not representable.

A launch failure is recorded as its own subtype (`launch_failed`) with
`interrupted=True`; it does not raise out of the collector with a fabricated
exit code. `StreamingResult.succeeded` is true only for a clean `exited` code 0
with no signal. A non-zero exit is never reconciled to success by inspecting a
stdout JSON payload.

## Failure semantics

Persistence is **fail-visible**, never silently degraded:

* **I/O failure while ingesting/sealing** — `StreamPersistenceError` is raised
  by the store. `run_process_streaming` catches it, kills the child group, and
  records a truthful `storage_failed` result subtype; the store exposes
  `degraded=True` and a non-empty `last_error` so the caller sees the loss. No
  durable ack is fabricated.
* **No disk configured for a protected run** — `execute_streaming_native`
  refuses a memory-only store (`is_durable` False) at stage `"stream"` rather
  than running a supported protected path without durable evidence.
* **Partial seal** — if writing the sealed state fails, the spool stays
  recoverable and the honest `interrupted`/`incomplete` typing is preserved.

## Criterion 3 — reconnect/replay without replacing raw evidence

`runtime/snapshot.py` exposes two read shapes:

* `progress_frames(store, run_id, after_cursor=..., task_id=..., limit=...)` —
  the **full** ordered, gap-free trace. Each `ProgressFrame` carries its cursor,
  kind, digest, `byte_length` (observed), `retained` (kept) and `task_id`.
* `preview_frames(store, run_id, after_cursor=..., max_bytes=..., task_id=...)` —
  a bounded *decoration*: at most `max_bytes` of retained text, always marked
  `is_full_evidence=False`. The raw frames stay resolvable through
  `progress_frames`; a preview limit never replaces them.

`ReadOnlyObserver.read_progress` resumes from the observer's persisted offset
and returns `is_full_evidence=True`; `read_preview` returns
`is_full_evidence=False`. Both accept `task_id`. The observer cannot mutate the
run: `update_offset` and `mutate_state` raise `ReadOnlyError` via the
`__setattr__` guard.

## Run snapshots (`run-snapshot-v1`)

`build_run_snapshot` produces a record conforming to the frozen SDK schema
`https://skillweave.dev/schemas/execution/run-snapshot/v1`. Three mode fields
are **distinct and never conflated**:

* `methodology` — one of `assay`, `rex`, `ralph`, `gauntlet`.
* `autonomy_policy` — one of `conservative`, `medium`, `unicorn`
  (`ASSAY_TO_AUTONOMY` maps Assay policy names to this enum by name only).
* `session_schedule` — a non-empty string.

`process_identity` is a **native-handle object** (`handle_id` +
`service_principal`) issued by the trusted run service, not a model-authored
string. Every digest, the handle, and the three mode fields are *validated
rather than defaulted*: a missing or malformed trusted field raises
`SnapshotError`, never a fabricated value. When a store is supplied and the
caller left `event_cursor` at 0, the store's committed cursor pins the
snapshot.

## Run-service integration

`RunApplicationService.__init__` takes an optional `stream_store` (lazily
resolved). Two streaming surfaces exist:

* **`execute_streaming_native(...)`** — the declared **opt-in native execution
  path**. It mirrors the accepted `execute(...)`: it authenticates the worker
  (`worker_token` + `require_identity`), requires a durable store, opens the
  protected worker isolation scope, and applies the owning lane's
  `wrap_command` (e.g. `StoreProtection`/`SandboxExecAdapter`) verbatim to the
  child. It binds the collected evidence and a separate `Verifier` /
  `CompletionContract` through the same six record kinds as `execute`, and
  returns `durable=True`. It does not fabricate enforcement of its own.
* **`stream_read(run_id, *, attempt, task_id, after_cursor)`** — the narrow
  collector interface the owning composed lane can wire: it returns the committed
  cursor, per-stream summaries, the immutable fact, the ordered frames, the
  retained bytes, and `is_full_evidence=True`.

`stream_frames`, `stream_preview`, and `stream_attempt_events` accept
`task_id` and expose replay to callers without a second controller.

## Legacy compatibility

`run_process(cmd, env_allowlist=..., extra_env=..., secrets=...,
output_limit_bytes=...)` is preserved **unchanged** for its existing callers:
it still runs a bounded child, redacts secrets and returns decoded
`(returncode, stdout, stderr)`. It is not durable and does not use the stream
store. The streaming path is additive, so no legacy caller is broken by
SW-1510-STREAM.

The previous `execute_streaming` sibling that bypassed the authenticated
identity interface has been **removed**; its functionality is superseded by
`execute_streaming_native`, which routes through the accepted admission seam.

## What this layer does **not** do

* No script-owned controller and no second authority: the CAS store, journal,
  and run service remain the only writers.
* No gate decision: the gate state is passed in, derived elsewhere.
* No scope/envelope enforcement and no native Assay policy of its own: the
  owning lane composes isolation and policy; they enter here as data/wrappers.
* No paid-provider call: the fixtures are local disposable subprocesses.
