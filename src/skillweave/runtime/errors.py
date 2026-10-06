class StoreError(Exception):
    pass


class InvalidTransitionError(StoreError):
    def __init__(self, current_state, target_state, run_id, extra=None):
        self.current_state = current_state
        self.target_state = target_state
        self.run_id = run_id
        self.extra = extra or {}
        super().__init__(
            f"Invalid transition from '{current_state}' to '{target_state}' "
            f"for run '{run_id}'"
        )


class VersionConflictError(StoreError):
    def __init__(self, run_id, expected_version, actual_version):
        self.run_id = run_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"Version conflict for run '{run_id}': "
            f"expected {expected_version}, got {actual_version}"
        )


class ProcessIdentityError(StoreError):
    """A cancellation refused because the recorded process identity did not
    match the live process.

    Raised *before* any signal is sent when the OS facts (creation instant,
    exec image, argument vector) show the target is a recycled PID, a stale
    command, a foreign worker, or no longer observable.  The verification
    ``reason`` is carried so a caller can distinguish "definitely not ours"
    from "unknown, not confirmed terminated".
    """

    def __init__(self, run_id, pid, reason, verification=None):
        self.run_id = run_id
        self.pid = pid
        self.reason = reason
        self.verification = verification
        super().__init__(
            f"refusing to signal run '{run_id}' pid {pid}: "
            f"process identity verification failed ({reason})"
        )


class KillOutcomeError(StoreError):
    """A kill request could not be durably reconciled to a terminal outcome.

    Raised instead of returning a successful-looking result when cancellation
    cannot be committed for a reason other than CAS contention (for example a
    storage I/O failure).  A CAS race is *not* an error: it is reconciled by
    re-reading and retrying, or by confirming that an unrelated terminal
    winner already stands.
    """

    def __init__(self, run_id, diag, result="unknown", cause=None):
        self.run_id = run_id
        self.diag = diag
        self.result = result
        self.cause = cause
        super().__init__(
            f"kill could not be durably reconciled for run '{run_id}': "
            f"{diag.get('reason', 'unknown')} (result={result})"
        )
