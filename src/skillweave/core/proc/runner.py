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
from typing import Any, Dict, List, Optional, Tuple

class ProcessLimitExceeded(Exception):
    pass

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
) -> Tuple[int, str, str]:
    """
    Run a process with environment allowlist, secret redaction, and output limits.
    Returns (returncode, stdout, stderr).
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
        
    # Start process
    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False # read as bytes for exact limit
    )
    
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

    A launch failure is recorded as its own subtype; it does not raise out of
    this collector with a fabricated exit code.
    """
    secrets = secrets or []

    # Resolve the optional runtime subpackage lazily (GLE-020): this
    # foundational module must import without the runtime subpackage present.
    import importlib

    importlib.import_module("skillweave.runtime.stream")

    env = _build_env(env_allowlist, extra_env)

    store.begin_attempt(
        run_id=run_id,
        attempt=attempt,
        task_id=task_id,
        retain_limit=stdout_retain_limit,
    )
    if stderr_retain_limit is not None:
        store.set_retain_limit(attempt, "stderr", stderr_retain_limit)

    started_at = _iso_now()
    start_monotonic = time.monotonic()
    pid = 0
    try:
        proc = subprocess.Popen(
            list(cmd),
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
    timed_out = False
    cancelled = False

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
                store.ingest(attempt=attempt, stream=name, chunk=chunk)

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
            if rest:
                store.ingest(attempt=attempt, stream=name, chunk=rest)

    returncode = proc.returncode
    duration_ns = _elapsed_ns(start_monotonic)

    if timed_out:
        termination = "timed_out"
        exit_code, sig = None, None
        interrupted = True
    else:
        exit_code, sig = _split_returncode(returncode)
        termination = "signaled" if sig is not None else "exited"
        interrupted = False

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
    return _result_from_store(
        store, cmd, attempt, run_id, task_id, fact, secrets, message="",
    )


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
    out_summary = store.summary(attempt, "stdout")
    err_summary = store.summary(attempt, "stderr")
    stdout_bytes = store.spool(attempt, "stdout").read_retained()
    stderr_bytes = store.spool(attempt, "stderr").read_retained()
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
