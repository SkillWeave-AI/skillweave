"""SW-SCOPE-010 unit C, criterion 3: irreversible CMS *publication*
authorization through the controller.

The single production caller under test is the CLI entry point
:func:`skillweave.cli.native_controller.main`, reached through its additive
``--authorize-cms-publication`` branch.  Publication authorization requires
facts that are *read from durable state*, never from the caller's hope:

* the durable, **server-recorded** independent review must bind the exact
  subject revision and artifact digest -- a caller-constructed review, or a
  reference to a review that was never recorded, is refused by name;
* the publishing attempt's **producer** must match the identity the controller
  derived from the owned attempt's durable record;
* a **designated human authorization** must be presented, distinct from the
  producer and bound to the exact revision/digest, and the governing autonomy
  policy must approve the irreversible action;
* every declared **credential** reference must be observed available;
* the publication endpoint must be permitted to **egress** under the effective
  policy (egress is default-deny).

Each of those refusals fails closed with a *named* hold (following the existing
holds in the module) and exit code ``EXIT_REFUSED``; nothing is published and
no remote CMS is contacted.  One adversarial test pins each refusal, plus a
positive control proving the five gates *can* pass -- so the refusals are not
vacuous.

Every test drives the *real* entry point over a real ``SQLiteRunStore`` on a
temp path.  The durable transaction is seeded through the native controller
seam; the durable review is recorded through the real MCP ``review_action`` tool
under a server-issued session.  **No provider, gateway, network or CMS call is
made**: the egress/credential gate is a pure decision over the operator's
declared admission environment.  The process-wide state the controller touches
(``SIGTERM``/``SIGINT`` handlers, the MCP authority/ready-session) is restored
in fixtures so the tests are order-independent.

On the base ``f959627`` the ``--authorize-cms-publication`` flag does not exist,
so ``main`` returns ``EXIT_INVALID_INPUT`` and every test here fails -- the red
proofs for this criterion.
"""

from __future__ import annotations

import json
import signal

import pytest

from skillweave.cli import native_controller as nc
from skillweave.dispatch.work_contract import ContentSubject
from skillweave.mcp import server as mcp_server
from skillweave.runtime import process_identity
from skillweave.runtime.authority import MCPPrincipalAuthority
from skillweave.runtime.native_process import (
    NativeProcessService,
    OwnerSandboxDeclaration,
    RawContentAdapter,
    STEP_READY_DISPATCH,
    SandboxBinding,
)
from skillweave.runtime.store import SQLiteRunStore

_RUN_ID = "run-1"
_TASK_ID = "task-cms"
_ATTEMPT = 1
_REVISION = "rev-1"
_DIGEST = "sha256:x"
_CRITERIA = "cms-c2"
_BASELINE = {"title": "old", "body": "hello"}
_SANDBOX = SandboxBinding(target="sandbox/ch")
_OWNER_SANDBOX = OwnerSandboxDeclaration(targets=("sandbox/ch",))

#: The operator's declared admission facts and governing autonomy policy.  Named
#: as literals (the module constants they mirror are new on this candidate) so
#: the *setup* of every test succeeds on the base and only the assertion fails.
_ENV_ADMISSION = "SKILLWEAVE_CMS_PUBLICATION_ADMISSION"
_ENV_POLICY = "SKILLWEAVE_CMS_PUBLICATION_POLICY"


# ── Fixtures (restore process-wide state) ───────────────────────────────────


@pytest.fixture
def restore_signal_handlers():
    """Snapshot and restore the process-wide SIGTERM/SIGINT handlers.

    The controller's lease guard installs handlers for the duration of a drive;
    restoring them afterwards keeps a later test (or the runner) from inheriting
    a displaced handler regardless of test order.
    """
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)


@pytest.fixture
def store(tmp_path):
    """A real ``SQLiteRunStore`` on a temp path, closed on teardown."""
    s = SQLiteRunStore(str(tmp_path / "store.db"))
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def authority(tmp_path, monkeypatch):
    """A live MCP authority + durable mailbox, so ``review_action`` records.

    The bound authority and the ready-session token are process-wide; both are
    snapshotted and restored so a later test never inherits a displaced
    authority.
    """
    mailbox = tmp_path / "agent-mailbox.jsonl"
    monkeypatch.setenv(mcp_server.MAILBOX_PATH_ENV, str(mailbox))
    auth = MCPPrincipalAuthority()
    previous_authority = mcp_server._AUTHORITY
    previous_ready = mcp_server._READY_SESSION_TOKEN
    mcp_server.bind_authority(auth)
    mcp_server._READY_SESSION_TOKEN = None
    try:
        yield auth, str(mailbox)
    finally:
        mcp_server._AUTHORITY = previous_authority
        mcp_server._READY_SESSION_TOKEN = previous_ready


# ── Helpers ─────────────────────────────────────────────────────────────────


def _live_process_identity() -> dict:
    """A *real* durable process identity captured from this test process."""
    captured = process_identity.capture(os_pid())
    assert captured is not None, "could not capture the test process identity"
    return captured.to_dict()


def os_pid() -> int:
    import os

    return os.getpid()


def _derived_producer(identity: dict) -> str:
    """The producer identity the controller derives from an owned attempt."""
    record = process_identity.ProcessIdentity.from_dict(identity)
    assert record is not None
    return f"attempt:{record.creation_token}"


def _seed_restored_transaction(
    store: SQLiteRunStore,
    *,
    holder: str = "seeder",
    producer_override: object = None,
):
    """Drive a managed transaction to ``restored`` and release the lease.

    Returns ``(txn, producer)`` where ``producer`` is the identity the
    controller derived from the owned attempt -- the value a caller must
    declare.  ``producer_override`` (when given) replaces the transaction's
    recorded ``producer_identity`` durably, modelling a forged/unverified
    producer: the persisted record is tampered *before* the CLI loads it.
    """
    service = NativeProcessService(store, holder=holder)
    service.acquire(ttl_seconds=300)
    try:
        identity = _live_process_identity()
        intent, _ = service.dispatch(
            run_id=_RUN_ID, task_id=_TASK_ID, attempt=_ATTEMPT,
            step=STEP_READY_DISPATCH, process_identity=identity,
        )
        producer = _derived_producer(identity)
        subject = ContentSubject(channel="ch", content_id="e1", revision=_REVISION)
        txn = service.open_cms_transaction(
            intent.intent_id, subject, RawContentAdapter(),
            baseline=_BASELINE, producer_identity=producer,
            binding=_SANDBOX, owner_sandbox=_OWNER_SANDBOX,
        )
        change = {"title": "new", "body": "hello"}
        txn = service.draft_cms_transaction(txn, change=change)
        txn = service.apply_cms_transaction(txn, change=change)
        txn, blocker = service.restore_cms_transaction(txn)
        assert blocker == "", blocker
    finally:
        service.release()

    if producer_override is not None:
        from dataclasses import replace

        tampered = replace(txn, producer_identity=producer_override)
        store.save_cms_transaction(tampered, upsert=True)
        txn = tampered
    return txn, producer


def _record_durable_review(
    authority,
    *,
    message_id: str = "review-1",
    role: str = "reviewer",
    client_id: str = "rev",
    verdict: str = "accepted",
    revision: str = _REVISION,
    digest: str = _DIGEST,
    criteria: str = _CRITERIA,
) -> str:
    """Record an independent review through the real MCP tool.

    The role is read from the server-issued session, never the wire, and the
    review's own facts (verdict, revision, digest, criteria) are stamped by the
    server.  Returns the durable record's *server-recorded* principal.
    """
    auth, _mailbox = authority
    token = auth.issue_session(
        role=role, client_id=client_id, transport="http", run_ids=[_RUN_ID],
    )
    written = json.loads(
        mcp_server.review_action(
            action="write", recipient=client_id, body=criteria,
            run_id=_RUN_ID, task_id=_TASK_ID, attempt=_ATTEMPT,
            message_id=message_id, session_token=token,
            verdict=verdict, subject_revision=revision, artifact_digest=digest,
            criteria=criteria,
        )
    )
    assert written["status"] == "ok", written
    return written["message"]["principal"]


def _argv(
    store: SQLiteRunStore,
    txn,
    *,
    message_id: str = "review-1",
    mailbox: str = "",
    authorizer: str = "owner-1",
    binding: str = "",
    digest: str = _DIGEST,
) -> list[str]:
    """The real ``native-controller`` argv for one publication authorization."""
    argv = [
        "--run-id", _RUN_ID,
        "--task-id", _TASK_ID,
        "--attempt", str(_ATTEMPT),
        "--db-path", store.db_path,
        "--authorize-cms-publication", "1",
        "--cms-txn-id", txn.txn_id,
        "--cms-artifact-digest", digest,
        "--cms-review-message-id", message_id,
    ]
    if mailbox:
        argv += ["--cms-review-mailbox", mailbox]
    if authorizer:
        argv += ["--cms-publication-authorizer", authorizer]
    if binding:
        argv += ["--cms-binding", binding]
    return argv


# ── 1. No durable review -> refused by name ─────────────────────────────────


def test_undurable_review_is_refused_by_name(
    store, capsys, authority, restore_signal_handlers
):
    """A reference to a review that was never recorded is refused fail-closed.

    Adversarial on the base ``f959627``: the flag does not exist there, so this
    test fails.  Here the durable transaction exists but no review was ever
    recorded under the referenced id, so the publication presents no durable,
    server-recorded review -- a caller-constructed one is never accepted.
    """
    txn, _producer = _seed_restored_transaction(store)
    _, mailbox = authority

    code = nc.main(
        _argv(store, txn, message_id="review-never-recorded", mailbox=mailbox),
        store=store,
    )
    err = capsys.readouterr().err

    assert code == nc.EXIT_REFUSED
    assert nc.HOLD_CMS_PUBLICATION_NO_DURABLE_REVIEW in err
    # Nothing was published: the durable transaction is untouched.
    assert store.get_cms_transaction(_RUN_ID, txn.txn_id)["stage"] == "restored"


# ── 2. Unverified producer -> refused by name ───────────────────────────────


def test_unverified_producer_is_refused_by_name(
    store, capsys, authority, restore_signal_handlers
):
    """A transaction whose producer cannot be verified is refused by name.

    The durable transaction's recorded producer identity is blanked, so the
    publishing attempt's producer is unknown.  Even with a fully valid durable
    review, the publication is refused at the producer check -- a producer can
    never authorize its own irreversible publication.
    """
    txn, _producer = _seed_restored_transaction(store, producer_override="")
    _record_durable_review(authority)
    _, mailbox = authority

    code = nc.main(_argv(store, txn, mailbox=mailbox), store=store)
    err = capsys.readouterr().err

    assert code == nc.EXIT_REFUSED
    assert nc.HOLD_CMS_PUBLICATION_PRODUCER_UNVERIFIED in err


# ── 3. No designated human authorization -> refused by name ─────────────────


def test_missing_designated_authorization_is_refused_by_name(
    store, capsys, authority, restore_signal_handlers
):
    """Publication without a designated human authorization is refused by name.

    The transaction and the durable review are both valid, but no authorizer is
    presented at all, so the irreversible publication has no designated human
    authorization bound to the exact revision/digest.
    """
    txn, _producer = _seed_restored_transaction(store)
    _record_durable_review(authority)
    _, mailbox = authority

    code = nc.main(_argv(store, txn, mailbox=mailbox, authorizer=""), store=store)
    err = capsys.readouterr().err

    assert code == nc.EXIT_REFUSED
    assert nc.HOLD_CMS_PUBLICATION_UNAUTHORIZED in err


# ── 4. Missing credential -> refused by name ────────────────────────────────


def test_missing_credential_is_refused_by_name(
    store, capsys, authority, monkeypatch, restore_signal_handlers
):
    """A declared credential that is not observed available is refused by name.

    The declared admission facts name a credential reference with no observed
    availability, so the publication is refused at the credential gate -- never
    simulated, and no unprovisioned publication is attempted.  The gate is a
    pure decision: no network is contacted.
    """
    txn, _producer = _seed_restored_transaction(store)
    _record_durable_review(authority)
    _, mailbox = authority
    monkeypatch.setenv(
        _ENV_ADMISSION, json.dumps({"credential_refs": ["cms-token"]})
    )

    code = nc.main(_argv(store, txn, mailbox=mailbox), store=store)
    err = capsys.readouterr().err

    assert code == nc.EXIT_REFUSED
    assert nc.HOLD_CMS_PUBLICATION_CREDENTIAL_MISSING in err


# ── 5. Unpermitted egress -> refused by name ────────────────────────────────


def test_unpermitted_egress_is_refused_by_name(
    store, capsys, authority, monkeypatch, restore_signal_handlers
):
    """An endpoint that cannot egress under the policy is refused by name.

    The credential is observed available but egress is default-deny, so the
    declared remote endpoint cannot be reached -- the publication is refused
    before any remote CMS is contacted.  The gate is a pure decision.
    """
    txn, _producer = _seed_restored_transaction(store)
    _record_durable_review(authority)
    _, mailbox = authority
    monkeypatch.setenv(
        _ENV_ADMISSION,
        json.dumps(
            {
                "endpoint": "https://cms.example/publish",
                "allow_network": False,
                "credential_refs": ["cms-token"],
                "credential_availability": {"cms-token": "available"},
            }
        ),
    )

    code = nc.main(
        _argv(store, txn, mailbox=mailbox, binding="sandbox/ch"), store=store
    )
    err = capsys.readouterr().err

    assert code == nc.EXIT_REFUSED
    assert nc.HOLD_CMS_PUBLICATION_EGRESS_UNPERMITTED in err


# ── Control: the five gates can pass ────────────────────────────────────────


def test_fully_provisioned_publication_is_authorized(
    store, capsys, authority, monkeypatch, restore_signal_handlers
):
    """A fully provisioned publication is *authorized* (a decision, not a hold).

    The durable review, the verified producer, the designated human
    authorization and the owner autonomy policy are all present, so the
    irreversible publication is approved.  This proves the named refusals above
    are not vacuous: the gates admit when every required fact is durable and
    provisioned.  No remote CMS is contacted -- the decision is returned.
    """
    txn, producer = _seed_restored_transaction(store)
    _record_durable_review(authority)
    _, mailbox = authority
    monkeypatch.setenv(_ENV_POLICY, "owner")

    code = nc.main(_argv(store, txn, mailbox=mailbox), store=store)
    out = capsys.readouterr().out

    assert code == nc.EXIT_OK
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["approved"] is True
    assert payload["txn_id"] == txn.txn_id
    assert payload["producer_identity"] == producer
    assert payload["subject_revision"] == _REVISION
    assert payload["artifact_digest"] == _DIGEST
