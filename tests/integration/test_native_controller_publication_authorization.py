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
* the **designated human authorization** must be durable *owner-authored* state
  -- ``owner-publication-approval.yaml`` in the same owner state directory as
  the sandbox declaration -- loaded exactly like the sandbox declaration (pinned
  file name, SHA-256 of the bytes read recorded with the receipt, and a change
  between load and apply refused).  It is **never** supplied through any tool,
  MCP argument or CLI argument: the entity that drives the publication can
  therefore never name -- and so never forge -- the human who authorized it.
  The approver must be bound to the exact revision/digest and be distinct from
  the producer, and the governing autonomy policy must approve the irreversible
  action;
* every declared **credential** reference must be observed available;
* the publication endpoint must be permitted to **egress** under the effective
  policy (egress is default-deny).

Each of those refusals fails closed with a *named* hold (following the existing
holds in the module) and exit code ``EXIT_REFUSED``; nothing is published and
no remote CMS is contacted.  One adversarial test pins each refusal, plus the
``--cms-publication-authorizer`` forging vector removed by this repair, plus a
positive control proving the gates *can* pass -- so the refusals are not
vacuous.

Every test drives the *real* entry point over a real ``SQLiteRunStore`` on a
temp path.  The durable transaction is seeded through the native controller
seam; the durable review is recorded through the real MCP ``review_action`` tool
under a server-issued session.  **No provider, gateway, network or CMS call is
made**: the egress/credential gate is a pure decision over the operator's
declared admission environment.  The process-wide state the controller touches
(``SIGTERM``/``SIGINT`` handlers, the MCP authority/ready-session,
``SKILLWEAVE_OWNER_STATE_DIR``) is restored in fixtures so the tests are
order-independent.

On the base ``f959627`` the ``--authorize-cms-publication`` flag does not exist.
On the criterion-3 commit ``111289c`` the flag exists but the designated human
authorization is a *caller-supplied* ``--cms-publication-authorizer`` argument
(the forging vector this repair removes): every test that expects the owner
approval seam, the removed argument or the four owner-approval holds fails there
-- the red proofs for this repair.
"""

from __future__ import annotations

import json
import os
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
_ENV_OWNER_STATE = "SKILLWEAVE_OWNER_STATE_DIR"


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
def restore_owner_state_env():
    """Snapshot and restore ``SKILLWEAVE_OWNER_STATE_DIR`` around a test."""
    saved = os.environ.get(_ENV_OWNER_STATE)
    try:
        yield _ENV_OWNER_STATE
    finally:
        if saved is None:
            os.environ.pop(_ENV_OWNER_STATE, None)
        else:
            os.environ[_ENV_OWNER_STATE] = saved


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


@pytest.fixture
def owner_state(tmp_path, restore_owner_state_env):
    """A temp owner-state directory, configured as the caller's own state.

    The production caller resolves the owner-state directory itself from
    ``SKILLWEAVE_OWNER_STATE_DIR``; the test only points it at a temp directory
    and writes the owner's approval file into it.  The test never passes the
    approval -- only the directory the *caller* configured.
    """
    directory = tmp_path / "owner-state"
    directory.mkdir(parents=True)
    os.environ[restore_owner_state_env] = os.fspath(directory)
    return directory


# ── Helpers ─────────────────────────────────────────────────────────────────


def _live_process_identity() -> dict:
    """A *real* durable process identity captured from this test process."""
    captured = process_identity.capture(os_pid())
    assert captured is not None, "could not capture the test process identity"
    return captured.to_dict()


def os_pid() -> int:
    import os as _os

    return _os.getpid()


def _derived_producer(identity: dict) -> str:
    """The producer identity the controller derives from an owned attempt."""
    record = process_identity.ProcessIdentity.from_dict(identity)
    assert record is not None
    return f"attempt:{record.creation_token}"


def _approval_bytes(
    *,
    approver: str = "owner-1",
    revision: str = _REVISION,
    digest: str = _DIGEST,
    approved_at: str = "2026-10-16T00:00:00Z",
    production: object = False,
) -> bytes:
    """The owner-authored publication approval file the caller must not supply.

    Mirrors the form the sandbox declaration takes: a pinned file name in the
    owner state directory, naming the ``approver`` and binding the exact
    ``transaction`` revision/digest, with an explicit ``production: false``.
    """
    return (
        "# Owner-authored publication approval. Only the owner writes this file.\n"
        f"approver: {approver}\n"
        "transaction:\n"
        f"  revision: {revision}\n"
        f"  digest: {digest}\n"
        f"approved_at: '{approved_at}'\n"
        f"production: {'false' if production is False else str(production).lower()}\n"
    ).encode("utf-8")


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
    binding: str = "",
    digest: str = _DIGEST,
) -> list[str]:
    """The real ``native-controller`` argv for one publication authorization.

    There is deliberately **no** authorizer argument: the designated human
    authorization is owner-authored durable state, so the caller that drives the
    publication cannot name it.
    """
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
    if binding:
        argv += ["--cms-binding", binding]
    return argv


# ── 1. No durable review -> refused by name ─────────────────────────────────


def test_undurable_review_is_refused_by_name(
    store, capsys, authority, owner_state, restore_signal_handlers
):
    """A reference to a review that was never recorded is refused fail-closed.

    Adversarial on the base ``f959627``: the flag does not exist there, so this
    test fails.  Here the durable transaction exists and a valid owner approval
    is in place, but no review was ever recorded under the referenced id, so the
    publication presents no durable, server-recorded review -- a
    caller-constructed one is never accepted.
    """
    txn, _producer = _seed_restored_transaction(store)
    _, mailbox = authority
    (owner_state / nc.OWNER_PUBLICATION_APPROVAL_FILENAME).write_bytes(
        _approval_bytes()
    )

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
    store, capsys, authority, owner_state, restore_signal_handlers
):
    """A transaction whose producer cannot be verified is refused by name.

    The durable transaction's recorded producer identity is blanked, so the
    publishing attempt's producer is unknown.  Even with a fully valid durable
    review and owner approval, the publication is refused at the producer check
    -- a producer can never authorize its own irreversible publication.
    """
    txn, _producer = _seed_restored_transaction(store, producer_override="")
    _record_durable_review(authority)
    _, mailbox = authority
    (owner_state / nc.OWNER_PUBLICATION_APPROVAL_FILENAME).write_bytes(
        _approval_bytes()
    )

    code = nc.main(_argv(store, txn, mailbox=mailbox), store=store)
    err = capsys.readouterr().err

    assert code == nc.EXIT_REFUSED
    assert nc.HOLD_CMS_PUBLICATION_PRODUCER_UNVERIFIED in err


# ── 3. No designated human authorization -> refused by name ─────────────────


def test_missing_designated_authorization_is_refused_by_name(
    store, capsys, authority, owner_state, restore_signal_handlers
):
    """Publication without an owner-authored approval is refused by name.

    The transaction and the durable review are both valid, but the owner state
    directory holds no ``owner-publication-approval.yaml`` at all: the
    irreversible publication has no designated human authorization bound to the
    exact revision/digest, and the caller cannot present one as an argument.
    """
    txn, _producer = _seed_restored_transaction(store)
    _record_durable_review(authority)
    _, mailbox = authority
    # The owner state directory is configured but holds no approval file.

    code = nc.main(_argv(store, txn, mailbox=mailbox), store=store)
    err = capsys.readouterr().err

    assert code == nc.EXIT_REFUSED
    assert nc.HOLD_CMS_PUBLICATION_NO_OWNER_APPROVAL in err


# ── 4. Missing credential -> refused by name ────────────────────────────────


def test_missing_credential_is_refused_by_name(
    store, capsys, authority, owner_state, monkeypatch, restore_signal_handlers
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
    (owner_state / nc.OWNER_PUBLICATION_APPROVAL_FILENAME).write_bytes(
        _approval_bytes()
    )
    monkeypatch.setenv(
        _ENV_ADMISSION, json.dumps({"credential_refs": ["cms-token"]})
    )

    code = nc.main(_argv(store, txn, mailbox=mailbox), store=store)
    err = capsys.readouterr().err

    assert code == nc.EXIT_REFUSED
    assert nc.HOLD_CMS_PUBLICATION_CREDENTIAL_MISSING in err


# ── 5. Unpermitted egress -> refused by name ────────────────────────────────


def test_unpermitted_egress_is_refused_by_name(
    store, capsys, authority, owner_state, monkeypatch, restore_signal_handlers
):
    """An endpoint that cannot egress under the policy is refused by name.

    The credential is observed available but egress is default-deny, so the
    declared remote endpoint cannot be reached -- the publication is refused
    before any remote CMS is contacted.  The gate is a pure decision.
    """
    txn, _producer = _seed_restored_transaction(store)
    _record_durable_review(authority)
    _, mailbox = authority
    (owner_state / nc.OWNER_PUBLICATION_APPROVAL_FILENAME).write_bytes(
        _approval_bytes()
    )
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


# ── 6. A forged CLI authorizer can no longer authorize -- refused ────────────


def test_caller_supplied_authorizer_argument_is_refused(
    store, capsys, authority, owner_state, restore_signal_handlers
):
    """The ``--cms-publication-authorizer`` forging vector is gone.

    This is the exact finding of the criterion-3 review ``...-review-250``: the
    caller that drives the publication could name the authorizer itself
    (``agent-bot-42``) and so authorize its own irreversible publication.  Three
    independent facts, all from the real source surface, pin that removal:

    * the ``native-controller`` parser exposes **no** publication-authorizer or
      publication-authority option, and passing one is a usage error (exit 2);
    * the ``main`` entry refuses the forged invocation by exit code, never with
      ``EXIT_OK`` -- nothing is authorized and nothing is published;
    * with a valid transaction and review but **no** owner approval file, the
      forged invocation cannot fall back to any caller-named approver: the
      refusal names :data:`HOLD_CMS_PUBLICATION_NO_OWNER_APPROVAL`.
    """
    # (a) The forging option does not exist on the production parser.
    parser = nc.build_parser()
    option_strings = {
        opt for action in parser._actions for opt in action.option_strings
    }
    assert not any("publication-author" in opt for opt in option_strings)
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(
            [
                "--run-id", _RUN_ID, "--task-id", _TASK_ID, "--attempt", "1",
                "--cms-publication-authorizer", "agent-bot-42",
            ]
        )
    assert excinfo.value.code == 2

    # (b) The real entry refuses the forged invocation: the old vector, whose
    # argument no longer exists, cannot be smuggled through as a real run.
    txn, _producer = _seed_restored_transaction(store)
    _record_durable_review(authority)
    _, mailbox = authority

    forged = _argv(store, txn, mailbox=mailbox) + [
        "--cms-publication-authorizer", "agent-bot-42",
    ]
    code = nc.main(forged, store=store)
    err = capsys.readouterr().err

    assert code == nc.EXIT_INVALID_INPUT
    assert code != nc.EXIT_OK
    # (c) Nothing was published: the durable transaction is untouched.
    assert store.get_cms_transaction(_RUN_ID, txn.txn_id)["stage"] == "restored"

    # (d) Without any caller-named approver the *real* invocation refuses by
    # name -- the caller has no fallback way to supply a human authorization.
    code = nc.main(_argv(store, txn, mailbox=mailbox), store=store)
    err = capsys.readouterr().err
    assert code == nc.EXIT_REFUSED
    assert nc.HOLD_CMS_PUBLICATION_NO_OWNER_APPROVAL in err


# ── 7. Missing approval file -> refused by name ─────────────────────────────


def test_missing_approval_file_is_refused_by_name(
    store, capsys, authority, owner_state, restore_signal_handlers
):
    """An empty owner state directory authorizes nothing, by name.

    No ``owner-publication-approval.yaml`` exists, so
    :func:`nc.resolve_publication_approval` fails closed under
    :data:`HOLD_CMS_PUBLICATION_NO_OWNER_APPROVAL` -- the publication is never
    authorized by an implicit or default approval.
    """
    txn, _producer = _seed_restored_transaction(store)
    _record_durable_review(authority)
    _, mailbox = authority

    code = nc.main(_argv(store, txn, mailbox=mailbox), store=store)
    err = capsys.readouterr().err

    assert code == nc.EXIT_REFUSED
    assert nc.HOLD_CMS_PUBLICATION_NO_OWNER_APPROVAL in err
    assert store.get_cms_transaction(_RUN_ID, txn.txn_id)["stage"] == "restored"


# ── 8. Approval binds a different revision/digest -> refused by name ─────────


@pytest.mark.parametrize(
    "revision, digest",
    [("rev-other", _DIGEST), (_REVISION, "sha256:other")],
    ids=["revision-mismatch", "digest-mismatch"],
)
def test_approval_mismatch_is_refused_by_name(
    store, capsys, authority, owner_state, restore_signal_handlers,
    revision, digest,
):
    """An approval that binds a different revision/digest is refused by name.

    The owner approval exists and is well-formed, but it authorizes a different
    transaction revision (or artifact digest) than the one under publication,
    so it does not authorize *this* irreversible publication: refused
    fail-closed under :data:`HOLD_CMS_PUBLICATION_APPROVAL_MISMATCH`.  A content
    change therefore invalidates the authorization rather than publishing an
    unreviewed revision.
    """
    txn, _producer = _seed_restored_transaction(store)
    _record_durable_review(authority)
    _, mailbox = authority
    (owner_state / nc.OWNER_PUBLICATION_APPROVAL_FILENAME).write_bytes(
        _approval_bytes(revision=revision, digest=digest)
    )

    code = nc.main(_argv(store, txn, mailbox=mailbox), store=store)
    err = capsys.readouterr().err

    assert code == nc.EXIT_REFUSED
    assert nc.HOLD_CMS_PUBLICATION_APPROVAL_MISMATCH in err


# ── 9. Approval changed between load and apply -> refused by name ────────────


def test_changed_approval_is_refused_between_load_and_apply(
    store, capsys, authority, owner_state, monkeypatch, restore_signal_handlers
):
    """Bytes that change between load and apply are refused under the hold.

    The loader records the SHA-256 of the bytes it read; when the owner approval
    file is rewritten *after* it is loaded and before it is applied, the
    recorded digest no longer matches and the publication is refused under
    :data:`HOLD_CMS_PUBLICATION_APPROVAL_CHANGED` -- never authorized against an
    approval the owner did not author.  The rewrite is injected between the
    caller's own load and the controller's recheck, modelling an owner edit in
    that window.
    """
    txn, _producer = _seed_restored_transaction(store)
    _record_durable_review(authority)
    _, mailbox = authority
    path = owner_state / nc.OWNER_PUBLICATION_APPROVAL_FILENAME
    path.write_bytes(_approval_bytes())

    real_load = nc.resolve_publication_approval
    recorded: list[str] = []

    def _load_then_rewrite(*args, **kwargs):
        approval = real_load(*args, **kwargs)
        recorded.append(approval.digest)
        # The owner edits the file in the window between load and apply.
        path.write_bytes(_approval_bytes() + b"# rewritten after load\n")
        return approval

    monkeypatch.setattr(nc, "resolve_publication_approval", _load_then_rewrite)

    code = nc.main(_argv(store, txn, mailbox=mailbox), store=store)
    err = capsys.readouterr().err

    assert code == nc.EXIT_REFUSED
    assert nc.HOLD_CMS_PUBLICATION_APPROVAL_CHANGED in err
    assert store.get_cms_transaction(_RUN_ID, txn.txn_id)["stage"] == "restored"
    # The refusal was decided against the bytes that were loaded.
    assert recorded


# ── 10. Approver equals the producer -> refused by name ─────────────────────


def test_approver_equal_to_producer_is_refused_by_name(
    store, capsys, authority, owner_state, restore_signal_handlers
):
    """An owner approval naming the producer as approver is self-authorization.

    The approval is well-formed and binds the exact revision/digest, but its
    ``approver`` is the publishing attempt's derived producer identity, so the
    producer would be authorizing its own irreversible publication.  That is
    refused fail-closed under :data:`HOLD_CMS_PUBLICATION_UNAUTHORIZED`.
    """
    txn, producer = _seed_restored_transaction(store)
    _record_durable_review(authority)
    _, mailbox = authority
    (owner_state / nc.OWNER_PUBLICATION_APPROVAL_FILENAME).write_bytes(
        _approval_bytes(approver=producer)
    )

    code = nc.main(_argv(store, txn, mailbox=mailbox), store=store)
    err = capsys.readouterr().err

    assert code == nc.EXIT_REFUSED
    assert nc.HOLD_CMS_PUBLICATION_UNAUTHORIZED in err


# ── 11. The approval cannot be supplied through any argument ────────────────


def test_approval_cannot_be_supplied_through_any_argument(owner_state):
    """No tool or CLI argument can supply the approval or its bytes.

    Two independent facts, both from the real source surface:

    * the loader takes exactly one parameter -- the owner-state *directory* --
      and appends the pinned file name itself, so it cannot be pointed at
      arbitrary bytes;
    * the production caller takes no approval argument; passing the approval
      itself as a keyword argument is a ``TypeError``.
    """
    import inspect

    params = list(inspect.signature(nc.load_owner_publication_approval).parameters)
    assert params == ["owner_state_dir"]
    with pytest.raises(TypeError):
        nc.load_owner_publication_approval(
            os.fspath(owner_state), filename="/tmp/other.yaml"
        )
    with pytest.raises(TypeError):
        nc.load_owner_publication_approval(
            os.fspath(owner_state), approver="agent-bot-42"
        )
    with pytest.raises(TypeError):
        nc.resolve_publication_approval(approver="agent-bot-42")


# ── Control: the gates can pass ─────────────────────────────────────────────


def test_fully_provisioned_publication_is_authorized(
    store, capsys, authority, owner_state, monkeypatch, restore_signal_handlers
):
    """A fully provisioned publication is *authorized* (a decision, not a hold).

    The durable review, the verified producer, the owner-authored approval and
    the owner autonomy policy are all present, so the irreversible publication
    is approved.  This proves the named refusals above are not vacuous: the
    gates admit when every required fact is durable and provisioned.  No remote
    CMS is contacted -- the decision is returned.
    """
    txn, producer = _seed_restored_transaction(store)
    _record_durable_review(authority)
    _, mailbox = authority
    (owner_state / nc.OWNER_PUBLICATION_APPROVAL_FILENAME).write_bytes(
        _approval_bytes()
    )
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
