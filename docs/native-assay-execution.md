# Native Assay execution

This document describes how the **Assay** methodology runs on the native
SkillWeave run path (`SW-1510-ASSAY`). Assay is a *declared methodology
binding*: a caller names it and the run either executes as Assay or is refused
with a named reason. It is never silently reinterpreted as another methodology
(in particular, never downgraded to Ralph).

## Where it runs

All four surfaces are thin wrappers over one implementation,
`RunApplicationService` (`src/skillweave/runsvc/service.py`). There is no
script-owned parallel controller.

| Surface | Entry point |
| --- | --- |
| CLI | `skillweave run --methodology assay ... -- <command>` (`src/skillweave/cli/run.py`) |
| MCP | `create_run(..., methodology="assay")` / `status_run(run_id)` (`src/skillweave/mcp/server.py`) |
| Service | `RunApplicationService.execute(..., methodology="assay")` |
| Replay / read | `RunApplicationService.run_snapshot(run_id)` |

The CLI subcommand is registered by `skillweave.cli.main` and installed as the
`skillweave` console script (`pyproject.toml`, `[project.scripts]`).

```console
$ skillweave run \
    --tool opencode --model <model> \
    --subject-repo <repo> --subject-commit <sha> \
    --methodology assay --autonomy-policy unicorn \
    --task-id <task> --attempt 1 \
    --db-path .skillweave/store.db --artifacts-path .skillweave/artifacts \
    -- python3 -c "print('probe output')"
```

The command after `--` is the **probe**: a real, bounded subprocess launched by
`runtime/runner_adapter.run_command`, distinct pid and all.

## What is persisted

A run leaves the six standard record kinds (Run, Journal, Raw Artifact,
Receipt, Verification, Gate). An Assay run additionally journals its ordered
drill phases as `metric` events. Each phase payload carries the
`run_id` / `task_id` / `attempt` triple:

| Phase | What it records |
| --- | --- |
| `pin` | content digests of the subject, brief and envelope the run is bound to |
| `hypothesis` | the falsifiable claim the probe is run against |
| `probe` | the child's **actual execution facts**: `pid`, `exit_code`, `signal`, `termination`, command |
| `measure` | the captured output content-addressed by `stdout_digest` + `byte_length` |
| `classify` | the independent verifier's `grade` / `gate_state` over the producer receipt |
| `pass` / `hold` | the terminal outcome (exactly one) |

Structured result (CLI stdout, MCP JSON, and the `assay` field of
`RunExecution`) has the same shape: `methodology`, `autonomy_policy`,
`task_id`, `attempt`, `phases`, `phase_events`, `outcome`, `reason`,
`gate_state`, `child_execution`.

The binding is also written to the run's metadata (`methodology`,
`autonomy_policy`, `task_id`, `attempt`, `methodology_binding`) while the run is
still at `verify`, so it survives the terminal transition and is readable
straight from the `runs` table.

## Autonomy policies

`--autonomy-policy` selects the Assay policy (default `unicorn`):

* `unicorn` — auto-approves Assay state transitions. A gate PASS yields outcome
  `pass`.
* `moderate` — self-heals within an approved wave.
* `conservative` — only read-only probes run automatically; the terminal
  transition requires approval.

Under a policy that cannot author the terminal transition the outcome is
**`hold`** with a named `reason` — the run's own lane may still advance (the
gate passed) but the methodology outcome is withheld. A self-declared pass is
impossible: PASS comes only from the completion-contract gate.

## Boundaries preserved

* **Probe facts are not a verdict.** The `probe` phase carries execution facts
  only; grading lives in `classify`, produced by a separate `Verifier` with its
  own receipt (`verify-runsvc-<run_id>`), distinct from the producer receipt
  (`runsvc-<run_id>`).
* **Model text is never terminal authority.** Output is graded by the
  completion contract (`runtime/verify.py`), never by a string a probe prints.
  A probe that prints `{"gate_state":"pass"}` and exits non-zero is a `fail`.
* **Empty is never a pass.** Exit 0 with empty output gates `inconclusive`.
* **Declared bindings only.** `rex`, `ralph`, `gauntlet` and `assay` each carry
  their own registered topology/policy; an unregistered name is refused.

## Methodologies and their declared bindings

| Methodology | Topology | Policy |
| --- | --- | --- |
| `assay` | `sequential` | `supervised` |
| `ralph` | `dag` | `approval_required` |
| `rex` | `sequential` | `supervised` |
| `gauntlet` | `parallel_lanes` | `collaborative` |

Each combination is validated against the versioned registry
(`runtime/methodology.py`): a request executes as declared or fails with a
*named* reason. An unknown methodology is refused at the CLI argument parser
(`--methodology` choices) or, for direct service calls, at the `methodology`
stage before any run state is written.

## Reading a run back

CLI, MCP and a journal replay all read the single `run_snapshot(run_id)` view —
`state`, `stop_reason`, `methodology`, `autonomy_policy`, `task_id`, `attempt`,
ordered `phases`, terminal `outcome`, and the verifier `verification` /
`gate_state`. They cannot disagree about what ran.

## MCP control surface (`SW-1510-MCP-CONTROL`)

MCP is *transport*, not the source of durable truth. The six legacy command
tools (`create_run`, `status_run`, `cancel_run`, `resume_run`, `get_evidence`,
`review_run`) keep their names, signatures and flat JSON shapes and delegate to
`skillweave.mcp.control`, which composes the same `RunApplicationService` the
CLI drives. Alongside them the control surface (`src/skillweave/mcp/control.py`)
exposes:

* **A namespaced, opt-in read shape.** `snapshot_v1(run_id)` emits the frozen
  `run-snapshot-v1` record; an unknown version name is *refused*
  (`SnapshotVersionRefused`), never coerced to a supported one.
* **Structured refusals, never raised exceptions.** A control tool *returns* a
  contract or storage refusal as a durable record —
  `{"error", "code", "run_id", "version", "supported": [...]}` — and never
  leaks an exception across the tool boundary. `code` is one of
  `unsupported_version` (an unknown version name), `snapshot_unavailable` (a run
  whose trusted fields cannot be expressed in the frozen enum — e.g. the
  default `collaborative` policy has no `run-snapshot-v1` slot;
  `SUPPORTED_SNAPSHOT_VERSIONS` is deliberately **not** widened), `not_found`,
  or `storage_unavailable`. `control_cancel` additionally reports an unverified
  process identity as `code == "identity_unverified"` with the verification
  reason.
* **First-run store creation.** The control surface creates the run store's
  parent directory on first use, as the CLI ingress does, so a control call from
  a fresh project cwd *creates* `.skillweave` rather than failing with `unable to
  open database file`. The directory-backed stores do not need pre-creation -- a
  durable stream store creates its own root and the raw artifact store is
  memory-backed -- so an inspect-only call has no artifacts-directory side
  effect on disk.
* **An honest dependency diagnostic.** Serving needs the real `mcp` package.
  When it is absent the placeholder server reports `can_serve == False` with an
  explicit reason and refuses to run; `python -m skillweave.mcp.server` exits
  non-zero with the diagnostic instead of silently no-op'ing.
* **Transport/capability negotiation.** `stdio` and the scoped Streamable HTTP
  transport are described and negotiated. Experimental MCP *Tasks* are
  negotiated only where the transport supports them **and** the client asks;
  otherwise the negotiated execution shape is the ordinary asynchronous tool
  plus a poll cursor (`control_poll`).
* **A validated scoped bind.** `serve(transport="http")` runs the real
  `streamable-http` transport only after `require_scoped_bind` validates the
  bind *before* any server is built: the default host is loopback
  (`127.0.0.1`), a non-loopback host is refused (`ScopedHttpRefused`) unless
  `allow_remote=True` is passed, and a scope token (`auth_token` or
  `SKILLWEAVE_MCP_HTTP_TOKEN`) is required. A bind that is not scoped refuses;
  nothing is served open.
* **Per-request scoped HTTP authentication** (`SW-1510-MCP-AUTH`). A scope
  token checked only at startup does not authenticate a *request*. The scope
  token is enforced on **every** HTTP request, ahead of any MCP handling, by an
  ASGI guard installed on the built server's Streamable HTTP app
  (`control._install_http_request_guard`). The comparison is constant-time
  (`hmac.compare_digest`); a missing or wrong token is refused with `401` and a
  `WWW-Authenticate: Bearer` challenge, and neither the presented nor the
  expected token is ever echoed in the response body, the log line or the
  refusal reason. When a session authority is bound at serve time the presented
  token is resolved to its registered principal and that principal is attached
  to the operation for the call (a `ContextVar`, so concurrent requests never
  see one another's principal, and reset between requests), so the per-tool
  gating sees an *authenticated* principal over HTTP exactly as it does over
  stdio. The authenticated principal's role comes from the server registry,
  never the wire.
* **An explicit supervisor identity for local stdio** (`SW-1510-MCP-AUTH`).
  The local stdio transport runs under a supervised session, so
  `python -m skillweave.mcp.server` requires the supervising principal to be
  named in `SKILLWEAVE_MCP_SUPERVISOR` (a real operator/launcher sets it; the
  value is the supervisor's identity, e.g. `supervisor:ops`). Without it the
  entry **refuses cleanly**: a one-line actionable message on stderr (naming
  the environment variable and the word *supervisor*) and the documented
  non-zero exit code `3` (`server.EXIT_REFUSED`) — never a bare traceback. The
  same non-zero exit covers the missing-`mcp`-dependency refusal, so a launcher
  sees one documented failure code for either refusal.

### Stub vs live transport proof

`tests/unit/test_1510_mcp_control.py::TestScopedHttpServingPath` executes the
real `control.serve` code path against an injected fake FastMCP module (the
module's `fastmcp_factory` seam). Those assertions **pin the serving contract** —
the exact `run(transport="streamable-http")` call, the host/port settings, the
loopback default and the pre-serve refusals — and are **not** a live transport
proof. The live proofs (`*_transport_initialize_and_list`) speak the MCP
protocol to a real server over stdio and over scoped Streamable HTTP; they
**skip, naming the missing package**, when the real `mcp` package is not
importable. On a host without `mcp` the live paths are therefore *unproven*, not
silently passed.

## Limitations

* The legacy/default path (`methodology="ralph"`, the historical default)
  carries no Assay block; it is unchanged.
* Assay binds one probe execution per run/attempt. Remediate / escalate-split
  waves are representable in the engine's state model but the native path
  records a single PIN→…→outcome pass today.
* The `skillweave run` subcommand's remainder positional is named `run_argv`
  (not `command`); renaming it back to `command` collides with the router's
  `add_subparsers(dest="command")` and makes the installed entry unreachable.
