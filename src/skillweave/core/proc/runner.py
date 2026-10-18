"""Production process collection: bounded run_process and durable streaming.

``run_process`` is the historical bounded collector: it runs a child, caps the
total output, redacts secrets, and returns decoded strings. It is preserved
unchanged for its existing callers.

``run_process_streaming`` is the production spawn/collection path for
SW-1510-STREAM. It starts the same kind of real child but, instead of returning
two decoded strings, durably streams per-attempt stdout/stderr (interleaved
under one monotonic cursor) into a ``DurableStreamStore`` with exact per-chunk
content digests, per-stream retention caps, and observable backpressure. It
records the observed exit code, the *measured* monotonic duration, and the raw
result subtype as immutable facts. Nothing it records coerces an error to
success, clips a duration, or mistakes a bounded retained prefix for the whole
stream.

The streaming path imports the runtime store lazily (the ``skillweave.runtime``
subpackage is optional, GLE-020) so importing this foundational module never
hard-depends on it.
"""

import os
import subprocess
import signal
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

class ProcessLimitExceeded(Exception):
    pass

class ScopeEnforcementUnavailable(RuntimeError):
    """No real isolation is available for the chosen workload; HOLD before launch.

    Raised *before* any unsafe launch. A lexical allowlist or an in-process role
    matrix is not isolation, so when the enforcement the workload needs cannot
    be attested the runner refuses rather than starting an unconfined child.
    """

    def __init__(self, workload: str, reason: str):
        self.workload = workload
        self.reason = reason
        super().__init__(
            f"isolation unavailable for workload {workload!r}: {reason} (HOLD)"
        )

    def to_hold(self) -> Dict[str, Any]:
        """A typed HOLD record a caller can surface without retrying unsafely."""
        return {
            "kind": "HOLD",
            "reason": "isolation_unavailable",
            "workload": self.workload,
            "detail": self.reason,
        }


class ScopeViolation(RuntimeError):
    """A mutation target lies outside the enforced execution scope.

    Raised before the target is touched (and, for a declared write set, before
    the child is even spawned). Names the raw target, the resolved target and
    the concrete scope so the refusal is attributable.
    """

    def __init__(self, target: str, resolved: Optional[str] = None, reason: str = ""):
        self.target = target
        self.resolved = resolved
        self.reason = reason
        super().__init__(
            f"write scope violation for {target!r}"
            + (f" (resolved {resolved!r})" if resolved else "")
            + (f": {reason}" if reason else "")
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "error": "scope_violation",
            "target": self.target,
            "resolved": self.resolved,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class IsolationAttestation:
    """The harness's record of the isolation actually in force for a launch.

    Every field is *observed*, never declared: ``adapter`` names the real OS
    mechanism, ``denies_writes``/``denies_reads`` are its proven capabilities,
    and ``protected_roots`` are the concrete filesystem roots it seals for the
    lifetime of the child. ``tools`` records that every spawned tool — including
    Bash children — runs inside this same window.
    """

    adapter: str
    denies_writes: bool
    denies_reads: bool
    confines_to_scope: bool
    protected_roots: List[str]
    checkout_root: Optional[str]
    tools: List[str]
    workload: str

    @property
    def isolates_all_tools(self) -> bool:
        """True when the attested window denies writes for every tool."""
        return self.denies_writes

    def to_dict(self) -> Dict[str, Any]:
        return {
            "adapter": self.adapter,
            "denies_writes": self.denies_writes,
            "denies_reads": self.denies_reads,
            "confines_to_scope": self.confines_to_scope,
            "isolates_all_tools": self.isolates_all_tools,
            "protected_roots": list(self.protected_roots),
            "checkout_root": self.checkout_root,
            "tools": list(self.tools),
            "workload": self.workload,
        }


@dataclass
class ScopeEnforcement:
    """The real, composed write-scope + isolation boundary for one launch.

    ``isolation`` is a live protection window (a ``StoreProtection`` from
    ``runtime.worker_launcher``) that owns a real OS mechanism. ``scope`` is a
    normalized execution scope (an ``ExecutionScope`` from
    ``runtime.write_scope``) whose ``check(target)`` resolves a target and
    returns the resolved in-scope path or ``None``. ``protected_roots`` are the
    receipt/store/telemetry directories a write must never reach.

    The runner calls :meth:`authorize_writes` for every declared output before
    spawning the child (so traversal/symlink escapes are refused up front) and
    :meth:`attest` to record what the OS actually enforces. :meth:`check_write`
    is available to re-validate a target at any point during or after the run.
    """

    isolation: Any = None
    scope: Any = None
    protected_roots: Sequence[str] = ()
    checkout_root: Optional[str] = None
    require_isolation: bool = True
    require_checkout_confinement: bool = False
    workload: str = "generic"
    tools: Sequence[str] = ("Bash",)
    _entered: bool = False
    _attestation: Optional[IsolationAttestation] = None

    def __post_init__(self) -> None:
        self.protected_roots = [str(os.path.realpath(os.path.abspath(p))) for p in self.protected_roots]
        if self.checkout_root is None and self.scope is not None:
            self.checkout_root = getattr(self.scope, "checkout_root", None)
        self.tools = list(self.tools)

    # -- attestation --------------------------------------------------------

    def attest(self) -> IsolationAttestation:
        """Return the observed isolation, or raise a typed HOLD.

        A workload that requires isolation but has no write-denying adapter is
        refused (:class:`ScopeEnforcementUnavailable`) before any launch. A
        workload that additionally requires checkout confinement — only writes
        inside the attempt checkout are allowed anywhere — is refused when the
        adapter cannot confine a process to that tree (a write-sealing adapter
        locks the protected stores but not an arbitrary outside path).
        """
        if self.isolation is None:
            if self.require_isolation:
                raise ScopeEnforcementUnavailable(
                    self.workload, "no isolation adapter was supplied"
                )
            return IsolationAttestation(
                adapter="none",
                denies_writes=False,
                denies_reads=False,
                confines_to_scope=False,
                protected_roots=list(self.protected_roots),
                checkout_root=self.checkout_root,
                tools=list(self.tools),
                workload=self.workload,
            )
        denies_writes = bool(getattr(self.isolation, "denies_writes", False))
        denies_reads = bool(getattr(self.isolation, "denies_reads", False))
        confines = bool(getattr(self.isolation, "confines_to_scope", False))
        adapter = str(getattr(self.isolation, "adapter_name", "unknown"))
        if self.require_isolation and not denies_writes:
            raise ScopeEnforcementUnavailable(
                self.workload, f"adapter {adapter!r} does not deny writes"
            )
        if self.require_checkout_confinement and not confines:
            raise ScopeEnforcementUnavailable(
                self.workload,
                f"adapter {adapter!r} does not confine writes to the attempt "
                f"checkout; checkout confinement is required",
            )
        return IsolationAttestation(
            adapter=adapter,
            denies_writes=denies_writes,
            denies_reads=denies_reads,
            confines_to_scope=confines,
            protected_roots=list(self.protected_roots),
            checkout_root=self.checkout_root,
            tools=list(self.tools),
            workload=self.workload,
        )

    @property
    def attestation(self) -> Optional[IsolationAttestation]:
        return self._attestation

    # -- write authorization ------------------------------------------------

    def _under_protected(self, resolved: str) -> Optional[str]:
        for root in self.protected_roots:
            if resolved == root or resolved.startswith(root + os.sep):
                return root
        return None

    def check_write(self, target: str) -> str:
        """Resolve and authorize one write *target*, or raise :class:`ScopeViolation`.

        The target is fully realpath-resolved. A target under any receipt/store/
        telemetry root is refused even when it would otherwise be inside the
        checkout. Otherwise the normalized execution scope decides; a scope that
        returns ``None`` (traversal escape, symlink leaving the checkout, or an
        explicit deny) is refused.
        """
        if not isinstance(target, str) or not target:
            raise ScopeViolation(str(target), None, "target must be a non-empty string")
        resolved = os.path.realpath(os.path.abspath(target))
        protected = self._under_protected(resolved)
        if protected is not None:
            raise ScopeViolation(
                target, resolved,
                f"target is inside the protected store root {protected!r}",
            )
        if self.scope is not None:
            try:
                checked = self.scope.check(target)
            except ScopeViolation:
                raise
            except Exception as exc:
                # The scope implementation refused the target by raising (e.g. a
                # lexical parent-directory preflight). Normalize it to this
                # boundary's typed refusal, preserving the reason, so the runner
                # emits exactly one refusal type rather than the scope's own.
                raise ScopeViolation(target, resolved, str(exc)) from exc
            if checked is None:
                raise ScopeViolation(
                    target, resolved,
                    "target escapes the attempt checkout (traversal, symlink or deny)",
                )
            return checked
        return resolved

    def authorize_writes(self, targets: Sequence[str]) -> List[str]:
        """Authorize every declared output before launch; first violation raises."""
        return [self.check_write(t) for t in targets]

    # -- command wrapping / window lifetime ---------------------------------

    def wrap_command(self, command: Sequence[str], *,
                     allowed_egress_host: str = "",
                     allowed_egress_port: int = 0) -> List[str]:
        """Return the argv actually spawned (the adapter may wrap it).

        When *allowed_egress_host*/*allowed_egress_port* are set, the
        isolation adapter's wrap (e.g. Seatbelt profile) also allows
        outbound TCP to only that address (SW-1510-ROUTE-LOCK criterion 2,
        composed WITH the store protection).
        """
        if self.isolation is None or not hasattr(self.isolation, "wrap_command"):
            return [str(c) for c in command]
        import pathlib

        scope = pathlib.Path(self.checkout_root) if self.checkout_root else None
        try:
            return self.isolation.wrap_command(
                command, writable_scope=scope,
                allowed_egress_host=allowed_egress_host,
                allowed_egress_port=allowed_egress_port,
            )
        except TypeError:
            return [str(c) for c in command]

    def __enter__(self) -> "ScopeEnforcement":
        self._attestation = self.attest()
        if self.isolation is not None and hasattr(self.isolation, "__enter__"):
            self.isolation.__enter__()
        self._entered = True
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._entered and self.isolation is not None and hasattr(self.isolation, "__exit__"):
            self.isolation.__exit__(exc_type, exc, tb)
        self._entered = False
        return False

def redact_secrets(text: str, secrets: List[str]) -> str:
    """Redact secret strings from text."""
    if not text:
        return text
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***REDACTED***")
    return text

def run_process(
    cmd: List[str],
    env_allowlist: Optional[List[str]] = None,
    extra_env: Optional[Dict[str, str]] = None,
    secrets: Optional[List[str]] = None,
    output_limit_bytes: int = 1024 * 1024, # 1MB limit by default
    enforcement: Optional[ScopeEnforcement] = None,
    declared_writes: Optional[Sequence[str]] = None,
) -> Tuple[int, str, str]:
    """
    Run a process with environment allowlist, secret redaction, and output limits.
    Returns (returncode, stdout, stderr).

    When ``enforcement`` is supplied, the child is launched inside the real
    write-scope / isolation window: declared writes are authorized up front (a
    traversal/symlink escape or a protected receipt/store/telemetry path raises
    :class:`ScopeViolation` before anything starts), the isolation is attested
    (an unavailable enforcement raises :class:`ScopeEnforcementUnavailable`, a
    typed HOLD), and the spawned argv is the adapter-wrapped command so every
    tool — including Bash children — runs inside that same window.
    """
    secrets = secrets or []

    # 1. Environment allowlist
    env = {}
    if env_allowlist is not None:
        for key in env_allowlist:
            if key in os.environ:
                env[key] = os.environ[key]
    else:
        env = os.environ.copy()

    if extra_env:
        env.update(extra_env)

    opened = False
    if enforcement is not None:
        # Refuse unsafe launches before spawning: attest the isolation (HOLD if
        # unavailable) and authorize every declared output against the resolved
        # scope and the protected receipt/store/telemetry roots.
        enforcement.attest()
        if declared_writes:
            enforcement.authorize_writes(declared_writes)
        cmd = enforcement.wrap_command(cmd)
        # Guarantee the real protection window is *open* for the lifetime of the
        # child. Some adapters (Seatbelt) apply per-launch inside ``wrap_command``
        # and need no window; others (the immutable-flag adapter) seal the
        # protected stores only inside ``__enter__``. Entering here means a
        # caller that passed an already-open enforcement (``__enter__`` done)
        # does not re-enter, while a caller that passed a prepared one gets the
        # seal held across the child and released in the finally below.
        if enforcement.isolation is not None and not enforcement._entered:
            enforcement.__enter__()
            opened = True

    # Start process
    try:
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False # read as bytes for exact limit
        )
    except BaseException:
        if opened:
            enforcement.__exit__(None, None, None)
        raise

    stdout_chunks = []
    stderr_chunks = []
    total_bytes = 0

    import select

    # 3. Output/artifact limits and read incrementally
    try:
        while True:
            reads = [proc.stdout, proc.stderr]
            reads = [r for r in reads if r is not None]
            if not reads:
                break
                
            ret = select.select(reads, [], [], 0.1)
            for fd in ret[0]:
                chunk = os.read(fd.fileno(), 4096)
                if not chunk:
                    if fd == proc.stdout:
                        proc.stdout = None
                    else:
                        proc.stderr = None
                    continue
                    
                total_bytes += len(chunk)
                if fd == proc.stdout:
                    stdout_chunks.append(chunk)
                else:
                    stderr_chunks.append(chunk)
                    
                if total_bytes > output_limit_bytes:
                    proc.kill()
                    raise ProcessLimitExceeded(f"Output limit of {output_limit_bytes} bytes exceeded")
            
            if proc.poll() is not None:
                # Read remaining
                if proc.stdout:
                    chunk = proc.stdout.read()
                    if chunk:
                        stdout_chunks.append(chunk)
                        total_bytes += len(chunk)
                if proc.stderr:
                    chunk = proc.stderr.read()
                    if chunk:
                        stderr_chunks.append(chunk)
                        total_bytes += len(chunk)
                        
                if total_bytes > output_limit_bytes:
                    proc.kill()
                    raise ProcessLimitExceeded(f"Output limit of {output_limit_bytes} bytes exceeded")
                break
                
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        # Release the protection window this call opened, after the child is
        # gone, so an unseal failure can never be masked by an in-flight child.
        if opened:
            enforcement.__exit__(None, None, None)

    stdout_str = b"".join(stdout_chunks).decode("utf-8", errors="replace")
    stderr_str = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    
    # 2. Secret redaction
    stdout_str = redact_secrets(stdout_str, secrets)
    stderr_str = redact_secrets(stderr_str, secrets)
    
    return proc.returncode, stdout_str, stderr_str


# ---------------------------------------------------------------------------
# Durable streaming collection (SW-1510-STREAM criterion 1 + 2)
# ---------------------------------------------------------------------------

class StreamLaunchError(Exception):
    """A child could not be started at all.

    Distinct from a child that ran and failed: a launch failure is its own raw
    result subtype (``launch_failed``), never a fabricated exit code and never a
    silent success.
    """


@dataclass
class StreamingResult:
    """The collected result of one streamed attempt.

    The digests and observed counts are exact over the bytes the child actually
    wrote; the immutable ``fact`` carries the observed exit code, the measured
    monotonic duration, and the raw result subtype. ``stdout_text``/``stderr_text``
    are redacted convenience views only — the durable evidence is in the store,
    addressed by cursor.
    """

    command: List[str]
    exit_code: Optional[int]
    signal: Optional[int]
    termination: str
    result_subtype: str
    pid: int
    duration_ns: int
    run_id: str
    task_id: str
    attempt: int
    stdout_digest: str
    stderr_digest: str
    stdout_observed_bytes: int
    stderr_observed_bytes: int
    stdout_incompleteness: str
    stderr_incompleteness: str
    store: Any = None
    fact: Any = None
    stdout_text: str = ""
    stderr_text: str = ""
    message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        return self.duration_ns / 1_000_000_000.0

    @property
    def succeeded(self) -> bool:
        return (
            self.termination == "exited"
            and self.exit_code == 0
            and self.signal is None
        )


def _subtype(termination: str) -> str:
    return {
        "exited": "exited",
        "signaled": "signaled",
        "cancelled": "cancelled",
        "timed_out": "timed_out",
        "launch_failed": "launch_failed",
    }.get(termination, termination)


def _build_env(
    env_allowlist: Optional[List[str]],
    extra_env: Optional[Dict[str, str]],
) -> Dict[str, str]:
    if env_allowlist is not None:
        env = {k: os.environ[k] for k in env_allowlist if k in os.environ}
    else:
        env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return env


def run_process_streaming(
    cmd: List[str],
    *,
    run_id: str,
    store: Any,
    attempt: int = 1,
    task_id: str = "task-1",
    env_allowlist: Optional[List[str]] = None,
    extra_env: Optional[Dict[str, str]] = None,
    secrets: Optional[List[str]] = None,
    stdout_retain_limit: Optional[int] = None,
    stderr_retain_limit: Optional[int] = None,
    timeout: Optional[float] = None,
    cwd: Optional[str] = None,
    wrap_command: Optional[Any] = None,
    liveness_recorder: Optional[Any] = None,
) -> StreamingResult:
    """Spawn ``cmd`` and durably stream its stdout/stderr into ``store``.

    * Per-stream retention caps bound the bytes kept as evidence; when a stream
      reaches its cap the child is still drained (never deadlocked) and the
      pressed point is recorded — the exact digest keeps covering every observed
      byte, so a bounded retainer is never mistaken for the whole stream.
    * Interleaving is real: chunks are ingested in the order ``select`` reports
      them ready, under one monotonic cursor shared by both streams.
    * The duration is measured with ``time.monotonic`` around the live child and
      is never clipped. The raw result subtype is derived from how the process
      actually ended.
    * ``secrets`` redact the returned convenience text only; the durable bytes
      and their digests stay exact.
    * ``liveness_recorder`` is the owned-attempt liveness watcher
      (``SW-1511A-LIVENESS-001``): when omitted a recorder is built for the
      launched child, sampled from this supervision loop and finished with the
      attempt's typed fact.  Liveness evidence is additive -- it is attached to
      the result's metadata and never changes the outcome of the attempt.

    A launch failure is recorded as its own subtype; it does not raise out of
    this collector with a fabricated exit code.
    """
    secrets = secrets or []

    # Resolve the optional runtime subpackage lazily (GLE-020): this
    # foundational module must import without the runtime subpackage present.
    import importlib

    stream_mod = importlib.import_module("skillweave.runtime.stream")
    PersistenceError = getattr(stream_mod, "StreamPersistenceError", Exception)

    env = _build_env(env_allowlist, extra_env)

    store.begin_attempt(
        run_id=run_id,
        attempt=attempt,
        task_id=task_id,
        retain_limit=stdout_retain_limit,
    )
    if stderr_retain_limit is not None:
        store.set_retain_limit(attempt, "stderr", stderr_retain_limit, run_id=run_id, task_id=task_id)

    argv = list(cmd)
    if wrap_command is not None:
        # The owning lane supplies the real isolation wrapper (e.g. StoreProtection
        # for the protected native path); the collector applies it verbatim and
        # never fabricates enforcement of its own.
        argv = list(wrap_command(argv))

    started_at = _iso_now()
    start_monotonic = time.monotonic()
    pid = 0
    try:
        proc = subprocess.Popen(
            argv,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=False,
            cwd=cwd,
            start_new_session=True,
        )
    except Exception as exc:  # noqa: BLE001 - a launch failure is data
        duration_ns = _elapsed_ns(start_monotonic)
        fact = store.finish_attempt(
            run_id=run_id,
            attempt=attempt,
            task_id=task_id,
            exit_code=None,
            signal=None,
            termination="launch_failed",
            duration_ns=duration_ns,
            pid=0,
            started_at=started_at,
            interrupted=True,
        )
        return _result_from_store(
            store, cmd, attempt, run_id, task_id, fact, secrets,
            message=f"worker failed to launch: {exc}",
        )

    pid = proc.pid

    # Owned-attempt liveness (SW-1511A-LIVENESS-001).  The recorder observes this
    # child through the same store it streams into; it is built here, around the
    # launched child, and its evidence is additive.  Any failure to build it is
    # swallowed: an observation failure must never become an attempt failure.
    if liveness_recorder is None:
        try:
            liveness_mod = importlib.import_module("skillweave.runtime.liveness")
            liveness_recorder = liveness_mod.LivenessRecorder.for_store(
                store, run_id=run_id, task_id=task_id, attempt=attempt, pid=pid,
            )
        except Exception:  # noqa: BLE001 - liveness is additive evidence
            liveness_recorder = None

    timed_out = False
    cancelled = False
    storage_failed = False
    storage_error = ""

    import select

    streams = [proc.stdout, proc.stderr]
    try:
        while True:
            ready = [r for r in streams if r is not None]
            if not ready:
                break
            ret = select.select(ready, [], [], 0.1)
            for fd in ret[0]:
                chunk = os.read(fd.fileno(), 4096)
                if not chunk:
                    if fd is proc.stdout:
                        streams = [r for r in streams if r is not proc.stdout]
                    else:
                        streams = [r for r in streams if r is not proc.stderr]
                    continue
                name = "stdout" if fd is proc.stdout else "stderr"
                try:
                    store.ingest(
                        attempt=attempt, stream=name, chunk=chunk,
                        run_id=run_id, task_id=task_id,
                    )
                except PersistenceError as exc:
                    # The bytes could not be durably recorded. Stop collecting
                    # and report a truthful storage failure rather than dropping
                    # evidence silently.
                    storage_failed = True
                    storage_error = str(exc)
                    _kill_group(proc)
                    break
            if storage_failed:
                break

            # Sample the child's liveness from this loop.  Rate-limited inside
            # the recorder; failures are swallowed there.
            if liveness_recorder is not None:
                try:
                    liveness_recorder.sample()
                except Exception:  # noqa: BLE001 - liveness is additive evidence
                    pass

            if timeout is not None and (time.monotonic() - start_monotonic) > timeout:
                timed_out = True
                _kill_group(proc)
                break

            if proc.poll() is not None:
                break
    finally:
        if proc.poll() is None:
            _kill_group(proc)

    # Drain any remaining buffered bytes after the child closed its pipes.
    for fd, name in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
        if fd is not None:
            try:
                rest = fd.read()
            except Exception:  # noqa: BLE001
                rest = b""
            if rest and not storage_failed:
                try:
                    store.ingest(
                        attempt=attempt, stream=name, chunk=rest,
                        run_id=run_id, task_id=task_id,
                    )
                except PersistenceError as exc:
                    storage_failed = True
                    storage_error = str(exc)

    returncode = proc.returncode
    duration_ns = _elapsed_ns(start_monotonic)

    if storage_failed:
        termination = "storage_failed"
        exit_code, sig = None, None
        interrupted = True
    elif timed_out:
        termination = "timed_out"
        exit_code, sig = None, None
        interrupted = True
    else:
        exit_code, sig = _split_returncode(returncode)
        termination = "signaled" if sig is not None else "exited"
        interrupted = False

    try:
        fact = store.finish_attempt(
            run_id=run_id,
            attempt=attempt,
            task_id=task_id,
            exit_code=exit_code,
            signal=sig,
            termination=termination,
            duration_ns=duration_ns,
            pid=pid,
            started_at=started_at,
            interrupted=interrupted,
        )
    except PersistenceError as exc:
        # Sealing/recording could not be made durable. Surface a truthful
        # storage-failure fact that is *not* recorded (so no false durable ack)
        # and let the caller see degraded=True/last_error on the store.
        storage_failed = True
        storage_error = str(exc)
        attempt_fact_cls = getattr(stream_mod, "AttemptFact")
        fact = attempt_fact_cls(
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
            exit_code=None,
            signal=None,
            result_subtype="storage_failed",
            termination="storage_failed",
            duration_ns=duration_ns,
            started_at=started_at,
            completed_at=_iso_now(),
            pid=pid,
        )
    message = ""
    if storage_failed:
        message = f"durable stream storage failed: {storage_error}"
    liveness_report = None
    if liveness_recorder is not None:
        try:
            liveness_recorder.finish(fact)
            liveness_report = liveness_recorder.report()
        except Exception:  # noqa: BLE001 - liveness is additive evidence
            liveness_report = None
    result = _result_from_store(
        store, cmd, attempt, run_id, task_id, fact, secrets, message=message,
    )
    if liveness_report is not None:
        result.metadata["liveness"] = liveness_report
    return result


def _result_from_store(
    store: Any,
    cmd: List[str],
    attempt: int,
    run_id: str,
    task_id: str,
    fact: Any,
    secrets: List[str],
    *,
    message: str,
) -> StreamingResult:
    out_summary = store.summary(attempt, "stdout", run_id=run_id, task_id=task_id)
    err_summary = store.summary(attempt, "stderr", run_id=run_id, task_id=task_id)
    stdout_bytes = store.spool(attempt, "stdout", run_id=run_id, task_id=task_id).read_retained()
    stderr_bytes = store.spool(attempt, "stderr", run_id=run_id, task_id=task_id).read_retained()
    if not message:
        joined = " ".join(cmd)
        if fact.termination == "signaled":
            message = (
                f"worker died on signal {fact.signal} without a result ({joined})"
            )
        elif fact.termination == "timed_out":
            message = f"worker timed out ({joined})"
        elif fact.termination == "exited" and fact.exit_code != 0:
            message = (
                f"worker exited with code {fact.exit_code} without success ({joined})"
            )
    return StreamingResult(
        command=list(cmd),
        exit_code=fact.exit_code,
        signal=fact.signal,
        termination=fact.termination,
        result_subtype=fact.result_subtype,
        pid=fact.pid,
        duration_ns=fact.duration_ns,
        run_id=run_id,
        task_id=task_id,
        attempt=attempt,
        stdout_digest=out_summary.digest,
        stderr_digest=err_summary.digest,
        stdout_observed_bytes=out_summary.observed_bytes,
        stderr_observed_bytes=err_summary.observed_bytes,
        stdout_incompleteness=out_summary.incompleteness,
        stderr_incompleteness=err_summary.incompleteness,
        store=store,
        fact=fact,
        stdout_text=redact_secrets(
            stdout_bytes.decode("utf-8", errors="replace"), secrets
        ),
        stderr_text=redact_secrets(
            stderr_bytes.decode("utf-8", errors="replace"), secrets
        ),
        message=message,
        metadata={
            "run_id": run_id,
            "task_id": task_id,
            "attempt": attempt,
            "stdout_backpressure_events": out_summary.backpressure_events,
            "stderr_backpressure_events": err_summary.backpressure_events,
            "stdout_metadata_limited": out_summary.metadata_limited,
            "stderr_metadata_limited": err_summary.metadata_limited,
            "durable": getattr(store, "is_durable", False),
            "persistence_mode": getattr(store, "persistence_mode", "memory"),
        },
    )


def _elapsed_ns(start_monotonic: float) -> int:
    """Measured monotonic interval in nanoseconds, never clipped to a floor."""
    return int((time.monotonic() - start_monotonic) * 1_000_000_000)


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _split_returncode(returncode: int) -> Tuple[Optional[int], Optional[int]]:
    if returncode is None:
        return None, None
    if returncode < 0:
        return None, -returncode
    return returncode, None


def _kill_group(proc: subprocess.Popen) -> None:
    """Terminate the child's whole process group, then reap it."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=0.5)
        return
    except subprocess.TimeoutExpired:
        pass
    except ProcessLookupError:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait()
    except Exception:  # noqa: BLE001
        pass
