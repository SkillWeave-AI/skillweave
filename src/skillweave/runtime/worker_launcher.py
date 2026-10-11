"""Real process isolation for delegated workers (SW-1510-IDENTITY).

Worker scope cannot write terminal receipts, gate state, observer signing
material, authority records or gateway telemetry. An in-memory role matrix, or a
``chmod`` against a same-user child, is not enough: an owner can simply
``chmod`` a file it owns. This module therefore enforces the boundary with real
OS mechanisms and *fails closed* when none is available.

Two real mechanisms are modeled:

* :class:`SandboxExecAdapter` — macOS Seatbelt (``sandbox-exec``) denies both
  reads of observer signing material and writes to every protected store. It is
  selected when the host permits Seatbelt, and its ``available()`` is a real
  probe, not a platform guess.
* :class:`ImmutableStoreAdapter` — sets the BSD/Mach immutable flag (``uchg``)
  on each protected store for the lifetime of the child. This is
  kernel-enforced: a same-user child's write/overwrite/unlink inside a sealed
  store fails with ``EPERM``, independently observable after the child exits.
  It does not deny reads, so it advertises ``denies_reads = False`` honestly;
  observer signing material is read-protected by design instead
  (:class:`ObserverSigningVault`, kept only in host memory).

If neither adapter is available, :func:`protect_stores` raises
:class:`IsolationUnavailableError`: the protected native path fails closed with
an honest capability limitation rather than launching an unsandboxed worker.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

#: The protected store families a worker must never write.
PROTECTED_STORE_NAMES: tuple[str, ...] = (
    "terminal_receipts",
    "gate_state",
    "authority_records",
    "gateway_telemetry",
    "observer_signing",
)

#: Protected stores whose *contents* are also withheld from a worker.
READ_PROTECTED_STORE_NAMES: frozenset[str] = frozenset({"observer_signing"})


def _is_loopback(host: str) -> bool:
    """Return True when *host* is a loopback address (SBPL-confinable).

    macOS Seatbelt (``sandbox-exec``) only accepts ``"*"`` or
    ``"localhost"`` as the host part of a ``remote ip`` or ``remote tcp``
    rule.  Loopback addresses (``127.0.0.1``, ``::1``, ``localhost``, and
    the full ``127.0.0.0/8`` range) map to ``localhost`` in the profile.
    Any non-loopback host cannot be expressed, and the caller must HOLD
    before admission.
    """
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    try:
        from ipaddress import ip_address
        return ip_address(host).is_loopback
    except (ValueError, ImportError):
        return False


class IsolationUnavailableError(RuntimeError):
    """No real isolation adapter is available; the protected path fails closed.

    Callers that see this must treat the protected path as unavailable, never
    retry with containment disabled.
    """


class WorkerIsolationAdapter:
    """Abstract real-isolation adapter. Subclasses own one OS mechanism."""

    name: str = "abstract"

    #: Advertised, honest capabilities. Never True unless actually enforced.
    denies_writes: bool = False
    denies_reads: bool = False
    #: True only when the adapter confines a *process* to a single writable
    #: tree (all writes outside the scope are denied), not merely sealing a set
    #: of protected stores. An adapter that only seals protected stores is
    #: honestly False: it cannot stop a child writing some other outside path.
    confines_to_scope: bool = False

    def available(self) -> bool:
        raise NotImplementedError

    def seal(self, protected: "ProtectedStores") -> None:
        raise NotImplementedError

    def unseal(self, protected: "ProtectedStores") -> None:
        raise NotImplementedError


@dataclass(frozen=True)
class ProtectedStores:
    """Concrete filesystem roots for each protected store family."""

    roots: Mapping[str, Path]

    def __post_init__(self) -> None:
        missing = [name for name in PROTECTED_STORE_NAMES if name not in self.roots]
        if missing:
            raise ValueError(f"protected stores missing roots for {missing}")

    def paths(self) -> list[Path]:
        return [Path(self.roots[name]) for name in PROTECTED_STORE_NAMES]

    def write_protected_paths(self) -> list[Path]:
        return self.paths()

    def read_protected_paths(self) -> list[Path]:
        return [Path(self.roots[name]) for name in sorted(READ_PROTECTED_STORE_NAMES)]

    def ensure(self, mode: int = 0o755) -> None:
        """Create each protected root as a host-owned directory.

        The mode is only a same-user convenience layer; the isolation guarantee
        comes from the adapter, not from this mode.
        """
        for path in self.paths():
            path.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(path, mode)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Observer signing material: host-memory vault
# ---------------------------------------------------------------------------


class ObserverSigningVault:
    """Host-only observer signing material, kept in memory, never on disk.

    A worker holds no reference to a vault instance, and the key is never
    written to any filesystem path, so there is no plaintext file a child can
    open. :meth:`read_attempt_paths` returns the paths a worker might *guess*;
    a worker's read attempt of them fails because nothing exists there. This is
    read protection by construction, complementary to a read-denying sandbox.
    """

    def __init__(self) -> None:
        import secrets

        self._key = secrets.token_bytes(32)

    @property
    def key_ref(self) -> str:
        """An opaque reference to the in-memory key (not the key itself)."""
        import hashlib

        return "vault:" + hashlib.sha256(self._key).hexdigest()[:16]

    def sign(self, material: bytes) -> bytes:
        import hashlib
        import hmac

        return hmac.new(self._key, material, hashlib.sha256).digest()

    def read_attempt_paths(self, signing_root: Path) -> list[Path]:
        """Paths a worker is likely to probe; none carries the key material."""
        return [
            Path(signing_root) / "observer.key",
            Path(signing_root) / "signing.key",
            Path(signing_root) / "key.pem",
        ]


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


@dataclass
class SandboxExecAdapter(WorkerIsolationAdapter):
    """macOS ``sandbox-exec`` (Seatbelt): denies reads and writes at the OS boundary."""

    name: str = "darwin-sandbox-exec"
    denies_writes: bool = True
    denies_reads: bool = True
    #: Seatbelt translates the writable scope into the sandbox profile, so a
    #: wrapped process can write only under that tree — this is real process
    #: confinement, not just protected-store sealing.
    confines_to_scope: bool = True

    def available(self) -> bool:
        sandbox_exec = shutil.which("sandbox-exec")
        if not sandbox_exec or sys.platform != "darwin":
            return False
        try:
            probe = subprocess.run(
                [sandbox_exec, "-p", "(version 1)(allow default)", "/bin/echo", "ok"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
        except Exception:  # noqa: BLE001
            return False
        return probe.returncode == 0 and probe.stdout.strip() == b"ok"

    def build_profile(self, writable_scope: Path, protected: ProtectedStores,
                      *,
                      allowed_egress_host: str = "",
                      allowed_egress_port: int = 0) -> str:
        """Build a Seatbelt sandbox profile.

        When *allowed_egress_host* and *allowed_egress_port* are set, the
        profile allows outbound TCP to only that address (SW-1510-ROUTE-LOCK
        criterion 2).  Without them, the default ``(deny default)`` still
        denies all network by virtue of not explicitly allowing any.
        """
        lines = ["(version 1)", "(deny default)", "(allow process*)"]
        lines.append("(allow sysctl-read)")
        lines.append("(allow mach-lookup)")
        lines.append("(allow file-read*)")
        for path in protected.read_protected_paths():
            lines.append(f'(deny file-read* (subpath "{path}"))')
        lines.append(f'(allow file-write* (subpath "{writable_scope}"))')
        for literal in ('"/dev/null"', '"/dev/stdout"', '"/dev/stderr"'):
            lines.append(f"(allow file-write-data (literal {literal}))")
        # SW-1510-ROUTE-LOCK: network confinement — allow only the approved
        # gateway host:port.  Without this the default ``(deny default)``
        # blocks all network, which is safe but breaks legitimate gateway
        # access.
        if allowed_egress_host and allowed_egress_port:
            lines.append(
                f'(allow network-outbound '
                f'(remote ip "localhost:{allowed_egress_port}"))'
            )
        return "\n".join(lines)

    def build_egress_profile(self, host: str, port: int) -> str:
        """Build a standalone Seatbelt profile for network egress only.

        Allows outbound TCP to only *host:port* plus minimal process
        machinery.  Used when wrapping a child without a full worker-
        protection window (e.g. the route-proof async path).

        File access is broadly allowed since this profile is strictly
        about *network* confinement — the child must read its binary,
        libraries, and any data files to function.
        """
        lines = ["(version 1)", "(deny default)", "(allow process*)"]
        lines.append("(allow sysctl-read)")
        lines.append("(allow mach-lookup)")
        lines.append("(allow file-read*)")
        lines.append('(allow file-write* (subpath "/dev/null") '
                     '(subpath "/dev/stdout") (subpath "/dev/stderr"))')
        # macOS SBPL accepts only "*" or "localhost" as the host part
        # of a remote ip / remote tcp rule.  Map loopback addresses to
        # "localhost"; non-loopback hosts cannot be confined via SBPL
        # and the caller must HOLD before calling this method.
        sbpl_host = "localhost" if _is_loopback(host) else host
        lines.append(
            f'(allow network-outbound '
            f'(remote ip "{sbpl_host}:{port}"))'
        )
        return "\n".join(lines)

    def wrap_command(
        self,
        command: Sequence[str],
        *,
        writable_scope: Path,
        protected: ProtectedStores,
        allowed_egress_host: str = "",
        allowed_egress_port: int = 0,
    ) -> list[str]:
        sandbox_exec = shutil.which("sandbox-exec")
        if not sandbox_exec:
            raise IsolationUnavailableError("sandbox-exec is not installed")
        return [
            sandbox_exec,
            "-p",
            self.build_profile(
                Path(writable_scope), protected,
                allowed_egress_host=allowed_egress_host,
                allowed_egress_port=allowed_egress_port,
            ),
            *[str(c) for c in command],
        ]

    def seal(self, protected: ProtectedStores) -> None:
        # Seatbelt applies per-launch; nothing persistent to seal here.
        return None

    def unseal(self, protected: ProtectedStores) -> None:
        return None


@dataclass
class ImmutableStoreAdapter(WorkerIsolationAdapter):
    """BSD/Mach immutable-flag (``uchg``) write isolation.

    Sealing a protected store sets ``uchg`` on the directory and its entries;
    the kernel then rejects any create/overwrite/unlink inside it from any
    process, including a same-user child. ``unseal`` clears the flag after the
    child exits. Reads are unaffected, so ``denies_reads`` is honestly False.
    """

    name: str = "bsd-immutable-flag"
    denies_writes: bool = True
    denies_reads: bool = False
    #: Optional directory for the availability probe. When None the ambient
    #: temp dir is used; tests pin it to their own basetemp so the probe never
    #: depends on the host's TMPDIR.
    probe_dir: Optional[Path] = None

    def available(self) -> bool:
        chflags = shutil.which("chflags")
        if not chflags or sys.platform not in ("darwin", "freebsd", "netbsd", "openbsd"):
            return False
        import tempfile

        parent = str(self.probe_dir) if self.probe_dir is not None else None
        try:
            probe_dir = Path(tempfile.mkdtemp(prefix="iso-probe-", dir=parent))
        except OSError:
            return False
        try:
            marker = probe_dir / "marker"
            marker.write_text("x")
            if not self._chflags(chflags, ["-R", "uchg", str(probe_dir)]):
                return False
            try:
                marker.write_text("y")
                wrote = True
            except OSError:
                wrote = False
            finally:
                self._chflags(chflags, ["-R", "nouchg", str(probe_dir)])
            return not wrote
        finally:
            shutil.rmtree(probe_dir, ignore_errors=True)

    def _chflags(self, chflags: str, args: list[str]) -> bool:
        try:
            return subprocess.run(
                [chflags, *args],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).returncode == 0
        except Exception:  # noqa: BLE001
            return False

    def seal(self, protected: ProtectedStores) -> None:
        chflags = shutil.which("chflags")
        if not chflags:
            raise IsolationUnavailableError("chflags is not installed")
        for path in protected.write_protected_paths():
            path.mkdir(parents=True, exist_ok=True)
            if not self._chflags(chflags, ["-R", "uchg", str(path)]):
                raise IsolationUnavailableError(f"could not seal protected store {path}")

    def unseal(self, protected: ProtectedStores) -> None:
        chflags = shutil.which("chflags")
        if not chflags:
            return
        for path in protected.write_protected_paths():
            self._chflags(chflags, ["-R", "nouchg", str(path)])


@dataclass
class FailClosedAdapter(WorkerIsolationAdapter):
    """The adapter used when no real isolation mechanism is available."""

    name: str = "fail-closed"
    reason: str = ""

    def available(self) -> bool:
        return False

    def seal(self, protected: ProtectedStores) -> None:
        raise IsolationUnavailableError(self._message())

    def unseal(self, protected: ProtectedStores) -> None:
        return None

    def _message(self) -> str:
        base = "no real worker isolation adapter is available on this host"
        if self.reason:
            base += f": {self.reason}"
        return base + "; refusing to launch an unsandboxed worker (fail closed)"


def select_adapter(preferred: Optional[str] = None) -> WorkerIsolationAdapter:
    """Return the strongest available real adapter, else a fail-closed adapter."""
    candidates: list[WorkerIsolationAdapter] = []
    order = preferred or "auto"
    if order in ("auto", "sandbox-exec"):
        candidates.append(SandboxExecAdapter())
    if order in ("auto", "immutable"):
        candidates.append(ImmutableStoreAdapter())
    for candidate in candidates:
        if candidate.available():
            return candidate
    return FailClosedAdapter(reason="no writable-store isolation mechanism available")


# ---------------------------------------------------------------------------
# Protection context manager
# ---------------------------------------------------------------------------


@dataclass
class StoreProtection:
    """A live protection window around one real worker subprocess.

    ``adapter_name``, ``denies_writes`` and ``denies_reads`` are the *observed*
    capabilities of the chosen adapter. ``read_isolation_available`` is False
    when only write isolation is enforced — an honest limitation the caller can
    assert on rather than assume away.
    """

    adapter: WorkerIsolationAdapter
    protected: ProtectedStores
    require_read_isolation: bool = False
    #: When True the window also refuses an adapter that cannot confine a
    #: process to the writable scope (see ``confines_to_scope``). A caller that
    #: must guarantee "no writes outside the attempt checkout anywhere" sets
    #: this; a caller that only needs the receipt/store/telemetry roots sealed
    #: leaves it False and still gets honest ``confines_to_scope`` reporting.
    require_checkout_confinement: bool = False
    sealed: bool = False
    #: The one directory tree the wrapped worker may write, used by adapters
    #: (e.g. Seatbelt) that translate a writable scope into their sandbox
    #: profile. It is set by the caller that opens the window.
    writable_scope: Optional[Path] = None

    @property
    def adapter_name(self) -> str:
        return self.adapter.name

    @property
    def denies_writes(self) -> bool:
        return self.adapter.denies_writes

    @property
    def denies_reads(self) -> bool:
        return self.adapter.denies_reads

    @property
    def confines_to_scope(self) -> bool:
        """True when the adapter confines writes to one tree (not just seals)."""
        return getattr(self.adapter, "confines_to_scope", False)

    @property
    def read_isolation_available(self) -> bool:
        return self.adapter.denies_reads

    def __enter__(self) -> "StoreProtection":
        if not self.adapter.available():
            raise IsolationUnavailableError(
                f"isolation adapter {self.adapter.name!r} is not available; "
                "refusing to launch an unsandboxed worker (fail closed)"
            )
        if self.require_read_isolation and not self.adapter.denies_reads:
            raise IsolationUnavailableError(
                "read isolation of observer signing material was required but "
                f"adapter {self.adapter.name!r} does not deny reads; fail closed"
            )
        if self.require_checkout_confinement and not self.confines_to_scope:
            raise IsolationUnavailableError(
                "checkout confinement was required but adapter "
                f"{self.adapter.name!r} cannot confine a process to the writable "
                "scope; fail closed"
            )
        try:
            self.adapter.seal(self.protected)
        except Exception:
            # Never leave a partially-sealed window behind: a half-applied seal
            # could lock out the host's own subsequent writes. Best-effort
            # unseal, then fail closed.
            self.adapter.unseal(self.protected)
            raise
        self.sealed = True
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self.sealed:
            self.adapter.unseal(self.protected)
            self.sealed = False
        return False

    def wrap_command(self, command: Sequence[str], *,
                     writable_scope: Path,
                     allowed_egress_host: str = "",
                     allowed_egress_port: int = 0) -> list[str]:
        """Return the argv to actually spawn (adapter may wrap it).

        When *allowed_egress_host*/*allowed_egress_port* are set, the
        Seatbelt profile also allows outbound TCP to only that address
        (SW-1510-ROUTE-LOCK criterion 2, composed WITH the store protection).
        """
        if isinstance(self.adapter, SandboxExecAdapter):
            return self.adapter.wrap_command(
                command, writable_scope=writable_scope, protected=self.protected,
                allowed_egress_host=allowed_egress_host,
                allowed_egress_port=allowed_egress_port,
            )
        return [str(c) for c in command]

    def run(self, command: Sequence[str], *,
            writable_scope: Optional[Path] = None,
            cwd: Optional[str] = None,
            env: Optional[Mapping[str, str]] = None,
            **popen_kwargs: Any) -> "subprocess.CompletedProcess":
        """Launch a real protected child inside this open window (the enforced path).

        This is the one correct way to spawn a worker while the window is open:

        * A confinement adapter (Seatbelt) confines per launch, so the child is
          always wrapped through :meth:`wrap_command`: writes outside the
          window's ``writable_scope`` fail at the OS boundary for the child's
          lifetime. Its ``seal()`` is intentionally a no-op — it confines at
          launch, never by mutating filesystem state — so spawning a *bare*
          ``subprocess`` under an open window silently defeats it (the child then
          writes freely into the protected stores).
        * A seal-only adapter (the immutable flag) already sealed the protected
          stores in :meth:`__enter__`; the child runs against those sealed
          stores.

        Raises :class:`IsolationUnavailableError` when the window is not open, or
        when a confinement adapter is in force but no writable scope is known, so
        a bare/unsandboxed child can never be launched by mistake.
        """
        if not self.sealed:
            raise IsolationUnavailableError(
                "protection window is not open; enter it before launching a "
                "protected child (fail closed)"
            )
        if self.confines_to_scope:
            scope = writable_scope if writable_scope is not None else self.writable_scope
            if scope is None:
                raise IsolationUnavailableError(
                    f"adapter {self.adapter.name!r} confines at launch but no "
                    "writable scope was set on the window; refusing to launch an "
                    "unconfined child (fail closed)"
                )
            argv = self.wrap_command(command, writable_scope=scope)
        else:
            argv = [str(c) for c in command]
        return subprocess.run(argv, cwd=cwd, env=env, **popen_kwargs)


def protect_stores(
    protected: ProtectedStores,
    *,
    adapter: Optional[WorkerIsolationAdapter] = None,
    require_read_isolation: bool = False,
) -> StoreProtection:
    """Open a protection window over *protected* stores for one worker launch."""
    return StoreProtection(
        adapter=adapter or select_adapter(),
        protected=protected,
        require_read_isolation=require_read_isolation,
    )


__all__ = [
    "PROTECTED_STORE_NAMES",
    "READ_PROTECTED_STORE_NAMES",
    "IsolationUnavailableError",
    "ProtectedStores",
    "ObserverSigningVault",
    "WorkerIsolationAdapter",
    "SandboxExecAdapter",
    "ImmutableStoreAdapter",
    "FailClosedAdapter",
    "select_adapter",
    "StoreProtection",
    "protect_stores",
    "_is_loopback",
]
