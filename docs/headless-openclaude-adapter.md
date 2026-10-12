# The scoped headless OpenClaude adapter (SW-1510-OPENCLAUDE)

OpenClaude stays an **explicitly selected** user adapter on top of the
host-neutral core contract (SW-158). This document describes the headless
transport it declares, the environment it launches a child in, and the
fail-closed controls around prompts, tools, plugins and stderr.

The core (`HarnessAdapter`, `HarnessPlan`, `admit_native_plan`) names no
concrete harness. Everything OpenClaude-specific lives in the plugin class
`OpenClaudeAdapter` in `src/skillweave/routing/harness_adapter.py`; the runtime
half that actually launches a process lives in
`src/skillweave/runtime/runner_adapter.py`.

## 1. Clean allowlisted environment; ambient keys cannot redirect traffic

A model request must reach the **approved gateway**, never an ambient alternate
provider that happens to be set in the parent shell. Two cooperating helpers
enforce this on both sides of the seam:

- `routing.harness_adapter.clean_launch_environment(*, base_env, pinned_env, allowlist)`
  — inherits **only** the allowlisted keys (`PATH`, `HOME`, `LANG`, `LC_ALL`,
  `TMPDIR`, `SYSTEMROOT`, `PYTHONPATH`) from the base environment, then adds the
  caller's **pinned** gateway settings (for example
  `APPROVED_GATEWAY_BASE_URL`). Every ambient provider credential is dropped.
- `runtime.runner_adapter.build_child_environment(*, env_allowlist, extra_env, base_env)`
  — the runtime-side mirror used by `start_process`/`run_command` and friends.

`OpenClaudeAdapter.launch_environment()` returns the allowlisted environment,
and the plugin's `gateway_env` pin is validated in the constructor.

The eight ambient provider keys are named once, in
`AMBIENT_PROVIDER_ENV_KEYS` (mirrored in both modules and in
`routing.faigate_adapter.detect_providers`):

`FAIGATE_API_KEY`, `OPENROUTER_API_KEY`, `CLAWROUTER_API_KEY`, `KILO_API_KEY`,
`OMNIROUTE_API_KEY`, `NINEROUTER_API_KEY`, `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`.

Fail closed: an ambient provider key placed in the allowlist, in `extra_env`, or
in a pinned `gateway_env` is **refused** rather than silently honoured — pinning
must not become a back door for the very key the allowlist drops.

`env_allowlist=None` preserves the historical inherit behaviour, so existing
callers and tests are unaffected; only callers that opt into the allowlist lose
the ambient environment.

## 2. Headless execution and typed lifecycle events

`OpenClaudeAdapter.build_command` emits an explicit, non-interactive argv:

```
<executable> --print --input-format stream-json --output-format stream-json \
             --permission-mode <mode> [--model <mixed-model>]
```

`--print` drives one headless pass with **no REPL fallback**. The model comes
from the router's decision (`plan.strategy.model_mix[0]`), never from the plugin.

The child's stream-json lifecycle is read as typed events:

- `classify_headless_event(payload)` — an **initial permission denial**
  (`permission_denied`), an **input request** (`input_required`), a **failed
  result** (`result_error`) or a **worker exit**. A `result` event that reports
  a failure — a failure `subtype` (`error_*`), an `is_error`/`error` flag, or a
  non-zero embedded exit `code` — is typed `result_error`, **not** a clean
  worker exit. These are read from structured fields only, never from prose: a
  message that merely *mentions* an error is still an ordinary event. Anything
  else is a benign `event` (surfaced, never dropped). A payload that is not a
  JSON object is refused (fail closed).
- `parse_headless_events(lines)` — one event per line; a non-JSON line is a
  visible `raw` event.
- `headless_terminal_status(events)` — a **failed result** (`result_error`) is
  terminal first and returns a typed `failed` lifecycle `JobResult` (the shared
  contract's fail-closed default), so an error result is never read as a
  successful exit **regardless of the process exit code** (OpenClaude can emit
  `is_error` and still exit 0). Failing that a worker exit is terminal and wins;
  otherwise an `input_required` request is terminal and returns the shared
  lifecycle `blocked_input` result
  (`skillweave.trace.contracts.blocked_input_result`), so a headless adapter
  **never waits** on a child that wants input; otherwise a permission denial is
  its own typed terminal.

### The typed vocabulary is composed into `execute`, not just exported

The helpers above are not a library beside the adapter: `OpenClaudeAdapter.execute`
uses them on its real path.

- A missing or non-callable `launch` seam is refused **first**, as a named
  `HarnessAdapterError(asset="launch")` — the adapter never leaks a bare
  `TypeError` (repair finding A). The refusal precedes `build_command`, so no
  argv is built and no process is spawned.
- When `stream_output` (the child's stdout lines) is supplied,
  `execute` ingests them with `ingest_headless_stream(lines, run_id=...)`, which
  parses, numbers each event with a `{run_id}:{index}` cursor and computes the
  typed terminal status. An `input_required` request maps to the shared
  `blocked_input` lifecycle result, and the returned `HeadlessResult.succeeded`
  **falls closed** — the adapter never reports a child waiting for input as a
  success, and never waits (repair finding B). A **failed result**
  (`result_error`) likewise maps to a typed `failed` lifecycle result and falls
  closed, so a child that reports an error while exiting 0 is never a success.
- `HeadlessResult` carries the typed stream, `exit_code`, `stderr_bytes`,
  `blocked`, `failed` and `backpressure_exceeded`;
  `to_dict()`/`HeadlessStreamIngest.to_attempt_events()` render the events for
  an `attempt-event-v1` consumer.
- With no `stream_output` the launcher's own result is preserved untouched, so a
  caller that supplies its own result object keeps the legacy contract.

### The observer-write and backpressure guards are on the execute path

- A `scratch_target` passed to `execute` goes through the write funnel
  `write_worker_scratch(target, data, observer_roots=...)`, which calls
  `assert_not_observer_owned` **before** any byte is written (repair finding C).
- A launcher result reporting `stderr_exceeded_cap` makes the outcome fall
  closed (`succeeded is False`) as well as carrying `backpressure_exceeded`.

### `dontAsk` is scoped, not blanket authorization

`PERMISSION_MODE_DEFAULT` (`"default"`) is the fail-closed default. `dontAsk`
must be opted in and is **refused** by `validate_permission_mode(native_admitted=...)`
unless the plan actually required and passed the measured native-tool
handshake. `dontAsk` on a raw-command plan is refused before any launch.

## 3. Controlled failure fixtures

All of these run **before** the injected `launch` seam is reached, so a refusal
performs zero work:

- `check_surface(...)` — a missing **plugin** (named before a missing tool) or
  missing **tool** is refused, naming the asset (`plugin:<name>` / `tool:<name>`).
- `check_prompt_size(work, *, max_bytes=DEFAULT_MAX_PROMPT_BYTES)` — an
  oversized prompt is refused rather than truncated (a truncated prompt is a
  *different request*).
- `assert_not_observer_owned(target, *, observer_roots=())` — the adapter may
  not write observer-owned state: `frames.jsonl`, `facts.jsonl`, `cursor.json`,
  `observer.json`, `meta.json`, or anything under an explicit observer root. The
  guard is **structural, not a spelling check**. The target is realpath-resolved
  first, so a symlink cannot smuggle a write in, and an unresolvable destination
  is refused fail-closed. The basename is compared **case-folded**, so on a
  case-insensitive volume (macOS APFS) `META.JSON` cannot clobber `meta.json`.
  The target and each ancestor are compared **by filesystem identity**
  (`st_dev`/`st_ino`) against the observer-owned entries, so a hard link to an
  observer-owned file under a neutral name (same inode) is refused. The name and
  identity checks apply even when no observer root is configured.
- `OpenClaudeAdapter(observer_roots=...)` — the durable stream/evidence roots the
  adapter instance is bound to. `execute` always unions these with any per-call
  `observer_roots`, so on the production path the guard is tied to a **live**
  store root rather than relying on a caller to pass one.
- **stderr backpressure** — `run_command(..., stderr_retain_limit=N)` caps the
  retained stderr; bytes beyond the cap are counted
  (`stderr_backpressure_events`, `stderr_observed_bytes`, `stderr_exceeded_cap`)
  rather than buffered whole. The stream is *drained*, never deadlocked, and a
  capped run reads as a controlled failure (`succeeded is False`).

## 4. Data contracts consumed

- `attempt-event-v1` — produced by the OpenClaude adapter, consumed by the run
  service (durable stream, SW-1510-STREAM).
- `capability-snapshot-v1` — produced by OpenClaude, consumed by preflight
  (native admission, `docs/harness-capability-admission.md`).

## Verification

- `tests/unit/test_1510_openclaude.py` — drives the real consumer wiring:
  - a **real child process** is launched through `runtime.runner_adapter` with
    the environment the routing adapter produced, and the child's own
    `os.environ` is read back to prove no ambient provider key reached it;
  - the typed permission-denial / `input_required` / worker-exit events,
    including `input_required` → `blocked_input`, driven by a **real emitting
    child through `execute`** (not by calling the helpers directly);
  - a **failed result** (`is_error` true, `error_max_turns`,
    `error_during_execution`, a non-zero embedded exit code, an `error` field)
    is a typed `result_error` terminal that **falls closed through `execute`**
    even though the real child exits 0, while a benign result still succeeds and
    prose that merely mentions an error stays untyped;
  - a missing / `None` / non-callable `launch` seam is a named refusal;
  - the observer-write funnel and the stderr backpressure cap driven through
    `execute` (a blocked-input or backpressure child falls closed);
  - a **case variant** of an observer-owned name, a **hard link** to one (refused
    by inode identity against the adapter's *configured* root, with no per-call
    root), an unresolvable destination, and an ordinary path under a configured
    root elsewhere — all driven through `execute`;
  - the adapter is selected **by name through the production
    `HarnessAdapterRegistry`** and executes through that registry path;
  - missing plugin/tool and oversized-prompt fixtures with an injected launch
    counter that stays at `0`;
  - stderr backpressure on a real capped run;
  - `assert_not_observer_owned` for every observer-owned file.
- `tests/unit/test_runner_adapter.py` — the runtime half (process, cancel,
  timeout, signal/exit distinction, batch stop, lane binding, cold-start).
- `tests/unit/test_1510_cap.py` — the native capability admission gate.
- `tests/integration/test_harness_adapter.py` — the adapter suite, including the
  headless argv and the eager-closure (layering) checks.
