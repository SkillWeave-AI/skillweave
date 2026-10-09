"""SW-1510-MCP-AUTH: authenticate MCP principals and bind each operation to
authority.

Authority is **server-owned**. A reachable local MCP server is not proof of
authorization, and neither is a caller passing ``role="reviewer"``. These tests
prove the three criteria at the *actual* consumer boundary -- the callables in
:mod:`skillweave.mcp.server` that an MCP client invokes, and the
:class:`~skillweave.runtime.authority.MCPPrincipalAuthority` that issues and
verifies principals -- never against a re-implementation:

1. Worker, controller, observer and reviewer sessions carry **distinct scoped
   authority**; a caller cannot obtain reviewer rights by passing
   ``role="reviewer"`` (the role is read from the server-owned registry, and a
   mismatch is refused).
2. Cross-client run access, arbitrary ``db_path``/``artifacts_path``/command
   injection, duplicate starts and replayed approvals are refused with
   **auditable typed errors** (a stable ``code``).
3. Local stdio carries an **explicit supervisor identity**; HTTP applies the
   applicable authorization controls and **no token passthrough** to unapproved
   providers.

Every one of the six protected tools is gated at the real server entrypoint,
not only ``review_run``; a duplicate start is refused at the real ``create_run``;
and the production issuance point binds the authority and mints the stdio
session the server startup serves under.

The frozen ``execution-envelope-v1`` boundary the middleware consumes is proven
too: the producer emits the SDK contract (``skillweave_sdk.validator``) and a
record that violates it -- an unknown version, a forbidden property or a
missing required field -- is refused by name.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest  # noqa: E402

from skillweave.mcp import server as mcp_server  # noqa: E402
from skillweave.runtime.authority import (  # noqa: E402
    EXECUTION_ENVELOPE_MCP_EXTENSION,
    EXECUTION_ENVELOPE_SCHEMA_DIGEST,
    EXECUTION_ENVELOPE_VERSION,
    CommandInjectionRefused,
    CrossClientAccessRefused,
    DuplicateStartRefused,
    EnvelopeSchemaInvalid,
    MCPAuthError,
    MCPPrincipalAuthority,
    PathInjectionRefused,
    ReplayRefused,
    RoleEscalationRefused,
    UnauthenticatedPrincipal,
    UnsupportedEnvelopeVersion,
    _EXECUTION_ENVELOPE_REQUIRED,
    _sdk_validator_module,
    verify_execution_envelope,
)
from skillweave.runtime.store import SQLiteRunStore  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _scratch_tmp(tmp_path, monkeypatch):
    """Point tempfile at the scratch basetemp so no probe touches host TMPDIR."""
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path), raising=False)
    return tmp_path


@pytest.fixture
def authority():
    """A fresh server-owned authority bound onto the MCP server module.

    Binding is process-wide and is torn down after the test so no test leaks an
    authority into another.
    """
    auth = MCPPrincipalAuthority()
    previous = mcp_server.get_authority()
    previous_ready = mcp_server._READY_SESSION_TOKEN
    mcp_server.bind_authority(auth)
    mcp_server._READY_SESSION_TOKEN = None
    yield auth
    mcp_server._AUTHORITY = previous
    mcp_server._READY_SESSION_TOKEN = previous_ready


def _child_env() -> dict:
    env = dict(os.environ)
    existing = [e for e in env.get("PYTHONPATH", "").split(os.pathsep) if e]
    if str(_SRC) not in existing:
        existing.insert(0, str(_SRC))
    env["PYTHONPATH"] = os.pathsep.join(existing)
    return env


def _make_run(tmp_path, run_id, label=None):
    """Create a run in a state from which ``advance_or_stop`` is legal.

    ``label`` selects the client directory so two distinct clients can each
    hold a run with the *same* ``run_id`` in separate stores (cross-client
    access); it defaults to the run id for the single-client case.
    """
    root = tmp_path / f"client-{label if label is not None else run_id}"
    root.mkdir(parents=True, exist_ok=True)
    db = str(root / "store.db")
    store = SQLiteRunStore(db)
    store.create_run(run_id)
    rec = store.get_run(run_id)
    store.transition(
        run_id, "batch_selection", expected_state="preflight",
        expected_version=rec.version, role="ops",
    )
    return db, str(root)


def _assert_typed_refusal(payload: str, code: str):
    record = json.loads(payload)
    assert record["status"] == "refused", record
    assert record["code"] == code, record
    return record


# ===========================================================================
# Criterion 1: distinct scoped authority; no reviewer rights via role="reviewer"
# ===========================================================================


class TestDistinctScopedAuthority:
    def test_the_four_roles_carry_distinct_scopes(self, authority):
        principals = {
            role: authority.verify_session(
                authority.issue_session(role=role, client_id="c", transport="stdio",
                                        run_ids=["r1"])
            )
            for role in ("worker", "observer", "reviewer", "controller")
        }
        worker = principals["worker"]
        observer = principals["observer"]
        reviewer = principals["reviewer"]
        controller = principals["controller"]

        # Worker cannot review/approve; reviewer can review but is read-only;
        # observer reads only; controller mutates state but does not review.
        assert not worker.has_scope("review_run")
        assert not worker.has_scope("approve_gate")
        assert reviewer.has_scope("review_run") and reviewer.has_scope("approve_gate")
        assert not reviewer.has_scope("write_run_state")
        assert observer.scopes == frozenset({"read_run"})
        assert controller.has_scope("write_run_state")
        assert not controller.has_scope("review_run")

        # Every role's scope set is genuinely distinct from every other's.
        scope_sets = [frozenset(p.scopes) for p in principals.values()]
        assert len(set(scope_sets)) == len(scope_sets)

    def test_authority_is_read_from_registry_not_a_caller_field(self, authority):
        """The wire cannot name a role: only the registry decides it."""
        token = authority.issue_session(
            role="observer", client_id="c", transport="stdio", run_ids=["r1"]
        )
        # A caller "demanding" reviewer authority changes nothing about the
        # verified principal; it is refused, not granted.
        principal = authority.verify_session(token)
        assert principal.role == "observer"
        with pytest.raises(RoleEscalationRefused) as exc:
            authority.refuse_role_escalation(principal, "reviewer")
        assert exc.value.code == "role_escalation_refused"

    def test_review_run_refuses_a_worker_demanding_the_reviewer_role(
        self, authority, tmp_path
    ):
        """The acceptance criterion's exact shape: role="reviewer" is refused."""
        db, root = _make_run(tmp_path, "run-1")
        worker_token = authority.issue_session(
            role="worker", client_id="c1", transport="stdio",
            run_ids=["run-1"], allowed_roots=[root],
        )
        out = mcp_server.review_run(
            "run-1", "pass", "ok", db_path=db,
            role="reviewer", session_token=worker_token,
        )
        record = _assert_typed_refusal(out, "role_escalation_refused")
        # The worker's real role is named and the requested role recorded.
        assert record["actual_role"] == "worker"
        assert record["requested_role"] == "reviewer"
        # And nothing was mutated.
        assert SQLiteRunStore(db).get_run("run-1").state == "batch_selection"

    def test_review_run_refuses_an_unauthenticated_caller(self, authority, tmp_path):
        db, _root = _make_run(tmp_path, "run-1")
        out = mcp_server.review_run("run-1", "pass", "ok", db_path=db)
        _assert_typed_refusal(out, "unauthenticated")

    def test_review_run_without_bound_authority_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "_AUTHORITY", None)
        db, root = _make_run(tmp_path, "run-1")
        out = mcp_server.review_run(
            "run-1", "pass", "ok", db_path=db, session_token="anything",
        )
        _assert_typed_refusal(out, "unauthenticated")

    def test_worker_cannot_review_even_without_naming_a_role(self, authority, tmp_path):
        db, root = _make_run(tmp_path, "run-1")
        worker_token = authority.issue_session(
            role="worker", client_id="c1", transport="stdio",
            run_ids=["run-1"], allowed_roots=[root],
        )
        out = mcp_server.review_run(
            "run-1", "pass", "ok", db_path=db, session_token=worker_token,
        )
        _assert_typed_refusal(out, "operation_not_permitted")

    def test_reviewer_reviews_and_the_persisted_role_is_the_authenticated_one(
        self, authority, tmp_path
    ):
        db, root = _make_run(tmp_path, "run-1")
        token = authority.issue_session(
            role="reviewer", client_id="c1", transport="stdio",
            run_ids=["run-1"], allowed_roots=[root],
        )
        out = json.loads(mcp_server.review_run(
            "run-1", "pass", "independent", db_path=db, session_token=token,
            role="reviewer",
        ))
        assert out["status"] == "reviewed"
        assert out["role"] == "reviewer"
        assert out["reviewer"] == "mcp:reviewer:c1"
        persisted = SQLiteRunStore(db).get_run("run-1")
        assert persisted.state == "advance_or_stop"
        # The durable record carries the *authenticated* principal, not a
        # caller-supplied string.
        assert persisted.role == "reviewer"

    def test_issue_session_refuses_an_unknown_role(self, authority):
        with pytest.raises(RoleEscalationRefused) as exc:
            authority.issue_session(role="superadmin", client_id="c", transport="stdio")
        assert exc.value.code == "role_escalation_refused"


# ===========================================================================
# Criterion 2: injected paths/commands, cross-client, duplicate start, replay
# ===========================================================================


class TestTypedRefusals:
    def test_cross_client_run_access_is_refused(self, authority, tmp_path):
        """A reviewer bound to client A cannot touch client B's run store."""
        db_a, root_a = _make_run(tmp_path, "shared-id", label="A")
        db_b, root_b = _make_run(tmp_path, "shared-id", label="B")
        token_a = authority.issue_session(
            role="reviewer", client_id="A", transport="stdio",
            run_ids=["shared-id"], allowed_roots=[root_a],
        )
        # Same run id, but stored under client B's root: refused.
        out = mcp_server.review_run(
            "shared-id", "pass", "ok", db_path=db_b, session_token=token_a,
        )
        _assert_typed_refusal(out, "path_injection_refused")
        assert SQLiteRunStore(db_b).get_run("shared-id").state == "batch_selection"

    def test_cross_client_run_id_is_refused(self, authority, tmp_path):
        db, root = _make_run(tmp_path, "run-mine")
        token = authority.issue_session(
            role="reviewer", client_id="A", transport="stdio",
            run_ids=["run-mine"], allowed_roots=[root],
        )
        out = mcp_server.review_run(
            "run-someone-elses", "pass", "ok", db_path=db, session_token=token,
        )
        _assert_typed_refusal(out, "cross_client_access_refused")

    def test_arbitrary_artifacts_path_is_refused(self, authority):
        token = authority.issue_session(
            role="controller", client_id="c", transport="stdio",
        )
        with pytest.raises(PathInjectionRefused) as exc:
            authority.authorize(
                token, "write_run_state",
                db_path=".skillweave/store.db",
                artifacts_path="/etc/evil",
                default_paths=(".skillweave/store.db",),
            )
        assert exc.value.code == "path_injection_refused"

    def test_arbitrary_command_is_refused(self, authority):
        token = authority.issue_session(
            role="controller", client_id="c", transport="stdio",
            allowed_argv0=["/usr/bin/true"],
        )
        with pytest.raises(CommandInjectionRefused) as exc:
            authority.authorize(token, "start_run", command="/bin/sh -c 'rm -rf /'")
        assert exc.value.code == "command_injection_refused"

    def test_principal_without_command_rights_may_not_supply_a_command(self, authority):
        token = authority.issue_session(
            role="reviewer", client_id="c", transport="stdio", run_ids=["r1"],
        )
        with pytest.raises(CommandInjectionRefused) as exc:
            authority.authorize(token, "read_run", command="echo hi", run_id="r1")
        assert exc.value.code == "command_injection_refused"

    def test_duplicate_start_is_refused(self, authority):
        token = authority.issue_session(
            role="controller", client_id="c", transport="stdio",
        )
        authority.authorize(token, "start_run", start_scope="task-1/attempt-1")
        with pytest.raises(DuplicateStartRefused) as exc:
            authority.authorize(token, "start_run", start_scope="task-1/attempt-1")
        assert exc.value.code == "duplicate_start_refused"

    def test_replayed_approval_is_refused(self, authority):
        token = authority.issue_session(
            role="reviewer", client_id="c", transport="stdio", run_ids=["r1"],
        )
        approval = authority.mint_approval(token, run_id="r1", decision="pass")
        # First use consumes the one-shot capability.
        authority.authorize(token, "approve_gate", run_id="r1", approval_token=approval)
        with pytest.raises(ReplayRefused) as exc:
            authority.authorize(token, "approve_gate", run_id="r1", approval_token=approval)
        assert exc.value.code == "replay_refused"

    def test_approval_for_a_different_run_is_refused(self, authority):
        token = authority.issue_session(
            role="reviewer", client_id="c", transport="stdio", run_ids=["r1", "r2"],
        )
        approval = authority.mint_approval(token, run_id="r1", decision="pass")
        with pytest.raises(CrossClientAccessRefused) as exc:
            authority.authorize(token, "approve_gate", run_id="r2", approval_token=approval)
        assert exc.value.code == "cross_client_access_refused"

    def test_expired_and_revoked_tokens_are_refused(self, authority):
        token = authority.issue_session(role="observer", client_id="c", transport="stdio")
        authority.revoke_token(token)
        with pytest.raises(UnauthenticatedPrincipal):
            authority.verify_session(token)
        token2 = authority.issue_session(role="observer", client_id="c", transport="stdio")
        authority.expire(token2)
        with pytest.raises(UnauthenticatedPrincipal):
            authority.verify_session(token2)

    def test_every_refusal_carries_a_stable_code(self):
        """Auditability: each refusal is a typed record, never a bare string."""
        for exc_type, code in [
            (RoleEscalationRefused, "role_escalation_refused"),
            (CrossClientAccessRefused, "cross_client_access_refused"),
            (PathInjectionRefused, "path_injection_refused"),
            (CommandInjectionRefused, "command_injection_refused"),
            (DuplicateStartRefused, "duplicate_start_refused"),
            (ReplayRefused, "replay_refused"),
            (UnauthenticatedPrincipal, "unauthenticated"),
        ]:
            error = exc_type("x")
            assert isinstance(error, MCPAuthError)
            assert error.code == code
            assert error.to_dict()["code"] == code


# ===========================================================================
# Every protected tool is gated (controller finding 1); duplicate start and
# injected paths are refused at the real tool (finding 2)
# ===========================================================================


class TestEveryToolIsGated:
    """No tool bypasses the authorization seam: with an authority bound and no
    resolvable credential, *every* protected tool refuses -- not only review."""

    def test_bound_authority_refuses_each_tool_without_a_credential(
        self, authority, tmp_path
    ):
        db, _root = _make_run(tmp_path, "run-1")
        from skillweave.mcp import control as _control

        calls = {
            "create_run": lambda: mcp_server.create_run(
                "cli", "m", "repo", "commit", command="true", db_path=db,
            ),
            "status_run": lambda: mcp_server.status_run("run-1", db_path=db),
            "cancel_run": lambda: mcp_server.cancel_run("run-1", db_path=db),
            "resume_run": lambda: mcp_server.resume_run("run-1", db_path=db),
            "get_evidence": lambda: mcp_server.get_evidence("run-1", db_path=db),
            "review_run": lambda: mcp_server.review_run(
                "run-1", "pass", "ok", db_path=db
            ),
            # SW-1510-MCP-AGENTS: the durable handoff/mailbox tools are gated at
            # the same seam as the legacy six -- an unauthenticated principal is
            # refused before any mailbox is opened.
            "mailbox_post": lambda: mcp_server.mailbox_post(
                kind="handoff", recipient="worker-1",
            ),
            "mailbox_read": lambda: mcp_server.mailbox_read(recipient="worker-1"),
            "mailbox_replay": lambda: mcp_server.mailbox_replay(recipient="worker-1"),
            "mailbox_ack": lambda: mcp_server.mailbox_ack("m-1"),
            "agent_capabilities": lambda: mcp_server.agent_capabilities(),
        }
        assert set(calls) == set(_control.LEGACY_TOOLS) | set(_control.MCP_AGENT_TOOLS)
        for name, call in calls.items():
            record = _assert_typed_refusal(call(), "unauthenticated")
            assert record["status"] == "refused", name

    def test_cross_client_status_and_evidence_are_refused(self, authority, tmp_path):
        db, root = _make_run(tmp_path, "run-mine")
        token = authority.issue_session(
            role="observer", client_id="A", transport="stdio",
            run_ids=["run-mine"], allowed_roots=[root],
        )
        _assert_typed_refusal(
            mcp_server.status_run("run-not-mine", db_path=db, session_token=token),
            "cross_client_access_refused",
        )
        _assert_typed_refusal(
            mcp_server.get_evidence("run-not-mine", db_path=db, session_token=token),
            "cross_client_access_refused",
        )

    def test_an_authenticated_read_is_admitted(self, authority, tmp_path):
        db, root = _make_run(tmp_path, "run-1")
        token = authority.issue_session(
            role="observer", client_id="A", transport="stdio",
            run_ids=["run-1"], allowed_roots=[root],
        )
        out = json.loads(
            mcp_server.status_run("run-1", db_path=db, session_token=token)
        )
        assert out.get("status") != "refused", out

    def test_injected_db_path_is_refused_at_the_real_tool(self, authority, tmp_path):
        db, root = _make_run(tmp_path, "run-1")
        token = authority.issue_session(
            role="observer", client_id="A", transport="stdio",
            run_ids=["run-1"], allowed_roots=[root],
        )
        _assert_typed_refusal(
            mcp_server.status_run(
                "run-1", db_path="/etc/other/store.db", session_token=token
            ),
            "path_injection_refused",
        )

    def test_duplicate_start_is_refused_at_the_real_create_run(self, authority, tmp_path):
        db, root = _make_run(tmp_path, "run-1")
        artifacts = os.path.join(root, "artifacts")
        token = authority.issue_session(
            role="controller", client_id="A", transport="stdio",
            allowed_roots=[root], allowed_argv0=["/usr/bin/true"],
        )
        first = mcp_server.create_run(
            "cli", "m", "repo", "commit", command="/usr/bin/true",
            db_path=db, artifacts_path=artifacts,
            task_id="task-1", attempt=1, session_token=token,
        )
        # The first start may legitimately execute or fail a storage contract;
        # either way it is not a refusal of the *start authority*.
        assert json.loads(first).get("code") != "duplicate_start_refused"
        second = mcp_server.create_run(
            "cli", "m", "repo", "commit", command="/usr/bin/true",
            db_path=db, artifacts_path=artifacts,
            task_id="task-1", attempt=1, session_token=token,
        )
        _assert_typed_refusal(second, "duplicate_start_refused")


# ===========================================================================
# Production issuance point (controller finding 4)
# ===========================================================================


class TestProductionIssuance:
    def test_startup_binds_authority_and_mints_a_stdio_session(self, monkeypatch):
        monkeypatch.delenv(mcp_server.SUPERVISOR_ENV, raising=False)
        monkeypatch.setattr(mcp_server, "_AUTHORITY", None)
        monkeypatch.setattr(mcp_server, "_READY_SESSION_TOKEN", None)

        with pytest.raises(UnauthenticatedPrincipal):
            mcp_server.startup_and_bind_stdio()  # no supervisor named

        token = mcp_server.startup_and_bind_stdio(supervisor="supervisor:ops-1")
        assert isinstance(token, str) and token
        auth = mcp_server.get_authority()
        principal = auth.verify_session(token)
        assert principal.role == "controller"
        assert principal.transport == "stdio"
        # The minted token is host memory, never the environment.
        assert mcp_server.session_token_from_env() == ""
        # ...and a protected call resolves to it with no wire credential.
        resolved = mcp_server.resolve_session_token("")
        assert resolved == token
        admitted, refusal = mcp_server.gate_operation(
            "read_run", "", db_path=".skillweave/store.db"
        )
        assert refusal is None and admitted.role == "controller"

    def test_main_performs_production_issuance_before_serving(self, monkeypatch):
        monkeypatch.setenv(mcp_server.SUPERVISOR_ENV, "supervisor:ops-3")
        monkeypatch.setattr(mcp_server, "_AUTHORITY", None)
        monkeypatch.setattr(mcp_server, "_READY_SESSION_TOKEN", None)
        monkeypatch.setattr(mcp_server.control, "require_mcp", lambda: None)

        served = {}

        class _Runner:
            def run(self):
                served["ran"] = True

        monkeypatch.setattr(mcp_server, "mcp", _Runner())
        mcp_server.main()

        assert served.get("ran") is True
        assert mcp_server.get_authority() is not None
        ready = mcp_server._READY_SESSION_TOKEN
        assert ready
        assert mcp_server.get_authority().verify_session(ready).role == "controller"


# ===========================================================================
# Criterion 3: stdio supervisor identity, HTTP controls, no token passthrough
# ===========================================================================


class TestStdioSupervisorIdentity:
    def test_stdio_requires_an_explicit_supervisor_identity(self, monkeypatch):
        monkeypatch.delenv(mcp_server.SUPERVISOR_ENV, raising=False)
        with pytest.raises(UnauthenticatedPrincipal):
            mcp_server.require_supervisor_identity()

    def test_named_supervisor_identity_is_returned(self, monkeypatch):
        monkeypatch.setenv(mcp_server.SUPERVISOR_ENV, "supervisor:ops-1")
        assert mcp_server.require_supervisor_identity() == "supervisor:ops-1"

    def test_main_refuses_before_serving_when_dependency_or_authority_absent(
        self, monkeypatch
    ):
        """The serving entry checks the dependency first (naming the missing
        package), then the stdio supervisor identity -- a local stdio client is
        never implicitly trusted."""
        monkeypatch.delenv(mcp_server.SUPERVISOR_ENV, raising=False)
        monkeypatch.setattr(mcp_server, "_AUTHORITY", None)
        from skillweave.mcp import control as _control

        if not _control.mcp_available():
            with pytest.raises(_control.McpDependencyUnavailable):
                mcp_server.main()
            return
        # With the real dependency present, the supervisor identity is the
        # next gate.
        with pytest.raises(UnauthenticatedPrincipal):
            mcp_server.main()


class TestHttpAuthorizationControls:
    def test_execution_envelope_is_the_frozen_sdk_contract(self, authority):
        """The producer emits the SDK ``execution-envelope-v1`` -- not a
        private contract -- and the consumer validates it against the SDK."""
        token = authority.issue_session(
            role="reviewer", client_id="c", transport="http", run_ids=["r1"],
        )
        envelope = authority.verify_session(token).to_envelope()
        # Exactly the eleven SDK-required properties: no private top-level
        # contract, no ``version``/``schema_digest`` keys of its own.
        assert set(envelope) == set(_EXECUTION_ENVELOPE_REQUIRED)
        assert envelope["schema_version"] == EXECUTION_ENVELOPE_VERSION
        assert verify_execution_envelope(envelope) == EXECUTION_ENVELOPE_VERSION
        # The MCP-principal facts ride a namespaced extension inside ``scope``,
        # never as competing top-level fields.
        facts = envelope["scope"][EXECUTION_ENVELOPE_MCP_EXTENSION]
        assert facts["role"] == "reviewer"
        assert facts["transport"] == "http"

    def test_envelope_is_validated_against_the_sdk_when_available(self, authority):
        """Against the *real* SDK validator, the produced envelope validates."""
        validator = _sdk_validator_module()
        if validator is None:
            pytest.skip("the SDK execution contract set is unavailable here")
        from skillweave.runtime.authority import EXECUTION_ENVELOPE_SCHEMA_ID

        envelope = authority.verify_session(
            authority.issue_session(role="controller", client_id="c", transport="http")
        ).to_envelope()
        assert validator.validate(dict(envelope), EXECUTION_ENVELOPE_SCHEMA_ID) == []
        # The pinned digest is the SDK's own canonical digest of the set.
        assert validator.canonical_digest() == EXECUTION_ENVELOPE_SCHEMA_DIGEST

    def test_unsupported_envelope_version_is_refused_by_name(self):
        record = {f: None for f in _EXECUTION_ENVELOPE_REQUIRED}
        record["schema_version"] = "2.0.0"
        with pytest.raises(UnsupportedEnvelopeVersion) as exc:
            verify_execution_envelope(record)
        assert exc.value.code == "unsupported_envelope_version"
        assert "2.0.0" in exc.value.message

    def test_envelope_with_a_forbidden_top_level_field_is_refused(self, authority):
        envelope = authority.verify_session(
            authority.issue_session(role="observer", client_id="c", transport="http")
        ).to_envelope()
        envelope["version"] = EXECUTION_ENVELOPE_VERSION  # a private, extra key
        with pytest.raises(EnvelopeSchemaInvalid) as exc:
            verify_execution_envelope(envelope)
        assert exc.value.code == "envelope_schema_invalid"

    def test_envelope_missing_a_required_field_is_refused(self, authority):
        envelope = authority.verify_session(
            authority.issue_session(role="observer", client_id="c", transport="http")
        ).to_envelope()
        del envelope["allowed_routes"]
        with pytest.raises(EnvelopeSchemaInvalid) as exc:
            verify_execution_envelope(envelope)
        assert exc.value.code == "envelope_schema_invalid"

    def test_unapproved_provider_route_is_refused(self):
        with pytest.raises(MCPAuthError) as exc:
            mcp_server.authorize_provider_forwarding(
                {"allowed_routes": ["provider-a"]}, "provider-b",
            )
        assert exc.value.code == "unapproved_provider_route"

    def test_no_token_passthrough_to_a_reflecting_route(self):
        """The client's MCP session token is authority over this server, never a
        provider bearer token -- a passthrough/echo route is refused."""
        for route in sorted(mcp_server.NON_FORWARDABLE_ROUTES):
            with pytest.raises(MCPAuthError) as exc:
                mcp_server.authorize_provider_forwarding(
                    {"allowed_routes": [route]}, route,
                )
            assert exc.value.code == "token_passthrough_refused"

    def test_approved_provider_route_is_permitted(self):
        # No refusal: the route is explicitly allowed and not a passthrough.
        mcp_server.authorize_provider_forwarding(
            {"allowed_routes": ["provider-a"]}, "provider-a",
        )

    def test_provider_forwarding_is_delegated_to_the_authenticated_principal(
        self, authority
    ):
        """With a session token, the route decision is the principal's bound
        routes -- never the envelope field a caller could write."""
        unbound = authority.issue_session(
            role="controller", client_id="c", transport="http",
        )
        # The envelope claims "provider-a", but the authenticated principal is
        # bound to no provider route: refused.
        with pytest.raises(MCPAuthError) as exc:
            mcp_server.authorize_provider_forwarding(
                {"allowed_routes": ["provider-a"]}, "provider-a",
                session_token=unbound,
            )
        assert exc.value.code == "unapproved_provider_route"

        bound = authority.issue_session(
            role="controller", client_id="c2", transport="http",
            allowed_routes=["provider-a"],
        )
        # The bound principal may forward to its approved provider...
        mcp_server.authorize_provider_forwarding(
            {"allowed_routes": ["provider-a"]}, "provider-a", session_token=bound,
        )
        # ...but never to a reflecting route, even one the envelope would allow.
        with pytest.raises(MCPAuthError) as exc:
            mcp_server.authorize_provider_forwarding(
                {"allowed_routes": ["echo"]}, "echo", session_token=bound,
            )
        assert exc.value.code == "token_passthrough_refused"

    def test_scoped_http_bind_still_requires_a_scope_token(self, monkeypatch):
        """The scoped HTTP controls remain in force (loopback default + token)."""
        from skillweave.mcp import control as _control

        monkeypatch.delenv(_control.HTTP_AUTH_ENV, raising=False)
        with pytest.raises(_control.ScopedHttpRefused):
            _control.require_scoped_bind("127.0.0.1")


# ===========================================================================
# Installed-origin proof (no stand-in module)
# ===========================================================================


def test_server_module_is_the_installed_origin(tmp_path):
    """The consumer module and the authority module are this checkout's src.

    Proven in a fresh interpreter (not this process's ``sys.modules``): the
    authorization wiring asserted above is the *installed* product module.
    """
    probe = (
        "import json\n"
        "from skillweave.mcp import server\n"
        "from skillweave.runtime import authority\n"
        "print(json.dumps({\n"
        "  'server_file': server.__file__,\n"
        "  'authority_file': authority.__file__,\n"
        "  'has_review_run': callable(getattr(server, 'review_run', None)),\n"
        "  'has_bind_authority': callable(getattr(server, 'bind_authority', None)),\n"
        "  'envelope_version': authority.EXECUTION_ENVELOPE_VERSION,\n"
        "}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-B", "-c", probe],
        capture_output=True, text=True, env=_child_env(), cwd=str(tmp_path), timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    info = json.loads(proc.stdout.strip().splitlines()[-1])
    assert info["server_file"].endswith("skillweave/mcp/server.py")
    assert info["authority_file"].endswith("skillweave/runtime/authority.py")
    assert info["has_review_run"] is True
    assert info["has_bind_authority"] is True
    assert info["envelope_version"] == EXECUTION_ENVELOPE_VERSION


def test_review_run_signature_exposes_role_but_authority_is_the_token(tmp_path):
    """The tool exposes ``role`` for *auditing*, but it is never the authority."""
    probe = (
        "import inspect, json\n"
        "from skillweave.mcp import server\n"
        "sig = inspect.signature(server.review_run)\n"
        "print(json.dumps(sorted(sig.parameters)))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-B", "-c", probe],
        capture_output=True, text=True, env=_child_env(), cwd=str(tmp_path), timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    params = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "session_token" in params
    assert "role" in params
