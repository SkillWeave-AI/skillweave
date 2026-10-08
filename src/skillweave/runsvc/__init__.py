"""Run Application Service (SW-RUN-SVC-001).

The single authoritative integration path through the runtime: one call goes
from a real harness subprocess, through the journal, the content-addressed raw
artifact store, the receipt registry, the independent verifier, and the gate,
leaving each of six record kinds behind without a gap.

The service owns the *wiring*, not the primitives. It consumes the existing
runtime pieces (``SQLiteRunStore``, ``EventJournal``, ``RawArtifactStore``,
``CompletionContract``/``Verifier``, and the gate-state surface) and makes the
run they collectively describe the only object a caller ever receives. A caller
does not call ``store`` and ``journal`` and ``verifier`` in some hand-rolled
order; it calls ``RunApplicationService.execute`` once and gets back a run that
carries all six records, each resolvable back to raw bytes.
"""

from .service import (
    RUN_SNAPSHOT_VERSION,
    SUPPORTED_RUN_SNAPSHOT_VERSIONS,
    RunApplicationService,
    RunExecution,
    RunExecutionError,
    RunIntegrationError,
    RunSnapshotContractError,
)

__all__ = [
    "RUN_SNAPSHOT_VERSION",
    "SUPPORTED_RUN_SNAPSHOT_VERSIONS",
    "RunApplicationService",
    "RunExecution",
    "RunExecutionError",
    "RunIntegrationError",
    "RunSnapshotContractError",
]
