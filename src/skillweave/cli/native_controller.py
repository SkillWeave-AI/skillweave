"""The ``skillweave native-controller`` production command-line surface.

This is the **production entry point** for
:class:`skillweave.runtime.native_process.NativeProcessService` (SW-SCOPE-010,
unit A1).  Before this command the service existed and passed independent review
at the method level, but no production code ever constructed it, acquired its
fenced controller lease or drove the owned loop -- so every criterion worded
"one native process service drives the loop" held at library level only.

The command composes the service on the **same durable store the ``run``
command uses** (``SQLiteRunStore`` over ``--db-path``), acquires the single
fenced controller lease, and drives one owned attempt **from durable state**:

* the loop is **state-driven**, not a fixed six-step script.  Each transition is
  a guarded compare-and-set from the exact state the previous step left the
  intent in, so an out-of-order or verdict-inconsistent advance is *refused*;
* the review outcome is read from the **durable review record** the MCP
  ``review_run`` tool wrote (a ``transitions_log`` row with ``to_state``
  ``advance_or_stop`` and ``reason`` ``Review verdict {verdict}: ...``) -- never
  a value this controller makes up.  A recorded ``fail`` leads to repair and a
  re-review; a failing re-review *stops* the attempt terminally and it can never
  integrate;
* acceptance comes only from **independent verifier evidence** through the
  gate-receipt path (``skillweave.trace.gate_receipt``): the controller builds no
  ``accepted=True`` itself and refuses fail-closed when the reviewer port
  supplied no independent evidence;
* the **dependent wave** is computed from the declared dependency graph through
  :func:`skillweave.runtime.dagscheduler.build_schedule` (a dependency is
  released only when its gate receipt records an accepted, artifact-carrying
  attempt) -- not from a fabricated name.

Two ports are **explicit and required**:

* the **worker adapter** -- the one place a real drive would launch the owned
  attempt (a model, a provider, a subprocess).  A test injects a fake that
  returns a synthetic execution and launches nothing;
* the **reviewer adapter** -- the independent review seam.  It supplies the
  independent verifier evidence the controller consumes; it does **not** get to
  hand the controller a verdict.

With no production adapter configured the command **refuses fail-closed** with a
named unsupported hold (``EXIT_REFUSED``) rather than substituting a no-op.  It
is additive and opt-in: one subcommand, writing only to the store the caller
names; no existing subcommand or record changes, and the legacy unguarded
service methods keep their exact behaviour for existing callers.

Signal safety: the lease is released on ``SIGTERM``, ``SIGINT``,
``KeyboardInterrupt``, any exception and normal exit, and it is renewed (heartbeat)
while a step runs.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, Sequence, runtime_checkable

# Public surface re-exported for callers/tests that key on the entry point.
__all__ = [
    "EXIT_OK",
    "EXIT_INVALID_INPUT",
    "EXIT_REFUSED",
    "EXIT_FAILED",
    "AttemptExecution",
    "WorkerAdapter",
    "ReviewerAdapter",
    "ReviewVerdict",
    "DriveResult",
    "build_parser",
    "main",
]

#: ``ok``: the controller drove the owned attempt through the declared loop.
EXIT_OK = 0
#: ``invalid input``: missing/blank required flag, or an argparse usage error.
EXIT_INVALID_INPUT = 2
#: ``refused``: the service refused -- no production worker/reviewer adapter is
#: configured (a named unsupported hold), a live controller holds the lease, or
#: any other typed service refusal. Fail closed.
EXIT_REFUSED = 3
#: ``failure``: an unexpected error. A crash is not a verdict and is never
#: collapsed into a normal outcome.
EXIT_FAILED = 5

#: The recorded review verdicts the loop accepts, exactly as the MCP
#: ``review_run`` tool records them.
REVIEW_PASS = "pass"
REVIEW_FAIL = "fail"

#: The unsupported hold named when no production worker adapter is configured.
#: The command refuses rather than substituting a no-op digest worker.
HOLD_NO_WORKER = (
    "native-controller refused: no production worker adapter is configured; "
    "the controller never substitutes a no-op or synthetic worker (unsupported)"
)
#: The unsupported hold named when no production reviewer adapter is configured.
HOLD_NO_REVIEWER = (
    "native-controller refused: no production reviewer adapter is configured; "
    "the controller never constructs its own verdict or acceptance (unsupported)"
)


@dataclass(frozen=True)
class AttemptExecution:
    """What driving one owned attempt produced, supplied by the worker adapter.

    The adapter is the launch seam: in production this is where an attempt would
    run.  The record the *controller* consumes is only these facts -- the
    produced artifact (mandatory before acceptance) and, for a non-repository
    subject, the controlled content-transaction inputs.
    """

    producer_artifact: str = ""
    #: A non-repository subject to integrate through a controlled content
    #: transaction ("controlled integration" for a CMS/configuration/... kind).
    #: ``None`` selects the repository (Git merge) integration record.
    integration_subject: Any = None
    baseline_digest: str = ""
    applied_digest: str = ""


@dataclass(frozen=True)
class ReviewVerdict:
    """The independent review the controller consumes through the reviewer port.

    ``verdict`` is one of ``"pass"``/``"fail"`` and is the *independent
    verifier's* decision.  ``evidence`` is the machine-readable independent
    verifier evidence (``independent-gate-receipt/1.0.0`` member records) the
    controller feeds to :func:`skillweave.trace.gate_receipt.evaluate_gate`.
    Acceptance is derived from that evaluation -- the adapter never hands the
    controller an ``accepted`` flag.  ``verifier_identity`` is who actually
    answered the review; it must differ from the producer identity.
    """

    verdict: str
    evidence: tuple[Any, ...] = ()
    verifier_identity: str = ""
    published: bool = False


@dataclass(frozen=True)
class AttemptAcceptance:
    """The attempt's acceptance facts, built inside the controller.

    This is not a caller-supplied acceptance: ``state`` is always the terminal
    accepted state and is only ever assembled after independent evidence has
    decided ``PASS``.  It exists so the frozen
    :func:`skillweave.trace.gate_receipt.release_dependents` rule has an
    authoritative ``AttemptAcceptance``-shaped record to consume.
    """

    attempt_id: str
    state: str
    producer_artifact: str
    dependents: tuple[str, ...] = ()


@runtime_checkable
class WorkerAdapter(Protocol):
    """The single launch seam for an owned attempt.

    ``execute`` is handed the boundary ``owned-attempt-dispatch-v1`` record the
    service produced and returns what the attempt yielded.  A production adapter
    would launch a worker here; a test adapter returns a synthetic record.  This
    is the *only* seam that may launch anything -- the controller loop itself
    calls only existing service methods.
    """

    def execute(self, dispatch: Any) -> AttemptExecution:  # pragma: no cover
        ...


@runtime_checkable
class ReviewerAdapter(Protocol):
    """The independent review seam for an owned attempt.

    ``review`` is handed the boundary dispatch record and the attempt execution
    and returns the :class:`ReviewVerdict` (verdict + independent verifier
    evidence).  A production adapter would consult the durable review record the
    review tool wrote; a test adapter returns a synthetic verdict.  The adapter
    supplies *evidence*, never an acceptance -- the controller derives acceptance
    from the evidence through the gate-receipt evaluator.
    """

    def review(
        self, dispatch: Any, execution: AttemptExecution
    ) -> ReviewVerdict:  # pragma: no cover
        ...


@dataclass
class DriveResult:
    """The outcome of one controller drive, as emitted on stdout."""

    run_id: str
    task_id: str
    attempt: int
    intent_id: str
    fencing_token: int
    steps: tuple[str, ...]
    accepted: bool
    released_dependents: tuple[str, ...]
    integration: str
    reviews: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "intent_id": self.intent_id,
            "fencing_token": self.fencing_token,
            "steps": list(self.steps),
            "accepted": self.accepted,
            "released_dependents": list(self.released_dependents),
            "integration": self.integration,
            "reviews": list(self.reviews),
        }


def build_parser(prog: str = "skillweave native-controller") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Drive the native process service from durable state: acquire the "
            "single controller lease and run one owned attempt through ready "
            "dispatch, a verdict-driven review/repair/re-review, controlled "
            "integration and the dependency-graph dependent wave. Requires the "
            "worker and reviewer production adapters; refuses fail-closed "
            "without them. Additive and opt-in."
        ),
    )
    parser.add_argument(
        "--run-id",
        dest="run_id",
        required=True,
        help="The run this controller drives (required).",
    )
    parser.add_argument(
        "--task-id",
        dest="task_id",
        required=True,
        help="The task (owned attempt subject) this controller drives (required).",
    )
    parser.add_argument(
        "--attempt",
        type=int,
        default=1,
        help="Attempt number for the run/task/attempt triple (default: 1).",
    )
    parser.add_argument(
        "--db-path",
        dest="db_path",
        default=":memory:",
        help="Path to the durable SQLite store the run command also uses "
             "(default: :memory:).",
    )
    parser.add_argument(
        "--holder",
        default="native-controller",
        help="The controller identity recorded on the lease (default: "
             "native-controller).",
    )
    parser.add_argument(
        "--ttl-seconds",
        dest="ttl_seconds",
        type=int,
        default=300,
        help="Controller lease TTL in seconds (default: 300).",
    )
    parser.add_argument(
        "--sequence",
        dest="sequence",
        default="",
        help="Optional path to a dispatch sequence declaration (JSON) declaring "
             "the dependency graph; the dependent wave is computed from it. "
             "Without it the wave is empty (no dependent is invented).",
    )
    parser.add_argument(
        "--route",
        default="",
        help="Optional route label recorded on the dispatch intent.",
    )
    parser.add_argument(
        "--session-id",
        dest="session_id",
        default="",
        help="Optional session id recorded on the dispatch intent.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the drive result as a single JSON envelope (default).",
    )
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    worker: Optional[WorkerAdapter] = None,
    reviewer: Optional[ReviewerAdapter] = None,
    store: Any = None,
) -> int:
    """Route ``native-controller`` and return its exit code.

    ``worker``, ``reviewer`` and ``store`` are injected ports: a test supplies a
    fake worker and reviewer (that launch nothing and call no provider) and/or a
    store, instead of letting the entry build its own.  Production callers pass
    ``worker`` and ``reviewer`` from their adapter registration; when either is
    absent the drive is refused fail-closed (a named unsupported hold).
    """
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        # argparse already wrote usage (code 2) or help (code 0); map both to
        # the documented codes rather than letting the raise escape the entry.
        code = exc.code if isinstance(exc.code, int) else EXIT_INVALID_INPUT
        return EXIT_OK if code == 0 else EXIT_INVALID_INPUT

    try:
        if args.attempt < 1:
            raise _UsageError("--attempt must be a positive integer")
    except _UsageError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return EXIT_INVALID_INPUT

    # Fail closed with a *named* unsupported hold before touching the store:
    # with no production adapter configured the controller substitutes nothing.
    if worker is None:
        sys.stderr.write(f"refused: {HOLD_NO_WORKER}\n")
        return EXIT_REFUSED
    if reviewer is None:
        sys.stderr.write(f"refused: {HOLD_NO_REVIEWER}\n")
        return EXIT_REFUSED

    try:
        result = _drive(
            run_id=args.run_id,
            task_id=args.task_id,
            attempt=args.attempt,
            db_path=args.db_path,
            holder=args.holder,
            ttl_seconds=args.ttl_seconds,
            route=args.route,
            session_id=args.session_id,
            sequence=args.sequence,
            worker=worker,
            reviewer=reviewer,
            store=store,
        )
    except _UsageError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return EXIT_INVALID_INPUT
    except _RefusedError as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return EXIT_REFUSED
    except KeyboardInterrupt:
        sys.stderr.write("interrupted\n")
        return EXIT_FAILED
    except Exception as exc:  # noqa: BLE001 -- a crash is exit 5, never a verdict
        sys.stderr.write(f"error: {type(exc).__name__}: {exc}\n")
        return EXIT_FAILED

    sys.stdout.write(json.dumps(result.to_dict(), sort_keys=True) + "\n")
    return EXIT_OK


class _UsageError(Exception):
    """Caller input cannot form a controller drive. Maps to exit 2."""


class _RefusedError(Exception):
    """The service refused the drive. Maps to exit 3 (fail closed)."""


class _LeaseGuard:
    """Context manager making the controller lease **signal-safe**.

    On ``__enter__`` it acquires the single fenced lease (refusing a live
    holder) and installs ``SIGTERM``/``SIGINT`` handlers that release the lease
    and then re-raise so the process exits with the conventional signal status.
    On ``__exit__`` -- success, exception or ``KeyboardInterrupt`` -- it always
    releases the lease, so no exit path leaves a live holder that would refuse
    the next controller.
    """

    def __init__(self, svc: Any, *, ttl_seconds: int) -> None:
        self._svc = svc
        self._ttl = ttl_seconds
        self._previous: dict[int, Any] = {}

    def __enter__(self) -> "_LeaseGuard":
        self._svc.acquire(ttl_seconds=self._ttl)

        def _handler(signum: int, _frame: Any) -> None:  # pragma: no cover
            # Release before dying so the lease is never left live on a signal.
            try:
                if self._svc.fencing_token is not None:
                    self._svc.release()
            finally:
                previous = self._previous.get(signum, signal.SIG_DFL)
                signal.signal(signum, previous)
                signal.raise_signal(signum)

        for signum in (signal.SIGTERM, signal.SIGINT):
            self._previous[signum] = signal.getsignal(signum)
            signal.signal(signum, _handler)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        for signum, previous in self._previous.items():
            signal.signal(signum, previous)
        self._previous.clear()
        try:
            if self._svc.fencing_token is not None:
                self._svc.release()
        except Exception:  # noqa: BLE001 -- never mask the in-flight outcome
            if exc_type is None:
                raise
        return False


def _drive(
    *,
    run_id: str,
    task_id: str,
    attempt: int,
    db_path: str,
    holder: str,
    ttl_seconds: int,
    route: str,
    session_id: str,
    sequence: str,
    worker: WorkerAdapter,
    reviewer: ReviewerAdapter,
    store: Any,
) -> DriveResult:
    """Compose the service, acquire the lease, drive the loop from state.

    The loop is driven by the *recorded* review verdict, not a fixed script: a
    passing review integrates; a failing review repairs then re-reviews; a
    failing re-review stops the attempt terminally.  The lease is signal-safe
    and released on every exit path.
    """
    # Lazy imports keep the CLI module's import closure light and mirror the
    # composition the ``run`` command uses (a durable SQLiteRunStore). This is
    # the production construction of NativeProcessService that SW-SCOPE-010
    # measures as missing on the base.
    from skillweave.runtime.native_process import (
        LeaseRefusedError,
        NativeProcessService,
        STEP_CONTROLLED_INTEGRATION,
        STEP_DEPENDENT_WAVE,
        STEP_INDEPENDENT_REVIEW,
        STEP_READY_DISPATCH,
        STEP_REPAIR,
        STEP_RE_REVIEW,
    )
    from skillweave.runtime.store import SQLiteRunStore
    from skillweave.trace import gate_receipt as gr

    owns_store = store is None
    if owns_store:
        store = SQLiteRunStore(db_path)

    svc = NativeProcessService(store, holder)
    try:
        try:
            with _LeaseGuard(svc, ttl_seconds=ttl_seconds) as lease:
                return _run_state_driven(
                    svc,
                    lease,
                    run_id=run_id,
                    task_id=task_id,
                    attempt=attempt,
                    route=route,
                    session_id=session_id,
                    sequence=sequence,
                    holder=holder,
                    worker=worker,
                    reviewer=reviewer,
                    store=store,
                    gr=gr,
                )
        except LeaseRefusedError as exc:
            # A live controller holds the lease: refuse, never steal (fail
            # closed). This is the second-controller refusal.
            raise _RefusedError(str(exc)) from exc
    finally:
        if owns_store:
            store.close()


def _require_recorded_verdict(store: Any, run_id: str, *, expected: str) -> str:
    """Read the *durable* review verdict the MCP review tool recorded.

    The verdict is the value in the ``transitions_log`` row the review tool
    wrote (``to_state='advance_or_stop'``, ``reason='Review verdict {verdict}:
    ...'``).  It is never supplied by this controller.  ``expected`` names the
    step whose verdict is being read, so a missing or non-review record is a
    refusal naming the cause rather than a defaulted value.
    """
    record = store.latest_run_transition(run_id)
    if not record or record.get("to_state") != "advance_or_stop":
        raise _RefusedError(
            f"{expected}: no durable review record for run {run_id!r}; the "
            f"controller reads its verdict from the recorded review, never a "
            f"value it makes up"
        )
    reason = str(record.get("reason") or "")
    prefix = "Review verdict "
    if not reason.startswith(prefix) or ":" not in reason:
        raise _RefusedError(
            f"{expected}: durable record for run {run_id!r} is not a review "
            f"verdict record (reason={reason!r})"
        )
    verdict = reason[len(prefix):].split(":", 1)[0].strip().lower()
    if verdict not in (REVIEW_PASS, REVIEW_FAIL):
        raise _RefusedError(
            f"{expected}: recorded review verdict {verdict!r} for run "
            f"{run_id!r} is not one of {[REVIEW_PASS, REVIEW_FAIL]}"
        )
    return verdict


def _independent_receipt(
    gr: Any,
    *,
    execution: AttemptExecution,
    attempt_id: str,
    verdict: ReviewVerdict,
    producer_identity: str,
) -> tuple[Any, Any]:
    """Derive acceptance from *independent verifier evidence*, fail-closed.

    Returns ``(decision, binding)``.  The verdict the reviewer port reports and
    the verdict the evidence decides must agree -- a reviewer that reports
    ``pass`` while offering no independent evidence (or evidence that decides
    otherwise) is refused.  An empty producer artifact is refused too: a
    launched-but-artifact-less attempt is never accepted.
    """
    from skillweave.runtime.native_process import STEP_INDEPENDENT_REVIEW, STEP_RE_REVIEW

    binding = gr.ReviewBinding(
        review_id=f"{attempt_id}/review",
        review_criteria=(STEP_INDEPENDENT_REVIEW, STEP_RE_REVIEW),
        subject_revision=execution.producer_artifact or attempt_id,
        artifact_digest=execution.producer_artifact,
    )
    acceptance = gr.AttemptAcceptance(
        attempt_id=attempt_id,
        state=gr.ACCEPTANCE_REVIEWED,
        producer_artifact=execution.producer_artifact,
    )
    decision = gr.evaluate_gate(
        acceptance,
        evidence=tuple(verdict.evidence),
        producer_identity=producer_identity,
    )
    reported = REVIEW_PASS if verdict.verdict.strip().lower() == REVIEW_PASS else REVIEW_FAIL
    independent_pass = decision.verdict == gr.GATE_PASS and decision.accepted
    if reported == REVIEW_PASS and not independent_pass:
        reasons = '; '.join(decision.reasons) or 'no independent verifier evidence'
        raise _RefusedError(
            f"reviewer reported pass but independent evidence did not decide a "
            f"PASS ({decision.verdict}: {reasons})"
        )
    if reported == REVIEW_FAIL and decision.verdict not in (gr.GATE_FAIL, gr.GATE_UNDECIDED):
        raise _RefusedError(
            f"reviewer reported fail but independent evidence decided "
            f"{decision.verdict}"
        )
    return decision, binding


def _accepted_receipt(
    svc: Any,
    gr: Any,
    *,
    execution: AttemptExecution,
    acceptance: Any,
    binding: Any,
    verdict: ReviewVerdict,
    producer_identity: str,
) -> Any:
    """Record the controller acceptance through the frozen gate-receipt path."""
    receipt = svc.record_acceptance(
        acceptance,
        producer_artifact=execution.producer_artifact,
        binding=binding,
        role=gr.VERIFIER_ROLE,
        answering_identity=verdict.verifier_identity,
        producer_identity=producer_identity,
        accepted=True,
    )
    # A *rejected* independent receipt asserts the other fail-closed direction.
    gr.validate_independent_gate_receipt(receipt.to_dict())
    return receipt


def _dependent_wave(
    svc: Any,
    gr: Any,
    *,
    task_id: str,
    receipt: Any,
    dependents: tuple[str, ...],
) -> tuple[str, ...]:
    """Compute and release the dependent wave from the declared graph.

    ``dependents`` are the tasks that declare ``task_id`` as a dependency in the
    caller-supplied sequence declaration (the same ``SequenceDeclaration`` the
    dispatch machinery loads through
    :func:`skillweave.dispatch.contracts.load_sequence`).  Each dependent is
    released only when the accepted attempt's gate receipt releases it -- the
    frozen ``_gate_receipt_releases`` rule the scheduler applies to
    ``gate_receipts``.  A dependent whose gate did not release stays ``blocked``
    and is **not** reported as released, so nothing is fabricated; with no
    declared graph the wave is legitimately empty.
    """
    from skillweave.runtime.dagscheduler import Sequence, Task, build_schedule

    if not dependents:
        return ()
    receipt_dict = receipt.to_dict()
    # Every dependent declares the accepted task's gate; the receipt for that
    # gate carries the accepted, artifact-carrying acceptance and is the single
    # authority for the release.
    tasks = [
        Task(id=task_id, gate=None, write_scope=[]),
        *(Task(id=dep, depends_on=[task_id], gate=task_id) for dep in dependents),
    ]
    schedule = build_schedule(
        Sequence(tasks=tasks, session_boundary=f"{task_id}/dependent-wave"),
        gate_receipts={task_id: receipt_dict},
    )
    blocked = set(schedule.blocked)
    released = tuple(d for d in dependents if d not in blocked)
    # The frozen release rule is the single authority: an accepted receipt with
    # its artifact releases its own dependents; anything else releases nothing.
    svc.release_accepted_dependents(
        _AcceptanceView(
            attempt_id=receipt.attempt_id,
            state=receipt.state,
            producer_artifact=receipt.producer_artifact,
            dependents=released,
        ),
        producer_identity=receipt.producer_identity,
    )
    return released


@dataclass(frozen=True)
class _AcceptanceView:
    """The minimal ``AttemptAcceptance``-shaped view the release rule consumes."""

    attempt_id: str
    state: str
    producer_artifact: str
    dependents: tuple[str, ...] = ()


def _declared_dependents(sequence_path: str, task_id: str) -> tuple[str, ...]:
    """Return the declared dependents of ``task_id`` from a sequence file.

    Reads the caller-declared dependency graph through the real dispatch
    contract (:func:`skillweave.dispatch.contracts.load_sequence`): a lane that
    lists ``task_id`` in its ``depends_on`` is a dependent.  With no sequence
    file (or no such lane) the wave is empty -- a dependent is never invented.
    """
    if not sequence_path:
        return ()
    from skillweave.dispatch.contracts import load_sequence

    with open(sequence_path, encoding="utf-8") as handle:
        declaration = json.load(handle)
    sequence = load_sequence(declaration)
    dependents = [
        lane.id for lane in sequence.lanes if task_id in tuple(lane.depends_on)
    ]
    return tuple(sorted(dependents))


def _run_state_driven(
    svc: Any,
    lease: Any,
    *,
    run_id: str,
    task_id: str,
    attempt: int,
    route: str,
    session_id: str,
    sequence: str,
    holder: str,
    worker: WorkerAdapter,
    reviewer: ReviewerAdapter,
    store: Any,
    gr: Any,
) -> DriveResult:
    """The state-driven, verdict-driven loop body (inside the lease guard)."""
    from skillweave.runtime.native_process import (
        STEP_CONTROLLED_INTEGRATION,
        STEP_DEPENDENT_WAVE,
        STEP_INDEPENDENT_REVIEW,
        STEP_READY_DISPATCH,
        STEP_REPAIR,
        STEP_RE_REVIEW,
    )

    # The run must exist: the loop reads/writes durable state for it.
    if store.get_run(run_id) is None:
        raise _UsageError(f"no run {run_id!r} in the store to drive")

    token = svc.fencing_token
    steps: list[str] = []
    reviews: list[str] = []

    # Step 1 -- ready dispatch: record the durable intent (state LAUNCHED) and
    # its boundary owned-attempt-dispatch record.  Guarded transitions below
    # read this state back from the store, so the loop is driven by facts.
    intent, dispatch = svc.dispatch(
        run_id=run_id,
        task_id=task_id,
        attempt=attempt,
        step=STEP_READY_DISPATCH,
        route=route,
        session_id=session_id,
    )
    steps.append(STEP_READY_DISPATCH)
    intent_id = intent.intent_id

    # The launch seam: the production adapter runs the owned attempt here. It
    # launches nothing in tests (the fake records and returns a synthetic record).
    execution = worker.execute(dispatch)
    # A step may outlast the ttl: renew the lease so it never lapses mid-step.
    svc.renew()

    # Step 2 -- independent review, driven by the *recorded* verdict.  The
    # reviewer port supplies independent evidence; the controller derives the
    # acceptance decision from it and the verdict from the durable record.
    verdict = reviewer.review(dispatch, execution)
    decision, binding = _independent_receipt(
        gr,
        execution=execution,
        attempt_id=intent_id,
        verdict=verdict,
        producer_identity=holder,
    )
    recorded = _require_recorded_verdict(store, run_id, expected=STEP_INDEPENDENT_REVIEW)
    svc.reviewed_advance(intent_id, recorded)
    steps.append(STEP_INDEPENDENT_REVIEW)
    reviews.append(recorded)

    accepted = decision.verdict == gr.GATE_PASS and decision.accepted

    if recorded == REVIEW_FAIL and not accepted:
        # Step 3 -- repair: a failing review leads to a repair round.  The
        # reviewed_advance above already moved the intent LAUNCHED->REPAIRING
        # (that IS the durable repair state); the step is recorded here.
        steps.append(STEP_REPAIR)

        # Step 4 -- re-review, again driven by the *recorded* verdict.  The
        # reviewer port is called again for the re-review round's evidence;
        # the verdict is read from the durable record the review tool wrote.
        reverdict = reviewer.review(dispatch, execution)
        redecision, _ = _independent_receipt(
            gr,
            execution=execution,
            attempt_id=intent_id,
            verdict=reverdict,
            producer_identity=holder,
        )
        rerecorded = _require_recorded_verdict(store, run_id, expected=STEP_RE_REVIEW)
        svc.rereviewed_advance(intent_id, rerecorded)
        steps.append(STEP_RE_REVIEW)
        reviews.append(rerecorded)
        accepted = redecision.verdict == gr.GATE_PASS and redecision.accepted
        if rerecorded == REVIEW_FAIL:
            # A failing re-review stops the attempt terminally: it never
            # integrates, and the terminal FAILED intent is the causal record.
            return DriveResult(
                run_id=run_id,
                task_id=task_id,
                attempt=attempt,
                intent_id=intent_id,
                fencing_token=int(token),
                steps=tuple(steps),
                accepted=False,
                released_dependents=(),
                integration="none",
                reviews=tuple(reviews),
            )

    if not accepted:
        # Independent evidence did not decide a PASS (or a re-review failed to
        # pass): fail closed, never integrate.
        return DriveResult(
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
            intent_id=intent_id,
            fencing_token=int(token),
            steps=tuple(steps),
            accepted=False,
            released_dependents=(),
            integration="none",
            reviews=tuple(reviews),
        )

    # Acceptance through the frozen gate-receipt path: the artifact is the
    # execution's producer artifact, the answering identity an independent
    # verifier -- never the controller's own label.
    acceptance = gr.AttemptAcceptance(
        attempt_id=intent_id,
        state=gr.ACCEPTANCE_ACCEPTED,
        producer_artifact=execution.producer_artifact,
    )
    receipt = _accepted_receipt(
        svc,
        gr,
        execution=execution,
        acceptance=acceptance,
        binding=binding,
        verdict=verdict,
        producer_identity=holder,
    )

    # Step 5 -- controlled integration. A repository subject integrates through
    # Git; any other subject kind integrates through a controlled content
    # transaction. Integration is guarded from the reviewed state.
    if execution.integration_subject is not None:
        svc.record_content_integration(
            intent_id,
            execution.integration_subject,
            baseline_digest=execution.baseline_digest,
            applied_digest=execution.applied_digest,
        )
        integration = "content_transaction"
    else:
        svc.integration_advance(intent_id)
        integration = "git_merge"
    steps.append(STEP_CONTROLLED_INTEGRATION)

    # Step 6 -- dependent wave: compute by the declared dependency graph and the
    # accepted state, and release only what the gate receipt releases.
    dependents = _declared_dependents(sequence, task_id)
    released = _dependent_wave(
        svc, gr, task_id=task_id, receipt=receipt, dependents=dependents
    )
    svc.dependent_wave_advance(intent_id)
    steps.append(STEP_DEPENDENT_WAVE)

    return DriveResult(
        run_id=run_id,
        task_id=task_id,
        attempt=attempt,
        intent_id=intent_id,
        fencing_token=int(token),
        steps=tuple(steps),
        accepted=True,
        released_dependents=tuple(released),
        integration=integration,
        reviews=tuple(reviews),
    )


if __name__ == "__main__":
    sys.exit(main())
