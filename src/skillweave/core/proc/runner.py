import os
import subprocess
import signal
from dataclasses import dataclass, field
from typing import Any, List, Dict, Optional, Sequence, Tuple

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

    def wrap_command(self, command: Sequence[str]) -> List[str]:
        """Return the argv actually spawned (the adapter may wrap it)."""
        if self.isolation is None or not hasattr(self.isolation, "wrap_command"):
            return [str(c) for c in command]
        import pathlib

        scope = pathlib.Path(self.checkout_root) if self.checkout_root else None
        try:
            return self.isolation.wrap_command(command, writable_scope=scope)
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
