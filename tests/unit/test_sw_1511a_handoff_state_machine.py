"""SW-1511A-HANDOFF-001 criterion 1 -- production-caller acceptance tests.

Criterion 1 (verbatim): *A typed offered/accepted/active/released or rejected
state machine transfers the controller lease to a server-issued destination
principal without carrying provider credentials.*

Proven through the **real production handoff entry point**
(``skillweave.mcp.server.handoff_adapter``) on in-process fakes -- no network,
no provider, no credential.  The negative control is the criterion's own
"production-caller standard": on the pre-change base the
``controller_handoff`` contract module does not exist, so the real entry point
cannot reach it and every test below fails; here the real tool call reaches
exactly that module's objects (object identity, never a re-implemented copy).

Every test uses a per-test temporary store and mailbox -- no fixed ids in a
shared temp path -- so a re-run is a re-run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_src = Path(__file__).resolve().parent.parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

import pytest  # noqa: E402

from skillweave.mcp import server as mcp_server  # noqa: E402
from skillweave.runtime import controller_handoff as ch  # noqa: E402
from skillweave.runtime.store import SQLiteRunStore  # noqa: E402

#: The reviewed candidate's own contract module: the production path must reach
#: *this* object, so the assertions pin object identity rather than re-assert a
#: parallel implementation of the same behaviour.
CONTRACT_MODULE = sys.modules["skillweave.runtime.controller_handoff"]


def _store(tmp_path: Path, run_id: str) -> tuple[SQLiteRunStore, str]:
    """A real per-test store whose *offering* controller already holds the lease.

    An offer is made by the controller that currently holds the lease, so the
    fixture acquires generation 1 as ``ops`` first: the destination's own later
    acquisition is then the exactly-once generation ``offered + 1``.  Without
    this the offer would carry generation 0 -- a controller that holds nothing.
    """
    db = str(tmp_path / "store.db")
    store = SQLiteRunStore(db)
    store.create_run(run_id, task_id="t", attempt=1)
    store.acquire_controller_lease("ops")
    return store, db


def _attestation(**over) -> dict:
    base = {
        "schema": "harness-controller-attestation/v1",
        "client_id": "claude-desktop",
        "configured_route": {"gateway_host": "127.0.0.1", "gateway_port": 8090},
        "recipient_principal": "dest-principal",
        "capabilities": {"session_resume": True, "notifications": True},
    }
    route = over.pop("configured_route", None)
    if route:
        base["configured_route"] = {**base["configured_route"], **route}
    caps = over.pop("capabilities", None)
    if caps:
        base["capabilities"] = {**base["capabilities"], **caps}
    base.update(over)
    return base


def _offer(**over) -> dict:
    base = {
        "schema": "controller-handoff/v2",
        "handoff_id": "ho-1",
        "from_holder": "ops",
        "destination_principal": "dest-principal",
        "scope": "run-1",
        "fencing_generation": 1,
        "credential_binding": "gateway://127.0.0.1:8090",
        "destination_attestation": _attestation(),
    }
    base.update(over)
    return base


def _drive(db: str, tmp_path: Path, monkeypatch, offer: dict, **kw) -> dict:
    """Call the **real** ``handoff_adapter`` tool with an offer (fakes only)."""
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
            **kw,
        )
    )


# ── C1: the typed state machine, reached through the real tool ─────────────


def test_real_tool_admits_a_typed_offered_handoff(tmp_path, monkeypatch):
    """The real entry point admits the offer and records a typed ``offered``."""
    store, db = _store(tmp_path, "run-1")
    payload = _drive(db, tmp_path, monkeypatch, _offer())

    assert payload["status"] == "ok"
    recorded = payload["controller_handoff"]
    assert recorded["schema"] == CONTRACT_MODULE.CONTROLLER_HANDOFF_SCHEMA_ID
    assert recorded["state"] == CONTRACT_MODULE.HANDOFF_STATE_OFFERED
    assert recorded["destination_principal"] == "dest-principal"
    # The record is *durable*: a fresh read reaches the same object the module
    # owns, so the entry point drove the contract module -- not a copy.
    reread = ch.read_handoff(store, "run-1")
    assert reread is not None and reread.handoff_id == "ho-1"
    assert reread.from_holder == "ops"
    store.close()


def test_the_admitted_record_is_typed_not_free_form(tmp_path, monkeypatch):
    """The state is one of the declared typed values, never an ad-hoc string."""
    store, db = _store(tmp_path, "run-1")
    payload = _drive(db, tmp_path, monkeypatch, _offer())
    assert payload["controller_handoff"]["state"] in {
        CONTRACT_MODULE.HANDOFF_STATE_OFFERED,
        CONTRACT_MODULE.HANDOFF_STATE_ACCEPTED,
        CONTRACT_MODULE.HANDOFF_STATE_ACTIVE,
        CONTRACT_MODULE.HANDOFF_STATE_RELEASED,
        CONTRACT_MODULE.HANDOFF_STATE_REJECTED,
    }
    store.close()


def test_full_offered_accepted_active_released_lifecycle(tmp_path, monkeypatch):
    """offered -> accepted -> active -> released, on the destination's real lease."""
    store, db = _store(tmp_path, "run-1")
    payload = _drive(db, tmp_path, monkeypatch, _offer())
    record = ch.read_handoff(store, "run-1")
    assert record.state == ch.HANDOFF_STATE_OFFERED
    accepted = ch.accept_handoff(record)
    assert accepted.state == ch.HANDOFF_STATE_ACCEPTED
    # The offerer yields the generation it held (1) before the destination takes
    # it, so the destination's acquisition is the exactly-once generation 2.
    assert store.release_controller_lease(record.fencing_generation) is True
    active = ch.activate_handoff(
        accepted, acquire=lambda: store.acquire_controller_lease("dest")
    )
    assert active.state == ch.HANDOFF_STATE_ACTIVE
    assert active.resulting_generation == int(active.fencing_generation) + 1
    released = ch.release_handoff(active)
    assert released.state == ch.HANDOFF_STATE_RELEASED
    assert released.live is False
    store.close()


def test_rejected_is_a_distinct_terminal_state(tmp_path, monkeypatch):
    """A rejected handoff names why it did not transfer, and is terminal."""
    store, db = _store(tmp_path, "run-1")
    _drive(db, tmp_path, monkeypatch, _offer())
    record = ch.read_handoff(store, "run-1")
    rejected = ch.reject_handoff(record, reason="destination declined")
    assert rejected.state == ch.HANDOFF_STATE_REJECTED
    assert rejected.rejected_reason == "destination declined"
    assert rejected.activated_generation is None
    with pytest.raises(ch.ControllerHandoffRefused):
        ch.accept_handoff(rejected)  # terminal: never reactivates
    store.close()


def test_a_credential_is_never_carried_by_the_transfer(tmp_path, monkeypatch):
    """A provider credential in the binding is refused by shape (never echoed)."""
    store, db = _store(tmp_path, "run-1")
    payload = _drive(
        db, tmp_path, monkeypatch, _offer(credential_binding="sk-abcdefghijklmnop")
    )
    assert payload["status"] == "hold"
    assert payload["code"] == CONTRACT_MODULE.HANDOFF_HOLD_TRANSFER_UNSUPPORTED
    # The secret-shaped value is never echoed back in the refusal.
    assert "sk-abcdefghijklmnop" not in json.dumps(payload)
    store.close()


def test_an_absent_attestation_is_a_named_hold(tmp_path, monkeypatch):
    """No attestation is a named HOLD, never an inferred capability."""
    store, db = _store(tmp_path, "run-1")
    payload = _drive(
        db, tmp_path, monkeypatch, _offer(destination_attestation=None)
    )
    assert payload["status"] == "hold"
    assert payload["code"] == CONTRACT_MODULE.HANDOFF_HOLD_ATTESTATION_ABSENT
    store.close()


def test_a_route_that_is_not_the_observed_one_is_a_named_hold():
    """Admission refuses an attested route that is not the configured one."""
    att = ch.HandoffAttestation(
        client_id="claude-desktop",
        gateway_host="127.0.0.1",
        gateway_port=8090,
        recipient_principal="dest-principal",
        notifications=True,
        session_resume=True,
    )
    with pytest.raises(ch.ControllerHandoffRefused) as exc:
        ch.offer_handoff(
            from_holder="ops",
            destination_principal="dest-principal",
            scope="run-1",
            destination_attestation=att,
            observed_route=("10.0.0.9", 9999),
            credential_binding="gateway://127.0.0.1:8090",
        )
    assert exc.value.code == ch.HANDOFF_HOLD_ATTESTATION_ROUTE


def test_a_mismatched_recipient_principal_is_a_named_hold():
    """A handoff addressed to another principal than the attested one is held."""
    att = ch.HandoffAttestation(
        client_id="claude-desktop",
        gateway_host="127.0.0.1",
        gateway_port=8090,
        recipient_principal="the-attested-one",
        notifications=True,
        session_resume=True,
    )
    with pytest.raises(ch.ControllerHandoffRefused) as exc:
        ch.offer_handoff(
            from_holder="ops",
            destination_principal="a-different-principal",
            scope="run-1",
            destination_attestation=att,
            observed_route=None,
            credential_binding="gateway://127.0.0.1:8090",
        )
    assert exc.value.code == ch.HANDOFF_HOLD_ATTESTATION_PRINCIPAL


def test_an_unsupported_client_remains_a_named_hold():
    """A client that attests it cannot accept a handoff is a named HOLD."""
    att = ch.HandoffAttestation(
        client_id="some-other-client",
        gateway_host="127.0.0.1",
        gateway_port=8090,
        recipient_principal="p",
        notifications=False,
        session_resume=False,
        can_accept_controller_handoff=False,
    )
    with pytest.raises(ch.ControllerHandoffRefused) as exc:
        ch.offer_handoff(
            from_holder="ops",
            destination_principal="p",
            scope="run-1",
            destination_attestation=att,
            observed_route=None,
            credential_binding="gateway://127.0.0.1:8090",
        )
    assert exc.value.code == ch.HANDOFF_HOLD_TRANSFER_UNSUPPORTED


def test_the_two_core_schemas_are_the_declared_identities():
    """The record attests the exact contract identity it speaks."""
    handoff = ch.load_handoff_schema("handoff")
    assert handoff["properties"]["schema"]["const"] == "controller-handoff/v2"
    assert handoff["properties"]["state"]["enum"] == [
        "offered", "accepted", "active", "released", "rejected",
    ]
    attestation = ch.load_handoff_schema("attestation")
    assert (
        attestation["properties"]["schema"]["const"]
        == "harness-controller-attestation/v1"
    )
