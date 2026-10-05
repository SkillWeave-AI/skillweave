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

## Limitations

* The legacy/default path (`methodology="ralph"`, the historical default)
  carries no Assay block; it is unchanged.
* Assay binds one probe execution per run/attempt. Remediate / escalate-split
  waves are representable in the engine's state model but the native path
  records a single PIN→…→outcome pass today.
* The `skillweave run` subcommand's remainder positional is named `run_argv`
  (not `command`); renaming it back to `command` collides with the router's
  `add_subparsers(dest="command")` and makes the installed entry unreachable.
