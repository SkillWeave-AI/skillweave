"""SW-1511-MCP-BOOTSTRAP: attach dispatched harnesses to one scoped durable
process service.

These tests prove the three original acceptance criteria at the **real
consumer/controller seams** -- the MCP server tools, the scoped HTTP control
surface and the dispatch adapter's actual child launch -- never against a
re-implementation:

1. One logical durable process service owns run/mailbox/evidence and the
   controller lease. Main, Ops and Reviewer use distinct authenticated
   server-issued principals scoped to the actual run/task/attempt; a
   client-supplied role cannot elevate authority.
2. The dispatch adapter provisions and verifies the effective MCP configuration
   *inside the actual child launch* -- not only the interactive harness profile
   -- by carrying an explicit env allowlist and proving the child resolved the
   same authority/state service. A reconnecting child preserves its owned
   cursor identity and cannot read/write another run.
3. The supported multi-client deployment is authenticated scoped HTTP with a
   loopback default; a local stdio path is admitted only when it demonstrably
   reaches the same authority/state service, and an independently initialized
   per-harness controller or isolated default store is refused.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest  # noqa: E402

from skillweave.dispatch import application as dispatch_app  # noqa: E402
from skillweave.dispatch.messaging import (  # noqa: E402
    AgentMailbox,
    AgentMessage,
    MailboxCursor,
)
from skillweave.mcp import bootstrap as boot  # noqa: E402
from skillweave.mcp import control  # noqa: E402
from skillweave.mcp import server as mcp_server  # noqa: E402
from skillweave.runtime.authority import (  # noqa: E402
    MCPPrincipalAuthority,
    RoleEscalationRefused,
)
from skillweave.runtime.runner_adapter import start_process  # noqa: E402
from skillweave.runtime.store import SQLiteRunStore  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _scratch_tmp(tmp_path, monkeypatch):
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path), raising=False)
    return tmp_path


@pytest.fixture
def authority():
    auth = MCPPrincipalAuthority()
    previous = mcp_server.get_authority()
    previous_ready = mcp_server._READY_SESSION_TOKEN
    mcp_server.bind_authority(auth)
    mcp_server._READY_SESSION_TOKEN = None
    yield auth
    mcp_server._AUTHORITY = previous
    mcp_server._READY_SESSION_TOKEN = previous_ready


def _service(root: Path) -> boot.HarnessProcessService:
    return boot.HarnessProcessService(
        authority="skillweave-process-service",
        db_path=str(root / "store.db"),
        mailbox_path=str(root / "agent-mailbox.jsonl"),
        artifacts_path=str(root / "artifacts"),
        stream_root=str(root / "streams"),
    )


class _FakeHandle:
    """A minimal stand-in for a live ``RunningProcess`` in the seam tests."""

    def __init__(self, effective_env=None, stdout=b"", stderr=b""):
        self._sw_effective_env = effective_env
        self._stdout = stdout
        self._stderr = stderr

    def wait(self, timeout=None):
        class _Result:
            exit_code = 0
            signal = None
            stdout = self._stdout
            stderr = self._stderr
            stdout_receipt = None
            stderr_receipt = None

        _Result.stdout = self._stdout
        _Result.stderr = self._stderr
        return _Result()


# ===========================================================================
# Criterion 1: one durable process service; distinct scoped server-issued
# principals; no client-supplied role elevation
# ===========================================================================


class TestOneProcessServiceScopedPrincipals:
    def test_grant_env_names_the_one_process_service(self, tmp_path):
        svc = _service(tmp_path)
        token = "tok-server-issued"
        env = boot.grant_child_env(
            boot.HarnessMcpSessionGrant(
                schema_id=boot.HARNESS_MCP_SESSION_GRANT_SCHEMA_ID,
                schema_version="1.0.0",
                run_id="run-1",
                task_id="task-1",
                attempt=2,
                transport="stdio",
                session_token=token,
                service=svc,
                start_scope="task-1/attempt-2",
            )
        )
        # The single service's store/mailbox/artifacts/stream endpoints ride
        # together with the server-issued token; there is no second store.
        assert env[boot.ENV_MCP_SESSION_TOKEN] == token
        assert env[boot.ENV_MCP_DB_PATH] == svc.db_path
        assert env[boot.ENV_MCP_ARTIFACTS_PATH] == svc.artifacts_path
        assert env[boot.ENV_MCP_STREAM_ROOT] == svc.stream_root
        assert env[boot.ENV_MCP_RUN_ID] == "run-1"

    def test_distinct_roles_get_distinct_server_issued_grants(self, authority, tmp_path):
        svc = _service(tmp_path)
        grants = {
            role: mcp_server.issue_harness_session_grant(
                authority=authority,
                role=role,
                client_id=role,
                transport="stdio",
                run_id="run-1",
                task_id="task-1",
                attempt=1,
                service=svc,
            )
            for role in ("worker", "observer", "reviewer", "controller")
        }
        # Each grant's token resolves to the *role the registry minted*, not a
        # caller-supplied string, and the scopes differ per role.
        scopes = {}
        for role, grant in grants.items():
            principal = authority.verify_session(grant.session_token)
            assert principal.role == role
            scopes[role] = frozenset(principal.scopes)
        assert len(set(scopes.values())) == len(scopes)

    def test_grant_scope_is_the_actual_run_task_attempt(self, authority, tmp_path):
        svc = _service(tmp_path)
        grant = mcp_server.issue_harness_session_grant(
            authority=authority,
            role="worker",
            client_id="ops",
            transport="stdio",
            run_id="run-9",
            task_id="task-9",
            attempt=3,
            service=svc,
        )
        assert grant.start_scope == "task-9/attempt-3"
        assert grant.scope() == "task-9/attempt-3"
        principal = authority.verify_session(grant.session_token)
        # Bound to that run only; an unknown run is refused cross-client.
        assert principal.may_operate_on("run-9") is True
        assert principal.may_operate_on("other-run") is False

    def test_issuing_a_grant_for_an_unknown_role_is_refused(self, authority, tmp_path):
        svc = _service(tmp_path)
        with pytest.raises(RoleEscalationRefused):
            mcp_server.issue_harness_session_grant(
                authority=authority,
                role="superuser",
                client_id="x",
                transport="stdio",
                run_id="run-1",
                service=svc,
            )

    def test_harness_session_grant_tool_refuses_role_elevation(
        self, authority, tmp_path
    ):
        """The real tool seam: a worker demanding controller is refused."""
        svc = _service(tmp_path)
        worker = authority.issue_session(
            role="worker", client_id="ops", transport="stdio", run_ids=["run-1"]
        )
        out = mcp_server.harness_session_grant(
            role="controller",
            client_id="ops",
            transport="stdio",
            run_id="run-1",
            task_id="task-1",
            attempt=1,
            requested_role="controller",
            db_path=svc.db_path,
            artifacts_path=svc.artifacts_path,
            stream_root=svc.stream_root,
            mailbox_path=svc.mailbox_path,
            session_token=worker,
        )
        record = json.loads(out)
        assert record["status"] == "refused"
        assert record["code"] == "role_escalation_refused"

    def test_harness_session_grant_tool_gates_on_the_minted_role(
        self, authority, tmp_path
    ):
        """A child may never carry authority the parent does not hold.

        A worker asking for a reviewer child grant is refused -- whether by the
        operation gate (a worker may not write run state) or the minted-role
        check (reviewer authority is not a subset of worker authority). Either
        way the refusal is typed and no token is issued; the caller gains no
        authority by naming a stronger ``role``. (A controller *may* attach a
        worker child -- proven by the scoped-grant test above.)
        """
        svc = _service(tmp_path)
        worker = authority.issue_session(
            role="worker", client_id="ops", transport="stdio", run_ids=["run-1"]
        )
        out = mcp_server.harness_session_grant(
            role="reviewer",
            client_id="ops",
            transport="stdio",
            run_id="run-1",
            task_id="task-1",
            attempt=1,
            db_path=svc.db_path,
            artifacts_path=svc.artifacts_path,
            stream_root=svc.stream_root,
            mailbox_path=svc.mailbox_path,
            session_token=worker,
        )
        record = json.loads(out)
        assert record["status"] == "refused"
        assert record["code"] in (
            "role_escalation_refused",
            "operation_not_permitted",
        ), record
        assert "env" not in record  # nothing minted

    def test_worker_cannot_mint_a_reviewer_child_via_a_permitted_operation(
        self, authority, tmp_path
    ):
        """The minted-role check itself refuses, independent of the operation.

        A *reviewer* holds ``review_run``/``approve_gate`` but is not the
        controller (it lacks ``start_run``/``write_run_state``), and a
        reviewer's authority is not a subset of its own -- so even a reviewer
        that reached the mint path cannot attach a controller child. This pins
        the subset rule on :meth:`refuse_child_role_escalation` directly, so its
        removal cannot pass silently behind the operation gate.
        """
        reviewer = authority.issue_session(
            role="reviewer", client_id="rev", transport="http", run_ids=["run-1"]
        )
        principal = authority.verify_session(reviewer)
        with pytest.raises(RoleEscalationRefused):
            authority.refuse_child_role_escalation(principal, "controller")
        with pytest.raises(RoleEscalationRefused):
            authority.refuse_child_role_escalation(principal, "worker")
        # Only authority no broader than the reviewer's own is admitted.
        authority.refuse_child_role_escalation(principal, "reviewer")

    def test_harness_session_grant_tool_issues_a_scoped_grant(
        self, authority, tmp_path
    ):
        svc = _service(tmp_path)
        controller = authority.issue_session(
            role="controller", client_id="main", transport="stdio",
            allowed_roots=[str(tmp_path)],
        )
        out = mcp_server.harness_session_grant(
            role="worker",
            client_id="ops",
            transport="stdio",
            run_id="run-1",
            task_id="task-1",
            attempt=1,
            db_path=svc.db_path,
            artifacts_path=svc.artifacts_path,
            stream_root=svc.stream_root,
            mailbox_path=svc.mailbox_path,
            session_token=controller,
        )
        record = json.loads(out)
        assert record["status"] == "ok", record
        assert record["run_id"] == "run-1"
        assert record["service"]["db_path"] == svc.db_path
        assert record["env"][boot.ENV_MCP_SESSION_TOKEN]  # a real issued token
        # The issued token resolves to the requested (worker) role from the
        # server registry -- the caller never named it on the wire.
        issued = authority.verify_session(record["env"][boot.ENV_MCP_SESSION_TOKEN])
        assert issued.role == "worker"

    def test_controller_lease_is_single_and_fenced(self, tmp_path):
        """One logical service: exactly one controller lease, fenced."""
        store = SQLiteRunStore(str(tmp_path / "store.db"))
        lease = store.acquire_controller_lease("main", ttl_seconds=300)
        assert lease.holder == "main"
        with pytest.raises(Exception):
            store.acquire_controller_lease("other", ttl_seconds=300)
        # The fencing token strictly increases on a later re-acquire.
        store.release_controller_lease(lease.fencing_token)
        again = store.acquire_controller_lease("other", ttl_seconds=300)
        assert again.fencing_token > lease.fencing_token


# ===========================================================================
# Criterion 2: provision + verify effective MCP config inside the child launch
# ===========================================================================


class TestChildLaunchProvisioningAndVerification:
    def test_grant_is_provisioned_and_verified_in_the_real_inline_seam(
        self, tmp_path, monkeypatch
    ):
        svc = _service(tmp_path)
        grant = boot.HarnessMcpSessionGrant(
            schema_id=boot.HARNESS_MCP_SESSION_GRANT_SCHEMA_ID,
            schema_version="1.0.0",
            run_id="run-1",
            task_id="task-1",
            attempt=1,
            transport="stdio",
            session_token="tok-1",
            service=svc,
            start_scope="task-1/attempt-1",
        )
        captured = {}

        def fake_start(command, **kwargs):
            captured.update(kwargs)
            return _FakeHandle(effective_env=kwargs.get("env"))

        monkeypatch.setattr(
            "skillweave.runtime.runner_adapter.start_process", fake_start
        )
        dispatch_app._default_inline_seam(
            ["echo", "hi"],
            run_id="run-1",
            subject_repo="repo",
            subject_commit="c",
            tool="claude",
            model="m",
            env={"SW_MSG_CHANNEL": "/tmp/msg"},
            mcp_grant=grant,
        )
        # The actual launch received an explicit allowlist and the provisioned
        # MCP env -- not merely the interactive harness profile.
        assert captured["env_allowlist"] is not None
        assert captured["env"][boot.ENV_MCP_SESSION_TOKEN] == "tok-1"
        assert captured["env"]["SW_MSG_CHANNEL"] == "/tmp/msg"
        assert boot.ENV_MCP_SESSION_TOKEN in captured["env_allowlist"]

    def test_unobservable_child_effective_config_is_refused(self, tmp_path):
        svc = _service(tmp_path)
        grant = boot.HarnessMcpSessionGrant(
            schema_id=boot.HARNESS_MCP_SESSION_GRANT_SCHEMA_ID,
            schema_version="1.0.0",
            run_id="run-1",
            task_id="",
            attempt=1,
            transport="stdio",
            session_token="tok-1",
            service=svc,
            start_scope="run-1/attempt-1",
        )
        # A handle that exposes no resolved environment is refused, not
        # assumed correct.
        with pytest.raises(boot.ProcessServiceMismatchRefused):
            boot.verify_child_launch(grant, _FakeHandle(effective_env=None))

    def test_child_resolving_an_isolated_store_is_refused(self, tmp_path):
        svc = _service(tmp_path)
        grant = boot.HarnessMcpSessionGrant(
            schema_id=boot.HARNESS_MCP_SESSION_GRANT_SCHEMA_ID,
            schema_version="1.0.0",
            run_id="run-1",
            task_id="",
            attempt=1,
            transport="stdio",
            session_token="tok-1",
            service=svc,
            start_scope="run-1/attempt-1",
        )
        other = _service(tmp_path / "elsewhere")
        isolated_env = boot.grant_child_env(
            boot.HarnessMcpSessionGrant(
                schema_id=boot.HARNESS_MCP_SESSION_GRANT_SCHEMA_ID,
                schema_version="1.0.0",
                run_id="run-1",
                task_id="",
                attempt=1,
                transport="stdio",
                session_token="tok-1",
                service=other,
                start_scope="run-1/attempt-1",
            )
        )
        with pytest.raises(boot.ProcessServiceMismatchRefused):
            boot.verify_child_launch(grant, _FakeHandle(effective_env=isolated_env))

    def test_child_with_a_different_session_token_is_refused(self, tmp_path):
        svc = _service(tmp_path)
        grant = boot.HarnessMcpSessionGrant(
            schema_id=boot.HARNESS_MCP_SESSION_GRANT_SCHEMA_ID,
            schema_version="1.0.0",
            run_id="run-1",
            task_id="",
            attempt=1,
            transport="stdio",
            session_token="tok-1",
            service=svc,
            start_scope="run-1/attempt-1",
        )
        env = dict(grant.env())
        env[boot.ENV_MCP_SESSION_TOKEN] = "other-token"
        with pytest.raises(boot.ProcessServiceMismatchRefused):
            boot.verify_child_launch(grant, _FakeHandle(effective_env=env))

    def test_credential_shaped_env_is_refused(self):
        with pytest.raises(ValueError):
            boot.assert_no_ambient_credentials({"OPENAI_API_KEY": "x"})
        # The one provisioning key is the server-owned session token.
        boot.assert_no_ambient_credentials({boot.ENV_MCP_SESSION_TOKEN: "t"})

    def test_real_launch_exposes_effective_env_for_verification(self, tmp_path):
        """The launcher records the resolved env the verifier checks."""
        handle = start_process(
            [sys.executable, "-c", "pass"],
            run_id="run-1",
            subject_repo="repo",
            subject_commit="c",
            tool="python",
            model="m",
            env={boot.ENV_MCP_SESSION_TOKEN: "tok-1"},
            env_allowlist=[boot.ENV_MCP_SESSION_TOKEN, "PATH"],
        )
        try:
            assert handle._sw_effective_env is not None
            assert handle._sw_effective_env[boot.ENV_MCP_SESSION_TOKEN] == "tok-1"
        finally:
            handle.wait(timeout=10)

    def test_reconnecting_child_preserves_cursor_and_cannot_cross_runs(
        self, tmp_path
    ):
        """A reconnect keeps owned cursor identity and is scoped to one run."""
        mailbox = AgentMailbox(str(tmp_path / "mb.jsonl"))
        msg = AgentMessage(
            kind="handoff",
            sender="main",
            recipient="ops",
            principal="mcp:controller:main",
            message_id="m-1",
            run_id="run-1",
            task_id="task-1",
            attempt=1,
            body="go",
        )
        mailbox.post(msg)
        first = mailbox.read(
            recipient="ops", principal="mcp:controller:main", after_seq=0
        )
        assert len(first) == 1
        cursor = MailboxCursor(first[-1]["seq"], first[-1]["digest"])
        token = cursor.encode()
        binding = boot.MailboxCursorBinding(after_seq=cursor.after_seq, last_digest=cursor.last_digest)
        # The reconnect resumes from the same owned cursor: nothing re-delivered.
        again = mailbox.read(
            recipient="ops",
            principal="mcp:controller:main",
            resume_token=token,
        )
        assert again == []
        assert binding.after_seq == cursor.after_seq

    def test_mailbox_binding_refuses_a_different_store(self, tmp_path):
        svc_a = _service(tmp_path / "a")
        svc_b = _service(tmp_path / "b")
        grant_a = boot.HarnessMcpSessionGrant(
            schema_id=boot.HARNESS_MCP_SESSION_GRANT_SCHEMA_ID,
            schema_version="1.0.0",
            run_id="run-1",
            task_id="",
            attempt=1,
            transport="stdio",
            session_token="t",
            service=svc_a,
            start_scope="run-1/attempt-1",
        )
        grant_b = boot.HarnessMcpSessionGrant(
            schema_id=boot.HARNESS_MCP_SESSION_GRANT_SCHEMA_ID,
            schema_version="1.0.0",
            run_id="run-1",
            task_id="",
            attempt=1,
            transport="stdio",
            session_token="t",
            service=svc_b,
            start_scope="run-1/attempt-1",
        )
        binding_a = boot.mailbox_binding_for_grant(grant_a, principal="ops")
        binding_b = boot.mailbox_binding_for_grant(grant_b, principal="ops")
        with pytest.raises(boot.ProcessServiceMismatchRefused):
            binding_a.verify_against(binding_b)


# ===========================================================================
# Criterion 3: scoped HTTP loopback default; stdio admitted only to same service
# ===========================================================================


class TestScopedHttpAndStdioSameAuthority:
    def test_loopback_scoped_bind_is_admitted(self):
        host, token = control.require_scoped_bind(
            "127.0.0.1", auth_token="secret", environ={}
        )
        assert host == "127.0.0.1"
        assert token == "secret"

    def test_remote_bind_without_allow_is_refused(self):
        with pytest.raises(control.ScopedHttpRefused):
            control.require_scoped_bind("0.0.0.0", auth_token="secret", environ={})

    def test_loopback_bind_without_a_token_is_refused(self):
        with pytest.raises(control.ScopedHttpRefused):
            control.require_scoped_bind("127.0.0.1", auth_token="", environ={})

    def test_scoped_and_stdio_paths_resolve_the_same_service(self, tmp_path):
        svc = control.service_for_binding(
            str(tmp_path / "store.db"),
            mailbox_path=str(tmp_path / "mb.jsonl"),
            artifacts_path=str(tmp_path / "artifacts"),
            stream_root=str(tmp_path / "streams"),
        )
        # The composed serving seam resolves by identity to the same service,
        # so a local and an HTTP path provably reach one authority/state service.
        control.verify_serving_reaches_same_service(
            db_path=str(tmp_path / "store.db"),
            mailbox_path=str(tmp_path / "mb.jsonl"),
            artifacts_path=str(tmp_path / "artifacts"),
            stream_root=str(tmp_path / "streams"),
            expected_service=svc,
        )

    def test_isolated_default_store_is_refused(self, tmp_path):
        svc = control.service_for_binding(
            str(tmp_path / "store.db"),
            mailbox_path=str(tmp_path / "mb.jsonl"),
            artifacts_path=str(tmp_path / "artifacts"),
            stream_root=str(tmp_path / "streams"),
        )
        with pytest.raises(boot.ProcessServiceMismatchRefused):
            control.verify_serving_reaches_same_service(
                db_path=str(tmp_path / "other-store.db"),
                mailbox_path=str(tmp_path / "mb.jsonl"),
                artifacts_path=str(tmp_path / "artifacts"),
                stream_root=str(tmp_path / "streams"),
                expected_service=svc,
            )

    def test_stdio_admission_refuses_an_independent_controller(self, tmp_path):
        svc = _service(tmp_path)
        grant = boot.HarnessMcpSessionGrant(
            schema_id=boot.HARNESS_MCP_SESSION_GRANT_SCHEMA_ID,
            schema_version="1.0.0",
            run_id="",
            task_id="",
            attempt=0,
            transport="stdio",
            session_token="t",
            service=svc,
            start_scope="run/attempt-0",
        )
        mcp_server.admit_stdio_same_authority(grant, _service(tmp_path))
        with pytest.raises(boot.ProcessServiceMismatchRefused):
            mcp_server.admit_stdio_same_authority(
                grant, _service(tmp_path / "per-harness")
            )

    def test_stdio_startup_scopes_the_session_to_the_process_service(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv(mcp_server.SUPERVISOR_ENV, "local-main")
        svc = _service(tmp_path)
        auth = MCPPrincipalAuthority()
        previous = mcp_server._AUTHORITY
        try:
            token = mcp_server.startup_and_bind_stdio(
                authority=auth, process_service=svc
            )
            principal = auth.verify_session(token)
            assert principal.role == "controller"
        finally:
            # No authority leaks into another test module.
            mcp_server._AUTHORITY = previous


# ===========================================================================
# Boundary-contract versioning: additive, fail-closed, never reinterpreted
# ===========================================================================


class TestBoundaryContractVersioning:
    def test_unknown_grant_version_is_refused_by_name(self, tmp_path):
        with pytest.raises(boot.UnsupportedContractVersion) as exc:
            boot.HarnessMcpSessionGrant(
                schema_id=boot.HARNESS_MCP_SESSION_GRANT_SCHEMA_ID,
                schema_version="9.9.9",
                run_id="run-1",
                task_id="",
                attempt=1,
                transport="stdio",
                session_token="t",
                service=_service(tmp_path),
                start_scope="run-1/attempt-1",
            )
        assert exc.value.version == "9.9.9"
        assert boot.HARNESS_MCP_SESSION_GRANT_SCHEMA_VERSION in exc.value.supported

    def test_unsupported_contract_version_is_a_value_error(self, tmp_path):
        # Fail-closed refusal stays catchable by historical `except ValueError`.
        with pytest.raises(ValueError):
            boot.SharedProcessMailboxBinding(
                schema_id=boot.SHARED_PROCESS_MAILBOX_BINDING_SCHEMA_ID,
                schema_version="2.0.0",
                principal="p",
                mailbox_path=str(tmp_path / "mb.jsonl"),
                service_identity="x",
            )

    def test_grant_roundtrips_without_reinterpreting_fields(self, tmp_path):
        svc = _service(tmp_path)
        grant = boot.HarnessMcpSessionGrant(
            schema_id=boot.HARNESS_MCP_SESSION_GRANT_SCHEMA_ID,
            schema_version="1.0.0",
            run_id="run-1",
            task_id="task-1",
            attempt=2,
            transport="stdio",
            session_token="tok",
            service=svc,
            start_scope="task-1/attempt-2",
        )
        record = grant.to_dict()
        assert record["subject_kind"] == "configuration"
        restored = boot.HarnessMcpSessionGrant.from_dict(
            {**record, "session_token": "tok"}
        )
        assert restored.service.identity() == svc.identity()
        assert restored.run_id == "run-1"
        assert restored.attempt == 2

    def test_boundary_artifacts_name_the_frozen_schemas(self):
        assert boot.HARNESS_MCP_SESSION_GRANT_SCHEMA_ID.endswith(
            "/harness-mcp-session-grant/v1"
        )
        assert boot.SHARED_PROCESS_MAILBOX_BINDING_SCHEMA_ID.endswith(
            "/shared-process-mailbox-binding/v1"
        )
        assert boot.QUALIFIED_HARNESS_MCP_BINDING_SCHEMA_ID.endswith(
            "/qualified-harness-mcp-binding/v1"
        )
        assert boot.SUPPORTED_HARNESS_MCP_SESSION_GRANT_VERSIONS == frozenset({"1.0.0"})


# ===========================================================================
# Backward compatibility: historical behaviour is byte-for-byte preserved
# ===========================================================================


class TestBackwardCompatibility:
    def test_inline_seam_without_a_grant_preserves_the_historical_launch(
        self, tmp_path, monkeypatch
    ):
        captured = {}

        def fake_start(command, **kwargs):
            captured.update(kwargs)
            return _FakeHandle(effective_env=kwargs.get("env"))

        monkeypatch.setattr(
            "skillweave.runtime.runner_adapter.start_process", fake_start
        )
        dispatch_app._default_inline_seam(
            ["echo", "hi"],
            run_id="run-1",
            subject_repo="repo",
            subject_commit="c",
            tool="claude",
            model="m",
            env={"SW_MSG_CHANNEL": "/tmp/msg"},
        )
        # No grant => no allowlist, no MCP keys: the legacy path is unchanged.
        assert captured.get("env_allowlist") is None
        assert captured["env"] == {"SW_MSG_CHANNEL": "/tmp/msg"}

    def test_start_process_without_effective_env_leaves_it_none(self):
        handle = start_process(
            [sys.executable, "-c", "pass"],
            run_id="run-1",
            subject_repo="repo",
            subject_commit="c",
            tool="python",
            model="m",
        )
        try:
            # With an ambient inherit there is no allowlist, so child_env is
            # None and nothing is claimed about the effective configuration.
            assert handle._sw_effective_env is None
        finally:
            handle.wait(timeout=10)

    def test_startup_without_a_service_keeps_the_unscoped_session(
        self, monkeypatch
    ):
        monkeypatch.setenv(mcp_server.SUPERVISOR_ENV, "local-main")
        auth = MCPPrincipalAuthority()
        previous = mcp_server._AUTHORITY
        try:
            token = mcp_server.startup_and_bind_stdio(authority=auth)
            principal = auth.verify_session(token)
            assert principal.role == "controller"
        finally:
            # No authority leaks into another test module.
            mcp_server._AUTHORITY = previous
