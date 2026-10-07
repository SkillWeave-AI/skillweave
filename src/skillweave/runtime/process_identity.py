"""Native process identity for owned worker lifecycle (SW-1510-CANCEL).

A PID is not an identity.  ``kill(pid)`` after a recorded PID is stale can
signal an unrelated process the kernel handed the same number to; a recorded
command string is not proof that the process still *is* that command.  This
module captures and re-verifies the facts the operating system actually holds
about a live process so the cancellation path can prove it owns the process
before it signals it:

* ``pid`` / ``ppid`` / ``pgid`` -- the numeric identity and owning group,
* ``start_time`` -- the process creation instant, the field that changes when a
  PID is recycled (macOS ``proc_pidinfo(PROC_PIDTBSDINFO)``; Linux
  ``/proc/<pid>/stat`` field 22),
* ``exec_path`` and ``argv`` -- the program image and its argument vector, so a
  *stale command* (same PID, different command) is a distinct fact from a
  *recycled PID*.

The captured facts yield a ``creation_token`` (a digest over the immutable
creation facts) and a full ``identity_token`` (also covering the command).
Verification compares a recorded identity against the current facts and returns
a declared reason -- ``match``, ``recycled_pid``, ``stale_command``,
``foreign_worker`` or ``not_alive`` -- never a boolean guess.  The kill path
treats every reason except ``match`` as a refusal, and ``not_alive`` as
*unknown* in-flight state rather than a fabricated termination.

Nothing here signals a process; it only observes.  The module is deliberately
dependency-free and imports the stdlib only, so the foundational runtime does
not depend on an optional subpackage.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Optional

__all__ = [
    "ProcessIdentity",
    "IdentityVerification",
    "capture",
    "verify",
    "identity_token",
    "is_alive",
    "capture_current",
    "list_process_group",
]

# Declared verification reasons.  Only ``match`` permits a signal to be sent.
REASON_MATCH = "match"
REASON_RECYCLED = "recycled_pid"
REASON_STALE_COMMAND = "stale_command"
REASON_FOREIGN = "foreign_worker"
REASON_NOT_ALIVE = "not_alive"
REASON_NO_RECORD = "no_recorded_identity"


def _canonical_bytes(value: Any) -> bytes:
    """A stable byte form for digesting an identity field, never a repr."""
    if value is None:
        return b"\x00none"
    if isinstance(value, (list, tuple)):
        return b"\x1e".join(_canonical_bytes(v) for v in value)
    if isinstance(value, bool):
        return b"1" if value else b"0"
    if isinstance(value, int):
        return b"i:" + str(value).encode("ascii")
    return b"s:" + str(value).encode("utf-8", "surrogatepass")


@dataclass(frozen=True)
class ProcessIdentity:
    """The OS-held facts about one live process at one instant.

    ``start_time`` is the canonical ``"<sec>.<usec>"`` creation instant and is
    the field that distinguishes a recycled PID.  ``argv`` is the observed
    argument vector (best effort: an argument vector change between capture and
    verification is exactly the *stale command* signal this exists to catch).
    """

    pid: int
    ppid: int
    pgid: int
    start_time: str
    exec_path: str
    argv: tuple = ()
    comm: str = ""
    #: The process has exited but not yet been reaped (OS state ``SZOMB``).  A
    #: zombie still answers identity probes on the same PID, but it is not a
    #: live, running process; ``is_alive`` reports it as not alive so a
    #: terminated-but-unreaped worker is never mistaken for an in-flight one.
    zombie: bool = False

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "ppid": self.ppid,
            "pgid": self.pgid,
            "start_time": self.start_time,
            "exec_path": self.exec_path,
            "argv": list(self.argv),
            "comm": self.comm,
            "zombie": self.zombie,
            "creation_token": self.creation_token,
            "identity_token": self.identity_token,
        }

    @property
    def creation_token(self) -> str:
        """Digest over the immutable creation facts (PID + when + image)."""
        h = hashlib.sha256()
        for part in (self.pid, self.start_time, self.exec_path):
            h.update(_canonical_bytes(part))
            h.update(b"\x1f")
        return h.hexdigest()

    @property
    def identity_token(self) -> str:
        """Digest over the creation facts *and* the observed command."""
        h = hashlib.sha256()
        h.update(_canonical_bytes(self.creation_token))
        h.update(b"\x1f")
        h.update(_canonical_bytes(list(self.argv)))
        return h.hexdigest()

    @classmethod
    def from_dict(cls, data: Any) -> Optional["ProcessIdentity"]:
        if not isinstance(data, dict):
            return None
        try:
            pid = int(data["pid"])
            start = str(data["start_time"])
        except (KeyError, TypeError, ValueError):
            return None
        argv = data.get("argv") or ()
        return cls(
            pid=pid,
            ppid=int(data.get("ppid", -1)),
            pgid=int(data.get("pgid", pid)),
            start_time=start,
            exec_path=str(data.get("exec_path", "")),
            argv=tuple(str(a) for a in argv),
            comm=str(data.get("comm", "")),
        )


@dataclass(frozen=True)
class IdentityVerification:
    """The declared outcome of comparing a recorded identity to the live one."""

    ok: bool
    reason: str
    recorded_pid: int
    current: Optional[ProcessIdentity] = None
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "recorded_pid": self.recorded_pid,
            "current_pid": self.current.pid if self.current else None,
            "detail": self.detail,
            "current_identity": self.current.to_dict() if self.current else None,
        }


# ---------------------------------------------------------------------------
# Platform observation
# ---------------------------------------------------------------------------

def _darwin_capture(pid: int) -> Optional[ProcessIdentity]:
    """Capture identity on macOS via libproc + sysctl (the ps(1) mechanism)."""
    import ctypes
    import ctypes.util

    libc_path = ctypes.util.find_library("c")
    if not libc_path:
        return None
    libc = ctypes.CDLL(libc_path, use_errno=True)

    class _ProcBsdInfo(ctypes.Structure):
        _fields_ = [
            ("pbi_flags", ctypes.c_uint32),
            ("pbi_status", ctypes.c_uint32),
            ("pbi_xstatus", ctypes.c_uint32),
            ("pbi_pid", ctypes.c_uint32),
            ("pbi_ppid", ctypes.c_uint32),
            ("pbi_uid", ctypes.c_uint32),
            ("pbi_gid", ctypes.c_uint32),
            ("pbi_ruid", ctypes.c_uint32),
            ("pbi_rgid", ctypes.c_uint32),
            ("pbi_svuid", ctypes.c_uint32),
            ("pbi_svgid", ctypes.c_uint32),
            ("rfu_1", ctypes.c_uint32),
            ("pbi_comm", ctypes.c_char * 16),
            ("pbi_name", ctypes.c_char * 32),
            ("pbi_nfiles", ctypes.c_uint32),
            ("pbi_pgid", ctypes.c_uint32),
            ("pbi_pjobc", ctypes.c_uint32),
            ("e_tdev", ctypes.c_uint32),
            ("e_tpgid", ctypes.c_uint32),
            ("pbi_nice", ctypes.c_int32),
            ("pbi_start_tvsec", ctypes.c_uint64),
            ("pbi_start_tvusec", ctypes.c_uint64),
        ]

    PROC_PIDTBSDINFO = 3
    libc.proc_pidinfo.restype = ctypes.c_int
    libc.proc_pidinfo.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
        ctypes.c_void_p, ctypes.c_int,
    ]
    libc.proc_pidpath.restype = ctypes.c_int
    libc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]

    def _read_bsd(p: int):
        info = _ProcBsdInfo()
        n = libc.proc_pidinfo(
            p, PROC_PIDTBSDINFO, 0, ctypes.byref(info), ctypes.sizeof(info)
        )
        if n <= 0:
            return None
        return info

    info = _read_bsd(pid)
    if info is None:
        return None

    start = f"{info.pbi_start_tvsec}.{info.pbi_start_tvusec}"
    exec_path = ""
    buf = ctypes.create_string_buffer(4096)
    if libc.proc_pidpath(pid, buf, 4096) > 0:
        exec_path = buf.value.decode("utf-8", "surrogateescape")
    argv = _darwin_argv(libc, pid)

    # Re-read the creation facts to close the recycle race between the reads.
    again = _read_bsd(pid)
    if again is not None and (
        f"{again.pbi_start_tvsec}.{again.pbi_start_tvusec}" != start
    ):
        return None

    return ProcessIdentity(
        pid=pid,
        ppid=int(info.pbi_ppid),
        pgid=int(info.pbi_pgid),
        start_time=start,
        exec_path=exec_path,
        argv=argv,
        comm=(info.pbi_name or info.pbi_comm or b"")
        .decode("utf-8", "replace")
        .rstrip("\x00"),
        zombie=int(info.pbi_status) == 5,  # SZOMB
    )


def _darwin_argv(libc: Any, pid: int) -> tuple:
    """Best-effort argv via ``sysctl KERN_PROCARGS2``; empty when unavailable."""
    import ctypes

    CTL_KERN = 1
    KERN_PROCARGS2 = 49
    mib = (ctypes.c_int * 3)(CTL_KERN, KERN_PROCARGS2, pid)
    size = ctypes.c_size_t(0)
    try:
        if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
            return ()
        if size.value <= 4 or size.value > (1 << 20):
            return ()
        raw = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 3, raw, ctypes.byref(size), None, 0) != 0:
            return ()
        data = raw.raw[: size.value]
    except Exception:  # noqa: BLE001 - argv is best effort, never fatal
        return ()
    argc = int.from_bytes(data[:4], sys.byteorder)
    body = data[4:]
    # The buffer is: exec_path NUL [alignment NULs] argv0 NUL argv1 NUL ... env
    segments = body.split(b"\x00")
    non_empty = [s for s in segments if s]
    # First non-empty is the exec path; the next *argc* are the arguments.
    args = non_empty[1 : 1 + argc]
    return tuple(a.decode("utf-8", "surrogateescape") for a in args)


def _linux_capture(pid: int) -> Optional[ProcessIdentity]:
    """Capture identity on Linux from ``/proc`` (the ps(1) source)."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            stat = fh.read()
    except OSError:
        return None
    try:
        rparen = stat.rindex(b")")
        fields = stat[rparen + 2:].split()
        # Fields after comm: state(0) ppid(1) pgrp(2) ... starttime(19)
        ppid = int(fields[1])
        pgrp = int(fields[2])
        starttime = fields[19].decode("ascii")
    except (ValueError, IndexError):
        return None
    try:
        exec_path = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        exec_path = ""
    argv: tuple = ()
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            argv = tuple(
                a.decode("utf-8", "surrogateescape")
                for a in fh.read().split(b"\x00") if a
            )
    except OSError:
        argv = ()
    # The kernel exposes only the boot-relative tick here; the raw tick is a
    # stable creation instant for the process's lifetime, which is all a
    # recycle check needs.
    state_field = fields[0].decode("ascii", "replace") if fields else ""
    return ProcessIdentity(
        pid=pid, ppid=ppid, pgid=pgrp, start_time=starttime,
        exec_path=exec_path, argv=argv, comm="", zombie=state_field == "Z",
    )


def capture(pid: int) -> Optional[ProcessIdentity]:
    """Return the observed identity of *pid*, or ``None`` when it is not live.

    ``None`` means the process could not be observed (it exited, or the platform
    cannot report identity).  Callers must treat that as *unknown*, never as
    proof of a particular state.
    """
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        if sys.platform == "darwin":
            return _darwin_capture(pid)
        if sys.platform.startswith("linux"):
            return _linux_capture(pid)
    except Exception:  # noqa: BLE001 - an observation failure is not a fact
        return None
    return None


#: Backwards-compatible alias.
capture_current = capture


def _darwin_all_pids() -> tuple:
    """Every live PID via libproc ``proc_listpids(PROC_ALL_PIDS)`` (ps(1)).

    Used only to locate the members of an *owned* process group by re-capturing
    each and comparing its ``pgid``.  Returns ``()`` when enumeration is
    unavailable, so callers degrade to signalling the group leader only.
    """
    import ctypes
    import ctypes.util

    libc_path = ctypes.util.find_library("c")
    if not libc_path:
        return ()
    libc = ctypes.CDLL(libc_path, use_errno=True)
    PROC_ALL_PIDS = 1
    libc.proc_listpids.restype = ctypes.c_int
    libc.proc_listpids.argtypes = [
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int,
    ]
    # First call sizes the buffer, second fills it.
    n = libc.proc_listpids(PROC_ALL_PIDS, 0, None, 0)
    if n <= 0:
        return ()
    cap = n + 4096
    buf = (ctypes.c_int * (cap // ctypes.sizeof(ctypes.c_int)))()
    got = libc.proc_listpids(PROC_ALL_PIDS, 0, buf, cap)
    if got <= 0:
        return ()
    count = got // ctypes.sizeof(ctypes.c_int)
    return tuple(p for p in buf[:count] if p > 0)


def list_process_group(pgid: int) -> tuple:
    """Return the PIDs that are members of process group *pgid* right now.

    Observed through the OS mechanisms (libproc on macOS via a full-PID scan
    and ``capture`` pgid comparison; ``/proc`` on Linux).  This lets a kill
    prove it is acting on the *owned group* rather than a single recycled PID.
    Exited-but-unreaped zombies are excluded: they are not running, cannot be
    signalled to any effect, and would otherwise keep a drained group
    non-empty.  Never signals anything.
    """
    if not isinstance(pgid, int) or pgid <= 0:
        return ()
    members = []
    if sys.platform == "darwin":
        candidates = _darwin_all_pids()
    elif sys.platform.startswith("linux"):
        try:
            names = os.listdir("/proc")
        except OSError:
            names = []
        candidates = tuple(int(n) for n in names if n.isdigit())
    else:
        candidates = ()
    seen = set()
    for pid in candidates:
        if pid in seen:
            continue
        seen.add(pid)
        ident = capture(pid)
        if ident is not None and ident.pgid == pgid and not ident.zombie:
            members.append(pid)
    return tuple(sorted(members))


def is_alive(pid: int) -> bool:
    """Return whether *pid* is a live, running process right now.

    Being *observable* is the test.  A process the OS still reports identity
    for is live (a zombie state, when the platform exposes one, is not).  A PID
    that answers ``kill(pid, 0)`` but yields *no* identity is a zombie that has
    already exited (macOS ``proc_pidinfo`` returns nothing for an unreaped
    corpse while null-signalling still succeeds) -- not a running process.

    A ``PermissionError`` from the null signal means a process owned by another
    user exists; we cannot observe it, but it is live and must not be reported
    dead.
    """
    for attempt in range(3):
        try:
            observed = capture(pid)
        except Exception:  # noqa: BLE001 - an observation failure is not a fact
            observed = None
        if observed is not None:
            return not observed.zombie
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        # Exists but unobservable: either a zombie, or a recycle race in which
        # the recorded PID was replaced between the two probes.  Retry once;
        # a persistent "signals but no identity" is a terminated process.
        if attempt < 2:
            time.sleep(0.02)
    return False


def identity_token(pid: int) -> Optional[str]:
    """Return the live identity token for *pid*, or ``None`` when unobservable."""
    identity = capture(pid)
    return identity.identity_token if identity is not None else None


def verify(
    recorded: Any,
    pid: int,
    *,
    require_argv: bool = True,
) -> IdentityVerification:
    """Compare *recorded* identity facts against the live process *pid*.

    Returns a :class:`IdentityVerification` whose ``reason`` is one of the
    declared values.  ``match`` is the *only* outcome that authorises a signal;
    ``not_alive`` is reported distinctly so the caller can record *unknown*
    in-flight state rather than a fabricated termination.
    """
    recorded_identity = ProcessIdentity.from_dict(recorded)
    if recorded_identity is None:
        return IdentityVerification(
            ok=False, reason=REASON_NO_RECORD, recorded_pid=pid,
            detail="no recorded process identity to verify against",
        )

    if recorded_identity.pid != pid:
        return IdentityVerification(
            ok=False, reason=REASON_RECYCLED, recorded_pid=pid,
            detail=(
                f"recorded pid {recorded_identity.pid} does not match the "
                f"requested pid {pid}"
            ),
        )

    current = capture(pid)
    if current is None:
        return IdentityVerification(
            ok=False, reason=REASON_NOT_ALIVE, recorded_pid=pid,
            detail=(
                "the recorded process is no longer observable; its in-flight "
                "state is unknown, not confirmed terminated"
            ),
        )

    if current.start_time != recorded_identity.start_time:
        return IdentityVerification(
            ok=False, reason=REASON_RECYCLED, recorded_pid=pid, current=current,
            detail=(
                f"pid {pid} was recycled: recorded start_time "
                f"{recorded_identity.start_time!r}, live start_time "
                f"{current.start_time!r}"
            ),
        )

    if (
        recorded_identity.exec_path
        and current.exec_path
        and current.exec_path != recorded_identity.exec_path
    ):
        return IdentityVerification(
            ok=False, reason=REASON_FOREIGN, recorded_pid=pid, current=current,
            detail=(
                f"pid {pid} runs {current.exec_path!r}, not the recorded "
                f"{recorded_identity.exec_path!r}"
            ),
        )

    if require_argv and recorded_identity.argv:
        if tuple(current.argv) != tuple(recorded_identity.argv):
            return IdentityVerification(
                ok=False, reason=REASON_STALE_COMMAND, recorded_pid=pid,
                current=current,
                detail=(
                    "the recorded command no longer matches the live process "
                    "argument vector"
                ),
            )

    return IdentityVerification(
        ok=True, reason=REASON_MATCH, recorded_pid=pid, current=current,
        detail="recorded identity matches the live process",
    )
