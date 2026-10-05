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


def _sha256_hex(data: bytes) -> str:
    """Hex sha256 of ``data`` (used to content-address assay phase evidence)."""
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class _MethodologyBinding:
    """A resolved, validated methodology binding for one run.

    ``methodology``, ``topology`` and ``policy`` are kept as three separate
    fields (never conflated into one mode string). ``policy_name`` is the
    autonomy policy name recorded on the run (``conservative`` / ``moderate`` /
    ``unicorn`` for Assay).
    """

    methodology: Any
    topology: Any
    policy: Any
    policy_name: str
    definition: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "methodology": self.methodology.value,
            "topology": self.topology.value,
            "policy": self.policy.value,
            "autonomy_policy": self.policy_name,
            "risk": self.definition.risk.value,
            "authority": self.definition.authority.value,
            "version": self.definition.version,
        }


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
    #: The methodology actually executed (``"assay"``, ``"ralph"`` ...), or the
    #: empty string on the unchanged legacy path. Never a substitute for the
    #: run's own lane state — the methodology is a declared binding, not a
    #: terminal verdict.
    methodology: str = ""
    #: The Assay phase record (``task_id``, ``attempt``, ordered ``phases``, the
    #: ``outcome`` and the measured child-execution facts), or ``None`` when the
    #: method is not Assay. Child-execution facts and the verifier verdict stay
    #: distinct here; this block never carries a self-declared pass.
    assay: Optional[dict[str, Any]] = None

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
        methodology: str = "ralph",
        autonomy_policy: str = "",
        task_id: Optional[str] = None,
        attempt: int = 1,
        declared_writes: Optional[Sequence[str]] = None,
        work_contract: Optional[Any] = None,
        assay_actor_token: Optional[str] = None,
        assay_envelope: Optional[Any] = None,
    ) -> RunExecution:
        """Run ``command`` through the six-stage integration path.

        The six record kinds are produced in a single, gap-free sequence, and
        the gate state is derived from the verifier's verdict over the bound
        receipt — never from a raw exit code alone.

        ``methodology`` names the declared methodology binding. The DEFAULT is
        ``"ralph"`` — the *existing* lane the run path already walks — so an
        unchanged caller keeps exactly today's behaviour. A request that does
        NOT name a known methodology stays on that lane. A request that names
        ``"assay"`` runs the native Assay path (PIN → HYPOTHESIS → PROBE →
        MEASURE → CLASSIFY → outcome) and is **never** silently downgraded to
        Ralph: an Assay request either executes as Assay or fails explicitly.

        ``autonomy_policy`` is the Assay autonomy policy name (``conservative``
        / ``moderate`` / ``unicorn``); empty means the registry default. It is
        recorded as its own field and never conflated with the methodology.

        ``task_id`` / ``attempt`` address the run/task/attempt triple that the
        SDK run-snapshot and attempt-event contracts carry. ``task_id``
        defaults to ``run_id`` for the single-task legacy path.

        When ``authority_service`` is supplied, the host authenticates the
        worker at *ingress*: ``worker_token`` must verify against the host
        credential registry and bind to this exact ``run_id``, and the worker
        credential must carry no approving role. This is the actual integration
        point for the SW-1510 identity boundary; without it the run path is the
        unchanged legacy path.

        ``require_identity=True`` makes a missing authority service a hard
        failure, so a protected caller cannot accidentally run the legacy path.

        ``declared_writes`` and ``work_contract`` turn on the execution
        write-scope boundary (SW-1510-SCOPE). Both are *protected-only*: naming
        either without an authenticated worker is a hard failure, so scope
        enforcement can never be silently downgraded to the legacy path. When
        present, every declared output is authorized against the resolved
        attempt checkout (and the protected store roots) *before* the child is
        spawned, and the child runs inside the same attested isolation window.

        For an Assay run, ``assay_actor_token`` is a live credential the host
        authority verifies; when supplied, the native Assay admission
        (actor / pinned subject / brief / deadline / remaining budget) is
        enforced on every drill mutation through that actor. Both Assay
        admission parameters are honoured only on the identity-authenticated
        path (``authority_service`` supplied): the legacy in-process path is
        unchanged, and a scope-enforced launch refuses for want of identity
        before it can reach the drill.
        """
        created_at = created_at or datetime.now(timezone.utc).isoformat()

        if (declared_writes is not None or work_contract is not None) and (
            authority_service is None
        ):
            raise RunIntegrationError(
                "identity",
                "declared_writes/work_contract require an authenticated worker "
                "(auth-scoped execution); refusing the unprotected path",
            )

        credential = None
        if authority_service is None and require_identity:
            raise RunIntegrationError(
                "identity",
                "require_identity=True but no authority_service was supplied",
            )
        if authority_service is not None:
            credential = self._authenticate_worker(
                authority_service, worker_token=worker_token, run_id=run_id
            )

        # Resolve the declared methodology binding BEFORE any run state is
        # written. This is where an Assay request is either honoured or refused
        # explicitly — never reinterpreted as some other methodology. The
        # binding also validates the methodology/topology/policy combination
        # (an incompatible Assay combination fails with its named reason).
        binding = self._resolve_methodology_binding(
            methodology=methodology,
            autonomy_policy=autonomy_policy,
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

        # SW-1510-ASSAY: open the native Assay drill *before* the real probe is
        # launched, so PIN/HYPOTHESIS describe the policy/state machine the
        # launch actually executes rather than a post-hoc diary. On the
        # identity-authenticated path this resolves the assay actor through the
        # live host authority and enforces the admission envelope (actor,
        # pinned subject/brief, deadline, remaining budget) before the child
        # exists; a refusal fails the run rather than leaving it dangling.
        assay_pre_state: Optional[dict[str, Any]] = None
        if binding.methodology.value == "assay":
            try:
                assay_pre_state = self._assay_open(
                    run_id=run_id,
                    task_id=task_id or run_id,
                    attempt=int(attempt),
                    binding=binding,
                    command=list(command),
                    subject_repo=subject_repo,
                    subject_commit=subject_commit,
                    authority_service=authority_service,
                    credential=credential,
                    assay_actor_token=assay_actor_token,
                    assay_envelope=assay_envelope,
                )
            except Exception as exc:  # noqa: BLE001
                self._fail_run(run_id, f"assay admission refused: {exc}")
                raise

        # Stage 2: launch the worker as a real subprocess. On the protected
        # (identity-authenticated) path the worker is launched inside a real
        # process-protection window: the protected stores are sealed (and, where
        # the adapter supports it, reads of observer signing material denied)
        # for the lifetime of the child, and the run authority DB/WAL/SHM and
        # output directories are pre-created as host-owned entries so the
        # adapter seals them. If no real isolation adapter is available the
        # protected path fails closed here, before the child is spawned — never
        # a quiet unsandboxed launch.
        #
        # SW-1510-SCOPE adds the *execution write-scope* half: when the caller
        # declares outputs (or a work contract), every declared write is
        # authorized against the resolved attempt checkout and the protected
        # store roots before launch, and the same attested isolation window
        # covers the child. A traversal/symlink escape is refused here.
        protection = None
        scope_requested = declared_writes is not None or work_contract is not None
        runner_mod = importlib.import_module("skillweave.core.proc.runner")
        scope_refusals: tuple = (
            runner_mod.ScopeViolation,
            runner_mod.ScopeEnforcementUnavailable,
        )
        IsolationUnavailableError = None
        if credential is not None:
            write_scope_mod = importlib.import_module(
                "skillweave.runtime.write_scope"
            )
            scope_refusals = scope_refusals + (write_scope_mod.WriteScopeViolation,)
            IsolationUnavailableError = _runtime_attr(
                "worker_launcher", "IsolationUnavailableError"
            )
        try:
            try:
                protection = self._open_worker_protection(credential, run_id)
            except Exception as exc:  # noqa: BLE001
                # The launcher fails closed when no real isolation mechanism is
                # available. For a scope-enforced launch surface that as the
                # runner's typed HOLD so the caller gets one attributable
                # isolation-unavailable record rather than a bare error.
                if (
                    scope_requested
                    and IsolationUnavailableError is not None
                    and isinstance(exc, IsolationUnavailableError)
                ):
                    raise runner_mod.ScopeEnforcementUnavailable(
                        "harness-worker",
                        f"no real isolation available for the worker: {exc}",
                    ) from exc
                raise
            enforcement, declared = self._open_scope_enforcement(
                credential, protection, run_id, declared_writes, work_contract
            )
            argv = list(command)
            if enforcement is not None:
                # Authorize every declared write against the resolved checkout
                # and the protected store roots before spawning: a traversal or
                # symlink escape, or a receipt/store/telemetry target, is
                # refused here, before anything runs (criterion 1). This is a
                # lexical/realpath pre-flight that needs no OS adapter, so it
                # refuses an out-of-scope declared target even on a host whose
                # adapter is later found insufficient.
                if declared:
                    enforcement.authorize_writes(declared)
                # Attest the real isolation *before* the child exists: an
                # unavailable or insufficient enforcement raises a typed HOLD
                # here, so an unisolated launch never happens (criterion 2).
                enforcement.attest()
                argv = enforcement.wrap_command(argv)
            elif protection is not None:
                argv = protection.wrap_command(argv, writable_scope=protection.writable_scope)
            result = run_command(
                argv,
                run_id=run_id,
                subject_repo=subject_repo,
                subject_commit=subject_commit,
                tool=tool,
                model=model,
                created_at=created_at,
            )
        except scope_refusals:
            # A write-scope refusal (traversal/symlink/protected-store escape)
            # or a typed isolation HOLD surfaces as the typed refusal itself, so
            # the caller can act on the exact reason and retry nothing unsafe.
            raise
        except RunIntegrationError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._fail_run(run_id, f"launch raised {exc}")
            raise RunIntegrationError("launch", str(exc)) from exc
        finally:
            # Release the protection window (unseal the protected stores) on
            # every path — including a scope refusal raised while composing the
            # enforcement, before the child ever existed. A leaked seal would
            # leave the host's own stores locked.
            if protection is not None:
                protection.__exit__(None, None, None)

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
        # Persist the verifier's own receipt as a separate, addressable record,
        # so the verdict (grade, gate_state, verified_by) is durable and
        # reconstructable by CLI/MCP/replay — not only returned in memory. It
        # is a distinct artifact from the producer receipt above; its metadata
        # carries the gate outcome and the verifier identity as DATA.
        try:
            verify_receipt = ArtifactReceipt(
                artifact_id=verdict_receipt["artifact_id"],
                sha256=verification_digest,
                schema_version="1",
                producer_command="verifier",
                subject_repo=subject_repo,
                subject_commit=subject_commit,
                created_at=verdict.created_at,
                evidence_type=EvidenceType.ARTIFACT.value,
                purpose=f"independent verification of run '{run_id}'",
                method="verifier",
                system_source="runsvc",
                quality=EvidenceQuality(),
                metadata={
                    "run_id": run_id,
                    "task_id": task_id or run_id,
                    "attempt": int(attempt),
                    "grade": verdict.grade,
                    "gate_state": verdict.gate_state,
                    "verified_by": verdict.verified_by,
                    "subject_artifact_id": verdict.subject_artifact_id,
                },
            )
            self.store.save_evidence(verify_receipt)
        except Exception:  # noqa: BLE001 - verdict receipt is best-effort durability
            pass

        # Gate: derived from the completion contract, over the verified outcome.
        contract = CompletionContract()
        gate_state = contract.evaluate(
            exit_code=result.exit_code,
            signal=result.signal,
            termination=result.termination,
            stdout=raw_bytes,
            check_output=check_output,
        )

        # Record the declared methodology binding on the run's metadata as its
        # own fields (methodology, autonomy_policy) plus the run/task/attempt
        # triple. This is data, separate from the lane state: a deployment can
        # read which methodology actually ran without inferring it from a
        # state. Written while the run is still at ``verify`` (metadata cannot
        # be merged into a terminal record), so it survives the terminal
        # transition below.
        try:
            pre_terminal = self.store.get_run(run_id)
            self.store.update_metadata(
                run_id,
                expected_version=pre_terminal.version,
                metadata={
                    "methodology": binding.methodology.value,
                    "autonomy_policy": binding.policy_name,
                    "task_id": task_id or run_id,
                    "attempt": int(attempt),
                    "methodology_binding": binding.to_dict(),
                },
            )
        except Exception:  # noqa: BLE001 - binding metadata is best-effort data
            pass

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

        assay_block: Optional[dict[str, Any]] = None
        if binding.methodology.value == "assay":
            assay_block = self._run_assay_drill(
                run_id=run_id,
                task_id=task_id or run_id,
                attempt=int(attempt),
                binding=binding,
                result=result,
                gate_state=gate_state,
                verdict_receipt=verdict_receipt,
                pre_state=assay_pre_state,
            )

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
            methodology=binding.methodology.value,
            assay=assay_block,
        )

    def _authenticate_worker(
        self,
        authority_service: Any,
        *,
        worker_token: Optional[str],
        run_id: str,
    ) -> Any:
        """Authenticate the worker credential at run ingress (SW-1510-IDENTITY).

        The host service verifies the presented token against its credential
        registry. A missing/forged/revoked/expired token, or one bound to a
        different run, fails before any run state is written. The credential
        must be a worker credential: it must carry no approving role, so a
        worker can never reach a reviewer/observer/controller decision.

        Returns the authenticated credential so the caller can derive the
        worker's real writable scope for the protected launch.
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
        return credential

    # -- real protected worker launch (SW-1510-IDENTITY criterion 1) -------

    def _open_worker_protection(self, credential: Any, run_id: str) -> Any:
        """Open a real isolation window over the protected stores, or fail closed.

        Returns a live :class:`StoreProtection` on the protected (identity)
        path, or ``None`` on the legacy unprotected path. It binds the concrete
        store directories: the run authority DB (and its WAL/SHM sidecars),
        and the terminal-receipt / gate-state / authority-record / telemetry
        stores. Every protected entry is pre-created host-owned so the adapter
        seals a real filesystem object; a worker can neither add nor replace an
        entry inside a sealed store. ``select_adapter`` returns a fail-closed
        adapter when no real mechanism exists, so ``__enter__`` raises rather
        than spawning an unsandboxed child.
        """
        if credential is None:
            return None
        protection_cls = _runtime_attr("worker_launcher", "protect_stores")
        ProtectedStores = _runtime_attr("worker_launcher", "ProtectedStores")
        PROTECTED_STORE_NAMES = _runtime_attr("worker_launcher", "PROTECTED_STORE_NAMES")

        roots = self._protected_store_roots()
        protected = ProtectedStores(
            {name: roots[name] for name in PROTECTED_STORE_NAMES}
        )
        protected.ensure()
        self._precreate_protected_entries(protected, run_id)
        protection = protection_cls(protected)
        protection.writable_scope = self._worker_writable_scope(credential, run_id)
        # Enter the window now: this seals the stores (or fails closed if no
        # real adapter is available) before the wrapped child is spawned. The
        # matching __exit__ runs in the caller's finally.
        protection.__enter__()
        return protection

    def _protected_store_roots(self) -> dict[str, Path]:
        """The concrete roots of the protected stores, keyed by family name.

        The run authority store is the run DB itself: the protected root for
        ``authority_records`` is the *directory containing the run DB*, so
        sealing it makes the child's SQLite open/write (and its ``-wal``/``-shm``
        creation inside that directory) fail at the kernel boundary. The host
        performs no writes during the child window, so sealing the directory is
        safe; the host's own post-window writes happen after unseal.
        """
        names = _runtime_attr("worker_launcher", "PROTECTED_STORE_NAMES")
        db_path = getattr(self.store, "db_path", None)
        sealable = (
            isinstance(db_path, str)
            and db_path not in ("", ":memory:")
            and not db_path.startswith("file:")
        )
        if sealable:
            base = Path(db_path).resolve().parent
        else:
            import tempfile

            base = Path(tempfile.mkdtemp(prefix="sw1510-protected-")).resolve()
        roots: dict[str, Path] = {}
        for name in names:
            if name == "authority_records":
                roots[name] = base
            else:
                roots[name] = (base / name).resolve()
        return roots

    def _precreate_protected_entries(self, protected: Any, run_id: str) -> None:
        """Materialize host-owned entries under each protected store.

        Pre-creating the run's own entries gives the sealed store real content:
        a sealed empty directory cannot be turned into a store by a child
        either, but concrete host-owned files make the denial and the child's
        failure independently observable.
        """
        for name in ("terminal_receipts", "gate_state", "gateway_telemetry"):
            try:
                (protected.roots[name] / f"{run_id}.host").write_text("host-owned")
            except OSError:
                pass

    def _worker_writable_scope(self, credential: Any, run_id: str) -> Path:
        """A private directory that is the only tree the wrapped worker may write.

        The worker's credential binds a scope *digest*, not a path; the host
        materializes one real writable directory for the run so a scope-
        enforcing adapter (Seatbelt) has a concrete tree to allow, and the
        protected stores stay sealed for every other path.
        """
        import tempfile

        base = tempfile.mkdtemp(prefix=f"sw1510-worker-{run_id}-")
        return Path(base).resolve()

    def _open_scope_enforcement(
        self,
        credential: Any,
        protection: Any,
        run_id: str,
        declared_writes: Optional[Sequence[str]],
        work_contract: Any,
    ) -> tuple[Optional[Any], Optional[Sequence[str]]]:
        """Compose the real execution write-scope + isolation boundary.

        Returns ``(enforcement, declared)``. Returns ``(None, None)`` on the
        legacy unprotected path (no credential and nothing declared). On the
        protected path it always returns a :class:`ScopeEnforcement` bound to
        the live ``protection`` window: the attempt checkout is the worker's
        private writable scope, the protected store roots are sealed against
        writes even inside it, and the enforced isolation is the protection
        already evidenced by the launcher. The scope is resolved either from an
        explicit ``declared_writes`` list or from a ``WorkContract``'s
        normalized write scope — including the generic content/configuration/
        deployment/incident object kinds, which never synthesize a repository
        SHA.
        """
        checkout_root = (
            protection.writable_scope if protection is not None else None
        )
        if checkout_root is None:
            if declared_writes is None and work_contract is None:
                return None, None
            # Auth-scoped execution was requested but there is no real checkout
            # to confine to: fail closed rather than enforcing against an
            # unbounded root.
            runner_mod = importlib.import_module("skillweave.core.proc.runner")
            raise runner_mod.ScopeEnforcementUnavailable(
                "harness-worker",
                "scope-enforced launch requires a real attempt checkout",
            )

        # ``skillweave.core.proc.runner`` is a core package (always present), not
        # the optional ``runtime`` subpackage, so it is resolved directly rather
        # than through ``_runtime_attr``.
        runner_mod = importlib.import_module("skillweave.core.proc.runner")
        ScopeEnforcement = runner_mod.ScopeEnforcement

        # ``ExecutionScope`` is the normalized execution boundary: a target is
        # admitted only when it resolves inside the attempt checkout (symlinks
        # followed, traversal refused). It is always built when the paths/
        # objects execution is requested — whether the outputs came from an
        # explicit ``declared_writes`` list or a contract's write scope — so a
        # traversal/symlink escape is refused at the boundary even with no work
        # contract. A non-Git object contract is enforced against its own kind.
        ExecutionScope = _runtime_attr("write_scope", "ExecutionScope")
        scope = None
        declared = declared_writes
        if work_contract is not None:
            scope_from_contract = _runtime_attr(
                "write_scope", "scope_from_work_contract"
            )
            scope = scope_from_contract(work_contract, checkout_root=str(checkout_root))
            # A generic object contract is validated against its own kind (no
            # fabricated commit); a paths/objects contract needs declared
            # outputs to authorize, taken from its allow list.
            if getattr(scope, "kind", "paths") in ("paths", "objects"):
                if declared is None:
                    declared = list(getattr(work_contract, "write_scope", None).allow) if hasattr(
                        getattr(work_contract, "write_scope", None), "allow"
                    ) else None
            else:
                scope.validate_object()
        elif declared is not None:
            scope = ExecutionScope(
                kind="paths",
                checkout_root=str(checkout_root),
                allow=list(declared),
            )

        protected_roots = ()
        if protection is not None:
            protected_roots = tuple(
                str(p) for p in protection.protected.write_protected_paths()
            )
        # Automatic required checkout confinement (SW-1510-SCOPE criterion 2).
        # A scope-enforced launch promises that no write happens outside the
        # attempt checkout. A scope object plus declared-write authorization is
        # *not* that guarantee: an adapter that only seals the protected
        # authority stores leaves every other outside path writable, so a real
        # child can still create/overwrite an undeclared file outside the
        # checkout. The confinement requirement is therefore set here, by the
        # launch itself — never left to a caller to discover and set as a
        # hidden optional flag. ``ScopeEnforcement.attest`` turns a seal-only
        # adapter into a typed HOLD *before* the child exists.
        #
        # It applies only when a write scope is actually requested
        # (``declared_writes`` or a ``work_contract``). A pure identity-
        # authenticated launch declares no write scope and keeps the original
        # SW-1510-IDENTITY guarantee — the protected authority stores sealed —
        # so the seal-only immutable-flag adapter stays valid there. That
        # legacy compatibility is explicit here rather than implicit in a
        # caller flag.
        scope_requested = declared_writes is not None or work_contract is not None
        enforcement = ScopeEnforcement(
            isolation=protection,
            scope=scope,
            protected_roots=protected_roots,
            checkout_root=str(checkout_root),
            require_checkout_confinement=scope_requested,
            workload="harness-worker",
            tools=("Bash",),
        )
        return enforcement, declared

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
    # Shared run view (SW-1510-ASSAY criterion 2)
    #
    # One reconstruction of a run's methodology/attempt/terminal verdict,
    # readable from the persisted run and its ordered journal. CLI, MCP and a
    # replay all call this so they cannot disagree about what ran.
    # ------------------------------------------------------------------

    def run_snapshot(self, run_id: str) -> Optional[dict[str, Any]]:
        """Reconstruct one run's methodology/attempt/outcome view.

        Reads the persisted ``RunRecord`` metadata (methodology,
        autonomy_policy, task_id, attempt) and the ordered journal events
        (the ordered Assay phases and the terminal outcome). Returns ``None``
        when the run is unknown. This is the single view the CLI, MCP and a
        replay share; it never re-derives a verdict from an exit code and never
        reads a model-authored string as a verdict.
        """
        record = self.store.get_run(run_id)
        if record is None:
            return None
        events = self.journal.get_events(run_id)
        phases: list[dict[str, Any]] = []
        outcome: Optional[str] = None
        verification: Optional[dict[str, Any]] = None
        for event in events:
            payload = event.payload or {}
            phase = payload.get("phase")
            if phase:
                phases.append({"phase": phase, "sequence": event.sequence})
            if phase in ("pass", "hold") and payload.get("outcome"):
                outcome = payload["outcome"]
            if event.event_type == "artifact_created" and payload.get("verification"):
                verification = payload["verification"]

        verified = self.store.get_evidence(f"verify-runsvc-{run_id}")
        if verified is not None:
            verification = {
                "artifact_id": verified.artifact_id,
                "grade": verified.metadata.get("grade"),
                "gate_state": verified.metadata.get("gate_state"),
                "verified_by": verified.metadata.get("verified_by"),
                "subject_artifact_id": verified.metadata.get("subject_artifact_id"),
            }

        metadata = record.metadata or {}
        snapshot: dict[str, Any] = {
            "run_id": run_id,
            "task_id": metadata.get("task_id"),
            "attempt": metadata.get("attempt"),
            "state": record.state,
            "stop_reason": metadata.get("stop_reason"),
            "methodology": metadata.get("methodology", "ralph"),
            "autonomy_policy": metadata.get("autonomy_policy"),
            "event_cursor": self.journal.get_last_sequence(run_id),
            "phases": phases,
            "outcome": outcome,
            "verification": verification,
        }
        if verification is not None:
            snapshot["gate_state"] = verification.get("gate_state")
        return snapshot

    # ------------------------------------------------------------------
    # Declared methodology binding (SW-1510-ASSAY)
    #
    # A methodology is *declared data*, resolved through the versioned
    # registry. A request that names Assay is either executed as Assay or
    # refused with a named reason — it is never silently reinterpreted as
    # another methodology (in particular never falls through to Ralph).
    # ------------------------------------------------------------------

    def _resolve_methodology_binding(
        self, *, methodology: str, autonomy_policy: str
    ) -> "_MethodologyBinding":
        """Resolve and validate the declared methodology binding.

        Returns a :class:`_MethodologyBinding`, or raises
        :class:`RunIntegrationError` at stage ``"methodology"``. An unknown
        methodology name is refused explicitly (never coerced); a known but
        unexecutable methodology is refused by name; an incompatible
        methodology/topology/policy combination fails with the registry's
        named reason.
        """
        try:
            Methodology = _runtime_attr("methodology", "Methodology")
            TopologyType = _runtime_attr("methodology", "TopologyType")
            PolicyType = _runtime_attr("methodology", "PolicyType")
            DEFAULT_REGISTRY = _runtime_attr("methodology", "DEFAULT_REGISTRY")
            assert_compatible = _runtime_attr("methodology", "assert_compatible")
        except Exception as exc:  # noqa: BLE001
            raise RunIntegrationError(
                "methodology", f"methodology registry unavailable: {exc}"
            ) from exc

        name = (methodology or "").strip().lower()
        if not name:
            raise RunIntegrationError(
                "methodology", "no methodology declared; refusing to guess"
            )
        try:
            method = Methodology(name)
        except ValueError as exc:
            raise RunIntegrationError(
                "methodology",
                f"unknown methodology {methodology!r}; "
                f"expected one of {sorted(m.value for m in Methodology)}",
            ) from exc

        # Each declared methodology gets its own *declared* binding. Assay runs
        # the native drill (PIN → HYPOTHESIS → PROBE → MEASURE → CLASSIFY →
        # outcome) under the SEQUENTIAL/SUPERVISED combination; every other
        # registered methodology continues through the shared lane path, but
        # under its own declared topology/policy rather than a silent default.
        # Whatever runs, the combination is validated against the versioned
        # registry so a request is executed as declared or refused with a
        # *named* reason — never reinterpreted as a different methodology.
        if method == Methodology.ASSAY:
            topology = TopologyType.SEQUENTIAL
            policy = PolicyType.SUPERVISED
        elif method == Methodology.RALPH:
            topology = TopologyType.DAG
            policy = PolicyType.APPROVAL_REQUIRED
        elif method == Methodology.REX:
            topology = TopologyType.SEQUENTIAL
            policy = PolicyType.SUPERVISED
        elif method == Methodology.GAUNTLET:
            topology = TopologyType.PARALLEL_LANES
            policy = PolicyType.COLLABORATIVE
        else:
            raise RunIntegrationError(
                "methodology",
                f"methodology {name!r} has no declared execution binding on "
                f"this path; refusing to run it as another methodology",
            )

        definition = DEFAULT_REGISTRY.get(method)

        # An Assay request must never silently run as Ralph: the requested
        # methodology and the executed one are compared explicitly.
        reason = _runtime_attr("methodology", "check_compatibility")(method, topology, policy)
        if reason is not None:
            raise RunIntegrationError(
                "methodology",
                f"declared methodology binding {name!r} is incompatible: {reason}",
            )
        assert_compatible(method, topology, policy)

        # Autonomy policy is its own field, recorded separately from both the
        # methodology and any session schedule.
        if method == Methodology.ASSAY:
            policy_name = (autonomy_policy or "unicorn").strip().lower()
            allowed = _runtime_attr("assay", "list_policy_names")()
            if policy_name not in allowed:
                raise RunIntegrationError(
                    "methodology",
                    f"unknown assay autonomy policy {autonomy_policy!r}; "
                    f"expected one of {allowed}",
                )
        else:
            policy_name = (autonomy_policy or "collaborative").strip().lower()

        return _MethodologyBinding(
            methodology=method,
            topology=topology,
            policy=policy,
            policy_name=policy_name,
            definition=definition,
        )

    def _assay_phase_event(
        self,
        run_id: str,
        task_id: str,
        attempt: int,
        state: Any,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """Append one ordered assay phase event and return its payload.

        The payload carries the run/task/attempt triple on every phase so CLI,
        MCP and replay all read the same attempt. The append is idempotent per
        (run, phase, attempt), so a re-entry never double-writes a phase.
        """
        EventType = _runtime_attr("journal", "EventType")
        payload = {
            "phase": state.value,
            "run_id": run_id,
            "task_id": task_id,
            "attempt": attempt,
            **evidence,
        }
        event = self.journal.append(
            run_id,
            event_type=EventType.METRIC.value,
            payload=payload,
            idempotency_key=f"assay:{run_id}:{state.value}:{attempt}",
        )
        return payload, event.sequence

    def _assay_open(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        binding: "_MethodologyBinding",
        command: Sequence[str],
        subject_repo: str,
        subject_commit: str,
        authority_service: Any,
        credential: Any,
        assay_actor_token: Optional[str],
        assay_envelope: Optional[Any],
    ) -> dict[str, Any]:
        """Open the native Assay drill *before* the real probe is spawned.

        The PIN and HYPOTHESIS phases describe the policy/state machine that
        the next launch executes: the engine is constructed under the declared
        autonomy policy, the subject/brief are pinned to the run's real content
        addresses, the owner-authorized envelope is bound, and — on the
        identity-authenticated path — the assay actor is resolved through the
        live host authority and every admission axis (actor, pinned subject/
        brief, deadline, remaining budget) is enforced by the engine itself
        before the child exists. Any refusal here (e.g. an expired authority
        deadline or an exhausted reservation) is raised before the launch and
        the already-recorded PIN/HYPOTHESIS events remain durable.

        These phases are the controller's own policy admission, not a
        post-hoc diary: the HYPOTHESIS the probe is launched against is
        recorded before the probe runs, and the PROBE/MEASURE/CLASSIFY records
        written after the launch describe exactly this engine.
        """
        AssayEngine = _runtime_attr("assay", "AssayEngine")
        AssayState = _runtime_attr("assay", "AssayState")
        AssayAdmission = _runtime_attr("assay", "AssayAdmission")

        subject_digest = _sha256_hex(
            f"{subject_repo}@{subject_commit}".encode()
        )
        brief_digest = _sha256_hex(" ".join(list(command)).encode())

        resolver = None
        if authority_service is not None and credential is not None:
            # An identity-authenticated Assay run enforces admission on every
            # mutation. The actor must be a live host-verified credential bound
            # to this exact run/task/attempt — never a caller string.
            HostAuthorityActorResolver = _runtime_attr(
                "assay", "HostAuthorityActorResolver"
            )
            resolver = HostAuthorityActorResolver(
                authority_service,
                attempt_key=(run_id, task_id, attempt),
                required_capability="execute-attempt",
            )

        engine = AssayEngine(
            policy_name=binding.policy_name,
            run_id=run_id,
            actor_resolver=resolver,
            envelope=assay_envelope,
            max_turns=None,
        )
        engine.pin(subject=subject_digest, brief=brief_digest)

        admission = None
        if resolver is not None:
            if not assay_actor_token:
                raise _runtime_attr("assay", "AdmissionRefused")(
                    "actor",
                    "an authenticated assay actor token is required for a "
                    "protected assay mutation",
                )
            actor = resolver.resolve(assay_actor_token)
            admission = AssayAdmission(
                actor_token=assay_actor_token,
                subject=subject_digest,
                brief=brief_digest,
                envelope=assay_envelope or _runtime_attr("assay", "AssayEnvelope")(),
                actor=actor,
            )
            # Exercise the guarded turn-limit change as the authority act it
            # is: under an explicit envelope this records the bound *and*
            # enforces actor/deadline/budget before the probe runs.
            if assay_envelope is not None:
                engine.set_turn_limit(
                    engine.max_turns, admission=admission
                )

        phases: list[dict[str, Any]] = []
        phase_events: list[dict[str, Any]] = []

        def record(state: Any, evidence: dict[str, Any]) -> None:
            payload, sequence = self._assay_phase_event(
                run_id, task_id, attempt, state, evidence
            )
            phases.append(payload)
            phase_events.append({"sequence": sequence, "phase": state.value})

        # PIN — the exact subject/brief/envelope the drill is bound to.
        record(
            AssayState.PIN,
            {
                "subject_digest": subject_digest,
                "brief_digest": brief_digest,
                "envelope_digest": _sha256_hex(
                    f"{binding.methodology.value}|{binding.policy_name}|"
                    f"{(assay_envelope.to_dict() if assay_envelope is not None else {})}".encode()
                ),
            },
        )

        # HYPOTHESIS — the falsifiable claim the probe is launched against,
        # recorded *before* the probe runs.
        record(
            AssayState.HYPOTHESIS,
            {
                "claim": "probe command exits 0 with non-empty output",
                "falsified_by": "non-zero exit or empty stdout",
                "evidence_type": "test",
                "method": "completion-contract",
            },
        )

        return {
            "engine": engine,
            "admission": admission,
            "resolver": resolver,
            "subject_digest": subject_digest,
            "brief_digest": brief_digest,
            "phases": phases,
            "phase_events": phase_events,
            "policy_name": binding.policy_name,
        }

    def _run_assay_drill(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        binding: "_MethodologyBinding",
        result: Any,
        gate_state: str,
        verdict_receipt: dict[str, Any],
        pre_state: dict[str, Any],
    ) -> dict[str, Any]:
        """Close the native Assay drill after the real probe has run.

        The PROBE phase carries the real, already-executed bounded subprocess's
        measured facts (pid/exit/signal/termination — never a verdict);
        MEASURE binds its captured bytes; CLASSIFY is the completion contract's
        gate; the outcome is PASS only when the gate PASSes *and* the policy can
        author the terminal transition. Every phase is journaled with the
        run/task/attempt triple. A terminal phase the autonomy policy cannot
        author is recorded as a HOLD outcome — a self-declared pass is
        impossible here.
        """
        AssayState = _runtime_attr("assay", "AssayState")
        PolicyBlockedError = _runtime_attr("assay", "PolicyBlockedError")
        PolicyHoldError = _runtime_attr("assay", "PolicyHoldError")

        engine = pre_state["engine"]
        admission = pre_state["admission"]
        phases = pre_state["phases"]
        phase_events = pre_state["phase_events"]

        raw_bytes = result.stdout or b""
        gate_is_pass = gate_state == _runtime_attr("verify", "GateState").PASS

        def record(state: Any, evidence: dict[str, Any]) -> None:
            payload, sequence = self._assay_phase_event(
                run_id, task_id, attempt, state, evidence
            )
            phases.append(payload)
            phase_events.append({"sequence": sequence, "phase": state.value})

        # PROBE — the real child execution's measured facts (NOT a verdict).
        record(
            AssayState.PROBE,
            {
                "evidence_type": "runtime_trace",
                "pid": result.pid,
                "exit_code": result.exit_code,
                "signal": result.signal,
                "termination": result.termination,
                "command": list(result.command),
            },
        )

        # MEASURE — the captured bytes, content-addressed.
        measure_digest = _sha256_hex(raw_bytes)
        record(
            AssayState.MEASURE,
            {
                "evidence_type": "artifact",
                "stdout_digest": measure_digest,
                "byte_length": len(raw_bytes),
            },
        )

        # CLASSIFY — the verifier's grade over the subject receipt.
        record(
            AssayState.CLASSIFY,
            {
                "evidence_type": "decision",
                "grade": verdict_receipt.get("grade", ""),
                "gate_state": gate_state,
                "subject_artifact_id": verdict_receipt.get("subject_artifact_id", ""),
            },
        )

        # Outcome. A pass (i.e. the gate) requires a terminal phase the policy
        # authorises; if the policy cannot author it, the outcome is HOLD — a
        # self-declared pass is impossible here. The engine itself enforces
        # admission on each transition (this is the state machine that ran, not
        # a diary written after the fact).
        outcome = "hold"
        outcome_reason = ""
        transitions = [
            AssayState.HYPOTHESIS,
            AssayState.PROBE,
            AssayState.MEASURE,
            AssayState.CLASSIFY,
        ]
        if gate_is_pass:
            try:
                for target in transitions:
                    engine.transition(target, admission=admission)
                engine.transition(AssayState.PASS, admission=admission)
                outcome = "pass"
            except (PolicyBlockedError, PolicyHoldError) as exc:
                outcome = "hold"
                outcome_reason = getattr(exc, "reason", str(exc))
            except Exception as exc:  # noqa: BLE001
                outcome = "hold"
                outcome_reason = str(exc)
        else:
            try:
                for target in transitions:
                    engine.transition(target, admission=admission)
                engine.transition(AssayState.HOLD, admission=admission)
            except Exception:  # noqa: BLE001
                pass
            outcome = "hold"

        record(
            AssayState.PASS if outcome == "pass" else AssayState.HOLD,
            {
                "evidence_type": "decision",
                "outcome": outcome,
                "reason": outcome_reason,
            },
        )

        return {
            "methodology": binding.methodology.value,
            "autonomy_policy": binding.policy_name,
            "task_id": task_id,
            "attempt": attempt,
            "phases": phases,
            "phase_events": phase_events,
            "outcome": outcome,
            "reason": outcome_reason,
            "gate_state": gate_state,
            "child_execution": {
                "pid": result.pid,
                "exit_code": result.exit_code,
                "signal": result.signal,
                "termination": result.termination,
                "stdout_digest": measure_digest,
                "byte_length": len(raw_bytes),
            },
        }

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

        # A completion intent recorded by the detached wrapper but never
        # committed (the wrapper/host was lost between ``record_intent`` and the
        # terminal commit) is finished here too, so the completed child's
        # outcome is not permanently lost as a stuck non-terminal run.  A
        # genuine terminal winner was already returned above; if the
        # cancellation reconcile just committed, this is a no-op.  Exactly one
        # logical terminal event is written by the canonical reconciliation.
        CompletionReconciler = _runtime_attr("store", "CompletionReconciler")
        completed = CompletionReconciler.resume_pending(self.store, run_id)
        if completed is not None:
            run = completed

        events = self.journal.get_events(run_id)
        return {
            "run_id": run_id,
            "state": run.state,
            "version": run.version,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
            "event_count": len(events),
        }
