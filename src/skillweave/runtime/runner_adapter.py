"""Process runner that turns a worker invocation into bound evidence.

This module is the half of the runtime that ``dagscheduler`` (010) deliberately
does not own: actually starting a process. Scheduling builds ordered batches of
tasks; this adapter executes one of them and collects what came back.

Concerns, each mapped to a dispatch criterion:

1. The worker runs as a *real* process (``subprocess``), never in-process, so a
   test can prove that a trivial command really executed (a distinct PID and
   captured output, not a mocked return value).

2. stdout and stderr are captured and bound to the run as ``ArtifactReceipt``
   evidence (the type introduced by 005), never as free text. The adapter
   hashes the raw byte streams and produces one receipt per stream, typed
   ``EvidenceType.RUNTIME_TRACE``, carrying the run id and the stream identity
   in ``metadata``. A caller can persist them wholesale via
   ``SQLiteRunStore.save_evidence`` without any text reconstruction.

3. Cancel kills the process for real, and a test may prove *no child survives*:
   the child runs in its own process group, and cancel (or timeout) kills that
   entire group and reaps it, so no descendant is left behind.

4. A timeout produces a *defined state* (``termination == "timed_out"`` on the
   returned ``ProcessResult``), never an unbounded wait and never a bare
   ``TimeoutExpired`` escaping the adapter.

5. A worker that dies without a result (signaled, cancelled, timed out, or a
   non-zero exit) is a *failure with a message*, never a silent success:
   ``ProcessResult.succeeded`` is ``False`` and ``message`` explains it.

6. Exit code and termination signal are distinguished, not conflated. POSIX
   reports a signal-terminated process as a negative returncode; the adapter
   splits that into an explicit ``exit_code`` (``None`` when signaled) and
   ``signal`` (``None`` on a clean exit).

7. The interface names no concrete provider and no concrete model. The target
   tool and the model are *required* parameters (``tool`` and ``model``, no
   default) that the caller supplies — lane 012's ``RoutingProfile`` carries
   them. A default baked into this module is the defect this criterion
   prevents: it would re-bind the whole layer to one model.

10. At the end of its batch a worker stops. ``dispatch_batch`` runs exactly
    the first batch and never dispatches a dependent batch into the live
    worker; remaining capacity is not a reason to continue.

8. One lane runs in one worker context. ``WorkerContext`` is bound to a single
   ``target_role`` and refuses to run a task for any other lane rather than
   silently serialising two lanes into the same worker.

9. A worker is handed its cold start as data — a ``ColdStartBundle`` (005) —
   never a transcript. ``start_from_bundle`` takes the bundle alone and derives
   everything it needs (the lane role, the repo, the tool and model a caller
   passes from the ``RoutingProfile``) to launch the worker.

This module imports from ``registry`` (receipt types) only. It does not touch
``dagscheduler`` (010's file) and does not modify ``store``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import hashlib
import os
import select
import signal as _signal
import subprocess
import time

from .handoff import ColdStartBundle
from .registry import ArtifactReceipt, EvidenceQuality, EvidenceType


def _hash_bytes(data: bytes) -> str:
    """Return the hex sha256 of ``data`` (empty input hashes to a stable value)."""
    return hashlib.sha256(data).hexdigest()


#: Environment keys that redirect a model request to an *alternate* provider.
#: A worker launched to run through the approved gateway must not silently
#: inherit one of these from the ambient shell: a leaked ambient key would let
#: a child's traffic bypass the gateway (SW-1510-OPENCLAUDE criterion 1). The
#: names mirror ``routing.harness_adapter.AMBIENT_PROVIDER_ENV_KEYS`` and
#: ``routing.faigate_adapter.detect_providers``.
AMBIENT_PROVIDER_ENV_KEYS: tuple[str, ...] = (
    "FAIGATE_API_KEY",
    "OPENROUTER_API_KEY",
    "CLAWROUTER_API_KEY",
    "KILO_API_KEY",
    "OMNIROUTE_API_KEY",
    "NINEROUTER_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)


def build_child_environment(
    *,
    env_allowlist: Optional[Sequence[str]],
    extra_env: Optional[dict[str, str]] = None,
    base_env: Optional[dict[str, str]] = None,
) -> Optional[dict[str, str]]:
    """Build a child's environment from an explicit allowlist.

    With ``env_allowlist`` given, *only* those keys are inherited from
    ``base_env`` (default ``os.environ``); every other ambient key — including
    every ambient alternate provider credential — is dropped, and any ambient
    provider key that slips into ``env_allowlist`` is refused (fail closed) so a
    typo cannot re-open the bypass. ``extra_env`` holds the caller's *pinned*
    settings (for example the approved gateway base URL) and is applied last.

    With ``env_allowlist=None`` the historical behaviour is preserved: the child
    inherits the parent environment (optionally extended by ``extra_env``).
    Returns ``None`` when there is nothing to pass (the child then inherits).
    """
    if env_allowlist is None:
        if not extra_env:
            return None
        return {**os.environ, **{str(k): str(v) for k, v in extra_env.items()}}
    for key in env_allowlist:
        if key in AMBIENT_PROVIDER_ENV_KEYS:
            raise ValueError(
                f"env_allowlist may not carry ambient provider key {key!r}; "
                "route the worker through the approved gateway instead"
            )
    source = os.environ if base_env is None else base_env
    env = {k: source[k] for k in env_allowlist if k in source}
    if extra_env:
        for key, value in extra_env.items():
            name = str(key)
            if name in AMBIENT_PROVIDER_ENV_KEYS:
                raise ValueError(
                    f"extra_env may not set ambient provider key {name!r}"
                )
            env[name] = str(value)
    return env


def _split_returncode(returncode: int) -> tuple[Optional[int], Optional[int]]:
    """Split a POSIX returncode into (exit_code, signal).

    A non-negative returncode is a process exit code (``signal`` is ``None``).
    A negative returncode is a low-word signal termination: ``-N`` means the
    process was killed by signal ``N`` (``exit_code`` is ``None``).

    The two are kept distinct so a consumer records both without decoding the
    sign convention itself (dispatch criterion 6).
    """
    if returncode < 0:
        return None, -returncode
    return returncode, None


def _pid_exists(pid: int) -> bool:
    """Return ``True`` if a process with ``pid`` still exists.

    ``os.kill(pid, 0)`` raises ``ProcessLookupError`` once the process is gone
    (or ``PermissionError`` when it exists but belongs to another user, which
    still means it *exists*). Used by tests to assert that a cancelled worker
    left no surviving child behind.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class ProcessResult:
    """The collected result of one worker invocation.

    ``stdout_receipt`` and ``stderr_receipt`` are ``ArtifactReceipt`` evidence,
    not raw text. ``exit_code`` and ``signal`` are mutually exclusive: exactly
    one of them is ``None`` after a completed process.

    ``termination`` records *how* the process ended, one of ``"exited"``,
    ``"signaled"``, ``"cancelled"``, or ``"timed_out"``. ``message`` is a
    human-readable failure explanation, and is ``""`` on success only.
    """

    command: List[str]
    exit_code: Optional[int]
    signal: Optional[int]
    termination: str
    pid: int
    tool: str
    model: str
    stdout_receipt: Optional[ArtifactReceipt]
    stderr_receipt: Optional[ArtifactReceipt]
    message: str = ""
    stdout: bytes = b""
    stderr: bytes = b""
    #: Bytes of stderr actually observed (equal to ``len(stderr)`` when the
    #: stream was not capped; larger than ``len(stderr)`` when a bounded run
    #: truncated the retained copy).
    stderr_observed_bytes: int = 0
    #: How many chunks arrived after the stderr retention cap was reached — the
    #: observable backpressure signal. ``0`` means the cap was never pressed.
    stderr_backpressure_events: int = 0
    #: Whether the stderr stream exceeded its configured retention cap.
    stderr_exceeded_cap: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        """True only when the worker exited cleanly with code 0 and produced.

        A cancelled, timed-out, signal-killed, or backpressure-failed worker
        never succeeds, even if by chance its process happened to exit 0 at the
        last instant. A worker whose stderr exceeded its configured retention
        cap (``stderr_exceeded_cap``) is a controlled failure: the run *did*
        produce a defined, non-silent outcome rather than buffering the stream
        whole."""
        return (
            self.termination == "exited"
            and self.exit_code == 0
            and self.signal is None
            and not self.stderr_exceeded_cap
        )

    @property
    def signaled(self) -> bool:
        return self.signal is not None


def _make_stream_receipt(
    stream: str,
    data: bytes,
    *,
    run_id: str,
    command: List[str],
    subject_repo: str,
    subject_commit: str,
    created_at: str,
    exit_code: Optional[int],
    signal: Optional[int],
    purpose: str,
) -> ArtifactReceipt:
    """Build an ``ArtifactReceipt`` for one captured byte stream.

    The receipt is bound to the run through ``metadata["run_id"]`` and the
    originating ``subject_commit``. ``stream`` is one of ``"stdout"``,
    ``"stderr"``. The bytes themselves are hashed into ``sha256``; they are not
    stored as free text, only referenced by digest.
    """
    return ArtifactReceipt(
        artifact_id=f"trace-{run_id}-{stream}",
        sha256=_hash_bytes(data),
        schema_version="1",
        producer_command=" ".join(command),
        subject_repo=subject_repo,
        subject_commit=subject_commit,
        created_at=created_at,
        evidence_type=EvidenceType.RUNTIME_TRACE.value,
        purpose=purpose,
        method="subprocess",
        system_source="runner_adapter",
        quality=EvidenceQuality(
            relevance="high",
            sufficiency="high",
            reliability="high",
            integrity="high",
        ),
        metadata={
            "run_id": run_id,
            "stream": stream,
            "byte_length": len(data),
            "exit_code": exit_code,
            "signal": signal,
        },
    )


@dataclass
class RunningProcess:
    """A live worker process the caller may wait on or cancel.

    Starts the command in its own session/process group so that cancelling it
    kills every descendant the worker spawned, leaving no surviving child.
    """

    process: subprocess.Popen
    command: List[str]
    _run_id: str
    _subject_repo: str
    _subject_commit: str
    _tool: str
    _model: str
    _created_at: Optional[str] = None
    _cancelled: bool = False
    #: the *effective* environment the child was launched with, so a downstream
    #: verifier can prove the actual launch resolved the granted MCP service
    #: rather than trusting the interactive harness profile (SW-1511-MCP-BOOTSTRAP).
    _sw_effective_env: Optional[Dict[str, str]] = None
    _stderr_retain_limit: Optional[int] = None
    _stderr_observed: int = 0
    _stderr_backpressure: int = 0
    _stderr_capped: bool = False

    @property
    def pid(self) -> int:
        return self.process.pid

    def _collect(
        self,
        *,
        timeout: Optional[float] = None,
        input_bytes: Optional[bytes] = None,
    ) -> tuple[bytes, bytes, bool]:
        """Drain both pipes, capping the retained stderr at the retain limit.

        Returns ``(stdout, stderr_retained, timed_out)``. stdout is collected
        unbounded (bounded by the timeout, exactly as before). When a stderr
        retention cap is configured, bytes beyond the cap are counted and
        dropped rather than retained, and each capped chunk increments the
        backpressure counter — the stream is *drained*, never deadlocked, so the
        child is not blocked on a full pipe. With no cap this is byte-for-byte
        the previous ``communicate`` behaviour.
        """
        cap = self._stderr_retain_limit
        if cap is None:
            try:
                stdout_b, stderr_b = self.process.communicate(
                    input=input_bytes, timeout=timeout
                )
            except subprocess.TimeoutExpired:
                self._kill_group()
                stdout_b, stderr_b = self.process.communicate()
                return stdout_b or b"", stderr_b or b"", True
            return stdout_b or b"", stderr_b or b"", False

        # Bounded stderr: stream both pipes ourselves so a large stderr burst
        # is capped rather than buffered whole.
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        retained = 0
        started = time.monotonic()
        if input_bytes:
            try:
                self.process.stdin.write(input_bytes)  # type: ignore[union-attr]
                self.process.stdin.close()  # type: ignore[union-attr]
            except (BrokenPipeError, OSError):
                pass
        elif self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        streams = [fd for fd in (self.process.stdout, self.process.stderr) if fd is not None]
        timed_out = False
        while streams:
            ready, _, _ = select.select(streams, [], [], 0.1)
            for fd in ready:
                chunk = os.read(fd.fileno(), 4096)
                if not chunk:
                    streams = [s for s in streams if s is not fd]
                    continue
                if fd is self.process.stdout:
                    stdout_chunks.append(chunk)
                else:
                    self._stderr_observed += len(chunk)
                    room = cap - retained
                    if room > 0:
                        keep = chunk[:room]
                        stderr_chunks.append(keep)
                        retained += len(keep)
                        if len(chunk) > room:
                            self._stderr_capped = True
                            self._stderr_backpressure += 1
                    else:
                        self._stderr_capped = True
                        self._stderr_backpressure += 1
            if not streams:
                break
            if self.process.poll() is not None:
                # Drain whatever is still buffered, capping stderr the same way.
                for fd in list(streams):
                    try:
                        rest = os.read(fd.fileno(), 1 << 20)
                    except OSError:
                        rest = b""
                    while rest:
                        if fd is self.process.stdout:
                            stdout_chunks.append(rest)
                        else:
                            self._stderr_observed += len(rest)
                            room = cap - retained
                            if room > 0:
                                keep = rest[:room]
                                stderr_chunks.append(keep)
                                retained += len(keep)
                                self._stderr_capped = True
                                if len(rest) > room:
                                    self._stderr_backpressure += 1
                            else:
                                self._stderr_backpressure += 1
                        try:
                            rest = os.read(fd.fileno(), 1 << 20)
                        except OSError:
                            rest = b""
                    streams = [s for s in streams if s is not fd]
                break
            if timeout is not None and (time.monotonic() - started) > timeout:
                timed_out = True
                self._kill_group()
                break
        return b"".join(stdout_chunks), b"".join(stderr_chunks), timed_out

    def _kill_group(self) -> None:
        """Terminate the child's whole process group, then reap it.

        SIGTERM first (graceful), then SIGKILL after a short grace period for
        workers that ignored the polite ask. Killing by *group* is what lets the
        cancel test assert that no descendant survives.
        """
        try:
            os.killpg(self.process.pid, _signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.process.pid, _signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            self.process.wait()
        except ProcessLookupError:
            pass

    def cancel(self) -> ProcessResult:
        """Cancel the worker and return an explicit ``cancelled`` result.

        Always kills the process group and reaps the child, so after this
        returns there is no surviving child (criterion 3)."""
        self._cancelled = True
        self._kill_group()

        from datetime import datetime, timezone

        created_at = self._created_at or datetime.now(timezone.utc).isoformat()

        # Drain through the same collector so a capped stderr burst is counted
        # on the cancel path too (criterion 3), not just on a clean wait.
        stdout_bytes, stderr_bytes, _ = self._collect()

        return self._build_result(
            exit_code=None,
            signal=None,
            termination="cancelled",
            created_at=created_at,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            message=f"worker cancelled by request ({' '.join(self.command)})",
        )

    def wait(
        self,
        timeout: Optional[float] = None,
        input_bytes: Optional[bytes] = None,
    ) -> ProcessResult:
        """Wait for the worker to finish, honouring an optional timeout.

        On timeout the worker's whole process group is killed and a definite
        ``timed_out`` result is returned (criterion 4) instead of raising.
        """
        from datetime import datetime, timezone

        stdout_bytes, stderr_bytes, timed_out = self._collect(
            timeout=timeout, input_bytes=input_bytes
        )
        # Reap the child so ``returncode`` is definite before we split it; the
        # uncapped path already reaped via ``communicate`` and this is a no-op.
        try:
            self.process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass

        returncode = self.process.returncode
        created_at = self._created_at or datetime.now(timezone.utc).isoformat()

        if timed_out:
            return self._build_result(
                exit_code=None,
                signal=None,
                termination="timed_out",
                created_at=created_at,
                stdout_bytes=stdout_bytes or b"",
                stderr_bytes=stderr_bytes or b"",
                message=(
                    f"worker timed out after {timeout}s "
                    f"({' '.join(self.command)})"
                ),
            )

        exit_code, signal = _split_returncode(returncode)
        termination = "signaled" if signal is not None else "exited"
        message = ""
        if termination == "signaled":
            message = (
                f"worker died on signal {signal} without a result "
                f"({' '.join(self.command)})"
            )
        elif exit_code != 0:
            message = (
                f"worker exited with code {exit_code} without success "
                f"({' '.join(self.command)})"
            )

        return self._build_result(
            exit_code=exit_code,
            signal=signal,
            termination=termination,
            created_at=created_at,
            stdout_bytes=stdout_bytes or b"",
            stderr_bytes=stderr_bytes or b"",
            message=message,
        )

    def _build_result(
        self,
        *,
        exit_code: Optional[int],
        signal: Optional[int],
        termination: str,
        created_at: str,
        stdout_bytes: bytes,
        stderr_bytes: bytes,
        message: str,
    ) -> ProcessResult:
        stdout_bytes = stdout_bytes if stdout_bytes is not None else b""
        stderr_bytes = stderr_bytes if stderr_bytes is not None else b""

        stdout_receipt = _make_stream_receipt(
            "stdout",
            stdout_bytes,
            run_id=self._run_id,
            command=self.command,
            subject_repo=self._subject_repo,
            subject_commit=self._subject_commit,
            created_at=created_at,
            exit_code=exit_code,
            signal=signal,
            purpose=f"stdout of run '{self._run_id}'",
        )
        stderr_receipt = _make_stream_receipt(
            "stderr",
            stderr_bytes,
            run_id=self._run_id,
            command=self.command,
            subject_repo=self._subject_repo,
            subject_commit=self._subject_commit,
            created_at=created_at,
            exit_code=exit_code,
            signal=signal,
            purpose=f"stderr of run '{self._run_id}'",
        )

        return ProcessResult(
            command=self.command,
            exit_code=exit_code,
            signal=signal,
            termination=termination,
            pid=self.pid,
            tool=self._tool,
            model=self._model,
            stdout_receipt=stdout_receipt,
            stderr_receipt=stderr_receipt,
            message=message,
            stdout=stdout_bytes,
            stderr=stderr_bytes,
            stderr_observed_bytes=self._stderr_observed,
            stderr_backpressure_events=self._stderr_backpressure,
            stderr_exceeded_cap=self._stderr_capped,
            metadata={
                "run_id": self._run_id,
                "subject_repo": self._subject_repo,
                "subject_commit": self._subject_commit,
                "created_at": created_at,
                "tool": self._tool,
                "model": self._model,
                "stderr_observed_bytes": self._stderr_observed,
                "stderr_backpressure_events": self._stderr_backpressure,
                "stderr_exceeded_cap": self._stderr_capped,
            },
        )


def start_process(
    command: Sequence[str],
    *,
    run_id: str,
    subject_repo: str,
    subject_commit: str,
    tool: str,
    model: str,
    created_at: Optional[str] = None,
    cwd: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    env_allowlist: Optional[Sequence[str]] = None,
    stderr_retain_limit: Optional[int] = None,
) -> RunningProcess:
    """Start ``command`` as a real process and return a live handle.

    ``tool`` and ``model`` are *required* and carry no default here: they are
    the target tool and model resolved by lane 012's ``RoutingProfile``. This
    module names no concrete provider and no concrete model (criterion 7).

    The worker runs in its own session (process group) so ``cancel()`` and
    ``wait(timeout=...)`` can kill every descendant it spawned. Use
    ``start_process`` when the caller needs cancel/timeout semantics;
    ``run_command`` is the blocking convenience wrapper over it.

    ``env`` (optional) is a set of extra environment variables merged into the
    child process's environment. The worker can discover the message channel
    via ``os.environ["SW_MSG_CHANNEL"]`` when ``env`` carries that key.

    ``env_allowlist`` (optional) switches the child environment to an explicit
    allowlist via :func:`build_child_environment`: only those keys are inherited
    and every ambient alternate-provider credential is dropped, so a worker
    pinned to the approved gateway cannot be redirected (criterion 1). With
    ``env_allowlist=None`` the historical inherit behaviour is preserved and
    ``env`` is applied as extra variables.

    ``stderr_retain_limit`` (optional) caps how many stderr bytes are retained;
    bytes beyond the cap are counted as backpressure instead of buffered whole
    (criterion 3). With ``None`` stderr is retained unbounded as before.
    """
    if env_allowlist is None:
        child_env = build_child_environment(
            env_allowlist=None, extra_env=env
        )
    else:
        child_env = build_child_environment(
            env_allowlist=env_allowlist, extra_env=env
        )
    proc = subprocess.Popen(
        list(command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=child_env,
        start_new_session=True,
    )
    return RunningProcess(
        process=proc,
        command=list(command),
        _run_id=run_id,
        _subject_repo=subject_repo,
        _subject_commit=subject_commit,
        _tool=tool,
        _model=model,
        _created_at=created_at,
        _sw_effective_env=(
            {str(k): str(v) for k, v in child_env.items()}
            if child_env is not None
            else None
        ),
        _stderr_retain_limit=stderr_retain_limit,
    )


def run_command(
    command: Sequence[str],
    *,
    run_id: str,
    subject_repo: str,
    subject_commit: str,
    tool: str,
    model: str,
    created_at: Optional[str] = None,
    input_bytes: Optional[bytes] = None,
    timeout: Optional[float] = None,
    cwd: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    env_allowlist: Optional[Sequence[str]] = None,
    stderr_retain_limit: Optional[int] = None,
) -> ProcessResult:
    """Run ``command`` to completion and return bound evidence.

    A blocking convenience over ``start_process``. ``tool`` and ``model`` are
    required (no default here) — they travel from the ``RoutingProfile`` and
    are recorded on the result. ``timeout``, when set, yields a defined
    ``termination == "timed_out"`` result rather than raising; a worker that
    dies without producing a result is reported as a failure with a message,
    never a silent success.

    ``env``, ``env_allowlist`` and ``stderr_retain_limit`` are forwarded to
    ``start_process`` (criterion 1 allowlist and criterion 3 stderr cap).
    """
    handle = start_process(
        command,
        run_id=run_id,
        subject_repo=subject_repo,
        subject_commit=subject_commit,
        tool=tool,
        model=model,
        created_at=created_at,
        cwd=cwd,
        env=env,
        env_allowlist=env_allowlist,
        stderr_retain_limit=stderr_retain_limit,
    )
    return handle.wait(timeout=timeout, input_bytes=input_bytes)


@dataclass
class BatchRun:
    """The outcome of dispatching one batch of tasks.

    ``ran_batch`` is the index (always ``0``) of the batch that actually ran
    and ``batches_total`` is how many were handed over. ``stopped`` is always
    ``True``: a worker stops at the end of its batch (criterion 10) and never
    carries on into a dependent batch.
    """

    results: List[ProcessResult]
    ran_batch: int
    batches_total: int
    stopped: bool = True


def dispatch_batch(
    batches: Sequence[Sequence[Sequence[str]]],
    *,
    run_id: str,
    subject_repo: str,
    subject_commit: str,
    tool: str,
    model: str,
    created_at: Optional[str] = None,
    cwd: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    env_allowlist: Optional[Sequence[str]] = None,
    stderr_retain_limit: Optional[int] = None,
) -> BatchRun:
    """Run exactly the first batch of tasks, then stop.

    ``batches`` is an ordered list of batches (each batch is a list of command
    line argument vectors), the shape ``dagscheduler`` produces. This adapter
    runs only ``batches[0]`` and returns immediately; it never dispatches a
    dependent batch into the live worker, and remaining capacity is not a
    reason to continue (criterion 10).

    ``tool`` and ``model`` are required and carry no default — the caller
    supplies them from the ``RoutingProfile`` (criterion 7). ``env``,
    ``env_allowlist`` and ``stderr_retain_limit`` reach every task's process.
    """
    if not batches:
        return BatchRun(results=[], ran_batch=0, batches_total=0, stopped=True)

    first = batches[0]
    results: List[ProcessResult] = []
    for index, argv in enumerate(first):
        result = run_command(
            list(argv),
            run_id=f"{run_id}-{index}",
            subject_repo=subject_repo,
            subject_commit=subject_commit,
            tool=tool,
            model=model,
            created_at=created_at,
            cwd=cwd,
            env=env,
            env_allowlist=env_allowlist,
            stderr_retain_limit=stderr_retain_limit,
        )
        results.append(result)

    return BatchRun(
        results=results,
        ran_batch=0,
        batches_total=len(batches),
        stopped=True,
    )


class WorkerContextError(Exception):
    """A worker was asked to run outside its single-lane context.

    Raised instead of silently serialising a second lane into an already-bound
    worker (criterion 8).
    """

    def __init__(self, bound_role: str, attempted_role: str):
        self.bound_role = bound_role
        self.attempted_role = attempted_role
        super().__init__(
            f"worker is bound to lane '{bound_role}', "
            f"refusing to run lane '{attempted_role}' in the same context"
        )


@dataclass
class WorkerContext:
    """A single-lane worker context (criterion 8).

    The context is bound to exactly one lane, identified by ``target_role``.
    There is no path that places two lanes in the same worker: ``run`` refuses
    any ``run_lane`` that differs from the bound role rather than silently
    serialising the second lane into this worker.
    """

    target_role: str
    tool: str
    model: str
    _bound: bool = False

    def __post_init__(self) -> None:
        if not self.target_role:
            raise WorkerContextError(self.target_role, "")
        self._bound = True

    @property
    def is_bound(self) -> bool:
        return self._bound

    def run(
        self,
        command: Sequence[str],
        *,
        run_lane: str,
        run_id: str,
        subject_repo: str,
        subject_commit: str,
        created_at: Optional[str] = None,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        env_allowlist: Optional[Sequence[str]] = None,
        stderr_retain_limit: Optional[int] = None,
    ) -> ProcessResult:
        """Run one task for ``run_lane``, refusing any lane but the bound role.

        The check happens *before* any process is started: a mismatched lane is
        a hard failure, never a silent serialisation into the live worker.
        """
        if run_lane != self.target_role:
            raise WorkerContextError(self.target_role, run_lane)
        return run_command(
            list(command),
            run_id=run_id,
            subject_repo=subject_repo,
            subject_commit=subject_commit,
            tool=self.tool,
            model=self.model,
            created_at=created_at,
            cwd=cwd,
            env=env,
            env_allowlist=env_allowlist,
            stderr_retain_limit=stderr_retain_limit,
        )


def start_from_bundle(
    bundle: ColdStartBundle,
    command: Sequence[str],
    *,
    tool: str,
    model: str,
    run_id: str,
    created_at: Optional[str] = None,
    cwd: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    env_allowlist: Optional[Sequence[str]] = None,
    stderr_retain_limit: Optional[int] = None,
) -> ProcessResult:
    """Start a worker from its ``ColdStartBundle`` alone (criterion 9).

    The bundle is pure data, not a transcript, and is the only source of the
    lane's identity here: ``target_role`` is the lane, ``repo_uri`` is the
    subject repository, and ``prd_digest`` pins the subject commit the worker
    was handed. ``tool`` and ``model`` still arrive from the ``RoutingProfile``
    (criterion 7) — the bundle carries data, not a baked model.
    """
    lane = bundle.target_role
    if not lane:
        raise WorkerContextError("", "")
    ctx = WorkerContext(target_role=lane, tool=tool, model=model)
    return ctx.run(
        list(command),
        run_lane=lane,
        run_id=run_id,
        subject_repo=bundle.repo_uri,
        subject_commit=bundle.prd_digest,
        created_at=created_at,
        cwd=cwd,
        env=env,
        env_allowlist=env_allowlist,
        stderr_retain_limit=stderr_retain_limit,
    )
