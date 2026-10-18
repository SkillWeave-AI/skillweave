"""SW-1511A-HANDOFF-001 accepted limitation -- counter monotonicity (GATE-001).

The review-002 report accepted one limitation: "'counters decrease between
observations' is NOT implemented (no stored prior count)", with the smallest
honest fix named as *store the last observed gateway_request_count per run in
durable metadata and refuse a subsequent handoff whose count is lower than the
prior observation*.  This file is the adversarial proof that the **real
admission boundary** (``skillweave.mcp.server.handoff_adapter`` ->
``_admit_controller_handoff_offer`` -> ``_verify_handoff_evidence``) now does
exactly that -- on in-process fakes, no network, no provider, no credential.

On the pre-repair head every "lower count" case below is admitted as ``ok``
(no prior count is stored at all), so each refuses-on-regression test fails
there; here the same real tool call returns the named HOLD.  The admitted cases
(first, equal, higher) and the independence cases (other run/route) pin the
*additive* half: the fix never refuses an honest observation.

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

ROUTE = "faigate/deepseek-v4"


def _store(tmp_path: Path, run_id: str) -> tuple[SQLiteRunStore, str]:
    db = str(tmp_path / "store.db")
    store = SQLiteRunStore(db)
    store.create_run(run_id, task_id="t", attempt=1)
    store.acquire_controller_lease("ops")
    return store, db


def _attestation(count, route: str = ROUTE, **over) -> dict:
    evidence = {"gateway_request_count": count, "exit_code": 0}
    if route:
        evidence["gateway_route"] = route
    base = {
        "schema": "harness-controller-attestation/v1",
        "client_id": "claude-desktop",
        "configured_route": {
            "gateway_host": "127.0.0.1",
            "gateway_port": 8090,
            "route": route or "",
            "credential_binding": "gateway://127.0.0.1:8090",
        },
        "recipient_principal": "dest-principal",
        "capabilities": {"session_resume": True, "notifications": True},
        "evidence": evidence,
    }
    base.update(over)
    return base


def _offer(attestation, run_id: str = "run-1", handoff_id: str = "ho-1", **over) -> dict:
    base = {
        "schema": "controller-handoff/v2",
        "handoff_id": handoff_id,
        "from_holder": "ops",
        "destination_principal": attestation["recipient_principal"],
        "scope": run_id,
        "fencing_generation": 1,
        "credential_binding": "gateway://127.0.0.1:8090",
        "destination_attestation": attestation,
    }
    base.update(over)
    return base


def _drive(db, tmp_path, monkeypatch, offer, run_id: str = "run-1") -> dict:
    monkeypatch.setenv(mcp_server.MAILBOX_PATH_ENV, str(tmp_path / "mailbox"))
    monkeypatch.setattr(mcp_server, "_AUTHORITY", None)
    return json.loads(
        mcp_server.handoff_adapter(
            client_id="claude-desktop",
            recipients="ops",
            run_id=run_id,
            db_path=db,
            session_token="",
            handoff_offer=json.dumps(offer),
        )
    )


def _release(db: str, run_id: str = "run-1") -> None:
    """Terminally release the run's recorded handoff.

    A *second* offer for one run is refused by the pre-existing exactly-once
    live-handoff guard (``HANDOFF_HOLD_LIVE_HANDOFF``), which is untouched here.
    Re-observing an already-carried series therefore means: the prior handoff
    reached a terminal state first (a release), exactly as the state machine
    prescribes.  The recorded counter observation is deliberately *not* cleared
    by the release, so the next observation is measured against it.
    """
    store = SQLiteRunStore(db)
    try:
        record = ch.read_handoff(store, run_id)
        if record is not None and record.state not in ch.HANDOFF_TERMINAL_STATES:
            # The transition itself is the module's real one; it is persisted
            # through the store's own metadata seam because ``record_handoff`` is
            # idempotent at the same handoff_id (a repeated observation is not a
            # state change) -- unchanged behaviour, only exercised here.
            terminated = ch.reject_handoff(record, reason="superseded observation")
            store.update_metadata(
                run_id, **{ch.HANDOFF_METADATA_KEY: terminated.to_dict()}
            )
    finally:
        store.close()


# ── the refusals (each fails on the pre-repair head) ────────────────────────


def test_a_lower_count_than_the_last_observation_is_a_named_hold(
    tmp_path, monkeypatch,
):
    """A later offer for the same run/route may not report *fewer* requests."""
    store, db = _store(tmp_path, "run-1")
    first = _drive(db, tmp_path, monkeypatch, _offer(_attestation(37)))
    assert first["status"] == "ok"
    store.close()
    _release(db)

    payload = _drive(
        db, tmp_path, monkeypatch, _offer(_attestation(36), handoff_id="ho-2")
    )
    assert payload["status"] == "hold"
    assert payload["code"] == ch.HANDOFF_HOLD_COUNTER_REGRESSION
    assert payload["prior_gateway_request_count"] == 37
    assert payload["observed_gateway_request_count"] == 36


def test_a_count_far_below_the_last_observation_is_a_named_hold(
    tmp_path, monkeypatch,
):
    """The refusal is a real regression check, not a one-off boundary case."""
    store, db = _store(tmp_path, "run-1")
    assert _drive(db, tmp_path, monkeypatch, _offer(_attestation(100)))["status"] == "ok"
    store.close()
    _release(db)

    payload = _drive(db, tmp_path, monkeypatch, _offer(_attestation(1), handoff_id="ho-2"))
    assert payload["status"] == "hold"
    assert payload["code"] == ch.HANDOFF_HOLD_COUNTER_REGRESSION


def test_the_admitted_observation_is_durable_in_the_run_metadata(tmp_path):
    """The stored value lives in the run's durable metadata, not in memory."""
    store, db = _store(tmp_path, "run-1")
    ch.record_gateway_counter_observation(store, "run-1", ROUTE, 37)
    run = store.get_run("run-1")
    blob = run.metadata[ch.HANDOFF_COUNTER_METADATA_KEY]
    assert blob["schema"] == ch.HANDOFF_COUNTER_SCHEMA
    assert blob["routes"][ROUTE] == 37
    assert ch.read_gateway_counter_observation(store, "run-1", ROUTE) == 37
    store.close()


def test_the_stored_observation_survives_a_process_restart(tmp_path, monkeypatch):
    """A *fresh* store object over the same file still sees the prior count."""
    store, db = _store(tmp_path, "run-1")
    assert _drive(db, tmp_path, monkeypatch, _offer(_attestation(37)))["status"] == "ok"
    store.close()  # the recording process goes away

    reopened = SQLiteRunStore(db)
    try:
        assert ch.read_gateway_counter_observation(reopened, "run-1", ROUTE) == 37
    finally:
        reopened.close()
    _release(db)

    payload = _drive(db, tmp_path, monkeypatch, _offer(_attestation(20), handoff_id="ho-2"))
    assert payload["status"] == "hold"
    assert payload["code"] == ch.HANDOFF_HOLD_COUNTER_REGRESSION


def test_an_unreadable_stored_value_fails_closed(tmp_path, monkeypatch):
    """A present-but-unreadable prior observation is refused, never ignored."""
    store, db = _store(tmp_path, "run-1")
    # A durable blob under the counter key that is not a tagged observation: the
    # server cannot know whether a prior count exists, so it must not guess.
    store.update_metadata("run-1", **{ch.HANDOFF_COUNTER_METADATA_KEY: "not-a-record"})
    store.close()

    payload = _drive(db, tmp_path, monkeypatch, _offer(_attestation(999)))
    assert payload["status"] == "hold"
    assert payload["code"] == ch.HANDOFF_HOLD_COUNTER_REGRESSION


def test_an_unreadable_count_inside_a_tagged_blob_fails_closed(tmp_path, monkeypatch):
    """A tagged blob whose value is not a positive integer is unreadable too."""
    store, db = _store(tmp_path, "run-1")
    store.update_metadata(
        "run-1",
        **{
            ch.HANDOFF_COUNTER_METADATA_KEY: {
                "schema": ch.HANDOFF_COUNTER_SCHEMA,
                "routes": {ROUTE: "many"},
            }
        },
    )
    store.close()

    payload = _drive(db, tmp_path, monkeypatch, _offer(_attestation(5)))
    assert payload["status"] == "hold"
    assert payload["code"] == ch.HANDOFF_HOLD_COUNTER_REGRESSION


def test_the_stored_observation_never_carries_a_secret(tmp_path):
    """Only the integer and its route key are stored -- no evidence passthrough."""
    store, db = _store(tmp_path, "run-1")
    ch.record_gateway_counter_observation(store, "run-1", ROUTE, 37)
    blob = store.get_run("run-1").metadata[ch.HANDOFF_COUNTER_METADATA_KEY]
    assert blob == {"schema": ch.HANDOFF_COUNTER_SCHEMA, "routes": {ROUTE: 37}}
    assert "sk-" not in json.dumps(blob)
    store.close()


# ── the additive half: honest observations are still admitted ───────────────


def test_a_first_observation_is_admitted_as_before(tmp_path, monkeypatch):
    """With no prior observation the offer is admitted exactly as before."""
    store, db = _store(tmp_path, "run-1")
    payload = _drive(db, tmp_path, monkeypatch, _offer(_attestation(37)))
    assert payload["status"] == "ok"
    assert ch.read_gateway_counter_observation(store, "run-1", ROUTE) == 37
    store.close()


def test_an_equal_count_is_admitted_and_keeps_the_stored_value(tmp_path, monkeypatch):
    """A repeated observation is not a regression."""
    store, db = _store(tmp_path, "run-1")
    assert _drive(db, tmp_path, monkeypatch, _offer(_attestation(37)))["status"] == "ok"
    store.close()
    _release(db)

    payload = _drive(db, tmp_path, monkeypatch, _offer(_attestation(37), handoff_id="ho-2"))
    assert payload["status"] == "ok"
    reopened = SQLiteRunStore(db)
    try:
        assert ch.read_gateway_counter_observation(reopened, "run-1", ROUTE) == 37
    finally:
        reopened.close()


def test_a_higher_count_is_admitted_and_advances_the_stored_value(tmp_path, monkeypatch):
    """An advancing counter is admitted and becomes the new observation."""
    store, db = _store(tmp_path, "run-1")
    assert _drive(db, tmp_path, monkeypatch, _offer(_attestation(37)))["status"] == "ok"
    store.close()
    _release(db)

    payload = _drive(db, tmp_path, monkeypatch, _offer(_attestation(52), handoff_id="ho-2"))
    assert payload["status"] == "ok"
    reopened = SQLiteRunStore(db)
    try:
        assert ch.read_gateway_counter_observation(reopened, "run-1", ROUTE) == 52
    finally:
        reopened.close()


# ── independence: another run/route never interferes ────────────────────────


def test_a_different_route_has_its_own_counter_series(tmp_path, monkeypatch):
    """A lower count on a *different* route is not a regression for this one."""
    store, db = _store(tmp_path, "run-1")
    assert _drive(db, tmp_path, monkeypatch, _offer(_attestation(100)))["status"] == "ok"
    store.close()
    _release(db)
    payload = _drive(
        db, tmp_path, monkeypatch,
        _offer(_attestation(1, route="faigate/other"), handoff_id="ho-2"),
    )
    assert payload["status"] == "ok"
    reopened = SQLiteRunStore(db)
    try:
        assert ch.read_gateway_counter_observation(reopened, "run-1", ROUTE) == 100
        assert ch.read_gateway_counter_observation(reopened, "run-1", "faigate/other") == 1
    finally:
        reopened.close()


def test_a_different_run_has_its_own_counter_series(tmp_path, monkeypatch):
    """A lower count on another *run* is not a regression for this run."""
    store, db = _store(tmp_path, "run-1")
    store.create_run("run-2", task_id="t", attempt=1)
    assert _drive(db, tmp_path, monkeypatch, _offer(_attestation(100)))["status"] == "ok"
    payload = _drive(
        db, tmp_path, monkeypatch,
        _offer(_attestation(1), run_id="run-2"), run_id="run-2",
    )
    assert payload["status"] == "ok"
    assert ch.read_gateway_counter_observation(store, "run-1", ROUTE) == 100
    assert ch.read_gateway_counter_observation(store, "run-2", ROUTE) == 1
    store.close()
