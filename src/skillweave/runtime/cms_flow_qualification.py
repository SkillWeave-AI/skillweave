"""The production consumer of the declared CMS paths (SW-1511-CMS-FLOWS).

:mod:`skillweave.blueprint.cms_path_qualification` owns the frozen declaration
of the five initial CMS paths and the ``cms-process-qualification-v1`` record an
observer is supposed to fill in -- but, on its own, that module has **no
production caller**: it is a contract nothing in the shipped flow actually
drives.  A contract with no consumer is a declaration, not a qualification.

This module is that consumer.  :func:`qualify_cms_path` takes one *declared*
path (a :class:`~skillweave.blueprint.cms_path_qualification.CmsPath` from
:data:`~skillweave.blueprint.cms_path_qualification.INITIAL_CMS_PATHS`) and drives
it through the **existing native controller seam** -- the durable, fenced
:class:`~skillweave.runtime.native_process.NativeProcessService` -- over the
repository's dummy :class:`~skillweave.runtime.native_process.RawContentAdapter`:

* one owned attempt is dispatched with a **real, live process identity** (the
  producer is *derived* from the owned attempt's durable identity, never
  declared by the caller);
* the reversible lifecycle runs to completion -- baseline -> draft -> dry-run ->
  bounded apply -> readback -> restore -- on an owner-authorized *sandbox*
  binding, then the transaction is read back **from durable state** so the
  readback/restore verdict comes from a persisted record, not a live claim;
* the record is built through the frozen fail-closed
  :meth:`CmsProcessQualification.from_mapping
  <skillweave.blueprint.cms_path_qualification.CmsProcessQualification.from_mapping>`.

The honesty of the result is the point.  This consumer genuinely *performs* the
``success`` outcome of the path's lifecycle -- so it carries the real readback
and restore evidence and the source-bound pins -- but it measures **only** that
outcome.  The contract requires all six measurable outcomes
(:data:`~skillweave.blueprint.cms_path_qualification.REQUIRED_OUTCOMES`) before a
record may claim ``performed``, so this consumer deliberately returns a
``not-performed`` record with the measured evidence attached and a *named*
limitation.  It never forges the remaining five outcomes into a prewritten
receipt: measuring ``wait``/``failure``/``cancel``/``resume``/
``publication-refusal`` is a later lane's job, exactly as the frozen module's
docstring says.

The seam is a **fake at the process boundary**: no provider, no gateway and no
CMS call is made or probed.  ``RawContentAdapter`` is an in-memory dummy and the
binding is a declared sandbox; the private provider implementation stays in
``skillweave-packs-pro``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..blueprint.cms_path_qualification import (
    CMS_PATHS_BY_ID,
    DISPOSITION_NOT_PERFORMED,
    INITIAL_CMS_PATHS,
    SCHEMA_VERSION,
    CmsPath,
    CmsProcessQualification,
)

__all__ = [
    "CMS_QUALIFICATION_SANDBOX_TARGET",
    "CmsFlowQualificationRefused",
    "CmsPathQualificationRun",
    "qualify_cms_path",
]

#: The owner-authorized *sandbox* target the qualification is driven on.  It is a
#: declared sandbox (:data:`~skillweave.runtime.native_process.CMS_BINDING_SANDBOX`)
#: and carries no production marker, so the sandbox discriminator and the
#: owner-declaration check both pass without touching a production surface.
CMS_QUALIFICATION_SANDBOX_TARGET = "sandbox/ch"

#: The baseline content the reversible lifecycle starts from (the dummy
#: adapter's in-memory value).  Kept as a module constant so a qualification run
#: is reproducible.
CMS_QUALIFICATION_BASELINE: dict[str, Any] = {"title": "old", "body": "hello"}

#: The bounded change the lifecycle applies and then restores away from.
CMS_QUALIFICATION_CHANGE: dict[str, Any] = {"title": "new", "body": "hello"}

#: The single outcome this consumer can genuinely measure through the reversible
#: lifecycle seam.  It is deliberately a one-element set: a record that carries
#: only this must remain ``not-performed``.
MEASURED_SUCCESS_ONLY: tuple[str, ...] = ("success",)

#: The limitation this consumer attaches to every record it produces, naming the
#: outcomes it did *not* measure rather than implying it did.
UNMEASURED_OUTCOME_LIMITATION = (
    "only the 'success' outcome was measured by driving the reversible lifecycle; "
    "'wait', 'failure', 'cancel', 'resume' and 'publication-refusal' are not "
    "measured by this consumer, so the record stays 'not-performed' by contract"
)


class CmsFlowQualificationRefused(ValueError):
    """A requested path is not one of the five declared CMS paths.

    Raised *before* any effect, so a qualification run can never be pointed at a
    path the frozen declaration does not name.
    """


@dataclass(frozen=True)
class CmsPathQualificationRun:
    """What driving one declared CMS path through the native seam produced.

    Carries the resolved :class:`CmsPath`, the fail-closed qualification record,
    and the durable facts of the driven transaction (the derived producer
    identity, the transaction id and its persisted final stage) so a caller can
    re-read the persisted record rather than trust this object.
    """

    path: CmsPath
    qualification: CmsProcessQualification
    producer_identity: str
    run_id: str
    txn_id: str
    final_stage: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path.to_dict(),
            "qualification": self.qualification.to_dict(),
            "producer_identity": self.producer_identity,
            "run_id": self.run_id,
            "txn_id": self.txn_id,
            "final_stage": self.final_stage,
        }


def _resolve_declared_path(path: Any) -> CmsPath:
    """Resolve ``path`` to a frozen declaration, refusing anything undeclared.

    Accepts one of the five frozen path ids or a :class:`CmsPath`; either way the
    **frozen** declaration is returned, so a caller cannot qualify a path it
    invented and the same path id always resolves to the same declaration.
    """
    path_id = path.path_id if isinstance(path, CmsPath) else path
    if not isinstance(path_id, str) or not path_id.strip():
        raise CmsFlowQualificationRefused(
            f"a CMS path qualification must name a declared path, got {path!r}"
        )
    declared = CMS_PATHS_BY_ID.get(path_id.strip())
    if declared is None:
        raise CmsFlowQualificationRefused(
            f"{path_id.strip()!r} is not one of the five declared CMS paths "
            f"{[p.path_id for p in INITIAL_CMS_PATHS]}"
        )
    return declared


def qualify_cms_path(
    store: Any,
    path: Any,
    *,
    run_id: str,
    task_id: str = "cms-flow-qualification",
    attempt: int = 1,
    holder: str = "cms-flow-qualification",
    ttl_seconds: int = 300,
) -> CmsPathQualificationRun:
    """Drive one declared CMS path and return its qualification record.

    ``store`` is the durable store the production controller uses; ``path`` is a
    declared path id or a :class:`CmsPath`.  The service acquires the single
    fenced controller lease, drives the reversible lifecycle through the dummy
    adapter, reads the transaction back from durable state, builds the
    fail-closed record and releases the lease on every exit path.
    """
    declared = _resolve_declared_path(path)

    # Imported lazily so that merely importing this module never pulls the whole
    # runtime into a caller that only wants the contract.
    from .native_process import (
        NativeProcessService,
        OwnerSandboxDeclaration,
        SandboxBinding,
    )
    from ..dispatch.work_contract import ContentSubject

    service = NativeProcessService(store, holder)
    service.acquire(ttl_seconds=ttl_seconds)
    try:
        subject = ContentSubject(
            channel=CMS_QUALIFICATION_SANDBOX_TARGET,
            content_id=f"cms-flow:{declared.path_id}",
            revision="rev-1",
        )
        # The existing native controller seam: one owned attempt (dispatched with
        # a live process identity), then the full reversible lifecycle through the
        # dummy in-memory adapter on an owner-authorized sandbox binding.
        driven = service.cms_run_no_transformer(
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
            subject=subject,
            baseline=dict(CMS_QUALIFICATION_BASELINE),
            binding=SandboxBinding(target=CMS_QUALIFICATION_SANDBOX_TARGET),
            owner_sandbox=OwnerSandboxDeclaration(
                targets=(CMS_QUALIFICATION_SANDBOX_TARGET,)
            ),
        )
        # Read the transaction back *from durable state*: the readback/restore
        # verdict must come from the persisted record, not a live in-memory claim.
        txn_id = driven.txn_id
        persisted = service.load_cms_transaction(run_id, txn_id)
        if persisted is None:
            raise CmsFlowQualificationRefused(
                f"the driven CMS transaction {txn_id!r} was not persisted; a "
                f"qualification cannot be read back from durable state"
            )

        applied = persisted.applied_hashes
        pins = {
            "controller": f"{holder}#{service.fencing_token}",
            "source_digest": persisted.baseline_hashes.semantic,
            "artifact_digest": (
                applied.semantic if applied is not None else persisted.draft_digest
            ),
        }
        qualification = CmsProcessQualification.from_mapping(
            {
                "artifact": SCHEMA_VERSION,
                "versioning": (
                    "v1 draft contract; schema URI, revision and digest are frozen "
                    "at the SW-1511 baseline task"
                ),
                "producer": persisted.producer_identity,
                "consumer": "release assessor",
                "compatibility": (
                    "additive opt-in contract; preserve existing v1 records and "
                    "original-skill execution; unsupported versions fail closed"
                ),
                "disposition": DISPOSITION_NOT_PERFORMED,
                "paths": [p.path_id for p in INITIAL_CMS_PATHS],
                "pins": pins,
                "readback_verified": persisted.verified(),
                "restore_verified": persisted.restored(),
                "measured_outcomes": list(MEASURED_SUCCESS_ONLY),
                "limitations": [UNMEASURED_OUTCOME_LIMITATION],
            }
        )
    finally:
        if service.fencing_token is not None:
            service.release()

    return CmsPathQualificationRun(
        path=declared,
        qualification=qualification,
        producer_identity=persisted.producer_identity,
        run_id=run_id,
        txn_id=txn_id,
        final_stage=persisted.stage,
    )
