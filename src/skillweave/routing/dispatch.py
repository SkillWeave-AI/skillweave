"""Tool-agnostic dispatch: launch a role's tool and bind the result as evidence.

``profile.py`` declares what a role's tool is — its name, launch command, and
arguments (:class:`~skillweave.routing.profile.ToolSpec`). This module is the
seam that *uses* that declaration: it hands the work to the launched process,
collects what came back, and binds it to the run as evidence, never as free
text. A role that carries no tool runs in place and is recorded as having done
so; a launch that never starts is a named failure carrying the command that
failed — never a run without a result.

The adapter is tool-agnostic by construction. It receives the tool name, the
launch command, the arguments, and the work, and it does not branch on any of
them: the tool name is recorded on the result, never inspected. The first
consumer of the seam is a caller, not the shape of the interface — no concrete
tool name appears anywhere in this module.

Four criteria are discharged here:

1. A role carrying a ``ToolSpec`` is launched from its ``launch_command`` and
   ``args``; the work is handed over to that process and the result is
   collected and bound to the run as evidence — an ``ArtifactReceipt`` carrying
   the collected output, typed ``artifact`` and addressed by digest, not free
   text.

2. The adapter branches on none of its inputs. The tool name, launch command,
   arguments, and work are all passed through: none is inspected to choose a
   path. A test asserts that no concrete tool name appears in this module.

3. A role WITHOUT a ``ToolSpec`` runs in place and is recorded as having done
   so — an explicit ``InPlaceRecord``, not an absent one. Running in the current
   harness is a declared configuration and must be distinguishable afterwards
   from a dispatch that silently did not happen.

4. A failing launch is a ``DispatchFailure`` carrying the command that failed.
   A tool that never starts is never recorded as a run without a result: the
   never-started case returns a typed failure naming the command, never a
   silent empty success.

5. ``dispatch`` and ``launch_from_role`` take a ``timeout`` and pass it to the
   runtime, which already accepts one. A caller that sets none gets a
   *documented* default (:data:`DEFAULT_DISPATCH_TIMEOUT`), never an unbounded
   wait and never a number invented by whoever wrote the proof. A timeout is
   reported as a timeout (``result.termination == "timed_out"``), never as a
   failure of the tool.

6. The record distinguishes DECLARED from TERMINATED. DECLARED is the timeout
   cap the caller set (or the documented default) — carried as ``timeout`` on
   the :class:`DispatchResult` and in the artifact metadata. TERMINATED is how
   the process actually ended — ``result.termination`` ("exited", "signaled",
   "cancelled", "timed_out"). A dispatch that launched correctly and was cut
   short has ``termination == "timed_out"`` while ``succeeded`` is False; a
   later reader needs both facts, so both are recorded separately.

The launch itself is delegated to ``runtime.runner_adapter``, which owns the
process concerns (real subprocess, capture, timeout, cancel, exit/signal split).
This module contributes only what routing adds on top: reading the launch
command from the spec, tokenising it, appending the spec's arguments, handing
the work over as input, promoting the collected output into bound evidence, and
turning the three role outcomes (launched, in-place, never-started) into typed
records — and, since SW-RT-008, passing the caller's timeout through and keeping
the declared cap apart from the terminating state.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any, Optional, Sequence, Union

from skillweave.routing.profile import ToolSpec
from skillweave.routing.modelspec import ModelSpec


#: Documented default timeout (seconds) for a dispatch launch. A caller that
#: passes no timeout gets this, never an unbounded wait and never an ad-hoc
#: number. The runtime treats ``None`` as "wait forever", so the default is
#: applied here, at the seam, before the call is handed down.
DEFAULT_DISPATCH_TIMEOUT: float = 900.0


def tokenize_launch(command: str) -> list[str]:
    """Tokenise a launch command string into a process argument vector.

    A ``ToolSpec.launch_command`` is a full command line (for example
    ``"python3 -m some.module"``), not an argument vector. ``subprocess`` needs
    the latter, so the adapter splits it here with ``shlex`` (POSIX-like
    tokenisation, honouring quotes) before appending the spec's own ``args``.
    No tool name is inspected; every command string goes through the same split.
    """
    return shlex.split(command)


@dataclass
class DispatchResult:
    """What a successful dispatch produced: the process result plus the evidence.

    ``result`` is the ``ProcessResult`` from ``runtime.runner_adapter`` — the
    collected output, exit code, signal, and termination. ``artifact`` is the
    ``ArtifactReceipt`` that promotes the collected stdout into evidence bound
    to the run, so a caller can persist it wholesale without reconstructing
    text. ``tool`` and ``launch_command`` are recorded so a later run can tell
    exactly what was launched; ``run_id`` names the run the evidence belongs to.
    """

    run_id: str
    tool: str
    launch_command: str
    args: list[str]
    result: "ProcessResult"
    artifact: Optional["ArtifactReceipt"]
    timeout: Optional[float]

    @property
    def succeeded(self) -> bool:
        return self.result.succeeded

    @property
    def termination(self) -> str:
        """How the process actually ended (TERMINATED), distinct from the cap."""
        return self.result.termination


@dataclass
class InPlaceRecord:
    """A role that ran in place: an explicit record, not an absent one.

    A role without a ``ToolSpec`` runs in the current harness. That is a
    declared configuration and must be recorded as having happened, so a later
    run can tell "ran in place" from "dispatch silently did not happen".
    """

    run_id: str
    role: str
    recorded_at: str

    @property
    def in_place(self) -> bool:
        return True


@dataclass
class DispatchFailure:
    """A launch that never started: a named failure carrying the command.

    ``command`` is the full argument vector that failed to start. ``message``
    names it and the reason, so a tool that never starts is a visible failure,
    never a run without a result and never an empty success.
    """

    run_id: str
    role: str
    tool: str
    command: list[str]
    message: str

    @property
    def succeeded(self) -> bool:
        return False


RoleOutcome = Union[DispatchResult, InPlaceRecord, DispatchFailure]


def _artifact_for(
    *,
    run_id: str,
    tool: str,
    command: Sequence[str],
    subject_repo: str,
    subject_commit: str,
    created_at: str,
    stdout: bytes,
    exit_code: Optional[int],
    signal: Optional[int],
    timeout: Optional[float],
    termination: str,
) -> "ArtifactReceipt":
    """Build the ``ArtifactReceipt`` that binds the collected output to the run.

    The output is stored by digest only (``sha256``); the raw bytes are never
    the object's identity. ``metadata`` carries the run id, the tool name, the
    exit code, the signal, — and, since SW-RT-008, the DECLARED timeout and the
    TERMINATED state as two separate keys, so the receipt answers "which run,
    which tool, how did it end, and what was declared" without a second lookup.
    """
    import hashlib
    import importlib
    reg = importlib.import_module("skillweave.runtime.registry")

    return reg.ArtifactReceipt(
        artifact_id=f"dispatch-{run_id}",
        sha256=hashlib.sha256(stdout or b"").hexdigest(),
        schema_version="1",
        producer_command=" ".join(command),
        subject_repo=subject_repo,
        subject_commit=subject_commit,
        created_at=created_at,
        evidence_type=reg.EvidenceType.ARTIFACT.value,
        purpose=f"output of dispatch run '{run_id}'",
        method="dispatch",
        system_source="routing.dispatch",
        quality=reg.EvidenceQuality(
            relevance="unassessed",
            sufficiency="unassessed",
            reliability="unassessed",
            integrity="unassessed",
        ),
        metadata={
            "run_id": run_id,
            "tool": tool,
            "byte_length": len(stdout or b""),
            "exit_code": exit_code,
            "signal": signal,
            "declared_timeout": timeout,
            "termination": termination,
        },
    )


def dispatch(
    tool: ToolSpec,
    work: bytes,
    *,
    run_id: str,
    subject_repo: str,
    subject_commit: str,
    model: str,
    created_at: Optional[str] = None,
    cwd: Optional[str] = None,
    timeout: Optional[float] = None,
) -> Union[DispatchResult, DispatchFailure]:
    """Launch ``tool`` from its spec, hand over ``work``, and bind the result.

    The full command is the tokenised ``launch_command`` followed by the spec's
    ``args``. The work is handed to the child over standard input as bytes. The
    collected stdout is promoted to an ``ArtifactReceipt`` (typed ``artifact``)
    bound to ``run_id``, so it persists as evidence, not free text.

    ``timeout`` is the DECLARED cap, in seconds, passed through to the runtime.
    ``None`` (the default) resolves to :data:`DEFAULT_DISPATCH_TIMEOUT` at the
    seam — the runtime never sees an unbounded wait and never an invented
    number. A launch cut short by the cap returns ``result.termination ==
    "timed_out"`` (a timeout, never a tool failure). The declared cap is kept on
    the ``DispatchResult`` and in the artifact metadata, separate from the
    TERMINATED state.

    A launch command that cannot be started (for example a non-existent
    executable) is caught and returned as a :class:`DispatchFailure` naming the
    command — never a run without a result, and never an empty success.

    ``model`` is required (as it is throughout the runner): it travels to the
    process result so the record names which model the run resolved to, but it
    plays no part in choosing how the tool launches. Nothing here branches on
    ``tool.name``, the launch command, the arguments, or the work — the adapter
    is tool-agnostic.
    """
    argv = tokenize_launch(tool.launch_command) + [str(a) for a in tool.args]
    effective_timeout = DEFAULT_DISPATCH_TIMEOUT if timeout is None else timeout
    try:
        import importlib
        run_command = importlib.import_module("skillweave.runtime.runner_adapter").run_command
        result = run_command(
            argv,
            run_id=run_id,
            subject_repo=subject_repo,
            subject_commit=subject_commit,
            tool=tool.name,
            model=model,
            created_at=created_at,
            input_bytes=work,
            cwd=cwd,
            timeout=effective_timeout,
        )
    except (FileNotFoundError, OSError) as exc:
        return DispatchFailure(
            run_id=run_id,
            role="",
            tool=tool.name,
            command=argv,
            message=f"failed to launch {' '.join(argv)}: {exc}",
        )

    artifact = _artifact_for(
        run_id=run_id,
        tool=tool.name,
        command=argv,
        subject_repo=subject_repo,
        subject_commit=subject_commit,
        created_at=created_at or result.metadata.get("created_at", ""),
        stdout=result.stdout,
        exit_code=result.exit_code,
        signal=result.signal,
        timeout=effective_timeout,
        termination=result.termination,
    )
    return DispatchResult(
        run_id=run_id,
        tool=tool.name,
        launch_command=tool.launch_command,
        args=list(tool.args),
        result=result,
        artifact=artifact,
        timeout=effective_timeout,
    )


def run_in_place(
    role: str,
    *,
    run_id: str,
    recorded_at: Optional[str] = None,
) -> InPlaceRecord:
    """Record that ``role`` ran in place in the current harness.

    This is the explicit record for a role without a ``ToolSpec`` (criterion 3):
    it exists so "ran in place" is a positive, distinguishble fact, not an
    absent entry that later looks like a dispatch that never happened.
    """
    from datetime import datetime, timezone

    return InPlaceRecord(
        run_id=run_id,
        role=role,
        recorded_at=recorded_at or datetime.now(timezone.utc).isoformat(),
    )


def launch_from_role(
    role: str,
    tool: Optional[ToolSpec],
    work: bytes,
    *,
    run_id: str,
    subject_repo: str,
    subject_commit: str,
    model: str,
    created_at: Optional[str] = None,
    cwd: Optional[str] = None,
    timeout: Optional[float] = None,
) -> RoleOutcome:
    """Launch ``role``'s tool, or record in place when the role has none.

    Three outcomes, each a typed record, never a silent gap:

    * a role carrying a ``ToolSpec`` → :class:`DispatchResult` (launched, bound
      evidence) or :class:`DispatchFailure` (the launch never started, naming
      the command);
    * a role without a ``ToolSpec`` → :class:`InPlaceRecord`, an explicit "ran
      in place" fact.

    ``timeout`` is the DECLARED cap passed through to the launch (criterion 5);
    ``None`` resolves to the documented default at the seam. An in-place run has
    no launch and therefore carries no cap.

    Nothing here branches on the tool name, the launch command, the arguments,
    or the work: only on whether a tool is present at all, which is the one
    declared structural axis, not a content decision.
    """
    if tool is None:
        return run_in_place(role, run_id=run_id, recorded_at=created_at)

    outcome = dispatch(
        tool,
        work,
        run_id=run_id,
        subject_repo=subject_repo,
        subject_commit=subject_commit,
        model=model,
        created_at=created_at,
        cwd=cwd,
        timeout=timeout,
    )
    if isinstance(outcome, DispatchFailure):
        outcome.role = role
    return outcome


def fan_out(
    commands: Sequence[Sequence[str]],
    *,
    run_id: str,
    subject_repo: str,
    subject_commit: str,
    model: Optional[str] = None,
    models: Optional[Sequence[ModelSpec]] = None,
    tool: str,
    created_at: Optional[str] = None,
    cwd: Optional[str] = None,
    process_service: Optional[Any] = None,
    db_path: Optional[str] = None,
    mailbox_path: Optional[str] = None,
    artifacts_path: Optional[str] = None,
    stream_root: Optional[str] = None,
):
    """Fan out independent commands as overlapping real processes (SW-FANOUT-001).

    This is the dispatch-surface seam for dependency-ready fan-out. It delegates
    the process concerns to ``skillweave.fanout.fan_out_dispatch``, which starts
    every command as a real process before reaping any, so two workers overlap
    in time.

    Per-child model freedom (SW-FANOUT-001-MODELSPEC): ``models`` is an optional
    list of ``ModelSpec`` aligned to ``commands`` — one concrete id or delegated
    router+scenario per child. When absent, the single ``model`` (backward
    compatible) is lifted to ``concrete(model)`` for every child. Nothing here
    branches on the tool, the launch command, the arguments, or the work: it
    passes all of them through to the same path.

    ``process_service`` (optional, criterion 2): when a process service is
    configured (``process_service`` truthy, or one of the binding paths named),
    this mints a controller session grant for the fan-out's run scope and
    threads it into :func:`fan_out_dispatch`, so each child is launched with the
    granted MCP configuration and verified from its own resolved environment.
    Omitting it (the default) preserves the historical pass-through exactly.
    """
    from skillweave.fanout import fan_out_dispatch

    mcp_grant = None
    if process_service is not None or any(
        p is not None for p in (db_path, mailbox_path, artifacts_path, stream_root)
    ):
        from skillweave.fanout.dispatch import mint_fanout_grant

        mcp_grant = mint_fanout_grant(
            run_id=run_id,
            client_id="routing-fanout",
            db_path=db_path,
            mailbox_path=mailbox_path,
            artifacts_path=artifacts_path,
            stream_root=stream_root,
        )

    return fan_out_dispatch(
        commands,
        run_id=run_id,
        subject_repo=subject_repo,
        subject_commit=subject_commit,
        tool=tool,
        model=model,
        models=models,
        created_at=created_at,
        cwd=cwd,
        mcp_grant=mcp_grant,
    )
