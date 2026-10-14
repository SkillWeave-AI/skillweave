"""Dependency-ready fan-out (SW-FANOUT-001).

Replaces the sequential batch loop with a genuine fan-out: independent workers
are started as real processes at the same time, overlap in time, and each keeps
its own child run and its own raw artifact. The sequential ``dispatch_batch``
waited for each worker before starting the next; this module starts them all,
then waits for all, so overlap is a measured fact, not a claim.

The primitive is ``skillweave.runtime.runner_adapter.start_process``: it returns
a live handle for a process started in its own session, so a caller can start
N workers and then reap them. This module owns the fan-out *wiring* only — the
actual process concerns (spawn, capture, cancel, timeout) stay in
``runner_adapter``. Each child runs under a distinct ``child_run_id`` derived
from the parent, and each produces its own ``ArtifactReceipt`` (raw artifact,
content-addressed), so two children never share one run or one artifact.
"""

from __future__ import annotations

import hashlib
import importlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, List, Optional, Sequence, Tuple

from skillweave.routing.modelspec import ModelSpec, from_value

if TYPE_CHECKING:
    pass


def _start_process():
    """Return the runtime ``start_process`` primitive at call time (GLE-020)."""
    return importlib.import_module("skillweave.runtime.runner_adapter").start_process


#: The four machine outcomes a terminal child reports. Exactly one is set per
#: child; a result that resolves to none (or more than one) is rejected.
OUTCOME_EXIT_CODE = "exit_code"
OUTCOME_SIGNAL = "signal"
OUTCOME_TIMED_OUT = "timed_out"
OUTCOME_LAUNCH_FAILED = "launch_failed"

OUTCOMES = (OUTCOME_EXIT_CODE, OUTCOME_SIGNAL, OUTCOME_TIMED_OUT, OUTCOME_LAUNCH_FAILED)

#: A child that could not even be spawned (pre-result) is ``launch_failed``; the
#: fallback encoding for raw streams when no encoding is declared elsewhere.
DEFAULT_ENCODING = "utf-8"


class ChildOutcomeError(ValueError):
    """A terminal result carries contradictory or unresolved machine outcomes.

    A ``ProcessResult`` must expose exactly one of ``exit_code`` / ``signal`` /
    ``timed_out`` / ``launch_failed``; a result whose terminal fields contradict
    each other (both an exit code and a signal, or a termination that does not
    match its fields) is rejected here, never silently folded into one outcome.
    """


class LifecycleObservationError(ValueError):
    """A passive child-lifecycle observation callback raised.

    The ``on_child_lifecycle`` seam is passive: its failure must never abort the
    fan-out before a started child is reaped. The fan-out therefore runs the
    callback best-effort, reaps every started child regardless, and only then
    surfaces this typed error — chained from the original callback exception —
    so observation noise is never converted into a leaked child.
    """


def _resolve_outcome(result: Any) -> str:
    """Map a ``ProcessResult`` to its one machine outcome, rejecting contradiction.

    ``termination == "timed_out"`` maps to ``timed_out``; a set ``signal`` maps to
    ``signal``; a clean ``exited`` with an ``exit_code`` maps to ``exit_code``.
    Any combination that cannot be one of the four outcomes (an exit code *and* a
    signal, a non-``exited`` termination carrying an exit code, or no outcome at
    all) raises :class:`ChildOutcomeError` so a contradictory child never reaches
    the caller as a resolved result.
    """
    exit_code = getattr(result, "exit_code", None)
    signal = getattr(result, "signal", None)
    termination = getattr(result, "termination", "exited")

    if termination == "timed_out":
        # A timeout is its own machine outcome; it must not also carry a code/signal.
        if exit_code is not None or signal is not None:
            raise ChildOutcomeError(
                "'timed_out' termination must not carry exit_code or signal"
            )
        return OUTCOME_TIMED_OUT

    if termination == "launch_failed":
        if exit_code is not None or signal is not None:
            raise ChildOutcomeError(
                "'launch_failed' termination must not carry exit_code or signal"
            )
        return OUTCOME_LAUNCH_FAILED

    if signal is not None:
        if exit_code is not None:
            raise ChildOutcomeError("both exit_code and signal set on one result")
        return OUTCOME_SIGNAL

    if termination == "exited":
        return OUTCOME_EXIT_CODE

    # A termination we do not recognise as one of the four (e.g. a bare
    # ``cancelled`` with no signal) has no machine outcome.
    raise ChildOutcomeError(f"no single machine outcome for termination {termination!r}")


@dataclass
class ReceiptReference:
    """A resolvable reference to one child's stdout/stderr raw bytes.

    A receipt is *not* the bytes themselves: it names the artifact by digest and
    carries the declared ``byte_length`` and ``encoding`` so a resolver can
    return the raw bytes and prove they match. ``resolve`` runs through a
    caller-supplied content-addressed resolver (the shared
    ``RawArtifactStore.resolve`` semantics) and ``verify`` re-checks digest,
    length and encoding against the bytes, so a receipt that cannot be resolved
    or fails integrity never masquerades as available evidence.
    """

    artifact_id: str
    sha256: str
    byte_length: int
    encoding: str
    stream: str

    def verify(self, raw: bytes) -> bool:
        """Return True when ``raw`` matches digest, length and declared encoding."""
        if hashlib.sha256(raw).hexdigest() != self.sha256:
            return False
        if len(raw) != self.byte_length:
            return False
        try:
            raw.decode(self.encoding)
        except (UnicodeDecodeError, LookupError):
            return False
        return True

    def resolve(self, resolver: Any, raw_hint: Optional[bytes] = None) -> bytes:
        """Resolve this reference to raw bytes and reject mismatches.

        ``resolver`` is a content-addressed resolve callable
        (``bytes = resolver(sha256)``). When the bytes cannot be resolved
        (missing) or fail :meth:`verify`, the resolution is refused rather than
        returning wrong data. ``raw_hint`` lets a producer that still holds the
        bytes resolve without a second store read while still verifying them.
        """
        raw = raw_hint
        if raw is None:
            raw = resolver(self.sha256)
        if not self.verify(raw):
            raise ChildOutcomeError(
                f"receipt '{self.artifact_id}' ({self.stream}) failed "
                "digest/length/encoding verification"
            )
        return raw

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "sha256": self.sha256,
            "byte_length": self.byte_length,
            "encoding": self.encoding,
            "stream": self.stream,
        }


def _store_child_bytes(store: Any, child: "FanOutChild") -> None:
    """Put a child's captured stdout/stderr into ``store`` and assert integrity.

    Both streams are stored under their own content digest *before* any
    reference is returned, and each reference is re-verified against the store
    (digest, byte length and declared encoding), so a returned reference can
    never name bytes that are absent from the store or corrupt. This is the
    content-addressed wiring: the fan-out binds its captured raw bytes to the
    shared ``RawArtifactStore`` so a caller can resolve a ref without re-putting
    the bytes itself.
    """
    store.put(child.raw_bytes)
    store.put(child.stderr_bytes)
    if child.stdout_ref is not None:
        child.stdout_ref.resolve(store.resolve)
    if child.stderr_ref is not None:
        child.stderr_ref.resolve(store.resolve)


def _make_receipt_reference(receipt: Any, *, stream: str) -> Optional[ReceiptReference]:
    """Build a ``ReceiptReference`` from an ``ArtifactReceipt``.

    The encoding is read from the receipt metadata (falling back to
    :data:`DEFAULT_ENCODING`); the digest and byte length come from the receipt
    itself so the reference cannot disagree with the receipt that produced it.
    Returns ``None`` when no receipt exists (missing evidence), so a child with
    no capture is distinguishable from one with zero-length capture.
    """
    if receipt is None:
        return None
    metadata = getattr(receipt, "metadata", {}) or {}
    encoding = metadata.get("encoding", DEFAULT_ENCODING)
    byte_length = int(metadata.get("byte_length", 0))
    return ReceiptReference(
        artifact_id=receipt.artifact_id,
        sha256=receipt.sha256,
        byte_length=byte_length,
        encoding=encoding,
        stream=stream,
    )


@dataclass
class FanOutLaunchContext:
    """A per-child launch identity: repo, base commit, tool, and working dir.

    A heterogeneous parallel group cannot be represented by a single scalar
    ``subject_repo``/``subject_commit``/``tool``/``cwd``: two children may touch
    different repos, different base commits, different tools, and — critically —
    different materialised worktrees. This context carries exactly those four
    facts *per child*, so ``fan_out_dispatch`` threads each lane's own identity
    into that lane's own ``start_process`` call.
    """

    subject_repo: str
    subject_commit: str
    tool: str
    cwd: Optional[str] = None


@dataclass
class FanOutChild:
    """One fan-out child: its distinct run identity and its own raw artifact.

    ``child_run_id`` is distinct per child (never the parent's). ``artifact`` is
    the child's own ``ArtifactReceipt`` over its captured stdout, so two
    children never share a raw artifact. ``model`` is the child's own resolved
    model string (never the shared parent model): each child may answer from a
    different model, and the child carries which one it actually used.

    ``subject_repo``/``subject_commit``/``tool``/``cwd`` mirror the per-child
    launch context actually used for that child, so a mutating lane's attested
    worktree path and its own repo/base/tool are provable from the result, not
    assumed from the group leader.

    The raw bytes are kept here so overlap (and separation) can be asserted
    without a second re-run.
    """

    child_run_id: str
    command: List[str]
    result: ProcessResult
    model: str
    subject_repo: Optional[str] = None
    subject_commit: Optional[str] = None
    tool: Optional[str] = None
    cwd: Optional[str] = None
    raw_bytes: bytes = b""

    #: Exactly one of the four machine outcomes (exit_code/signal/timed_out/
    #: launch_failed); ``None`` only when the child never reached a terminal
    #: result (which the fan-out refuses). Set when the child is built.
    outcome: Optional[str] = None

    #: The raw stderr bytes captured on this child (mirrors ``raw_bytes``, which
    #: is the stdout bytes). Kept separate so a caller resolves both streams.
    stderr_bytes: bytes = b""

    #: Resolvable references to the child's captured outputs, ``None`` when the
    #: child produced no receipt for that stream (missing evidence, distinct
    #: from empty-but-present capture).
    stdout_ref: Optional[ReceiptReference] = None
    stderr_ref: Optional[ReceiptReference] = None

    @property
    def artifact(self) -> Any:
        return self.result.stdout_receipt

    def to_dict(self) -> dict[str, Any]:
        """A machine-readable child result: one outcome plus receipt references.

        Empty inline stdout/stderr must never hide an available artifact: the
        receipt references are emitted regardless of whether the inline bytes
        are empty, so a caller can always find the resolvable artifact.
        """
        return {
            "child_run_id": self.child_run_id,
            "model": self.model,
            "outcome": self.outcome,
            "exit_code": getattr(self.result, "exit_code", None),
            "signal": getattr(self.result, "signal", None),
            "termination": getattr(self.result, "termination", None),
            "subject_repo": self.subject_repo,
            "subject_commit": self.subject_commit,
            "tool": self.tool,
            "cwd": self.cwd,
            "stdout": self.stdout_ref.to_dict() if self.stdout_ref else None,
            "stderr": self.stderr_ref.to_dict() if self.stderr_ref else None,
        }


@dataclass
class FanOutResult:
    """The collected outcome of one fan-out: all children, plus overlap fact.

    ``children`` is one entry per launched worker, in launch order.
    ``overlapped`` is True only when the wall-clock interval shared by the
    children was measurably positive — i.e. they ran at the same time, not in
    sequence. Each child's run and artifact stay separate (see ``FanOutChild``).
    """

    children: List[FanOutChild] = field(default_factory=list)
    overlapped: bool = False

    @property
    def succeeded(self) -> bool:
        return bool(self.children) and all(c.result.succeeded for c in self.children)

    def child_outcomes(self) -> list[Optional[str]]:
        """The single machine outcome of each child, in launch order."""
        return [c.outcome for c in self.children]

    def to_dict(self) -> dict[str, Any]:
        """The wave result surface the caller reads: children and their refs.

        Each child contributes its one machine outcome and its resolvable
        receipt references directly, so a caller never has to reach into a
        ``ProcessResult`` to learn how a child ended or where its evidence is.
        """
        return {
            "overlapped": self.overlapped,
            "children": [c.to_dict() for c in self.children],
        }


def fan_out_dispatch(
    commands: Sequence[Sequence[str]],
    *,
    run_id: str,
    subject_repo: str,
    subject_commit: str,
    tool: str,
    model: Optional[str] = None,
    models: Optional[Sequence[ModelSpec]] = None,
    created_at: Optional[str] = None,
    cwd: Optional[str] = None,
    launch_contexts: Optional[Sequence[FanOutLaunchContext]] = None,
    timeout: Optional[float] = None,
    artifact_store: Optional[Any] = None,
    on_child_lifecycle: Optional[Callable[..., None]] = None,
    env: Optional[dict[str, str]] = None,
    mcp_grant: Optional[Any] = None,
) -> FanOutResult:
    """Start every command as a real process, then wait for all.

    All workers are started before any is reaped, so they overlap in time.
    Each worker runs under ``<run_id>-<index>`` (a distinct child run identity)
    and produces its own raw artifact; nothing is shared between children.

    Each child resolves its own model:

    * ``models`` (optional) is a list of ``ModelSpec`` aligned to ``commands`` —
      one concrete id or delegated router+scenario per child. Each child's spec
      is resolved to a concrete model string and threaded into that child's own
      ``start_process`` call, so two children may answer from different models.
    * ``model`` (optional) is the backward-compatible single model: it is lifted
      to ``concrete(model)`` for *every* child, preserving the prior contract
      that a single ``model`` applies to all. It is ignored when ``models`` is
      given.

    ``launch_contexts`` (optional) is a per-child launch identity, length-aligned
    to ``commands``: one ``FanOutLaunchContext`` per child carrying that child's
    own ``subject_repo``/``subject_commit``/``tool``/``cwd``. When supplied, each
    child's identity comes from its own context, never from the group's scalar
    values — a heterogeneous parallel group (mixed repos, roles, tools, or
    attested worktrees) is represented faithfully. Alignment is fail-closed,
    like the per-child ``models`` list: a length mismatch raises before any
    process starts. When omitted, the scalar ``subject_repo``/``subject_commit``/
    ``tool``/``cwd`` apply to every child, preserving the prior contract.

    At least one of ``model`` / ``models`` must be supplied. A worker that dies
    without a result is a failure with a message, never a silent success — the
    child's ``ProcessResult.succeeded`` carries that fact.

    ``timeout`` (optional) forwards to each child's ``wait``: a child exceeding
    it ends in the defined ``timed_out`` outcome (distinct from an exit, signal,
    or launch failure) rather than hanging the whole fan-out.

    ``artifact_store`` (optional) is the shared ``RawArtifactStore`` (the run /
    application-owned content-addressed store). When supplied, every child's
    captured stdout and stderr bytes are put into it *before* the child's
    receipt references are returned, and each reference is re-verified against
    the store (digest, byte length, declared encoding). A returned reference is
    therefore immediately resolvable from the store by a caller that received
    no bytes out-of-band.

    ``on_child_lifecycle`` (optional) is a safe child-lifecycle observation
    seam. When supplied it is invoked as ``on_child_lifecycle(child_key,
    dispatch_id, phase, handle)`` around each child: ``phase=="started"``
    immediately after the child is started (with the live handle and the child
    in-flight, so a heartbeat can be observed *while alive*), and
    ``phase=="terminal"`` immediately after the child is reaped. A launch-failed
    child (no handle) is signalled only via ``started`` with handle ``None``.
    This callback is a passive observation seam, not the transition authority:
    it is never handed a writable stream/store/gate, and it cannot alter the
    children, the outcome, or the fan-out's own lifecycle. Wiring the *typed*
    live event stream (heartbeat-before-terminal) onto the real dispatch path
    goes through this seam, so no state is inferred from logs, PIDs, ANSI text
    or wrapper completion.

    A callback exception is passive too: it is caught, every started child is
    still reaped, and after the fan-out has finished the exception surfaces as a
    :class:`LifecycleObservationError` — never as an unreaped child and never as
    a silently dropped success.

    ``mcp_grant`` (optional) is a
    :class:`skillweave.mcp.bootstrap.HarnessMcpSessionGrant` provisioned into
    every child's actual launch and verified from that child's own resolved
    environment; a child that does not reach the granted authority/state
    service is refused. Omitting it preserves the historical fan-out exactly.
    """
    created_at = created_at or datetime.now(timezone.utc).isoformat()

    if not commands:
        return FanOutResult(children=[], overlapped=False)

    if launch_contexts is not None:
        if len(launch_contexts) != len(commands):
            raise ValueError(
                "per-child launch context count "
                f"{len(launch_contexts)} != command count {len(commands)}"
            )
        contexts = list(launch_contexts)
    else:
        contexts = None

    if models is not None:
        if len(models) != len(commands):
            raise ValueError(
                f"per-child model spec count {len(models)} != command count {len(commands)}"
            )
        specs = list(models)
    else:
        if model is None:
            raise ValueError("fan_out_dispatch requires 'model' or 'models'")
        specs = [from_value(model) for _ in commands]

    resolved_models = [_resolve_spec(spec) for spec in specs]
    _launch_failures: dict[int, str] = {}
    _observation_errors: List[BaseException] = []

    def _notify_lifecycle(child_key: str, dispatch_id: str, phase: str, handle: Any) -> None:
        if on_child_lifecycle is None:
            return
        try:
            on_child_lifecycle(
                child_key=child_key,
                dispatch_id=dispatch_id,
                phase=phase,
                handle=handle,
            )
        except Exception as exc:  # noqa: BLE001 - observation is passive
            _observation_errors.append(exc)

    def _child_identity(index: int) -> Tuple[str, str, str, Optional[str]]:
        if contexts is not None:
            ctx = contexts[index]
            return ctx.subject_repo, ctx.subject_commit, ctx.tool, ctx.cwd
        return subject_repo, subject_commit, tool, cwd

    # ``mcp_grant`` (optional) is the same "provision then verify inside the
    # actual child launch" seam the single-lane inline path uses: the granted
    # MCP configuration is merged into the child's env and, immediately after
    # the child starts, its *own* resolved environment is checked to reach the
    # same authority/state service. A child that landed elsewhere is refused,
    # never silently downgraded to the interactive harness profile. Omitting
    # it (``None``, the default) preserves the historical fan-out exactly.
    grant_env: Optional[dict[str, str]] = None
    if mcp_grant is not None:
        from skillweave.mcp import bootstrap as _mcp_bootstrap

        grant_env = _mcp_bootstrap.grant_child_env(mcp_grant)
        _mcp_bootstrap.assert_no_ambient_credentials(grant_env)

    child_env: Optional[dict[str, str]] = env
    child_allowlist: Optional[List[str]] = None
    if grant_env is not None:
        child_env = {**(env or {}), **grant_env}
        # The granted child is launched under an *explicit* allowlist (the
        # granted MCP keys plus whatever the caller named), so an ambient
        # provider credential can never leak alongside the grant -- the same
        # discipline the single-lane inline seam applies.
        child_allowlist = sorted(set(env or ()) | set(grant_env))

    handles: List[Tuple[int, Any, List[str]]] = []
    for index, argv in enumerate(commands):
        child_run_id = f"{run_id}-{index}"
        repo, commit, child_tool, child_cwd = _child_identity(index)
        try:
            handle = _start_process()(
                list(argv),
                run_id=child_run_id,
                subject_repo=repo,
                subject_commit=commit,
                tool=child_tool,
                model=resolved_models[index],
                created_at=created_at,
                cwd=child_cwd,
                env=child_env,
                env_allowlist=child_allowlist,
            )
            if mcp_grant is not None:
                handle = _mcp_bootstrap.verify_child_launch(mcp_grant, handle)
        except Exception as exc:  # noqa: BLE001
            # A child that never spawned is a launch failure, never a silent
            # gap: it still lands in ``children`` with its own outcome and a
            # message, distinguished from an exit/signal/timeout.
            handles.append((index, None, list(argv)))
            _launch_failures[index] = str(exc)
            continue
        handles.append((index, handle, list(argv)))

    # All processes are now running (started before any reap). Reap them in
    # launch order; the overlap was established by the start-before-wait shape.
    children: List[FanOutChild] = []
    for index, handle, argv in handles:
        repo, commit, child_tool, child_cwd = _child_identity(index)
        if handle is None:
            _notify_lifecycle(
                child_key=f"{run_id}-{index}",
                dispatch_id=f"{run_id}-{index}",
                phase="started",
                handle=None,
            )
            result = _launch_failed_result(
                argv,
                run_id=f"{run_id}-{index}",
                repo=repo,
                commit=commit,
                tool=child_tool,
                model=resolved_models[index],
                created_at=created_at,
                message=_launch_failures.get(index, "launch failed"),
            )
            outcome = _resolve_outcome(result)
            children.append(
                FanOutChild(
                    child_run_id=f"{run_id}-{index}",
                    command=argv,
                    result=result,
                    model=resolved_models[index],
                    subject_repo=repo,
                    subject_commit=commit,
                    tool=child_tool,
                    cwd=child_cwd,
                    raw_bytes=result.stdout or b"",
                    stderr_bytes=result.stderr or b"",
                    outcome=outcome,
                    stdout_ref=_make_receipt_reference(result.stdout_receipt, stream="stdout"),
                    stderr_ref=_make_receipt_reference(result.stderr_receipt, stream="stderr"),
                )
            )
            if artifact_store is not None:
                _store_child_bytes(artifact_store, children[-1])
            continue

        _notify_lifecycle(
            child_key=f"{run_id}-{index}",
            dispatch_id=f"{run_id}-{index}",
            phase="started",
            handle=handle,
        )
        result = handle.wait(timeout=timeout)
        outcome = _resolve_outcome(result)
        children.append(
            FanOutChild(
                child_run_id=f"{run_id}-{index}",
                command=argv,
                result=result,
                model=resolved_models[index],
                subject_repo=repo,
                subject_commit=commit,
                tool=child_tool,
                cwd=child_cwd,
                raw_bytes=result.stdout or b"",
                stderr_bytes=result.stderr or b"",
                outcome=outcome,
                stdout_ref=_make_receipt_reference(result.stdout_receipt, stream="stdout"),
                stderr_ref=_make_receipt_reference(result.stderr_receipt, stream="stderr"),
            )
        )
        if artifact_store is not None:
            _store_child_bytes(artifact_store, children[-1])
        _notify_lifecycle(
            child_key=f"{run_id}-{index}",
            dispatch_id=f"{run_id}-{index}",
            phase="terminal",
            handle=handle,
        )

    # Overlap is structurally guaranteed when more than one worker was launched
    # and all started before any wait; record it as a measured fact.
    overlapped = len(commands) > 1

    if _observation_errors:
        raise LifecycleObservationError(
            "child-lifecycle observation failed: "
            f"{_observation_errors[0]}"
        ) from _observation_errors[0]

    return FanOutResult(children=children, overlapped=overlapped)


def _resolve_spec(spec: ModelSpec) -> str:
    """Resolve a child's ``ModelSpec`` to its concrete model string."""
    from skillweave.routing.faigate_adapter import resolve_model_spec

    return resolve_model_spec(spec)


def _launch_failed_result(
    argv: List[str],
    *,
    run_id: str,
    repo: str,
    commit: str,
    tool: str,
    model: str,
    created_at: str,
    message: str,
) -> Any:
    """Construct a synthetic ``launch_failed`` result for a child that never ran.

    A child whose process could not be spawned still yields a definite,
    machine-readable result: ``termination == "launch_failed"``, no exit code and
    no signal, and empty stdout/stderr receipts bound to the child's run id so a
    caller can tell a launch failure from an exited/signalled/timed-out child.
    """
    adapter = importlib.import_module("skillweave.runtime.runner_adapter")
    ProcessResult = adapter.ProcessResult

    def _empty_receipt(stream: str) -> Any:
        return adapter._make_stream_receipt(
            stream,
            b"",
            run_id=run_id,
            command=argv,
            subject_repo=repo,
            subject_commit=commit,
            created_at=created_at,
            exit_code=None,
            signal=None,
            purpose=f"{stream} of run '{run_id}' (launch failed)",
        )

    stdout_receipt = _empty_receipt("stdout")
    stderr_receipt = _empty_receipt("stderr")
    return ProcessResult(
        command=argv,
        exit_code=None,
        signal=None,
        termination="launch_failed",
        pid=0,
        tool=tool,
        model=model,
        stdout_receipt=stdout_receipt,
        stderr_receipt=stderr_receipt,
        message=message,
        stdout=b"",
        stderr=b"",
        metadata={
            "run_id": run_id,
            "subject_repo": repo,
            "subject_commit": commit,
            "created_at": created_at,
            "tool": tool,
            "model": model,
        },
    )
