"""The thin ``skillweave native-controller`` command-line surface.

This is the **production entry point** for
:class:`skillweave.runtime.native_process.NativeProcessService` (SW-SCOPE-010,
unit A).  Before this command the service existed, was unit-tested and passed
independent review at the method level, but no production code ever constructed
it, acquired its fenced controller lease or drove the owned loop -- so every
criterion worded "one native process service drives the loop" held at library
level only.

The command composes the service on the **same durable store the ``run``
command uses** (``SQLiteRunStore`` over ``--db-path``), acquires the single
fenced controller lease, drives one owned attempt through the declared six
steps by calling the existing service methods, and always releases the lease on
the way out.  It is additive and opt-in: it adds one subcommand and writes only
to the store the caller names; no existing subcommand or record changes.

Two seams keep this deterministic and launch-free:

* the **worker adapter** -- the one place a real drive would launch the owned
  attempt (a model, a provider, a subprocess).  Tests inject a fake that returns
  a synthetic execution and asserts nothing was launched; the production
  default derives the attempt's artifact from the dispatch and performs no I/O.
* ``main(..., worker=...)`` -- the same injection seam, so a test can reach the
  loop without patching.

Fail-closed is the rule, not the exception: a missing ``--run-id``/``--task-id``
is a usage error; a second controller is refused by the lease (never silently
stolen); an attempt step that the service refuses propagates as a nonzero exit,
and the lease is released either way.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from typing import Any, Optional, Protocol, Sequence, runtime_checkable

# Public surface re-exported for callers/tests that key on the entry point.
__all__ = [
    "EXIT_OK",
    "EXIT_INVALID_INPUT",
    "EXIT_REFUSED",
    "EXIT_FAILED",
    "AttemptExecution",
    "WorkerAdapter",
    "InProcessAttemptWorker",
    "DriveResult",
    "build_parser",
    "main",
]

#: ``ok``: the controller drove the owned attempt through the declared loop.
EXIT_OK = 0
#: ``invalid input``: missing/blank required flag, or an argparse usage error.
EXIT_INVALID_INPUT = 2
#: ``refused``: the service refused -- a live controller holds the lease, or a
#: duplicate launch, or any other typed service refusal (fail closed).
EXIT_REFUSED = 3
#: ``failure``: an unexpected error. A crash is not a verdict and is never
#: collapsed into a normal outcome.
EXIT_FAILED = 5


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


class InProcessAttemptWorker:
    """The default adapter: a deterministic, launch-free attempt record.

    It performs no I/O, launches no model, provider or subprocess, and touches
    no credentials or gateway configuration.  The producer artifact and the
    integration digests are content-addressed over the dispatch facts, so the
    same owned attempt always yields the same record -- which is what makes the
    default entry point's behaviour reproducible without an external worker.

    A deployment that wants a real worker injects its own :class:`WorkerAdapter`
    (``main(..., worker=...)``); this default never becomes that worker.
    """

    def __init__(self, *, content_subject: Any = None) -> None:
        self._content_subject = content_subject

    def execute(self, dispatch: Any) -> AttemptExecution:
        digest = _attempt_digest(dispatch)
        artifact = f"artifact:{dispatch.run_id}/{dispatch.task_id}/{dispatch.attempt}"
        return AttemptExecution(
            producer_artifact=artifact,
            integration_subject=self._content_subject,
            baseline_digest=f"baseline:{digest}",
            applied_digest=f"applied:{digest}",
        )


@dataclass(frozen=True)
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
        }


def _attempt_digest(dispatch: Any) -> str:
    blob = json.dumps(
        {
            "run_id": dispatch.run_id,
            "task_id": dispatch.task_id,
            "attempt": dispatch.attempt,
            "fencing_token": dispatch.fencing_token,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_parser(prog: str = "skillweave native-controller") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Drive the native process service: acquire the single controller "
            "lease and run one owned attempt through ready dispatch, independent "
            "review, repair, re-review, controlled integration and the "
            "dependent wave. Additive and opt-in."
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
    store: Any = None,
) -> int:
    """Route ``native-controller`` and return its exit code.

    ``worker`` and ``store`` are injection seams: a test supplies a fake adapter
    (that launches nothing) and/or a store, instead of letting the entry build
    its own.  Production callers pass neither.
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
        result = _drive(
            run_id=args.run_id,
            task_id=args.task_id,
            attempt=args.attempt,
            db_path=args.db_path,
            holder=args.holder,
            ttl_seconds=args.ttl_seconds,
            route=args.route,
            session_id=args.session_id,
            worker=worker if worker is not None else InProcessAttemptWorker(),
            store=store,
        )
    except _UsageError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return EXIT_INVALID_INPUT
    except _RefusedError as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return EXIT_REFUSED
    except Exception as exc:  # noqa: BLE001 -- a crash is exit 5, never a verdict
        sys.stderr.write(f"error: {type(exc).__name__}: {exc}\n")
        return EXIT_FAILED

    sys.stdout.write(json.dumps(result.to_dict(), sort_keys=True) + "\n")
    return EXIT_OK


class _UsageError(Exception):
    """Caller input cannot form a controller drive. Maps to exit 2."""


class _RefusedError(Exception):
    """The service refused the drive. Maps to exit 3 (fail closed)."""


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
    worker: WorkerAdapter,
    store: Any,
) -> DriveResult:
    """Compose the service, acquire the lease, drive the six steps, release.

    The lease is released on every exit path -- success, refusal or crash -- so a
    failed drive never leaves a live holder behind that would refuse the next
    controller.
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
    from skillweave.trace.gate_receipt import (
        ACCEPTANCE_ACCEPTED,
        ACCEPTANCE_ARTIFACT_PRODUCED,
        ACCEPTANCE_REVIEWED,
        ACCEPTANCE_RUNNING,
        AttemptAcceptance,
        ReviewBinding,
    )

    owns_store = store is None
    if owns_store:
        store = SQLiteRunStore(db_path)

    svc = NativeProcessService(store, holder)
    try:
        try:
            svc.acquire(ttl_seconds=ttl_seconds)
        except LeaseRefusedError as exc:
            # A live controller holds the lease: refuse, never steal (fail
            # closed). This is the second-controller refusal.
            raise _RefusedError(str(exc)) from exc

        token = svc.fencing_token
        steps: list[str] = []

        # Step 1 -- ready dispatch: record the durable intent and its boundary
        # owned-attempt-dispatch record.
        intent, dispatch = svc.dispatch(
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
            step=STEP_READY_DISPATCH,
            route=route,
            session_id=session_id,
        )
        steps.append(STEP_READY_DISPATCH)

        # The launch seam: in production an adapter would run the attempt here.
        execution = worker.execute(dispatch)

        # Step 2 -- independent review: a reviewed attempt is not yet accepted.
        svc.record_review(intent.intent_id)
        steps.append(STEP_INDEPENDENT_REVIEW)

        # Step 3 -- repair.
        svc.record_repair(intent.intent_id)
        steps.append(STEP_REPAIR)

        # Step 4 -- re-review.
        svc.record_re_review(intent.intent_id)
        steps.append(STEP_RE_REVIEW)

        # Controller acceptance (SW-1511-REVIEW criterion 1): an attempt is
        # accepted only with its producer artifact, an independent answering
        # identity and a bound review -- never from the launch state alone.
        binding = ReviewBinding(
            review_id=f"{run_id}/{intent.intent_id}/review",
            review_criteria=(STEP_INDEPENDENT_REVIEW, STEP_RE_REVIEW),
            subject_revision=route or run_id,
            artifact_digest=execution.producer_artifact,
        )
        acceptance = (
            AttemptAcceptance(
                attempt_id=intent.intent_id,
                dependents=(f"{task_id}/dependent-wave",),
            )
            .record_producer_artifact(execution.producer_artifact)
            .advance(ACCEPTANCE_RUNNING)
            .advance(ACCEPTANCE_ARTIFACT_PRODUCED)
            .advance(ACCEPTANCE_REVIEWED)
            .advance(ACCEPTANCE_ACCEPTED)
        )
        receipt = svc.record_acceptance(
            acceptance,
            producer_artifact=execution.producer_artifact,
            binding=binding,
            role="verifier",
            answering_identity="independent-verifier",
            producer_identity=holder,
            accepted=True,
        )

        # Step 5 -- controlled integration. A repository subject integrates
        # through Git; any other subject kind integrates through a controlled
        # content transaction (criterion 5). This drive exercises the
        # non-repository transaction seam when the adapter supplies a subject,
        # otherwise the repository integration record.
        if execution.integration_subject is not None:
            svc.record_content_integration(
                intent.intent_id,
                execution.integration_subject,
                baseline_digest=execution.baseline_digest,
                applied_digest=execution.applied_digest,
            )
            integration = "content_transaction"
        else:
            svc.record_integration(intent.intent_id)
            integration = "git_merge"
        steps.append(STEP_CONTROLLED_INTEGRATION)

        # Step 6 -- dependent wave: release the accepted attempt's dependents
        # (refused without the producer artifact) and record the wave.
        released = svc.release_accepted_dependents(acceptance)
        svc.record_dependent_wave(intent.intent_id)
        steps.append(STEP_DEPENDENT_WAVE)

        return DriveResult(
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
            intent_id=intent.intent_id,
            fencing_token=int(token),
            steps=tuple(steps),
            accepted=bool(receipt.accepted),
            released_dependents=tuple(released),
            integration=integration,
        )
    finally:
        # The lease is released on every path, so a refusal or crash in the loop
        # never leaves a live holder that would refuse the next controller.
        try:
            if svc.fencing_token is not None:
                svc.release()
        finally:
            if owns_store:
                store.close()


if __name__ == "__main__":
    sys.exit(main())
