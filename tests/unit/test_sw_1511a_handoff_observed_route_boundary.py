"""SW-1511A-HANDOFF-001 finding F-1511A-02 -- the observed route reaches admission.

Review-001 (C3) found ``src/skillweave/mcp/server.py`` passed a hardcoded
``observed_route=None`` into the admission, so the module's *route-match* axis --
the axis :func:`skillweave.runtime.controller_handoff.offer_handoff` already
enforces whenever it is given an observation -- was dead at the real boundary: a
harness could attest a gateway the server never observed and still be admitted.

This file is the adversarial proof that the **real admission boundary**
(``skillweave.mcp.server.handoff_adapter``) now derives the observation from the
server's *own* durable records and enforces it -- on in-process fakes, no
network, no provider, no credential, no live harness.

On the pre-repair head ``observed_route`` is ``None`` at the boundary, so an
offer attesting a gateway that contradicts the server's observed endpoint is
admitted as ``ok`` and each refusal test below fails there; here the same real
tool call returns the named HOLD.  The positive controls prove the axis is
*enforced*, not merely made to refuse everything.

Every test uses a per-test temporary store and mailbox, so a re-run is a re-run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_src = Path(__file__).resolve().parent.parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from skillweave.mcp import server as mcp_server  # noqa: E402
from skillweave.runtime import controller_handoff as ch  # noqa: E402
from skillweave.runtime.registry import ArtifactReceipt  # noqa: E402
from skillweave.runtime.store import SQLiteRunStore  # noqa: E402

#: The gateway the harness *attests* is configured for it (the owner pin).
ATTESTED_HOST = "127.0.0.1"
ATTESTED_PORT = 8090


def _store(tmp_path: Path, run_id: str) -> tuple[SQLiteRunStore, str]:
    db = str(tmp_path / "store.db")
    store = SQLiteRunStore(db)
    store.create_run(run_id, task_id="t", attempt=1)
    store.acquire_controller_lease("ops")
    return store, db


def _seed_observed_receipt(
    store: SQLiteRunStore, run_id: str, *, endpoint: str, route: str
) -> None:
    """Write a *server-observed* upstream receipt for the run.

    This is the durable, server-written observation the boundary reads: the
    gateway endpoint the upstream actually answered on and its route.  A caller
    cannot set it from the offer -- it is evidence the server recorded.
    """
    store.save_evidence(
        ArtifactReceipt(
            artifact_id=f"upstream-{run_id}",
            sha256="a" * 64,
            schema_version="upstream-receipt/1.0.0",
            producer_command="gateway-observe",
            subject_repo="repo",
            subject_commit="c0ffee",
            created_at="2026-10-16T00:00:00+00:00",
            evidence_type="observation",
            purpose="route-observation",
            metadata={
                "run_id": run_id,
                "receipt": {"gateway_endpoint": endpoint, "route": route},
            },
        )
    )


def _seed_dispatch_route(store: SQLiteRunStore, run_id: str, route: str) -> None:
    """Record the route the server dispatched an owned attempt on."""
    store.record_dispatch_intent(
        run_id, task_id="t", attempt=1, execution_scope=f"{run_id}:scope",
        step="ready_dispatch", route=route,
    )


def _attestation(*, evidence=None, **over) -> dict:
    """The attestation a harness presents; its configured gateway is the pin."""
    base = {
        "schema": "harness-controller-attestation/v1",
        "client_id": "claude-desktop",
        "configured_route": {
            "gateway_host": ATTESTED_HOST,
            "gateway_port": ATTESTED_PORT,
            "route": "faigate/deepseek-v4",
            "credential_binding": "gateway://127.0.0.1:8090",
        },
        "recipient_principal": "dest-principal",
        "capabilities": {"session_resume": True, "notifications": True},
        # Route-less evidence by default: the *endpoint* axis (this finding) is
        # isolated from the evidence *route* cross-check (F-1511A-01).
        "evidence": evidence if evidence is not None else {"exit_code": 0},
    }
    base.update(over)
    return base


def _offer(attestation: dict, **over) -> dict:
    base = {
        "schema": "controller-handoff/v2",
        "handoff_id": "ho-1",
        "from_holder": "ops",
        "destination_principal": attestation["recipient_principal"],
        "scope": "run-1",
        "fencing_generation": 1,
        "credential_binding": "gateway://127.0.0.1:8090",
        "destination_attestation": attestation,
    }
    base.update(over)
    return base


def _drive(db, tmp_path, monkeypatch, offer) -> dict:
    monkeypatch.setenv(mcp_server.MAILBOX_PATH_ENV, str(tmp_path / "mailbox"))
    monkeypatch.setattr(mcp_server, "_AUTHORITY", None)
    return json.loads(
        mcp_server.handoff_adapter(
            client_id="claude-desktop",
            recipients="ops",
            run_id="run-1",
            db_path=db,
            session_token="",
            handoff_offer=json.dumps(offer),
        )
    )


# ── the refusal (fails on the pre-repair head, which hardcodes None) ────────


def test_an_attested_gateway_the_server_did_not_observe_is_a_named_hold(
    tmp_path, monkeypatch,
):
    """The server observed the upstream on ``10.0.0.9:9999``; an attestation
    naming a *different* configured gateway is refused at the real boundary."""
    store, db = _store(tmp_path, "run-1")
    _seed_observed_receipt(
        store, "run-1", endpoint="http://10.0.0.9:9999", route="faigate/deepseek-v4",
    )
    payload = _drive(db, tmp_path, monkeypatch, _offer(_attestation()))

    assert payload["status"] == "hold"
    assert payload["code"] == ch.HANDOFF_HOLD_ATTESTATION_ROUTE
    assert ch.read_handoff(store, "run-1") is None  # nothing was recorded
    store.close()


# ── the positive controls: the axis is enforced, not blanket-refusing ───────


def test_an_offer_matching_the_observed_endpoint_is_admitted(tmp_path, monkeypatch):
    """A gateway the server *did* observe is admitted: the axis compares, it
    does not merely refuse."""
    store, db = _store(tmp_path, "run-1")
    _seed_observed_receipt(
        store, "run-1",
        endpoint=f"http://{ATTESTED_HOST}:{ATTESTED_PORT}",
        route="faigate/deepseek-v4",
    )
    payload = _drive(db, tmp_path, monkeypatch, _offer(_attestation()))

    assert payload["status"] == "ok"
    assert ch.read_handoff(store, "run-1") is not None
    store.close()


def test_an_absent_server_observation_leaves_the_axis_unevaluated(
    tmp_path, monkeypatch,
):
    """With no server observation there is nothing to compare, so the offer is
    admitted exactly as before -- the boundary never fabricates an observation."""
    store, db = _store(tmp_path, "run-1")
    payload = _drive(db, tmp_path, monkeypatch, _offer(_attestation()))

    assert payload["status"] == "ok"
    store.close()


# ── the same observation, from the dispatch record ─────────────────────────


def test_a_dispatch_route_contradicting_the_evidence_route_is_a_named_hold(
    tmp_path, monkeypatch,
):
    """The route the server *dispatched* on is an observation too: evidence
    claiming a different route is refused at the real boundary."""
    store, db = _store(tmp_path, "run-1")
    _seed_dispatch_route(store, "run-1", "faigate/route-alpha")
    payload = _drive(
        db, tmp_path, monkeypatch,
        _offer(_attestation(evidence={
            "gateway_request_count": 37,
            "gateway_route": "faigate/deepseek-v4",
            "exit_code": 0,
        })),
    )

    assert payload["status"] == "hold"
    assert payload["code"] == ch.HANDOFF_HOLD_EVIDENCE_UNVERIFIED
    assert ch.read_handoff(store, "run-1") is None
    store.close()
