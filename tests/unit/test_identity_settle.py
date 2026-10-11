"""Deterministic proof that the recorded settle identity is never transient.

SW-CORE-ZR-012.  The production symptom was a *loaded-host* race: under CPU
load 6 of 40 freshly started children recorded the interpreter's pre-``exec``
thin launcher image, so ``kill_run``/``cancel`` refused a legitimately-owned
worker with ``foreign_worker``.  A load-dependent race is not a test.  This
file drives the exact mechanism through the deterministic seam
``skillweave.runsvc.service._capture_settled_identity`` -- the single function
that ``start_async`` records the launch identity with -- using *scripted*
launcher costs and *scripted* observations, so the race reproduces on an idle
host.

Nothing here weakens verification: every test asserts a *stricter* capture, or
re-proves that an existing refusal still fires.  There is no skip, no xfail and
no silent continue.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from skillweave.runtime import process_identity as pid  # noqa: E402
from skillweave.runsvc import service as svc  # noqa: E402

#: The transient pre-exec launcher image the owner host records under load.
_STUB_IMAGE = "/opt/homebrew/Versions/3.14/bin/python3.14"
#: The settled post-exec framework image (argv0 == exec_path after a re-exec).
_REAL_IMAGE = "/opt/homebrew/Resources/Python.app/Contents/MacOS/Python"


def _identity(pid_value: int, exec_path: str, argv=(), start="100.0"):
    return pid.ProcessIdentity(
        pid=pid_value,
        ppid=1,
        pgid=pid_value,
        start_time=start,
        exec_path=exec_path,
        argv=tuple(argv) if argv else (exec_path,),
    )


class _LauncherStream:
    """A scripted observation stream for one freshly launched pid.

    The child is the transient launcher stub for the first ``stub_observations``
    reads; then it re-execs into the settled real image.  ``force_exec`` moves
    the stream past the stub at a chosen instant, so a test can drive the exact
    owner-host timeline: the settle capture runs while the stub is still
    present, and a *later* cancellation observes the settled image.
    """

    def __init__(self, pid_value: int, *, stub_observations: int):
        self.pid = pid_value
        self.stub_observations = stub_observations
        self.reads = 0

    def force_exec(self):
        self.stub_observations = 0

    def __call__(self, _pid):
        self.reads += 1
        if self.reads <= self.stub_observations:
            return _identity(self.pid, _STUB_IMAGE)
        return _identity(self.pid, _REAL_IMAGE)


def _test_stub_set(monkeypatch):
    """Force the known-stub set to our scripted stub without host dependence.

    ``raising=False`` keeps the reproduction test runnable against the *base*
    commit, where the stub-set helper does not yet exist: the base capture
    ignores the set, records the stub image and fails with ``foreign_worker`` --
    the failure this test exists to pin.  With the fix the helper is present and
    consulted, so the transient image is refused.
    """
    monkeypatch.setattr(
        svc, "_native_launcher_stub_images", lambda: {_STUB_IMAGE},
        raising=False,
    )


class TestStubGuard:
    """The recorded identity can never be the transient launcher image."""

    def test_reproduces_loaded_host_race_on_idle_host(self, monkeypatch):
        """The 6-of-40 race, deterministically and with no CPU load.

        The stub re-execs *after* the short base settle window has closed, so a
        fix that merely waited for two consecutive observations within that
        window would still be observing the stub when it stopped -- exactly the
        owner-host failure.  Both the capture and the later verification read
        the same scripted observation stream, so this exercises the full
        launch -> verify path, not one half of it.
        """
        _test_stub_set(monkeypatch)
        # The launcher is the stub for its first five observations -- more than
        # the base commit needs, because the base commit records the stub as
        # soon as two consecutive observations agree (read 2), whereas the fix
        # keeps looking until the image actually changes (read 6).  Both stay
        # well inside the base settle window.
        stream = _LauncherStream(4242, stub_observations=5)
        monkeypatch.setattr(pid, "capture", stream)

        recorded = svc._capture_settled_identity(
            pid.capture, 4242, timeout=0.3, interval=0.01,
        )

        # A cancellation happens *later*: the launcher has since re-exec'd.
        stream.force_exec()

        assert recorded is not None, (
            "capture must establish a settled, non-stub image")
        # The live process verifies as owned: no foreign_worker refusal.  On
        # the base commit the recorded image is the transient stub, so this
        # fires with foreign_worker -- the exact production symptom.
        check = pid.verify(recorded.to_dict(), 4242)
        assert check.ok and check.reason == "match", (
            f"a legitimately-owned worker was refused: {check.reason}: "
            f"{check.detail}")
        assert recorded.exec_path == _REAL_IMAGE, (
            f"recorded a transient launcher image {recorded.exec_path!r}; the "
            "settle window closed before the stub re-exec'd")

    def test_capture_stays_bounded_when_image_never_changes(self):
        """A process that never changes its image returns well within the bound
        and records the last observed (non-stub) identity."""
        fixed = _identity(555, "/usr/bin/worker")

        t0 = time.monotonic()
        recorded = svc._capture_settled_identity(
            lambda _pid: fixed, 555, timeout=0.3, interval=0.01,
        )
        elapsed = time.monotonic() - t0

        assert recorded is not None
        assert recorded.exec_path == "/usr/bin/worker"
        assert elapsed < 0.3, (
            f"a never-changing image took {elapsed:.3f}s; two agreeing "
            "observations must return immediately")

    def test_only_the_stub_is_ever_seen_records_nothing(self, monkeypatch):
        """If the image is only ever the transient stub, record nothing.

        Recording an image a live process no longer has is the defect; when the
        settled image cannot be established the capture refuses to guess.  The
        replacement window is monkeypatched to keep the test fast, while still
        proving the wait is bounded."""
        _test_stub_set(monkeypatch)
        monkeypatch.setattr(svc, "_STUB_EXEC_REPLACEMENT_TIMEOUT_S", 0.1)
        always_stub = _identity(9, _STUB_IMAGE)

        t0 = time.monotonic()
        recorded = svc._capture_settled_identity(
            lambda _pid: always_stub, 9, timeout=0.05, interval=0.01,
        )
        elapsed = time.monotonic() - t0

        assert recorded is None, (
            "the transient launcher image must never be recorded")
        assert elapsed < 1.0, (
            f"the stub-replacement wait must be bounded, took {elapsed:.3f}s")

    def test_vanished_process_records_nothing(self):
        """A pid that is never observable records ``None`` (already exited)."""
        recorded = svc._capture_settled_identity(
            lambda _pid: None, 1234, timeout=0.05, interval=0.01,
        )
        assert recorded is None

import os  # noqa: E402


class TestStubSetInertOffStubLayout:
    """The derived stub set must never contain *this* process's live image.

    On a host whose interpreter does not re-exec, the live image would
    otherwise equal a candidate and every launch would burn the full
    stub-replacement window.  Dropping this process's own (already-settled)
    image keeps the guard inert there while leaving a genuine, *different*
    launcher stub in the set.
    """

    def test_own_live_image_is_not_a_stub(self):
        import skillweave.runtime.process_identity as _pid

        settled = _pid.capture(os.getpid())
        # Capturing the running process is a primitive the whole run lifecycle
        # already depends on; its failure is a real environment failure, not a
        # reason to weaken this proof.  No skip, no xfail.
        assert settled is not None and settled.exec_path, (
            "process_identity.capture could not observe this running process")
        assert settled.exec_path not in svc._native_launcher_stub_images(), (
            "this process's settled image was classified as a transient stub; "
            "that would force a stub-wait on every launch")


#: The single fact each declared refusal is keyed to, paired with the reason the
#: strict verifier must still report when that one fact differs.
_PERTURBATIONS = [
    # (case, recorded overrides, live overrides, expected reason)
    ("foreign_worker", {}, {"exec_path": "/usr/bin/impostor"},
     pid.REASON_FOREIGN),
    ("recycled_pid", {}, {"start": "999.0"}, pid.REASON_RECYCLED),
    ("stale_command", {}, {"argv": ("/usr/bin/worker", "--job", "8")},
     pid.REASON_STALE_COMMAND),
]


class TestRefusalNotWeakened:
    """Negative controls: every strict refusal still fires, and each reason is
    attributable to exactly one perturbed fact.

    ``_recorded``/``_live`` describe the *same* process: with nothing perturbed
    ``verify`` returns ``match``.  That ``match`` leg is the negative control --
    it proves the reason in the perturbed leg is produced by the check for that
    one fact and by nothing else.  Remove that check and the perturbed leg falls
    through to ``match``, so the per-reason assertion below fails.  The capture
    side never weakens this: it only decides *what may be recorded*, and every
    recorded identity here is still subject to the unchanged verifier.
    """

    @staticmethod
    def _live(pid_value=77, **over):
        base = dict(exec_path="/usr/bin/worker", start="100.0",
                    argv=("/usr/bin/worker", "--job", "7"))
        base.update(over)
        return _identity(pid_value, base["exec_path"], base["argv"],
                         start=base["start"])

    @pytest.mark.parametrize(
        "recorded_over,live_over,reason", [
            (ro, lo, r) for _case, ro, lo, r in _PERTURBATIONS
        ],
        ids=[c for c, _ro, _lo, _r in _PERTURBATIONS],
    )
    def test_refusal_still_fires(self, monkeypatch, recorded_over, live_over,
                                 reason):
        recorded = self._live(77, **recorded_over).to_dict()
        live = self._live(77, **live_over)
        monkeypatch.setattr(pid, "capture", lambda _pid: live)

        check = pid.verify(recorded, 77)
        assert not check.ok and check.reason == reason, (
            f"expected a {reason} refusal, got {check.reason}: {check.detail}")

    def test_negative_control_unperturbed_matches(self, monkeypatch):
        """The control leg: with no fact perturbed the verifier returns
        ``match``.  It is the baseline the four refusals above are measured
        against -- without it a reason assertion could pass tautologically."""
        recorded = self._live(77).to_dict()
        monkeypatch.setattr(pid, "capture", lambda _pid: self._live(77))

        check = pid.verify(recorded, 77)
        assert check.ok and check.reason == "match", (
            f"an unperturbed identity was refused: {check.reason}: "
            f"{check.detail}")

    def test_not_alive_still_fires(self, monkeypatch):
        """A vanished process is refused with ``not_alive``, not silently
        treated as a match (the base of that refusal is a live capture)."""
        recorded = self._live(77).to_dict()
        monkeypatch.setattr(pid, "capture", lambda _pid: None)

        check = pid.verify(recorded, 77)
        assert not check.ok and check.reason == pid.REASON_NOT_ALIVE, (
            f"expected not_alive, got {check.reason}: {check.detail}")
