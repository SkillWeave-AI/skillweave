"""SW-1511A-HANDOFF-001 finding F-1511A-01 -- evidence is *verified* at admission.

The review-001 verdict (C3) found the attestation's ``evidence`` field was a
verbatim passthrough: a client could attest *any* route with *any* counters and
be admitted, so "prove their configured route with gateway counters" proved
nothing.  This file is the adversarial proof that the **real admission boundary**
(``skillweave.mcp.server.handoff_adapter``) now verifies the claim before it is
recorded -- on in-process fakes, no network, no provider, no credential.

On the pre-repair head the entry point admits every offer below as ``ok`` (the
evidence is copied through untouched), so each adversarial test here fails
there; here the same real tool call returns the named HOLD the criterion asks
for.  Every test uses a per-test temporary store and mailbox, so a re-run is a
re-run.
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
from skillweave.runtime.store import SQLiteRunStore  # noqa: E402


def _store(tmp_path: Path, run_id: str) -> tuple[SQLiteRunStore, str]:
    db = str(tmp_path / "store.db")
    store = SQLiteRunStore(db)
    store.create_run(run_id, task_id="t", attempt=1)
    store.acquire_controller_lease("ops")
    return store, db


def _attestation(evidence, **over) -> dict:
    base = {
        "schema": "harness-controller-attestation/v1",
        "client_id": "claude-desktop",
        "configured_route": {
            "gateway_host": "127.0.0.1",
            "gateway_port": 8090,
            "route": "faigate/deepseek-v4",
            "credential_binding": "gateway://127.0.0.1:8090",
        },
        "recipient_principal": "dest-principal",
        "capabilities": {"session_resume": True, "notifications": True},
        "evidence": evidence,
    }
    base.update(over)
    return base


def _offer(attestation, **over) -> dict:
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


# ── the refusals (each fails on the pre-repair head) ────────────────────────


def test_a_route_claim_with_zero_observed_requests_is_a_named_hold(
    tmp_path, monkeypatch,
):
    """A client claiming a route it made *no* gateway request on proves nothing."""
    store, db = _store(tmp_path, "run-1")
    payload = _drive(
        db, tmp_path, monkeypatch,
        _offer(_attestation({
            "gateway_request_count": 0,
            "gateway_route": "faigate/deepseek-v4",
            "exit_code": 0,
        })),
    )
    assert payload["status"] == "hold"
    assert payload["code"] == ch.HANDOFF_HOLD_EVIDENCE_UNVERIFIED
    assert ch.read_handoff(store, "run-1") is None  # nothing was recorded
    store.close()


def test_a_route_claim_with_no_counter_at_all_is_a_named_hold(tmp_path, monkeypatch):
    """An absent counter is an *unverifiable* claim, never a satisfied one."""
    store, db = _store(tmp_path, "run-1")
    payload = _drive(
        db, tmp_path, monkeypatch,
        _offer(_attestation({
            "gateway_route": "faigate/deepseek-v4",
            "exit_code": 0,
        })),
    )
    assert payload["status"] == "hold"
    assert payload["code"] == ch.HANDOFF_HOLD_EVIDENCE_UNVERIFIED
    store.close()


def test_a_route_claim_whose_run_failed_is_a_named_hold(tmp_path, monkeypatch):
    """The evidence's own non-zero exit code shows the proving run did not succeed."""
    store, db = _store(tmp_path, "run-1")
    payload = _drive(
        db, tmp_path, monkeypatch,
        _offer(_attestation({
            "gateway_request_count": 37,
            "gateway_route": "faigate/deepseek-v4",
            "exit_code": 1,
        })),
    )
    assert payload["status"] == "hold"
    assert payload["code"] == ch.HANDOFF_HOLD_EVIDENCE_UNVERIFIED
    store.close()


# ── the additive control: no route claim, nothing to verify ─────────────────


def test_an_attestation_naming_no_route_makes_no_claim_and_is_admitted(
    tmp_path, monkeypatch,
):
    """Route-less attestations stay admitted exactly as before (additive)."""
    store, db = _store(tmp_path, "run-1")
    payload = _drive(
        db, tmp_path, monkeypatch,
        _offer(_attestation({"exit_code": 0})),
    )
    assert payload["status"] == "ok"
    store.close()


# ── the positive control: a backed claim is admitted and records the check ──


def test_a_backed_route_claim_is_admitted_and_the_check_is_recorded(
    tmp_path, monkeypatch,
):
    """A positive counter admits the offer, and the record states what was checked."""
    store, db = _store(tmp_path, "run-1")
    payload = _drive(
        db, tmp_path, monkeypatch,
        _offer(_attestation({
            "gateway_request_count": 37,
            "gateway_route": "faigate/deepseek-v4",
            "exit_code": 0,
            "artifact": "sha256:claude-desktop-run",
        })),
    )
    assert payload["status"] == "ok"
    record = ch.read_handoff(store, "run-1")
    assert record is not None
    verification = record.destination_attestation.evidence["server_verification"]
    assert verification["gateway_request_count_observed"] == 37
    # The live qualification the criterion also asks for is NOT performed here,
    # and the record says so rather than implying a proof it did not perform.
    assert verification["live_qualification"] == "NOT PERFORMED"
    store.close()
