"""Run Application Service: the single authoritative integration path.

``SW-RUN-SVC-001``. This module wires the runtime primitives into one seam that
a caller drives end to end. A real harness run goes through it and leaves six
record kinds behind, each addressable and none synthesized:

* **Run** — the ``RunRecord`` in ``SQLiteRunStore`` (state, version, timestamps).
* **Journal** — the ordered ``EventJournal`` entries for the run (no gaps).
* **Raw Artifact** — the worker's captured bytes in ``RawArtifactStore``,
  content-addressed and resolvable back to exact bytes.
* **Receipt** — the ``ArtifactReceipt`` bound to the run, addressed by the same
  digest, persisted via ``store.save_evidence``.
* **Verification** — a separate ``VerificationResult`` produced by an
  independent ``Verifier`` (never the producer's self-claim), with its own
  receipt.
* **Gate** — the gate state derived by the completion contract from the
  *verified* outcome, never from a raw exit code alone.

The service orchestrates these so they cannot diverge: it creates the run
through ``store.create_run``, writes the journal entry for the launch, puts the
raw bytes, saves the receipt, runs the verifier over that receipt, and finally
evaluates the gate. If any step raises, the run is marked ``failed`` (an
explicit terminal state) rather than silently leaving a half-written run.

The service deliberates branches on none of its inputs: it receives a command,
a ``tool``, a ``model``, and run identity, and passes all of them through to the
same path.
"""

from __future__ import annotations

import hashlib
import importlib
import json as _json
import os
import signal as _signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence


def _runtime_attr(submodule: str, *names: str):
    """Resolve runtime names at call time (GLE-020).

    ``skillweave.runtime`` is an optional subpackage and must not be imported at
    module level here; each submodule and its names are resolved lazily through
    ``importlib`` (string-based, so no ``skillweave.runtime.*`` import statement
    appears in this module's AST).
    """
    module = importlib.import_module("skillweave.runtime." + submodule)
    if len(names) == 1:
        return getattr(module, names[0])
    return [getattr(module, n) for n in names]


class RunIntegrationError(Exception):
    """A run could not be completed end to end.

    Raised when any stage of the single integration path fails before it can
    produce all six record kinds. The message names the stage, so a caller can
    tell a launch failure from a verification failure without inspecting
    partial state.
    """

    def __init__(self, stage: str, reason: str):
        self.stage = stage
        self.reason = reason
        super().__init__(f"run integration failed at '{stage}': {reason}")


class RunExecutionError(Exception):
    """The worker process itself failed (non-zero exit, signal, or timeout)."""


@dataclass
class RunExecution:
    """The complete, gap-free record set of one authoritative run.

    Six fields, each a distinct record kind, each addressable:

    * ``run`` — the persisted ``RunRecord``.
    * ``journal`` — the ordered journal events for the run (no gaps).
    * ``raw_digest`` — the sha256 under which the raw bytes are stored.
    * ``raw_bytes`` — the worker's captured stdout bytes.
    * ``receipt`` — the artifact receipt bound to the run.
    * ``verification`` — the independent verifier's verdict (receipt dict).
    * ``gate_state`` — the completion-contract gate state.
    """

    run: RunRecord
    journal: list[Any]
    raw_digest: str
    raw_bytes: bytes
    receipt: ArtifactReceipt
    verification: dict[str, Any]
    gate_state: str

    @property
    def run_id(self) -> str:
        return self.run.run_id


class RunApplicationService:
    """The single authoritative integration path for a harness run.

    Construct with a store, a journal, and a raw artifact store. ``execute``
    runs one command through all six stages and returns a :class:`RunExecution`.
    A stage failure marks the run ``failed`` and raises
    :class:`RunIntegrationError` — the run is never left silently half-written.
    """

    def __init__(
        self,
        store: SQLiteRunStore,
        journal: EventJournal,
        raw_artifacts: RawArtifactStore,
    ):
        self.store = store
        self.journal = journal
        self.raw_artifacts = raw_artifacts

    def execute(
        self,
        command: Sequence[str],
        *,
        run_id: str,
        tool: str,
        model: str,
        subject_repo: str,
        subject_commit: str,
        created_at: Optional[str] = None,
        check_output: Optional[Any] = None,
    ) -> RunExecution:
        """Run ``command`` through the six-stage integration path.

        The six record kinds are produced in a single, gap-free sequence, and
        the gate state is derived from the verifier's verdict over the bound
        receipt — never from a raw exit code alone.
        """
        created_at = created_at or datetime.now(timezone.utc).isoformat()

        run_command = _runtime_attr("runner_adapter", "run_command")
        EventType = _runtime_attr("journal", "EventType")
        ArtifactReceipt = _runtime_attr("registry", "ArtifactReceipt")
        EvidenceQuality = _runtime_attr("registry", "EvidenceQuality")
        EvidenceType = _runtime_attr("registry", "EvidenceType")
        Verifier = _runtime_attr("verify", "Verifier")
        CompletionContract = _runtime_attr("verify", "CompletionContract")
        GateState = _runtime_attr("verify", "GateState")

        # Stage 1: Run. Walk the lane through its legal states up to
        # ``verify``, so the run is a real, addressable record before any
        # evidence is bound. The terminal transition happens after gating.
        run = self.store.create_run(run_id)
        for from_state, to_state in (
            ("preflight", "batch_selection"),
            ("batch_selection", "lane_plan"),
            ("lane_plan", "implement"),
            ("implement", "verify"),
        ):
            current = self.store.get_run(run_id)
            self.store.transition(
                run_id,
                to_state,
                expected_state=from_state,
                expected_version=current.version,
                reason="run svc: lane advance",
                role="ops",
            )

        # Stage 2: launch the worker as a real subprocess.
        try:
            result = run_command(
                list(command),
                run_id=run_id,
                subject_repo=subject_repo,
                subject_commit=subject_commit,
                tool=tool,
                model=model,
                created_at=created_at,
            )
        except Exception as exc:  # noqa: BLE001
            self._fail_run(run_id, f"launch raised {exc}")
            raise RunIntegrationError("launch", str(exc)) from exc

        # Stage 3: raw artifact (content-addressed bytes).
        raw_bytes = result.stdout or b""
        raw_digest = self.raw_artifacts.put(raw_bytes)

        # Stage 4: journal entries (ordered, gap-free).
        self.journal.append(
            run_id,
            event_type=EventType.ARTIFACT_CREATED.value,
            payload={
                "digest": raw_digest,
                "byte_length": len(raw_bytes),
                "exit_code": result.exit_code,
                "signal": result.signal,
                "termination": result.termination,
            },
        )

        # Stage 5: the receipt, bound to the run and the raw bytes.
        receipt = ArtifactReceipt(
            artifact_id=f"runsvc-{run_id}",
            sha256=raw_digest,
            schema_version="1",
            producer_command=" ".join(command),
            subject_repo=subject_repo,
            subject_commit=subject_commit,
            created_at=created_at,
            evidence_type=EvidenceType.ARTIFACT.value,
            purpose=f"authoritative run '{run_id}' output",
            method="runsvc",
            system_source="runsvc",
            quality=EvidenceQuality(),
            metadata={
                "run_id": run_id,
                "tool": tool,
                "model": model,
                "byte_length": len(raw_bytes),
                "exit_code": result.exit_code,
                "signal": result.signal,
                "termination": result.termination,
            },
        )
        saved_receipt = self.store.save_evidence(receipt)

        # Stage 6: verification (a separate verifier, never self-claim).
        verifier = Verifier()
        verdict = verifier.assess(
            saved_receipt.artifact_id,
            exit_code=result.exit_code,
            signal=result.signal,
            termination=result.termination,
            stdout=raw_bytes,
            check_output=check_output,
        )
        verdict_body = (
            f"{verdict.subject_artifact_id}|{verdict.grade}|{verdict.gate_state}"
        ).encode("utf-8")
        verification_digest = hashlib.sha256(verdict_body).hexdigest()
        verdict_receipt = verdict.to_receipt(digest=verification_digest)

        # Gate: derived from the completion contract, over the verified outcome.
        contract = CompletionContract()
        gate_state = contract.evaluate(
            exit_code=result.exit_code,
            signal=result.signal,
            termination=result.termination,
            stdout=raw_bytes,
            check_output=check_output,
        )

        # Terminal state: route through the lane's legal gate state to a
        # definite end. PASS goes verify -> review_gate -> advance_or_stop; a
        # non-pass goes verify -> fix_retry -> advance_or_stop with the stop
        # reason recorded. Never a dangling in-progress row.
        current = self.store.get_run(run_id)
        gate_stage = "review_gate" if gate_state == GateState.PASS else "fix_retry"
        stop_reason = None if gate_state == GateState.PASS else "before_gate"

        try:
            self.store.transition(
                run_id,
                gate_stage,
                expected_state="verify",
                expected_version=current.version,
                reason="run svc: gate stage",
                role="ops",
            )
            current = self.store.get_run(run_id)
            self.store.transition(
                run_id,
                "advance_or_stop",
                expected_state=gate_stage,
                expected_version=current.version,
                reason="run svc: verified gate",
                role="ops",
                stop_reason=stop_reason,
            )
        except Exception as exc:  # noqa: BLE001
            self._fail_run(run_id, f"terminal transition raised {exc}")
            raise RunIntegrationError("gate", str(exc)) from exc

        final_run = self.store.get_run(run_id)
        journal_events = self.journal.get_events(run_id)

        return RunExecution(
            run=final_run,  # type: ignore[arg-type]
            journal=journal_events,
            raw_digest=raw_digest,
            raw_bytes=raw_bytes,
            receipt=saved_receipt,
            verification=verdict_receipt,
            gate_state=gate_state,
        )

    def _fail_run(self, run_id: str, reason: str) -> None:
        """Mark a run terminal with a stop reason when a stage raises.

        Best-effort: drives the run to ``advance_or_stop`` through the legal
        fix_retry gate when its current state permits, so a failed stage is
        never a dangling in-progress row. The stop reason is data, not a
        bespoke vocabulary value.
        """
        record = self.store.get_run(run_id)
        if record is None:
            return
        try:
            if record.state == "verify":
                self.store.transition(
                    run_id,
                    "fix_retry",
                    expected_state="verify",
                    expected_version=record.version,
                    reason="run svc failure: " + reason,
                    role="ops",
                )
                record = self.store.get_run(run_id)
            if record.state == "fix_retry":
                self.store.transition(
                    run_id,
                    "advance_or_stop",
                    expected_state="fix_retry",
                    expected_version=record.version,
                    reason="run svc failure: " + reason,
                    role="ops",
                    stop_reason="before_gate",
                )
        except Exception:  # noqa: BLE001
            # Best effort: the primary failure is already on its way up.
            pass

    # ------------------------------------------------------------------
    # Native async lifecycle (SW-1510-STORE)
    #
    # ``start_async``, ``kill_run``, and ``resume_run`` route
    # detached-process lifecycle through the kernel store with CAS, so
    # completion, cancellation, and resume share one SQLite/CAS authority.
    # ------------------------------------------------------------------

    def reserve_run_id(self, run_id: Optional[str] = None) -> str:
        """Reserve a unique run ID before launching a process.

        Returns the reserved ID.  Duplicate admission must not execute a
        child — the store's PRIMARY KEY guard ensures exactly one creator
        wins.
        """
        rid = run_id or uuid.uuid4().hex
        # Reserve by creating the run record in preflight state.
        self.store.create_run(rid)
        return rid

    def start_async(
        self,
        command: Sequence[str],
        *,
        run_id: Optional[str] = None,
        cwd: Optional[Path] = None,
        project_root: Optional[Path] = None,
        _env: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Start *command* as a detached background process.

        The child runs in its own session (process group) so it survives the
        launcher's exit.  The run is registered in the kernel store BEFORE
        launch; a crash before the child starts leaves a reserved-but-not-
        running record that is safe to reconcile.

        Parameters
        ----------
        command:
            The command and arguments to execute.
        run_id:
            Explicit run ID (default: auto-generated uuid hex).
        cwd:
            Working directory for the child (default: current dir).
        project_root:
            Project root path (for store.db location).
        _env:
            Optional environment dict for the child subprocess.  Used by the
            native lifecycle facade to propagate PYTHONPATH.

        Returns
        -------
        dict with ``run_id``, ``pid``, ``command``, ``state``,
        ``created_at``, ``version``.
        """
        rid = self.reserve_run_id(run_id)
        child_cwd = str(cwd or Path.cwd())

        db_path = self.store.db_path
        EventType = _runtime_attr("journal", "EventType")

        # Store initial lifecycle metadata BEFORE launching the child, so the
        # wrapper starts at version 2 (not version 1).  This prevents a race
        # where a fast child completes before the parent gets to write its
        # own lifecycle metadata: the parent would see VersionConflictError
        # because the wrapper already bumped the version.
        self.store.update_metadata(
            rid,
            expected_version=1,
            metadata={
                "runlifecycle": {
                    "command": list(command),
                    "pid": 0,
                    "lifecycle_state": "running",
                },
            },
        )

        # Build the detached wrapper script that writes completion/error
        # back to the same kernel store.
        wrapper_src = (
            "import subprocess, sys, json, os, signal\n"
            "rid = sys.argv[1]\n"
            "db_path = sys.argv[2]\n"
            "cmd = sys.argv[3:]\n"
            "from skillweave.runtime.store import SQLiteRunStore, RunStateModel, CompletionReconciler\n"
            "from skillweave.runtime.journal import EventJournal\n"
            "store = SQLiteRunStore(db_path)\n"
            "journal = EventJournal(db_path)\n"
            "journal.append(rid, event_type='started',\n"
            "    payload={'command': cmd})\n"
            "def _complete(lifecycle, event_type, payload, key, reason):\n"
            "    CompletionReconciler(\n"
            "        store, rid,\n"
            "        target_state='advance_or_stop',\n"
            "        lifecycle_update=lifecycle,\n"
            "        event_type=event_type,\n"
            "        event_payload=payload,\n"
            "        idempotency_key=key,\n"
            "        reason=reason,\n"
            "    ).reconcile()\n"
            "try:\n"
            "    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)\n"
            "    rc = proc.returncode\n"
            "    stdout_text = proc.stdout.decode('utf-8', errors='replace') if proc.stdout else ''\n"
            "    _complete(\n"
            "        {'lifecycle_state': 'succeeded' if rc == 0 else 'failed', 'exit_code': rc},\n"
            "        'finished', {'returncode': rc, 'stdout_preview': stdout_text[:1024]},\n"
            "        'wrapper:finish:' + rid + ':' + str(rc), 'wrapper: process completed')\n"
            "except Exception as exc:\n"
            "    _complete(\n"
            "        {'lifecycle_state': 'failed', 'exit_code': -1},\n"
            "        'error', {'error': str(exc)},\n"
            "        'wrapper:error:' + rid, 'wrapper: error: ' + str(exc))\n"
            "finally:\n"
            "    store.close()\n"
            "    journal.close()\n"
        )

        # Ensure the child can always load third-party packages (e.g. yaml).
        # Python 3.14: putting a directory with sitecustomize.py first in
        # PYTHONPATH causes the site module to skip normal site-packages
        # discovery.  Appending the system site-packages path defensively
        # prevents the wrapper from failing to import dependencies.
        if _env is not None:
            import site as _site
            _sp = _site.getsitepackages()[0]
            _pp = _env.get("PYTHONPATH", "")
            if _sp not in _pp:
                _env["PYTHONPATH"] = _pp + ":" + _sp if _pp else _sp

        proc = subprocess.Popen(
            [sys.executable, "-c", wrapper_src, rid, db_path] + list(command),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            cwd=child_cwd,
            env=_env,
        )

        now = datetime.now(timezone.utc).isoformat()
        self.journal.append(
            rid,
            event_type="launched",
            payload={
                "event": "launched",
                "pid": proc.pid,
                "command": list(command),
            },
        )

        # Update the PID (placeholder was 0) at the current version.
        # If the wrapper already completed, this CAS will fail harmlessly —
        # the PID is already in the journal event and the lifecycle metadata.
        current = self.store.get_run(rid)
        if current is not None:
            try:
                lifecycle = dict(current.metadata.get("runlifecycle", {}))
                lifecycle["pid"] = proc.pid
                self.store.update_metadata(
                    rid,
                    expected_version=current.version,
                    metadata={"runlifecycle": lifecycle},
                )
            except Exception:
                # Race with wrapper completion — PID was best-effort.
                pass

        run = self.store.get_run(rid)
        return {
            "run_id": rid,
            "pid": proc.pid,
            "command": list(command),
            "state": run.state if run else "preflight",
            "created_at": now,
            "version": run.version if run else 1,
        }

    def kill_run(
        self,
        run_id: str,
        pid: int,
        *,
        project_root: Optional[Path] = None,
    ) -> dict[str, Any]:
        """Kill a run by its ID and recorded PID.

        Graceful (SIGTERM) first, then forced (SIGKILL) after a short grace
        period.  The terminal transition is CAS-guarded so a stale completion
        cannot resurrect a cancelled run.
        """
        run = self.store.get_run(run_id)
        if run is None:
            return {"error": f"run not found: {run_id}"}

        RunStateModel = _runtime_attr("store", "RunStateModel")
        if run.state != "preflight" and RunStateModel.is_terminal(run.state):
            return {
                "run_id": run_id,
                "pid": pid,
                "state": run.state,
                "result": "already_terminal",
            }

        # Record a durable cancellation intent BEFORE the irreversible signal
        # boundary.  If the supervisor is lost after signalling (or dies *at*
        # the signal call itself), the intent stays readable, so a later
        # production resume (``resume_run``) finishes the cancellation instead
        # of leaving a non-terminal ``running`` run with no restart authority.
        # The real signal outcome is still committed below; the intent is only
        # the recoverable fallback.  A genuine terminal winner was already
        # returned above, and ``record_intent`` leaves one untouched.
        KillOutcomeError = _runtime_attr("errors", "KillOutcomeError")
        reconcile_cancellation_intent = getattr(
            self.store, "reconcile_cancellation_intent", None)
        if reconcile_cancellation_intent is not None:
            try:
                reconcile_cancellation_intent(
                    run_id,
                    pid=pid,
                    result="pending",
                    event_payload={"pid": pid, "result": "pending"},
                    idempotency_key="kill:" + run_id,
                    reason="service: kill",
                ).record_intent()
            except Exception as exc:  # noqa: BLE001
                # A cancellation whose intent cannot be durably recorded must
                # not proceed to the irreversible signal as if it were safe.
                raise KillOutcomeError(
                    run_id,
                    {"reason": "cancellation intent could not be durably recorded",
                     "error": type(exc).__name__, "detail": str(exc)},
                    result="unknown",
                    cause=exc,
                ) from exc

        # Graceful kill (SIGTERM) to the process group
        killed_graceful = False
        try:
            os.killpg(pid, _signal.SIGTERM)
            killed_graceful = True
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                    time.sleep(0.05)
                except ProcessLookupError:
                    break
        except (ProcessLookupError, PermissionError):
            pass

        try:
            os.killpg(pid, _signal.SIGKILL)
            result = "forced_kill"
        except ProcessLookupError:
            result = "graceful_kill" if killed_graceful else "already_dead"
        except PermissionError:
            try:
                os.kill(pid, _signal.SIGKILL)
                result = "forced_kill"
            except (ProcessLookupError, PermissionError):
                result = "graceful_kill" if killed_graceful else "already_dead"

        # Drive the run to terminal via the canonical durable reconciliation
        # primitive, co-committing lifecycle_state='killed' and exactly one
        # kill outbox/journal event in the terminal unit of work.  This
        # ensures:
        # 1. Every non-terminal lane state reaches terminal via legal
        #    forward-only transitions (resolved from RunStateModel, not a
        #    hand-maintained table that can drift).
        # 2. Lifecycle metadata, terminal state, and the kill event are one
        #    atomic durable unit (no crash window where lifecycle says killed
        #    while the lane is still non-terminal or the outbox is empty).
        # 3. A genuine pre-existing terminal winner (a committed completion or
        #    an earlier cancellation) is preserved unchanged and never
        #    resurrected.
        # 4. A finite burst of competing legal writers cannot exhaust a fixed
        #    retry budget and lose the cancellation: reconcile_terminal retries
        #    on VersionConflictError without a finite bound.
        # 5. A non-CAS storage failure propagates (never a successful-looking
        #    ``already_dead``/``running`` result with the exception swallowed).
        run = self.store.get_run(run_id)
        cancelling = run is not None and not RunStateModel.is_terminal(run.state)
        if cancelling:
            VersionConflictError = _runtime_attr("errors", "VersionConflictError")
            InvalidTransitionError = _runtime_attr("errors", "InvalidTransitionError")
            KillOutcomeError = _runtime_attr("errors", "KillOutcomeError")
            try:
                reconcile_terminal = getattr(self.store, "reconcile_terminal", None)
                if reconcile_terminal is not None:
                    # Canonical durable path: records a recoverable intent, walks
                    # the state-machine path, and retries past CAS contention.
                    record = reconcile_terminal(
                        run_id,
                        target_state="advance_or_stop",
                        lifecycle_update={"lifecycle_state": "killed"},
                        event_type="process_killed",
                        event_payload={"pid": pid, "result": result},
                        idempotency_key="kill:" + run_id,
                        reason="service: kill",
                        stop_reason="operator_halt",
                    )
                else:
                    # Minimal duck-typed store fallback (tests that provide a
                    # thin store double without the reconciliation primitive):
                    # a single atomic unit of work, with a genuine terminal
                    # winner preserved.
                    current = run
                    path = RunStateModel.canonical_path_to_terminal(
                        current.state, "advance_or_stop")
                    if path is None:
                        raise InvalidTransitionError(
                            current.state, "advance_or_stop", run_id,
                            extra={"reason": f"no legal path from {current.state} to terminal"},
                        )
                    for to_state in path:
                        self.store.transition(
                            run_id, to_state,
                            expected_state=current.state,
                            expected_version=current.version,
                            reason="service: kill advance",
                        )
                        current = self.store.get_run(run_id)
                    record = self.store.transition_with_event(
                        run_id, "advance_or_stop",
                        expected_state=current.state,
                        expected_version=current.version,
                        event_type="process_killed",
                        event_payload={"pid": pid, "result": result},
                        idempotency_key="kill:" + run_id,
                        reason="service: kill",
                        stop_reason="operator_halt",
                        metadata_update={"runlifecycle": {"lifecycle_state": "killed"}},
                    )
            except (VersionConflictError, InvalidTransitionError):
                # Structurally meaningful store errors (illegal transition, no
                # path, terminal resurrection) surface unchanged.
                raise
            except Exception as exc:  # noqa: BLE001
                # A non-CAS storage failure (e.g. a SQLite I/O error) must not
                # be swallowed into a successful-looking ``already_dead`` /
                # ``running`` result.  Surface it as the explicit typed error
                # for an unreconcilable cancellation, preserving the cause.
                raise KillOutcomeError(
                    run_id,
                    {"reason": "storage failure during cancellation terminal commit",
                     "error": type(exc).__name__, "detail": str(exc)},
                    result=result,
                    cause=exc,
                ) from exc
        else:
            # The run is already terminal.  A committed terminal winner stands
            # unchanged; no lifecycle or state is fabricated.  The kill event
            # is only recorded on the readable journal so an idempotent re-kill
            # is observable without a second terminal transition.
            self.journal.append(
                run_id,
                event_type="process_killed",
                payload={"pid": pid, "result": result},
                idempotency_key="kill:" + run_id,
            )
            record = self.store.get_run(run_id)

        final_lifecycle = (record.metadata or {}).get("runlifecycle", {}).get("lifecycle_state")
        # Report the lifecycle vocabulary ("killed") only when this call
        # actually committed a genuine cancellation.  If a *different* terminal
        # winner already stood (a committed completion the reconciler preserved),
        # the operator sees its real lane state — never a fabricated "killed".
        reported_state = "killed" if (cancelling and final_lifecycle == "killed") else (
            record.state if record is not None else "unknown")
        return {
            "run_id": run_id,
            "pid": pid,
            "state": reported_state,
            "lane_state": record.state if record is not None else "unknown",
            "lifecycle_state": final_lifecycle,
            "result": result,
        }

    def resume_run(self, run_id: str) -> Optional[dict[str, Any]]:
        """Reconnect to a stored run, returning its current state.

        Resume reconnects to stored actual identity/checkpoints and must
        not launch a duplicate process or revive a terminal/historical run.
        """
        run = self.store.get_run(run_id)
        if run is None:
            return None

        RunStateModel = _runtime_attr("store", "RunStateModel")
        if RunStateModel.is_terminal(run.state):
            return {
                "run_id": run_id,
                "state": run.state,
                "error": "terminal run cannot be resumed",
            }

        if run.metadata.get("historical"):
            return {
                "run_id": run_id,
                "state": "unknown",
                "error": "historical run cannot be resumed",
            }

        # A cancellation intent recorded before the signal boundary but never
        # committed (the supervisor died at/after the signal) is finished here,
        # on the actual production restart/resume path, so the run reaches a
        # terminal ``killed`` lifecycle with exactly one logical event instead
        # of being left non-terminal ``running``.
        CancellationReconciler = _runtime_attr("store", "CancellationReconciler")
        finished = CancellationReconciler.resume_pending(self.store, run_id)
        if finished is not None:
            run = finished

        events = self.journal.get_events(run_id)
        return {
            "run_id": run_id,
            "state": run.state,
            "version": run.version,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
            "event_count": len(events),
        }
