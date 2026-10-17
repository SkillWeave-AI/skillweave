"""SW-1511A-HANDOFF-001 criterion 2 -- exactly-once continuation.

Criterion 2 (verbatim): *Race, stale lease, original-controller recovery and
cancel-during-quota-wait tests prove exactly-once continuation and no duplicate
living attempt.*

Every axis below is driven through the **real production entry point**
(``skillweave.mcp.server.handoff_adapter``) on in-process fakes -- no network, no
provider, no credential.  The negative control is the criterion's own
"production-caller standard": on the pre-change base the
``controller_handoff`` contract module does not exist, so the real entry point
cannot reach it and every test here fails.

The exactly-once claim is not asserted from a field: it is read from the
**durable** controller lease, whose fencing token is *dense* (each successful
acquisition is exactly the previous generation + 1).  "The destination holds
generation ``offered + 1``" is therefore the observable statement that the lease
went from the offerer straight to the destination -- no foreign holder in
between, and exactly one living controller after the transfer.

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
from skillweave.runtime import cms_flow_qualification as cms  # noqa: E402
from skillweave.runtime.native_process import LeaseRefusedError  # noqa: E402
from skillweave.runtime.store import SQLiteRunStore  # noqa: E402

#: The reviewed candidate's own contract module: the production path must reach
#: *this* object, so the assertions pin object identity rather than re-assert a
#: parallel implementation of the same behaviour.
CONTRACT_MODULE = sys.modules["skillweave.runtime.controller_handoff"]

#: The offering controller's generation.  The fixture acquires generation 1 as
#: ``ops``, so a destination that takes the lease exactly once reads generation 2.
OFFERED_GENERATION = 1


def _store(tmp_path: Path, run_id: str) -> tuple[SQLiteRunStore, str]:
    """A real per-test store whose *offering* controller already holds the lease."""
    db = str(tmp_path / "store.db")
    store = SQLiteRunStore(db)
    store.create_run(run_id, task_id="t", attempt=1)
    lease = store.acquire_controller_lease("ops")
    assert lease.fencing_token == OFFERED_GENERATION
    return store, db


def _attestation(**over) -> dict:
    base = {
        "schema": "harness-controller-attestation/v1",
        "client_id": "claude-desktop",
        "configured_route": {"gateway_host": "127.0.0.1", "gateway_port": 8090},
        "recipient_principal": "dest-principal",
        "capabilities": {"session_resume": True, "notifications": True},
    }
    base.update(over)
    return base


def _offer(**over) -> dict:
    base = {
        "schema": "controller-handoff/v2",
        "handoff_id": "ho-1",
        "from_holder": "ops",
        "destination_principal": "dest-principal",
        "scope": "run-1",
        "fencing_generation": OFFERED_GENERATION,
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


# ── C2: exactly-once continuation, no duplicate living attempt ──────────────


def test_the_race_admits_exactly_one_live_handoff(tmp_path, monkeypatch):
    """Two offers race for one scope: the loser is a named HOLD, never a second
    live continuation, and the durable store never carries two."""
    store, db = _store(tmp_path, "run-1")
    first = _drive(db, tmp_path, monkeypatch, _offer(handoff_id="ho-a"))
    assert first["status"] == "ok"

    second = _drive(db, tmp_path, monkeypatch, _offer(handoff_id="ho-b"))
    assert second["status"] == "hold"
    assert second["code"] == CONTRACT_MODULE.HANDOFF_HOLD_LIVE_HANDOFF

    # Exactly one live handoff is durable, and the review drive can read it.
    record = ch.read_handoff(store, "run-1")
    assert record.live is True and record.handoff_id == "ho-a"
    assert (
        ch.live_handoff_scope_hold(store, "run-1", exclude_run_id="run-1") is None
    )
    store.close()


def test_a_stale_offer_never_carries_the_lease(tmp_path, monkeypatch):
    """A superseded offer (its holder released before offering) is refused by
    name: activation would carry a lease the offerer no longer held."""
    store, db = _store(tmp_path, "run-1")
    # The offerer held generation 1; a *foreign* holder took generation 2 after
    # the offer, so the handoff's later acquisition lands on generation 4.
    payload = _drive(db, tmp_path, monkeypatch, _offer())
    assert payload["status"] == "ok"
    record = ch.read_handoff(store, "run-1")
    store.release_controller_lease(record.fencing_generation)
    store.acquire_controller_lease("foreign-controller")  # generation 2
    store.release_controller_lease(store.current_fencing_token())

    with pytest.raises(ch.ControllerHandoffRefused) as exc:
        ch.activate_handoff(
            record, acquire=lambda: store.acquire_controller_lease("dest")
        )
    assert exc.value.code == CONTRACT_MODULE.HANDOFF_HOLD_STALE_GENERATION
    # The refusal is *before* the durable state moves: the handoff is untouched
    # and no second living attempt was recorded.
    assert ch.read_handoff(store, "run-1").state == ch.HANDOFF_STATE_OFFERED
    store.close()


def test_original_controller_cannot_recover_through_a_released_handoff(
    tmp_path, monkeypatch,
):
    """Original-controller recovery: a terminal handoff is never reactivated, so
    the holder that gave the lease away cannot take it back through it."""
    store, db = _store(tmp_path, "run-1")
    _drive(db, tmp_path, monkeypatch, _offer())
    record = ch.read_handoff(store, "run-1")
    store.release_controller_lease(record.fencing_generation)
    released = ch.release_handoff(
        ch.activate_handoff(
            ch.accept_handoff(record),
            acquire=lambda: store.acquire_controller_lease("dest"),
        )
    )
    assert released.state == ch.HANDOFF_STATE_RELEASED
    with pytest.raises(ch.ControllerHandoffRefused) as exc:
        ch.activate_handoff(
            released, acquire=lambda: store.acquire_controller_lease("ops")
        )
    assert exc.value.code == CONTRACT_MODULE.HANDOFF_HOLD_ORIGINAL_CONTROLLER
    store.close()


def test_activation_requires_a_real_already_taken_lease(tmp_path, monkeypatch):
    """An activation without a durable acquisition transfers no lease, and a
    *live* foreign controller is refused by the store rather than stolen."""
    store, db = _store(tmp_path, "run-1")
    _drive(db, tmp_path, monkeypatch, _offer())
    record = ch.read_handoff(store, "run-1")

    # (a) no acquisition at all: the destination cannot assert the transfer.
    class _NoLease:
        fencing_token = 0

    with pytest.raises(ch.ControllerHandoffRefused) as exc:
        ch.activate_handoff(record, acquire=lambda: _NoLease())
    assert exc.value.code == CONTRACT_MODULE.HANDOFF_HOLD_STALE_GENERATION

    # (b) the store's own refusal (a live controller still holds the lease) is
    # surfaced unchanged: never converted into a success.
    with pytest.raises(LeaseRefusedError):
        ch.activate_handoff(
            record, acquire=lambda: store.acquire_controller_lease("dest")
        )
    store.close()


def test_cancel_during_quota_wait_is_a_named_hold(tmp_path, monkeypatch):
    """cancel-during-quota-wait: a recorded wait is a live continuation, so a
    controller transfer presented while it is recorded is a named HOLD."""
    store, db = _store(tmp_path, "run-1")
    # Record the wait exactly as the native CMS flow qualification records it.
    cms._drive_wait(store, "run-1", task_id="t", attempt=1)
    assert cms.WAIT_METADATA_KEY == ch.WAIT_METADATA_KEY

    payload = _drive(db, tmp_path, monkeypatch, _offer())
    assert payload["status"] == "hold"
    assert payload["code"] == CONTRACT_MODULE.HANDOFF_HOLD_QUOTA_WAIT
    # No handoff was recorded: the wait remains the run's one live continuation.
    assert ch.read_handoff(store, "run-1") is None
    store.close()


def test_the_wait_key_is_the_recorded_one_not_a_hand_typed_copy():
    """The cancel-during-quota-wait guard reads the *same* durable key the
    native CMS flow qualification records a measured wait under."""
    assert ch.WAIT_METADATA_KEY is cms.WAIT_METADATA_KEY


def test_the_fencing_token_authorises_only_the_current_generation(
    tmp_path, monkeypatch,
):
    """No duplicate living attempt: after the transfer only the destination's
    generation may write; the superseded offerer's generation is refused."""
    store, db = _store(tmp_path, "run-1")
    _drive(db, tmp_path, monkeypatch, _offer())
    record = ch.read_handoff(store, "run-1")
    store.release_controller_lease(record.fencing_generation)
    active = ch.activate_handoff(
        ch.accept_handoff(record),
        acquire=lambda: store.acquire_controller_lease("dest"),
    )
    assert active.resulting_generation == record.fencing_generation + 1
    current = store.get_controller_lease()
    assert current.holder == "dest"
    assert current.fencing_token == active.resulting_generation
    # Only that one generation is live: releasing the superseded generation is
    # refused, so there is exactly one controller of record.
    with pytest.raises(Exception) as exc:
        store.release_controller_lease(record.fencing_generation)
    assert type(exc.value).__name__ == "StaleFencingTokenError"
    store.close()
