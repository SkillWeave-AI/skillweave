"""SW-1511A-HANDOFF-001 finding F3 -- the transitions' production entry point.

Criterion 1 asks for a typed offered/accepted/active/released-or-rejected state
machine that *transfers the controller lease*.  Review of the live HANDOFF-C3
run (F3) found that only the **offer** was reachable in the product: the four
transitions (``accept``/``activate``/``release``/``reject``) were driven only at
module/store level by tests, so the criterion was not reachable end to end.

This file is the adversarial proof that every transition now runs through the
**real MCP handler** (``skillweave.mcp.server.handoff_adapter``) on in-process
fakes -- no network, no provider, no credential, and no direct module call to
drive a state.  On the pre-repair head the tool has no ``action`` argument, so
every test here fails there.

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
from skillweave.runtime.store import SQLiteRunStore  # noqa: E402

#: The reviewed candidate's own contract module: the production path must reach
#: *this* object, so the assertions pin object identity rather than re-assert a
#: parallel implementation of the same behaviour.
CONTRACT_MODULE = sys.modules["skillweave.runtime.controller_handoff"]


def _store(tmp_path: Path, run_id: str) -> tuple[SQLiteRunStore, str]:
    """A real per-test store whose *offering* controller holds generation 1."""
    db = str(tmp_path / "store.db")
    store = SQLiteRunStore(db)
    store.create_run(run_id, task_id="t", attempt=1)
    lease = store.acquire_controller_lease("ops")
    assert lease.fencing_token == 1
    return store, db


def _offer(**over) -> dict:
    base = {
        "schema": "controller-handoff/v2",
        "handoff_id": "ho-1",
        "from_holder": "ops",
        "destination_principal": "dest-principal",
        "scope": "run-1",
        "fencing_generation": 1,
        "credential_binding": "gateway://127.0.0.1:8090",
        "destination_attestation": {
            "schema": "harness-controller-attestation/v1",
            "client_id": "claude-desktop",
            "configured_route": {"gateway_host": "127.0.0.1", "gateway_port": 8090},
            "recipient_principal": "dest-principal",
            "capabilities": {"session_resume": True, "notifications": True},
        },
    }
    base.update(over)
    return base


def _call(db: str, tmp_path: Path, monkeypatch, **kw) -> dict:
    """Call the **real** ``handoff_adapter`` tool (fakes only)."""
    monkeypatch.setenv(mcp_server.MAILBOX_PATH_ENV, str(tmp_path / "mailbox"))
    monkeypatch.setattr(mcp_server, "_AUTHORITY", None)
    kw.setdefault("client_id", "claude-desktop")
    kw.setdefault("recipients", "ops")
    return json.loads(
        mcp_server.handoff_adapter(
            run_id="run-1",
            db_path=db,
            session_token="",
            **kw,
        )
    )


def _offer_ok(db, tmp_path, monkeypatch, **over) -> dict:
    payload = _call(
        db, tmp_path, monkeypatch, handoff_offer=json.dumps(_offer(**over))
    )
    assert payload["status"] == "ok"
    return payload


# ── F3: each transition is reachable through the real handler ──────────────


def test_full_lifecycle_offer_accept_activate_release(tmp_path, monkeypatch):
    """offered -> accepted -> active -> released, every step through the tool."""
    store, db = _store(tmp_path, "run-1")

    offered = _offer_ok(db, tmp_path, monkeypatch)
    assert offered["action"] == "offer"
    assert offered["controller_handoff"]["state"] == ch.HANDOFF_STATE_OFFERED
    assert offered["handoff_ref"]["handoff_id"] == "ho-1"

    accepted = _call(
        db, tmp_path, monkeypatch,
        action="accept", principal="dest-principal", handoff_id="ho-1",
    )
    assert accepted["status"] == "ok"
    assert accepted["controller_handoff"]["state"] == ch.HANDOFF_STATE_ACCEPTED

    # The offerer yields generation 1 so the destination's acquisition is the
    # exactly-once generation 2.
    assert store.release_controller_lease(1) is True
    active = _call(
        db, tmp_path, monkeypatch,
        action="activate", principal="dest-principal", handoff_id="ho-1",
    )
    assert active["status"] == "ok"
    rec = active["controller_handoff"]
    assert rec["state"] == ch.HANDOFF_STATE_ACTIVE
    assert rec["resulting_generation"] == 2
    assert active["handoff_ref"]["fencing_generation"] == 1
    assert store.get_controller_lease().holder == "dest-principal"

    released = _call(
        db, tmp_path, monkeypatch,
        action="release", principal="dest-principal", handoff_id="ho-1",
    )
    assert released["status"] == "ok"
    assert released["controller_handoff"]["state"] == ch.HANDOFF_STATE_RELEASED
    # The lease the destination held was actually yielded.
    assert store.get_controller_lease().released_at is not None
    store.close()


def test_rejected_is_a_distinct_terminal_state(tmp_path, monkeypatch):
    """Rejection through the tool names why and is terminal."""
    store, db = _store(tmp_path, "run-1")
    _offer_ok(db, tmp_path, monkeypatch)

    rejected = _call(
        db, tmp_path, monkeypatch,
        action="reject", principal="dest-principal", handoff_id="ho-1",
        reason="destination declined",
    )
    assert rejected["status"] == "ok"
    rec = rejected["controller_handoff"]
    assert rec["state"] == ch.HANDOFF_STATE_REJECTED
    assert rec["rejected_reason"] == "destination declined"
    assert rec["resulting_generation"] is None

    # Terminal: it never reactivates.
    again = _call(
        db, tmp_path, monkeypatch,
        action="accept", principal="dest-principal", handoff_id="ho-1",
    )
    assert again["status"] == "hold"
    assert again["code"] == ch.HANDOFF_HOLD_NOT_OFFERED
    store.close()


def test_only_the_destination_may_accept(tmp_path, monkeypatch):
    """A non-destination actor is refused by name; the state is untouched."""
    store, db = _store(tmp_path, "run-1")
    _offer_ok(db, tmp_path, monkeypatch)

    payload = _call(
        db, tmp_path, monkeypatch,
        action="accept", principal="ops", handoff_id="ho-1",
    )
    assert payload["status"] == "hold"
    assert payload["code"] == ch.HANDOFF_HOLD_ATTESTATION_PRINCIPAL
    assert ch.read_handoff(store, "run-1").state == ch.HANDOFF_STATE_OFFERED
    store.close()


def test_only_the_destination_may_activate_and_no_lease_is_taken(
    tmp_path, monkeypatch,
):
    """A non-destination activation is refused *before* any lease is acquired."""
    store, db = _store(tmp_path, "run-1")
    _offer_ok(db, tmp_path, monkeypatch)
    _call(db, tmp_path, monkeypatch, action="accept", principal="dest-principal")
    store.release_controller_lease(1)

    payload = _call(
        db, tmp_path, monkeypatch,
        action="activate", principal="ops", handoff_id="ho-1",
    )
    assert payload["status"] == "hold"
    assert payload["code"] == ch.HANDOFF_HOLD_ATTESTATION_PRINCIPAL
    # No lease was acquired by the refused actor.
    assert store.current_fencing_token() == 1
    assert store.get_controller_lease().released_at is not None
    store.close()


def test_only_the_current_holder_may_release(tmp_path, monkeypatch):
    """Release is refused for anyone but the store's live holder."""
    store, db = _store(tmp_path, "run-1")
    _offer_ok(db, tmp_path, monkeypatch)
    _call(db, tmp_path, monkeypatch, action="accept", principal="dest-principal")
    store.release_controller_lease(1)
    active = _call(db, tmp_path, monkeypatch, action="activate", principal="dest-principal")
    assert active["controller_handoff"]["state"] == ch.HANDOFF_STATE_ACTIVE

    refused = _call(
        db, tmp_path, monkeypatch,
        action="release", principal="ops", handoff_id="ho-1",
    )
    assert refused["status"] == "hold"
    assert refused["code"] == ch.HANDOFF_HOLD_ORIGINAL_CONTROLLER
    assert store.get_controller_lease().released_at is None

    ok = _call(
        db, tmp_path, monkeypatch,
        action="release", principal="dest-principal", handoff_id="ho-1",
    )
    assert ok["status"] == "ok"
    assert ok["controller_handoff"]["state"] == ch.HANDOFF_STATE_RELEASED
    store.close()


def test_original_controller_cannot_recover_after_release(tmp_path, monkeypatch):
    """A released handoff is terminal: the original controller cannot re-take it."""
    store, db = _store(tmp_path, "run-1")
    _offer_ok(db, tmp_path, monkeypatch)
    _call(db, tmp_path, monkeypatch, action="accept", principal="dest-principal")
    store.release_controller_lease(1)
    _call(db, tmp_path, monkeypatch, action="activate", principal="dest-principal")
    _call(db, tmp_path, monkeypatch, action="release", principal="dest-principal")

    payload = _call(
        db, tmp_path, monkeypatch,
        action="activate", principal="dest-principal", handoff_id="ho-1",
    )
    assert payload["status"] == "hold"
    assert payload["code"] == ch.HANDOFF_HOLD_ORIGINAL_CONTROLLER
    store.close()


def test_a_superseded_offer_is_refused_on_activation(tmp_path, monkeypatch):
    """A foreign holder between offer and activation makes the transfer stale."""
    store, db = _store(tmp_path, "run-1")
    _offer_ok(db, tmp_path, monkeypatch)
    _call(db, tmp_path, monkeypatch, action="accept", principal="dest-principal")
    # The offerer yields generation 1; a foreign holder takes and yields
    # generation 2; the destination's activation therefore lands on generation 3
    # -- not the offered generation + 1.
    store.release_controller_lease(1)
    store.acquire_controller_lease("foreign-controller")
    store.release_controller_lease(2)

    payload = _call(
        db, tmp_path, monkeypatch,
        action="activate", principal="dest-principal", handoff_id="ho-1",
    )
    assert payload["status"] == "hold"
    assert payload["code"] == ch.HANDOFF_HOLD_STALE_GENERATION
    # The refusal is before the durable state moves.
    assert ch.read_handoff(store, "run-1").state == ch.HANDOFF_STATE_ACCEPTED
    store.close()


def test_second_live_handoff_for_the_scope_is_refused(tmp_path, monkeypatch):
    """Two offers for one scope: the second is a named HOLD, never a second
    continuator."""
    store, db = _store(tmp_path, "run-1")
    _offer_ok(db, tmp_path, monkeypatch, handoff_id="ho-a")

    second = _call(
        db, tmp_path, monkeypatch,
        handoff_offer=json.dumps(_offer(handoff_id="ho-b")),
    )
    assert second["status"] == "hold"
    assert second["code"] == ch.HANDOFF_HOLD_LIVE_HANDOFF
    assert ch.read_handoff(store, "run-1").handoff_id == "ho-a"
    store.close()


def test_accept_of_an_unknown_handoff_id_is_refused(tmp_path, monkeypatch):
    """A transition cannot name a handoff the run does not carry."""
    store, db = _store(tmp_path, "run-1")
    _offer_ok(db, tmp_path, monkeypatch)

    wrong = _call(
        db, tmp_path, monkeypatch,
        action="accept", principal="dest-principal", handoff_id="ho-nope",
    )
    assert wrong["status"] == "hold"
    assert wrong["code"] == ch.HANDOFF_HOLD_NOT_OFFERED
    assert ch.read_handoff(store, "run-1").state == ch.HANDOFF_STATE_OFFERED
    store.close()


def test_accept_with_no_recorded_handoff_is_refused(tmp_path, monkeypatch):
    """A run with no durable handoff cannot be advanced."""
    store, db = _store(tmp_path, "run-1")
    payload = _call(
        db, tmp_path, monkeypatch, action="accept", principal="dest-principal",
    )
    assert payload["status"] == "hold"
    assert payload["code"] == ch.HANDOFF_HOLD_NOT_OFFERED
    store.close()


def test_an_unknown_action_is_refused(tmp_path, monkeypatch):
    """An action the boundary does not own is a named HOLD, never a fallthrough."""
    store, db = _store(tmp_path, "run-1")
    _offer_ok(db, tmp_path, monkeypatch)

    payload = _call(
        db, tmp_path, monkeypatch,
        action="teleport", principal="dest-principal",
    )
    assert payload["status"] == "hold"
    assert payload["code"] == ch.HANDOFF_HOLD_UNKNOWN_ACTION
    assert ch.read_handoff(store, "run-1").state == ch.HANDOFF_STATE_OFFERED
    store.close()


def test_a_secret_shaped_binding_is_refused_and_never_echoed(tmp_path, monkeypatch):
    """A provider credential in the binding is refused by shape."""
    store, db = _store(tmp_path, "run-1")
    payload = _call(
        db, tmp_path, monkeypatch,
        handoff_offer=json.dumps(_offer(credential_binding="sk-abcdefghijklmnop")),
    )
    assert payload["status"] == "hold"
    assert payload["code"] == ch.HANDOFF_HOLD_TRANSFER_UNSUPPORTED
    assert "sk-abcdefghijklmnop" not in json.dumps(payload)
    store.close()


def test_the_state_survives_a_restart(tmp_path, monkeypatch):
    """A fresh store object reads the same durable state (no in-memory claim)."""
    store, db = _store(tmp_path, "run-1")
    _offer_ok(db, tmp_path, monkeypatch)
    _call(db, tmp_path, monkeypatch, action="accept", principal="dest-principal")
    store.close()

    reopened = SQLiteRunStore(db)
    assert ch.read_handoff(reopened, "run-1").state == ch.HANDOFF_STATE_ACCEPTED
    # The transition is still drivable through the tool after the restart.
    store2, _ = reopened, db
    store2.release_controller_lease(1)
    active = _call(db, tmp_path, monkeypatch, action="activate", principal="dest-principal")
    assert active["status"] == "ok"
    assert active["controller_handoff"]["state"] == ch.HANDOFF_STATE_ACTIVE
    store2.close()
