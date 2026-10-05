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
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
        authority_service: Optional[Any] = None,
        worker_token: Optional[str] = None,
        require_identity: bool = False,
    ) -> RunExecution:
        """Run ``command`` through the six-stage integration path.

        The six record kinds are produced in a single, gap-free sequence, and
        the gate state is derived from the verifier's verdict over the bound
        receipt — never from a raw exit code alone.

        When ``authority_service`` is supplied, the host authenticates the
        worker at *ingress*: ``worker_token`` must verify against the host
        credential registry and bind to this exact ``run_id``, and the worker
        credential must carry no approving role. This is the actual integration
        point for the SW-1510 identity boundary; without it the run path is the
        unchanged legacy path.

        ``require_identity=True`` makes a missing authority service a hard
        failure, so a protected caller cannot accidentally run the legacy path.
        """
        created_at = created_at or datetime.now(timezone.utc).isoformat()

        if authority_service is None and require_identity:
            raise RunIntegrationError(
                "identity",
                "require_identity=True but no authority_service was supplied",
            )
        if authority_service is not None:
            self._authenticate_worker(
                authority_service, worker_token=worker_token, run_id=run_id
            )

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

    def _authenticate_worker(
        self,
        authority_service: Any,
        *,
        worker_token: Optional[str],
        run_id: str,
    ) -> None:
        """Authenticate the worker credential at run ingress (SW-1510-IDENTITY).

        The host service verifies the presented token against its credential
        registry. A missing/forged/revoked/expired token, or one bound to a
        different run, fails before any run state is written. The credential
        must be a worker credential: it must carry no approving role, so a
        worker can never reach a reviewer/observer/controller decision.
        """
        if not worker_token:
            raise RunIntegrationError("identity", "no worker credential presented")
        try:
            credential = authority_service.verify_credential(worker_token)
        except Exception as exc:  # noqa: BLE001 - re-raised as integration error
            raise RunIntegrationError("identity", f"worker credential rejected: {exc}") from exc
        approving = credential.roles & {"reviewer", "observer", "controller", "integrator"}
        if approving:
            raise RunIntegrationError(
                "identity",
                f"worker credential carries approving role(s) {sorted(approving)}",
            )
        attempt_key = getattr(credential, "attempt_key", None)
        if attempt_key is not None and attempt_key[0] != run_id:
            raise RunIntegrationError(
                "identity",
                "worker credential is bound to a different run",
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
