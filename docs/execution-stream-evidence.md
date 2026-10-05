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
        │  run_process_streaming(cmd, run_id=..., store=...)
        ▼
runtime/stream.py              DurableStreamStore  (durable evidence)
        │  begin_attempt / ingest / seal_attempt / finish_attempt
        │  frames / resolve / summary / fact / preview
        ▼
runtime/snapshot.py            progress_frames / preview_frames / build_run_snapshot
        ▼
core/observer/observer.py      ReadOnlyObserver.read_progress / read_preview
        ▼
runsvc/service.py              execute_streaming  (authoritative integration)
```

The three layers are separable:

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

## Cursor model

There is exactly **one** monotonic cursor per store, shared by both streams and
by attempt facts. Ingesting a stdout chunk, a stderr chunk, or recording a
nested event advances the same counter. A consumer that stopped at cursor `N`
resumes with `after_cursor=N` and receives exactly the frames with
`cursor > N` — ordered, gap-free, no duplication.

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
  `interrupted`.

When a stream reaches its retain cap the child is **still drained** (never
deadlocked) and the pressed point is recorded as a `backpressure_events` count
plus a `limit_reached` incompleteness. A bounded retained prefix therefore can
never be mistaken for the whole stream.

Nested-worker events (`{"event": ..., "attempt": ...}` JSON lines) are parsed
into discrete nested events by `DurableStreamStore.nested_events`; non-JSON
lines stay as raw evidence. They are never dropped.

## Criterion 2 — immutable observed facts

`AttemptFact` (frozen) carries the raw result:

| Field | Source |
|-------|--------|
| `exit_code` / `signal` | the child's real return code, split by `_split_returncode` |
| `duration_ns` | `time.monotonic` delta, **never clipped** |
| `result_subtype` | verbatim termination: `exited`, `signaled`, `cancelled`, `timed_out`, `launch_failed` |
| `termination` | `exited` / `signaled` / `timed_out` / `launch_failed` |

`DurableStreamStore.record_fact` refuses to overwrite an attempt's fact with a
*conflicting* one (`StreamFactConflict`): a second write that would change the
exit code, signal, duration, or subtype is rejected. Coercing an error to
success, or clipping a measured duration, is therefore not representable.

A launch failure is recorded as its own subtype (`launch_failed`) with
`interrupted=True`; it does not raise out of the collector with a fabricated
exit code. `StreamingResult.succeeded` is true only for a clean `exited` code 0
with no signal.

## Criterion 3 — reconnect/replay without replacing raw evidence

`runtime/snapshot.py` exposes two read shapes:

* `progress_frames(store, run_id, after_cursor=..., limit=...)` — the **full**
  ordered, gap-free trace. Each `ProgressFrame` carries its cursor, kind,
  digest, `byte_length` (observed) and `retained` (kept).
* `preview_frames(store, run_id, after_cursor=..., max_bytes=...)` — a bounded
  *decoration*: at most `max_bytes` of retained text, always marked
  `is_full_evidence=False`. The raw frames stay resolvable through
  `progress_frames`; a preview limit never replaces them.

`ReadOnlyObserver.read_progress` resumes from the observer's persisted offset
and returns `is_full_evidence=True`; `read_preview` returns
`is_full_evidence=False`. The observer cannot mutate the run: `update_offset`
and `mutate_state` raise `ReadOnlyError` via the `__setattr__` guard.

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
resolved). `execute_streaming(...)` reuses the accepted lane walk, calls
`run_process_streaming`, stores redacted bytes in the raw-artifact store,
journals the facts and digests, saves a receipt (`runsvc-stream-{run_id}`),
runs a separate `Verifier` and `CompletionContract`, and drives terminal
transitions — the same six record kinds as `execute`, through the same CAS
authority.

`stream_frames`, `stream_preview`, and `stream_attempt_events` expose replay to
callers without a second controller.

## What this layer does **not** do

* No script-owned controller and no second authority: the CAS store, journal,
  and run service remain the only writers.
* No gate decision: the gate state is passed in, derived elsewhere.
* No scope/envelope enforcement and no native Assay policy: those are composed
  independently and are consumed here only as data.
* No paid-provider call: the fixtures are local disposable subprocesses.
