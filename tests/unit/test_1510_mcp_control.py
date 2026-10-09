"""SW-1510-MCP-CONTROL: the shared-service MCP control surface.

Three criteria are proven here against the real, shared
:class:`~skillweave.runsvc.service.RunApplicationService` and against the
importable shape of the *installed* product module (never a stand-in handler):

1. Validated start/inspect/cancel/resume/evidence operations return durable
   structured results from the *same* service the CLI drives, and idempotency
   keys are preserved end to end -- a replayed key returns the attempt already
   launched, not a duplicate.
2. A missing ``mcp`` dependency produces an *explicit* unavailable diagnostic;
   the in-tree dummy FastMCP fallback cannot advertise a working server (it
   refuses to serve) and the module's ``__main__`` exits non-zero.
3. ``stdio`` and scoped Streamable HTTP are described and *negotiated*;
   experimental MCP Tasks are negotiated only where the transport supports
   them, with ordinary asynchronous tools + poll cursors as the fallback.

The real ``mcp`` package is absent on this host, so the genuinely-served
transport proofs (which need it) skip rather than fabricate a transport, exactly
as the accepted integration suite does.

Stub vs live transport proof
----------------------------
``TestScopedHttpServingPath`` executes the *real* :func:`skillweave.mcp.control.serve`
code path against an injected fake FastMCP module (the module's injectable
``fastmcp_factory`` seam). Those assertions **pin the serving contract** -- the
exact ``run(transport='streamable-http')`` call, the host/port settings, the
loopback default and the pre-serve refusals -- and are **not** a live transport
proof. The live proofs (``*_transport_initialize_and_list``) speak the MCP
protocol to a real server and skip, naming the missing ``mcp`` package, when it
is not importable.
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

from skillweave.mcp import control  # noqa: E402
from skillweave.mcp import server as mcp_server  # noqa: E402


requires_real_mcp = pytest.mark.skipif(
    not control.mcp_available(),
    reason="real 'mcp' package not importable on this host",
)


def _child_env() -> dict:
    env = dict(os.environ)
    existing = [e for e in env.get("PYTHONPATH", "").split(os.pathsep) if e]
    if str(_SRC) not in existing:
        existing.insert(0, str(_SRC))
    env["PYTHONPATH"] = os.pathsep.join(existing)
    return env


def _free_loopback_port() -> int:
    import socket as _socket

    with _socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(port: int, *, timeout: float = 30.0) -> None:
    """Block until *port* accepts a TCP connection (readiness before connect).

    A scoped HTTP server binds asynchronously; a client that connects before it
    listens gets ``ConnectionError``. Poll instead of racing.
    """
    import socket as _socket
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        with _socket.socket() as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        _time.sleep(0.05)
    raise AssertionError(f"scoped HTTP server never listened on 127.0.0.1:{port}")


def _asgi_get(app, headers: dict) -> tuple:
    """Drive one ASGI ``GET`` through *app*, returning (status, headers, body).

    A minimal, dependency-free driver: it runs the ASGI app to completion on a
    fresh event loop and collects the response start/body messages. Starlette
    is the only dependency and ships with ``mcp``; no live socket is opened.
    """
    import asyncio as _asyncio

    raw = [
        (k.lower().encode("latin-1"), v.encode("latin-1"))
        for k, v in headers.items()
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "root_path": "",
        "headers": raw,
        "server": ("127.0.0.1", 8765),
        "client": ("127.0.0.1", 51234),
    }
    messages: list = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    loop = _asyncio.new_event_loop()
    try:
        loop.run_until_complete(app(scope, receive, send))
    finally:
        loop.close()

    status = 0
    out_headers: dict = {}
    body = b""
    for message in messages:
        if message["type"] == "http.response.start":
            status = message["status"]
            out_headers = {
                k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in message.get("headers", [])
            }
        elif message["type"] == "http.response.body":
            body += message.get("body", b"")
    return status, out_headers, body


class _FakeSettings:
    """The ``settings`` object a FastMCP-shaped server exposes."""

    def __init__(self) -> None:
        self.host = None
        self.port = None


class _FakeServer:
    """A FastMCP-shaped server that records how it was told to run.

    It implements exactly the surface :func:`control.serve` touches -- a
    ``tool()`` registration decorator, a ``settings`` object with host/port, and
    a ``run`` that records its call -- so the serving code is exercised
    unmodified.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.settings = _FakeSettings()
        self.registered: list = []
        self.run_calls: list = []

    def tool(self):
        def decorator(func):
            self.registered.append(func)
            return func

        return decorator

    def run(self, *args, **kwargs):
        self.run_calls.append((args, kwargs))


class _FakeFastMCPModule:
    """A stand-in for ``mcp.server.fastmcp`` injected via the factory seam."""

    def __init__(self) -> None:
        self.servers: list = []

    def FastMCP(self, name):  # noqa: N802 - mirrors the real attribute name
        server = _FakeServer(name)
        self.servers.append(server)
        return server


# ===========================================================================
# Criterion 1: durable structured results from the shared service; idempotency
# ===========================================================================


class TestDurableSharedServiceResults:
    def test_control_surface_returns_durable_results_from_the_shared_service(self, tmp_path):
        """The MCP control operations return the *service's* durable records."""
        db = str(tmp_path / "store.db")
        art = str(tmp_path / "artifacts")
        streams = str(tmp_path / "streams")

        # The control surface drives the shared service the CLI also drives.
        svc = control.get_run_service(db, art, streams)
        assert type(svc).__name__ == "RunApplicationService"

        payload = control.create_run_payload(
            tool="opencode",
            model="m",
            subject_repo="skillweave",
            subject_commit="d34dbeef" * 5,
            command=f"{sys.executable} -c print(4242)",
            db_path=db,
            artifacts_path=art,
            stream_root=streams,
            methodology="ralph",
            task_id="t-shared",
            attempt=1,
        )
        run_id = payload["run_id"]
        assert payload["stream"]["durable"] is True

        # Durable stream evidence landed on disk under the shared root.
        for name in ("frames.jsonl", "facts.jsonl", "cursor.json", "meta.json"):
            assert (Path(streams) / name).exists(), f"missing durable file {name}"

        # Inspect: the control status reads the same durable record service-side.
        status = control.control_status(run_id, db_path=db, artifacts_path=art)
        assert status["run_id"] == run_id
        assert status["task_id"] == "t-shared"
        assert status["methodology"] == "ralph"
        direct = control.get_run_service(db, art).run_snapshot(run_id)
        assert status == direct, "MCP status must equal the shared service snapshot"

        # Evidence: the ordered journal the service persisted is returned.
        ev = control.control_evidence(run_id, db_path=db)
        assert ev["run_id"] == run_id
        assert isinstance(ev["events"], list) and ev["events"]

        # Poll (the fallback async shape) advances a real cursor over the frames.
        poll = control.control_poll(run_id, db_path=db, artifacts_path=art, stream_root=streams)
        assert poll["execution_shape"] == "async_tool_poll_cursor"
        assert poll["frame_count"] >= 1
        assert poll["latest_cursor"] >= 1
        assert poll["frames"] == sorted(
            poll["frames"], key=lambda f: f["cursor"]
        )

    def test_mcp_status_agrees_with_cli_snapshot_on_one_run(self, tmp_path, capsys):
        """A CLI run and the MCP ``status_run`` read the *same* durable view."""
        from skillweave.cli import run as run_cli

        db = str(tmp_path / "store.db")
        art = str(tmp_path / "artifacts")
        streams = str(tmp_path / "streams")

        rc = run_cli.main([
            "--tool", "opencode", "--model", "m",
            "--subject-repo", "skillweave", "--subject-commit", "d34dbeef" * 5,
            "--methodology", "ralph", "--task-id", "t-agree", "--attempt", "1",
            "--db-path", db, "--stream-root", streams, "--artifacts-path", art,
            "--", sys.executable, "-c", "print(99)",
        ])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        run_id = payload["run_id"]

        mcp_status = json.loads(mcp_server.status_run(run_id, db_path=db))
        assert mcp_status["run_id"] == run_id
        assert mcp_status["task_id"] == "t-agree"
        # Both surfaces are reading the one shared snapshot: the CLI-reported
        # gate/state must equal what the MCP inspect returns.
        assert mcp_status["state"] == payload["state"]
        assert mcp_status["gate_state"] == payload["gate_state"]

    def test_mcp_cancel_and_resume_report_not_found_for_unknown_run(self):
        assert json.loads(mcp_server.cancel_run("invalid-run", "test", db_path=":memory:")) == {
            "error": "Run not found"
        }
        assert json.loads(mcp_server.resume_run("invalid-run", db_path=":memory:")) == {
            "error": "Run not found"
        }


class TestIdempotencyKeysPreserved:
    def test_resume_idempotency_key_replays_the_same_attempt(self, tmp_path):
        from skillweave.runlifecycle.handle import start_async, restart_attempt

        def _sleep(seconds=30):
            return [sys.executable, "-c", f"import time; time.sleep({seconds})"]

        source = start_async(_sleep(), project_root=tmp_path)
        new_pid = None
        try:
            first = restart_attempt(
                source.run_id, _sleep(), project_root=tmp_path,
                idempotency_key="k-shared",
            )
            new_pid = first["pid"]
            assert first["restarted"] is True
            assert first["idempotent_replay"] is False
            assert first["run_id"] != source.run_id

            replay = restart_attempt(
                source.run_id, _sleep(), project_root=tmp_path,
                idempotency_key="k-shared",
            )
            # The *same* launch is returned: no duplicate process, same id/pid.
            assert replay["run_id"] == first["run_id"]
            assert replay["pid"] == first["pid"]
            assert replay["idempotent_replay"] is True
        finally:
            import os as _os
            import signal as _signal

            for pid in (source.pid, new_pid):
                if pid:
                    try:
                        _os.killpg(pid, _signal.SIGKILL)
                    except OSError:
                        pass

    def test_mcp_resume_carries_idempotency_key_to_the_service(self, tmp_path):
        """``control_resume`` forwards the key; the service de-duplicates on it."""
        from skillweave.runlifecycle.handle import start_async

        def _sleep(seconds=30):
            return [sys.executable, "-c", f"import time; time.sleep({seconds})"]

        source = start_async(_sleep(), project_root=tmp_path)
        new_pid = None
        db = str(tmp_path / ".skillweave" / "store.db")
        try:
            cmd = " ".join(_sleep())
            first = control.control_resume(
                source.run_id, command=cmd, idempotency_key="k-mcp", db_path=db
            )
            assert first["status"] == "restarted"
            assert first["restart_kind"] == "new_identified_attempt"
            new_pid = first["pid"]
            assert first["pid"] != source.pid

            replay = control.control_resume(
                source.run_id, command=cmd, idempotency_key="k-mcp", db_path=db
            )
            assert replay["run_id"] == first["run_id"]
            assert replay["pid"] == first["pid"]
            assert replay["idempotent_replay"] is True
        finally:
            import os as _os
            import signal as _signal

            for pid in (source.pid, new_pid):
                if pid:
                    try:
                        _os.killpg(pid, _signal.SIGKILL)
                    except OSError:
                        pass

    def test_shared_service_cancel_refusal_is_structured_not_fabricated(self, tmp_path):
        """A refused cancel returns the verification reason; nothing claims a kill."""
        from skillweave.runlifecycle.handle import start_async

        def _sleep(seconds=30):
            return [sys.executable, "-c", f"import time; time.sleep({seconds})"]

        handle = start_async(_sleep(), project_root=tmp_path)
        db = str(tmp_path / ".skillweave" / "store.db")
        try:
            # Tamper the recorded native identity to a foreign worker (test-only).
            from skillweave.runlifecycle.store import RunStateStore

            store = RunStateStore(tmp_path)
            record = store._kernel.get_run(handle.run_id)
            identity = dict((record.metadata or {}).get("process_identity") or {})
            identity["exec_path"] = "/bin/sh"
            store._kernel.update_metadata(
                handle.run_id,
                expected_version=record.version,
                metadata={"process_identity": identity},
            )

            result = control.control_cancel(handle.run_id, db_path=db)
            assert "error" in result
            assert result["reason"] == "foreign_worker"
            # Not fabricated: the run is not reported killed by a refusal.
            assert control._get_store(db).get_run(handle.run_id).state != "killed"
        finally:
            import os as _os
            import signal as _signal

            try:
                _os.killpg(handle.pid, _signal.SIGKILL)
            except OSError:
                pass


# ===========================================================================
# Criterion 1 (versioned surface): opt-in run-snapshot-v1, unknown refused
# ===========================================================================


class TestVersionedSnapshotSurface:
    def test_supported_snapshot_version_is_declared_and_default(self):
        assert control.RUN_SNAPSHOT_VERSION == "run-snapshot-v1"
        assert control.SUPPORTED_SNAPSHOT_VERSIONS == frozenset({"run-snapshot-v1"})

    def test_snapshot_v1_refuses_unknown_version(self, tmp_path):
        # The refusal is *returned* as a structured error, never raised across
        # the tool boundary, and names the versions actually supported.
        result = control.snapshot_v1(
            "any-run", version="run-snapshot-v9",
            db_path=str(tmp_path / "store.db"),
            artifacts_path=str(tmp_path / "artifacts"),
        )
        assert result["code"] == "unsupported_version"
        assert result["run_id"] == "any-run"
        assert result["version"] == "run-snapshot-v9"
        assert result["supported"] == ["run-snapshot-v1"]
        assert "run-snapshot-v9" in result["error"]

    def test_snapshot_version_refused_is_still_constructible(self):
        # The typed refusal remains part of the surface (other modules raise it);
        # the tool boundary simply does not leak it.
        with pytest.raises(control.SnapshotVersionRefused) as exc:
            control._require_snapshot_version("run-snapshot-v9")
        assert "run-snapshot-v9" in str(exc.value)

    def test_service_refuses_unknown_version_by_name(self):
        from skillweave.runsvc import service as service_mod

        svc = control.get_run_service(":memory:")
        with pytest.raises(service_mod.RunSnapshotContractError) as exc:
            svc.run_snapshot_v1("any-run", version="run-snapshot-v2")
        assert exc.value.version == "run-snapshot-v2"
        assert "unsupported version" in exc.value.reason

    def test_snapshot_v1_is_frozen_fields_and_distinct_modes(self, tmp_path):
        db = str(tmp_path / "store.db")
        art = str(tmp_path / "artifacts")
        streams = str(tmp_path / "streams")
        payload = control.create_run_payload(
            tool="opencode", model="m", subject_repo="skillweave",
            subject_commit="d34dbeef" * 5,
            command=f"{sys.executable} -c print(1)",
            db_path=db, artifacts_path=art, stream_root=streams,
            methodology="ralph", autonomy_policy="medium",
            task_id="t-v1", attempt=1,
        )
        snap = control.snapshot_v1(payload["run_id"], db_path=db, artifacts_path=art)
        assert set(snap) == {
            "schema_version", "run_id", "version", "task_id", "attempt", "state",
            "gate_state", "methodology", "autonomy_policy", "session_schedule",
            "subject_digest", "brief_digest", "envelope_digest",
            "process_identity", "event_cursor", "authority_decision_digest",
        }
        # Three distinct mode fields, none conflated.
        assert snap["methodology"] == "ralph"
        assert snap["autonomy_policy"] == "medium"
        assert isinstance(snap["session_schedule"], str)
        assert snap["process_identity"]["handle_id"].endswith(payload["run_id"])

    def test_snapshot_v1_unknown_run_is_absent_not_fabricated(self, tmp_path):
        db = str(tmp_path / "store.db")
        result = control.snapshot_v1(
            "no-such-run", db_path=db, artifacts_path=str(tmp_path / "artifacts")
        )
        assert result == {
            "error": "Run not found", "code": "not_found",
            "run_id": "no-such-run", "version": control.RUN_SNAPSHOT_VERSION,
            "supported": [],
        }


# ===========================================================================
# Criterion 1 (first run): a fresh project cwd creates its store, never raises
# ===========================================================================


class TestFirstRunFromEmptyCwd:
    """The control surface creates its store on first use, mirroring the CLI.

    A fresh project has no ``.skillweave`` directory. The CLI ingress creates
    it; the control surface must too, or every start/inspect call raises
    ``sqlite3.OperationalError: unable to open database file``.
    """

    def test_get_run_service_creates_the_store_from_an_empty_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert not (tmp_path / ".skillweave").exists()

        svc = control.get_run_service()  # defaults -> .skillweave/store.db
        assert type(svc).__name__ == "RunApplicationService"
        # The parent directory (and the db file) now exist on disk.
        assert (tmp_path / ".skillweave").is_dir()
        assert (tmp_path / ".skillweave" / "store.db").exists()

    def test_control_status_from_an_empty_cwd_is_a_durable_read_not_a_crash(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        # No store existed a moment ago; the inspect must create it and return
        # the durable not-found record -- not raise a sqlite error.
        result = control.control_status("no-such-run")
        assert result == {
            "error": "Run not found", "code": "not_found",
            "run_id": "no-such-run", "version": "", "supported": [],
        }
        assert (tmp_path / ".skillweave" / "store.db").exists()

    def test_create_run_from_an_empty_cwd_succeeds(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert not (tmp_path / ".skillweave").exists()

        payload = control.create_run_payload(
            tool="opencode",
            model="m",
            subject_repo="skillweave",
            subject_commit="d34dbeef" * 5,
            command=f"{sys.executable} -c print(7)",
            methodology="ralph",
            task_id="t-first-run",
            attempt=1,
        )
        assert "error" not in payload, payload
        assert payload["run_id"]
        # The durable locations were created under the fresh cwd: the run store
        # (its parent directory made here) and the durable stream root (which the
        # stream store creates itself). The raw artifact store is memory-backed,
        # so no ``.skillweave/artifacts`` directory is written for a first run.
        assert (tmp_path / ".skillweave" / "store.db").exists()
        assert (tmp_path / ".skillweave" / "streams").is_dir()
        assert not (tmp_path / ".skillweave" / "artifacts").exists()


# ===========================================================================
# Criterion 1 (structured refusals): a contract refusal is returned, not raised
# ===========================================================================


class TestStructuredContractRefusals:
    """A contract refusal crosses the tool boundary as a structured dict.

    The frozen ``run-snapshot-v1`` enum
    (:data:`skillweave.mcp.control.SUPPORTED_SNAPSHOT_VERSIONS`) is deliberately
    *not* widened to accommodate a run; a run whose trusted fields cannot be
    expressed is refused by name and that refusal is returned.
    """

    def _collaborative_run(self, db, art, streams):
        """A default-policy (``collaborative``) run -- unrepresentable in v1."""
        payload = control.create_run_payload(
            tool="opencode", model="m", subject_repo="skillweave",
            subject_commit="d34dbeef" * 5,
            command=f"{sys.executable} -c print(1)",
            db_path=db, artifacts_path=art, stream_root=streams,
            methodology="ralph", autonomy_policy="",  # -> 'collaborative'
            task_id="t-collab", attempt=1,
        )
        return payload["run_id"]

    def test_status_unknown_version_returns_structured_refusal(self, tmp_path):
        result = control.control_status(
            "any-run", version="run-snapshot-v9",
            db_path=str(tmp_path / "store.db"),
            artifacts_path=str(tmp_path / "artifacts"),
        )
        assert result == {
            "error": result["error"], "code": "unsupported_version",
            "run_id": "any-run", "version": "run-snapshot-v9",
            "supported": ["run-snapshot-v1"],
        }
        assert "run-snapshot-v9" in result["error"]

    def test_status_unrepresentable_run_returns_structured_refusal(self, tmp_path):
        db = str(tmp_path / "store.db")
        art = str(tmp_path / "artifacts")
        streams = str(tmp_path / "streams")
        run_id = self._collaborative_run(db, art, streams)

        result = control.control_status(
            run_id, version="run-snapshot-v1", db_path=db, artifacts_path=art
        )
        assert result["code"] == "snapshot_unavailable"
        assert result["run_id"] == run_id
        assert result["version"] == "run-snapshot-v1"
        assert result["supported"] == ["run-snapshot-v1"]
        # The refusal is *named*, not fabricated into a snapshot.
        assert "schema_version" not in result

    def test_frozen_mapping_is_not_widened_for_collaborative(self, tmp_path):
        # The mapping itself stays frozen: 'collaborative' has no v1 enum slot.
        from skillweave.runsvc import service as service_mod

        assert service_mod.SUPPORTED_RUN_SNAPSHOT_VERSIONS == frozenset(
            {"run-snapshot-v1"}
        )
        db = str(tmp_path / "store.db")
        art = str(tmp_path / "artifacts")
        streams = str(tmp_path / "streams")
        run_id = self._collaborative_run(db, art, streams)
        svc = control.get_run_service(db, art)
        # The raw service still refuses by name -- the surface only *returns* it.
        with pytest.raises(service_mod.RunSnapshotContractError):
            svc.run_snapshot_v1(run_id, version="run-snapshot-v1")

    def test_legacy_status_without_version_still_returns_the_snapshot(self, tmp_path):
        # A 'collaborative' run is readable through the unversioned legacy read.
        db = str(tmp_path / "store.db")
        art = str(tmp_path / "artifacts")
        streams = str(tmp_path / "streams")
        run_id = self._collaborative_run(db, art, streams)
        status = control.control_status(run_id, db_path=db, artifacts_path=art)
        assert status["run_id"] == run_id
        assert status["autonomy_policy"] == "collaborative"

    def test_cancel_and_resume_not_found_are_structured(self, tmp_path):
        # Explicit tmp paths so the test never writes into the repo cwd.
        db = str(tmp_path / "store.db")
        art = str(tmp_path / "artifacts")
        cancel = control.control_cancel("ghost", db_path=db, artifacts_path=art)
        assert cancel["code"] == "not_found"
        assert cancel["error"] == "Run not found"
        resume = control.control_resume("ghost", db_path=db, artifacts_path=art)
        assert resume["code"] == "not_found"


# ===========================================================================
# Criterion 2: explicit unavailable diagnostic; dummy cannot advertise a server
# ===========================================================================


class TestDependencyHonesty:
    def test_dummy_fallback_cannot_advertise_a_working_server(self):
        # On this host the placeholder is active; on a host with `mcp` the real
        # server is. Either way the *placeholder* must never claim to serve.
        degraded = mcp_server._DegradedFastMCP(control.SERVER_NAME)
        assert degraded.can_serve is False
        assert degraded.degradation_reason == control.DEGRADED_REASON
        with pytest.raises(control.McpDependencyUnavailable):
            degraded.run()
        # The placeholder does not expose a FastMCP-style tool listing, so a
        # caller cannot read it as a live registry of served tools.
        assert not hasattr(degraded, "list_tools")

    def test_module_level_server_state_is_truthful(self):
        if control.mcp_available():
            # A real FastMCP has no ``can_serve`` flag (it serves by construction).
            assert not hasattr(mcp_server.mcp, "can_serve")
        else:
            assert mcp_server.mcp.can_serve is False
            assert mcp_server.mcp.degradation_reason == control.DEGRADED_REASON

    def test_require_mcp_raises_explicit_diagnostic(self):
        if control.mcp_available():
            assert control.require_mcp() is not None
            return
        with pytest.raises(control.McpDependencyUnavailable) as exc:
            control.require_mcp()
        assert "mcp" in exc.value.reason
        assert "refuses to serve" in exc.value.reason

    def test_unavailable_payload_is_structured_and_durable(self):
        payload = control.unavailable_payload()
        assert payload["available"] is False
        assert payload["status"] == "unavailable"
        assert payload["reason"] == control.DEGRADED_REASON
        assert payload["legacy_tools_preserved"] == list(control.LEGACY_TOOLS)

    def test_serve_refuses_when_dependency_missing(self):
        if control.mcp_available():
            pytest.skip("real mcp present; refusal path is host-specific")
        # The scoped HTTP case supplies a valid loopback bind + token so the
        # *dependency* refusal (not a bind refusal) is what is exercised.
        for call in (
            lambda: control.serve_legacy_stdio(),
            lambda: control.serve(transport="stdio"),
            lambda: control.serve(
                transport="http", host="127.0.0.1", port=8765, auth_token="tok"
            ),
        ):
            with pytest.raises(control.McpDependencyUnavailable):
                call()

    def test_main_refuses_and_dunder_main_exits_nonzero(self, tmp_path):
        # ``main`` refuses with the diagnostic instead of no-op'ing.
        if not control.mcp_available():
            with pytest.raises(control.McpDependencyUnavailable):
                mcp_server.main()

            # Running the module as __main__ exits non-zero with the diagnostic
            # on stderr -- proven in a real fresh interpreter.
            proc = subprocess.run(
                [sys.executable, "-B", "-m", "skillweave.mcp.server"],
                capture_output=True, text=True, env=_child_env(),
                cwd=str(tmp_path), timeout=60,
            )
            assert proc.returncode != 0
            assert "unavailable" in proc.stderr.lower()
            assert "mcp" in proc.stderr.lower()

    def test_installed_module_import_shape_is_real(self, tmp_path):
        """The module imports and its six tools are the *installed* callables.

        Proven in a fresh interpreter (not this test process's sys.modules): the
        legacy command tools exist, are bound to this module, and the module
        exposes a server object carrying the product name.
        """
        probe = (
            "import json\n"
            "from skillweave.mcp import server\n"
            "print(json.dumps({\n"
            "  'name': getattr(server.mcp, 'name', None),\n"
            "  'tools': [n for n in ['create_run','status_run','cancel_run',"
            "'resume_run','get_evidence','review_run'] if callable(getattr(server, n, None))],\n"
            "  'server_file': server.__file__,\n"
            "}))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-B", "-c", probe],
            capture_output=True, text=True, env=_child_env(),
            cwd=str(tmp_path), timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        info = json.loads(proc.stdout.strip().splitlines()[-1])
        assert info["name"] == control.SERVER_NAME
        assert info["tools"] == [
            "create_run", "status_run", "cancel_run",
            "resume_run", "get_evidence", "review_run",
        ]
        # The installed origin is this checkout's src tree, not a stand-in.
        assert info["server_file"].endswith("skillweave/mcp/server.py")


# ===========================================================================
# Criterion 3: the scoped Streamable HTTP *serving path* (stub-pinned)
# ===========================================================================


class TestScopedHttpServingPath:
    """Execute ``control.serve`` itself and pin what it would run.

    These tests drive the real :func:`control.serve` through its documented
    ``fastmcp_factory`` seam with a fake FastMCP module. They pin the serving
    *contract* -- the transport argument, bind settings and refusals. They are
    **not** a live transport proof; the live proof is
    ``TestTransportNegotiation.test_real_scoped_http_*`` which skips without the
    real ``mcp`` package.
    """

    def test_scoped_http_runs_streamable_http_with_settings(self):
        module = _FakeFastMCPModule()
        control.serve(
            "http", host="127.0.0.1", port=8765, auth_token="tok",
            fastmcp_factory=lambda: module,
        )
        assert len(module.servers) == 1, "serve must build exactly one server"
        server = module.servers[0]
        # The exact call the scoped transport makes: streamable-http.
        assert server.run_calls == [((), {"transport": "streamable-http"})]
        # Host/port are taken from the scoped settings, not left undefined.
        assert server.settings.host == "127.0.0.1"
        assert server.settings.port == 8765
        # The server carries the product name and the six legacy tools.
        assert server.name == control.SERVER_NAME
        assert [fn.__name__ for fn in server.registered] == list(control.LEGACY_TOOLS)

    def test_scoped_http_default_bind_is_loopback(self):
        """With no host/port named, the default bind is loopback, never wildcard."""
        module = _FakeFastMCPModule()
        control.serve("http", auth_token="tok", fastmcp_factory=lambda: module)
        server = module.servers[0]
        assert server.settings.host == control.DEFAULT_HTTP_HOST == "127.0.0.1"
        assert server.settings.port == control.DEFAULT_HTTP_PORT
        assert server.run_calls == [((), {"transport": "streamable-http"})]

    def test_non_loopback_bind_refused_without_opt_in(self):
        module = _FakeFastMCPModule()
        with pytest.raises(control.ScopedHttpRefused) as exc:
            control.serve(
                "http", host="0.0.0.0", auth_token="tok",
                fastmcp_factory=lambda: module,
            )
        assert "non-loopback" in exc.value.reason
        # Refused before any server is built: the factory was never invoked.
        assert module.servers == []

    def test_non_loopback_bind_allowed_only_with_explicit_opt_in(self):
        module = _FakeFastMCPModule()
        control.serve(
            "http", host="0.0.0.0", port=9000, auth_token="tok", allow_remote=True,
            fastmcp_factory=lambda: module,
        )
        server = module.servers[0]
        assert server.settings.host == "0.0.0.0"
        assert server.settings.port == 9000

    def test_scope_token_required_before_serving(self, monkeypatch):
        monkeypatch.delenv(control.HTTP_AUTH_ENV, raising=False)
        module = _FakeFastMCPModule()
        with pytest.raises(control.ScopedHttpRefused) as exc:
            control.serve(
                "http", host="127.0.0.1", port=8765,
                fastmcp_factory=lambda: module,
            )
        assert "scope token" in exc.value.reason
        assert module.servers == [], "no server may be built without a scope token"

    def test_scope_token_comes_from_environment(self, monkeypatch):
        monkeypatch.setenv(control.HTTP_AUTH_ENV, "env-token")
        module = _FakeFastMCPModule()
        control.serve("http", host="127.0.0.1", port=8765,
                      fastmcp_factory=lambda: module)
        assert len(module.servers) == 1

    def test_require_scoped_bind_returns_effective_host_and_token(self, monkeypatch):
        monkeypatch.delenv(control.HTTP_AUTH_ENV, raising=False)
        host, token = control.require_scoped_bind("", auth_token="arg-token")
        assert host == control.DEFAULT_HTTP_HOST
        assert token == "arg-token"

    def test_stdio_serving_path_does_not_request_http(self):
        module = _FakeFastMCPModule()
        control.serve("stdio", fastmcp_factory=lambda: module)
        server = module.servers[0]
        # stdio runs the bare server: no streamable-http transport argument.
        assert server.run_calls == [((), {})]

    def test_serve_scoped_http_wrapper_serves_http(self):
        module = _FakeFastMCPModule()
        control.serve_scoped_http(
            host="127.0.0.1", port=8765, auth_token="tok",
            fastmcp_factory=lambda: module,
        )
        assert module.servers[0].run_calls == [
            ((), {"transport": "streamable-http"})
        ]

    def test_scoped_serve_refuses_when_dependency_missing(self, monkeypatch):
        """With a valid bind but no injectable factory, the missing dep refuses."""
        if control.mcp_available():
            pytest.skip("real mcp present; refusal path is host-specific")
        with pytest.raises(control.McpDependencyUnavailable):
            control.serve("http", host="127.0.0.1", port=8765, auth_token="tok")


# ===========================================================================
# Criterion 3: the scope token is enforced on EVERY HTTP request (per-request)
# ===========================================================================


class _FakeStreamableServer(_FakeServer):
    """A FastMCP-shaped server that also exposes ``streamable_http_app``."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.streamable_calls = 0

        async def _app(scope, receive, send):
            # A stand-in for the MCP app: any request that reaches it "executes".
            self.reached = getattr(self, "reached", 0) + 1
            await send(
                {"type": "http.response.start", "status": 200, "headers": []}
            )
            await send({"type": "http.response.body", "body": b"mcp"})

        self._app = _app

    def streamable_http_app(self, *args, **kwargs):  # noqa: D401 - mirrors real
        self.streamable_calls += 1
        return self._app


class _FakeStreamableModule:
    """A ``mcp.server.fastmcp`` stand-in whose servers expose the HTTP app."""

    def __init__(self) -> None:
        self.servers: list = []

    def FastMCP(self, name):  # noqa: N802 - mirrors the real attribute name
        server = _FakeStreamableServer(name)
        self.servers.append(server)
        return server


class TestScopedHttpPerRequestEnforcement:
    """The scope token authenticates *requests*, not merely the bind.

    These drive the real ASGI request guard via a dependency-free ASGI driver:
    no token executes the MCP app; the good token does, with its authenticated
    principal attached. The live end-to-end proof (a real server over a real
    socket) is ``TestTransportNegotiation.test_real_scoped_http_*`` and skips
    without the real ``mcp`` package.
    """

    @pytest.fixture(autouse=True)
    def _isolate_http_globals(self):
        """No test leaks the process-wide HTTP authority or request context."""
        previous_authority = control._HTTP_AUTHORITY
        control._HTTP_AUTHORITY = None
        try:
            yield
        finally:
            control._HTTP_AUTHORITY = previous_authority
            control._CURRENT_HTTP_PRINCIPAL.set(None)
            control._CURRENT_HTTP_TOKEN.set("")

    @pytest.fixture
    def bound_authority(self):
        from skillweave.runtime.authority import MCPPrincipalAuthority

        auth = MCPPrincipalAuthority()
        auth.register_scope_token(
            "tok", client_id="http:127.0.0.1:8765", transport="http",
        )
        return auth

    def test_good_token_reaches_the_app_with_its_principal(self, bound_authority):
        reached = {}

        async def inner(scope, receive, send):
            reached["principal"] = control.current_http_principal()
            reached["token"] = control.current_http_token()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = control._make_http_auth_middleware(
            "tok", authority=bound_authority
        )
        status, _headers, body = _asgi_get(
            middleware(inner), {"Authorization": "Bearer tok"}
        )
        assert status == 200
        assert body == b"ok"
        # The authenticated principal (role read from the registry) is bound
        # for the call, and the presented token is visible to the tool gate.
        assert reached["principal"] is not None
        assert reached["principal"].role == "controller"
        assert reached["token"] == "tok"
        # No request in scope => no leaked principal/token across requests.
        assert control.current_http_principal() is None
        assert control.current_http_token() == ""

    def test_wrong_token_is_refused_before_any_mcp_handling(self, bound_authority):
        async def inner(scope, receive, send):  # pragma: no cover - must not run
            raise AssertionError("the MCP app must not be reached without a token")

        middleware = control._make_http_auth_middleware(
            "tok", authority=bound_authority
        )
        status, headers, body = _asgi_get(
            middleware(inner), {"Authorization": "Bearer WRONG"}
        )
        assert status == 401
        assert headers.get("www-authenticate") == "Bearer"
        # The refusal body echoes neither the presented nor the expected token.
        assert b"WRONG" not in body and b"tok" not in body

    def test_missing_token_is_refused_before_any_mcp_handling(self, bound_authority):
        async def inner(scope, receive, send):  # pragma: no cover - must not run
            raise AssertionError("the MCP app must not be reached without a token")

        middleware = control._make_http_auth_middleware(
            "tok", authority=bound_authority
        )
        status, headers, _body = _asgi_get(middleware(inner), {})
        assert status == 401
        assert headers.get("www-authenticate") == "Bearer"

    def test_a_bare_token_header_is_accepted(self, bound_authority):
        reached = {}

        async def inner(scope, receive, send):
            reached["ok"] = True
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = control._make_http_auth_middleware(
            "tok", authority=bound_authority
        )
        status, _headers, _body = _asgi_get(middleware(inner), {"Authorization": "tok"})
        assert status == 200 and reached.get("ok") is True

    def test_a_token_unknown_to_the_authority_is_refused(self):
        """Even a matching string is refused when the authority does not know it."""
        from skillweave.runtime.authority import MCPPrincipalAuthority

        empty = MCPPrincipalAuthority()  # knows no token

        async def inner(scope, receive, send):  # pragma: no cover - must not run
            raise AssertionError("an unknown token must not reach the app")

        middleware = control._make_http_auth_middleware("tok", authority=empty)
        status, _headers, _body = _asgi_get(
            middleware(inner), {"Authorization": "Bearer tok"}
        )
        assert status == 401

    def test_install_guard_wraps_the_streamable_http_app(self, bound_authority):
        module = _FakeStreamableModule()
        control.serve(
            "http", host="127.0.0.1", port=8765, auth_token="tok",
            fastmcp_factory=lambda: module,
        )
        server = module.servers[0]
        # The built server carries the request guard...
        assert callable(getattr(server, "http_auth_middleware", None))
        # ...and its ``streamable_http_app`` is wrapped so the running transport
        # cannot bypass it: a no-token request is refused with 401 and the
        # underlying MCP app is never reached.
        guarded = server.streamable_http_app()
        status, headers, _body = _asgi_get(guarded, {})
        assert status == 401
        assert headers.get("www-authenticate") == "Bearer"
        assert getattr(server, "reached", 0) == 0, "MCP app ran without a token"
        # The good token passes through to the MCP app exactly once.
        status2, _h2, body2 = _asgi_get(guarded, {"Authorization": "Bearer tok"})
        assert status2 == 200 and body2 == b"mcp"
        assert server.reached == 1


# ===========================================================================
# Criterion 3: stdio + scoped HTTP transports, experimental Tasks negotiation
# ===========================================================================


class TestTransportNegotiation:
    def test_stdio_profile_supports_tools_only(self):
        profile = control.transport_profile("stdio")
        assert profile.supported is True
        assert profile.scoped is False
        assert profile.experimental_tasks is False
        caps = control.server_capabilities(profile)
        assert caps == {"tools": {}}

    def test_scoped_http_profile_advertises_experimental_tasks(self):
        profile = control.transport_profile("http", host="127.0.0.1", port=8765)
        assert profile.supported is True
        assert profile.scoped is True
        assert profile.experimental_tasks is True
        assert profile.url.endswith("/mcp")
        caps = control.server_capabilities(profile)
        assert caps["tools"] == {}
        assert caps["experimental"]["tasks"] == {}

    def test_unknown_transport_is_refused_not_defaulted(self):
        profile = control.transport_profile("carrier-pigeon")
        assert profile.supported is False
        assert "unsupported transport" in profile.reason

    def test_tasks_negotiated_only_when_supported_and_requested(self):
        stdio = control.transport_profile("stdio")
        http = control.transport_profile("http")
        want_tasks = {"experimental": {"tasks": {}}}

        # stdio can never negotiate experimental Tasks -> fallback shape.
        n = control.negotiate_capabilities(want_tasks, stdio)
        assert n["tasks"]["negotiated"] is False
        assert n["execution_shape"] == "async_tool_poll_cursor"

        # http supports them but the client did not ask -> still fallback.
        n = control.negotiate_capabilities({}, http)
        assert n["tasks"]["negotiated"] is False
        assert n["execution_shape"] == "async_tool_poll_cursor"

        # http supports them and the client asked -> negotiated.
        n = control.negotiate_capabilities(want_tasks, http)
        assert n["tasks"]["negotiated"] is True
        assert n["tasks"]["experimental"] is True
        assert n["execution_shape"] == "experimental_tasks"
        # Never claims tasks for an unsupported transport.
        assert control.negotiate_capabilities(want_tasks, stdio)["tasks"]["supported"] is False

    def test_poll_cursor_is_the_declared_fallback_tool(self):
        specs = {s.name: s for s in control.control_tool_specs()}
        assert set(specs) == {"status", "evidence", "cancel", "resume", "poll"}
        assert specs["poll"].poll_cursor is True
        assert specs["poll"].async_tool is True
        schema = control.tool_schema(specs["poll"])
        assert schema["name"] == "skillweave/poll"
        assert schema["version"] == control.CONTROL_VERSION

    @requires_real_mcp
    def test_real_mcp_can_build_a_server_over_stdio(self):
        """Where the real ``mcp`` package exists, a FastMCP server is buildable."""
        built = control._build_server(control.require_mcp())
        assert built is not None
        assert getattr(built, "name", control.SERVER_NAME) == control.SERVER_NAME

    @requires_real_mcp
    def test_real_stdio_refuses_cleanly_without_a_supervisor_identity(self, tmp_path):
        """Start the REAL stdio server with no supervisor identity.

        Without ``SKILLWEAVE_MCP_SUPERVISOR`` the production stdio entry must
        refuse: a one-line actionable diagnostic on stderr and the documented
        non-zero exit code -- never a bare traceback. (Runs only where the real
        ``mcp`` package is importable; skipped with a named reason otherwise.)
        """
        env = _child_env()
        env.pop(mcp_server.SUPERVISOR_ENV, None)
        proc = subprocess.run(
            [sys.executable, "-B", "-m", "skillweave.mcp.server"],
            capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=60,
        )
        assert proc.returncode == mcp_server.EXIT_REFUSED
        assert mcp_server.SUPERVISOR_ENV in proc.stderr
        assert "supervisor" in proc.stderr.lower()
        # A friendly refusal, not a crash: no exception traceback.
        assert "Traceback" not in proc.stderr
        assert "refused" in proc.stderr.lower()

    def test_real_stdio_transport_initialize_and_list(self, tmp_path):
        """Drive the *real* product server over a real stdio MCP transport.

        This is not an in-process handler call: a subprocess client speaks the
        MCP protocol (``initialize`` -> ``tools/list``) to a subprocess server
        launched as ``python -m skillweave.mcp.server``. Skips when the real
        ``mcp`` package is unavailable, rather than fabricating a transport.
        """
        if not control.mcp_available():
            pytest.skip("real 'mcp' package not importable on this host")
        pytest.importorskip("mcp")
        driver = tmp_path / "stdio_driver.py"
        driver.write_text(
            "import asyncio, os\n"
            "from mcp import ClientSession\n"
            "from mcp.client.stdio import stdio_client, StdioServerParameters\n"
            "# SW-1510-MCP-AUTH: a real local stdio launcher names the explicit\n"
            "# supervisor identity the server now requires.\n"
            "server_env = dict(os.environ)\n"
            "server_env['SKILLWEAVE_MCP_SUPERVISOR'] = os.environ['SUP']\n"
            "params = StdioServerParameters(\n"
            "    command=os.environ['PY'], args=['-B', '-m', 'skillweave.mcp.server'],\n"
            "    env=server_env, cwd=os.environ['WORK'])\n"
            "async def main():\n"
            "    async with stdio_client(params) as (r, w):\n"
            "        async with ClientSession(r, w) as s:\n"
            "            init = await s.initialize()\n"
            "            tools = await s.list_tools()\n"
            "            print('INIT', init.serverInfo.name)\n"
            "            print('TOOLS', ','.join(sorted(t.name for t in tools.tools)))\n"
            "asyncio.run(main())\n"
        )
        env = _child_env()
        env["PY"] = sys.executable
        env["WORK"] = str(tmp_path)
        env["SUP"] = "supervisor:ops-stdio"
        proc = subprocess.run(
            [sys.executable, "-B", str(driver)],
            capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=120,
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        out = dict(
            ln.split(" ", 1) for ln in proc.stdout.splitlines() if " " in ln
        )
        assert out["INIT"] == control.SERVER_NAME
        assert out["TOOLS"] == (
            "cancel_run,create_run,get_evidence,resume_run,review_run,status_run"
        )

    def test_real_scoped_http_transport_initialize_list_and_call(self, tmp_path):
        """Drive the *real* product server over scoped Streamable HTTP.

        A subprocess serves :func:`control.serve_scoped_http` on a loopback port
        with a scope token; a subprocess client speaks MCP over Streamable HTTP
        (``initialize`` -> ``tools/list`` -> ``tools/call``). Skips -- naming the
        missing package -- when the real ``mcp`` package is not importable,
        rather than fabricating a transport.
        """
        if not control.mcp_available():
            pytest.skip("real 'mcp' package not importable on this host")
        pytest.importorskip("mcp")
        pytest.importorskip("mcp.client.streamable_http")
        port = _free_loopback_port()
        token = "test-scope-token"
        server_code = (
            "from skillweave.mcp import control\n"
            f"control.serve_scoped_http(host='127.0.0.1', port={port}, "
            f"auth_token={token!r})\n"
        )
        client_driver = tmp_path / "http_driver.py"
        client_driver.write_text(
            "import asyncio, os\n"
            "from mcp import ClientSession\n"
            "from mcp.client.streamable_http import streamablehttp_client\n"
            "URL = os.environ['URL']\n"
            "HEADERS = {'Authorization': 'Bearer ' + os.environ['TOK']}\n"
            "async def main():\n"
            "    async with streamablehttp_client(URL, headers=HEADERS) as (r, w, _):\n"
            "        async with ClientSession(r, w) as s:\n"
            "            init = await s.initialize()\n"
            "            tools = await s.list_tools()\n"
            "            res = await s.call_tool('status_run', {\n"
            "                'run_id': 'no-such-run', 'db_path': ':memory:'})\n"
            "            print('INIT', init.serverInfo.name)\n"
            "            print('TOOLS', ','.join(sorted(t.name for t in tools.tools)))\n"
            "            print('CALL', res.content[0].text)\n"
            "asyncio.run(main())\n"
        )
        env = _child_env()
        env["URL"] = f"http://127.0.0.1:{port}/mcp"
        env["TOK"] = token
        server = subprocess.Popen(
            [sys.executable, "-B", "-c", server_code],
            env=env, cwd=str(tmp_path),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            # The server binds asynchronously; poll for readiness before the
            # client connects, else the client races the listener and fails
            # with httpx.ConnectError.
            _wait_for_port(port)
            proc = subprocess.run(
                [sys.executable, "-B", str(client_driver)],
                capture_output=True, text=True, env=env, cwd=str(tmp_path),
                timeout=120,
            )
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=10)
        assert proc.returncode == 0, proc.stderr[-2000:]
        out = dict(
            ln.split(" ", 1) for ln in proc.stdout.splitlines() if " " in ln
        )
        assert out["INIT"] == control.SERVER_NAME
        assert out["TOOLS"] == (
            "cancel_run,create_run,get_evidence,resume_run,review_run,status_run"
        )
        assert "Run not found" in out["CALL"]

    def test_real_scoped_http_transport_enforces_token_per_request(self, tmp_path):
        """The scope token authenticates *every* request, not merely the bind.

        A real scoped HTTP server is started on a loopback port; a real HTTP
        client then sends three requests to ``/mcp``: with no token, with a
        wrong token and with the good token. The first two are refused with
        ``401`` and a ``WWW-Authenticate: Bearer`` challenge *before* any MCP
        handling (so no tool can execute -- the guard runs ahead of the MCP
        app, proven in-process by ``test_wrong_token_is_refused_before_any_mcp
        _handling``); the good token is admitted past the guard (the endpoint
        no longer answers ``401``). Skips -- naming the missing package -- when
        the real ``mcp`` package is not importable.
        """
        if not control.mcp_available():
            pytest.skip("real 'mcp' package not importable on this host")
        pytest.importorskip("mcp")
        port = _free_loopback_port()
        token = "test-scope-token"
        server_code = (
            "from skillweave.mcp import control\n"
            f"control.serve_scoped_http(host='127.0.0.1', port={port}, "
            f"auth_token={token!r})\n"
        )
        client_driver = tmp_path / "http_probe.py"
        client_driver.write_text(
            "import os\n"
            "import httpx\n"
            "URL = os.environ['URL']\n"
            "TOK = os.environ['TOK']\n"
            "with httpx.Client(timeout=10.0) as c:\n"
            "    none = c.post(URL, json={})\n"
            "    wrong = c.post(URL, json={}, headers={'Authorization': 'Bearer WRONG-TOKEN'})\n"
            "    good = c.post(URL, json={}, headers={'Authorization': 'Bearer ' + TOK})\n"
            "print('NONE', none.status_code, none.headers.get('www-authenticate', ''))\n"
            "print('WRONG', wrong.status_code, wrong.headers.get('www-authenticate', ''))\n"
            "print('GOOD', good.status_code)\n"
            "print('LEAK_NONE', TOK in none.text or 'WRONG-TOKEN' in none.text)\n"
            "print('LEAK_WRONG', TOK in wrong.text or 'WRONG-TOKEN' in wrong.text)\n"
        )
        env = _child_env()
        env["URL"] = f"http://127.0.0.1:{port}/mcp"
        env["TOK"] = token
        server = subprocess.Popen(
            [sys.executable, "-B", "-c", server_code],
            env=env, cwd=str(tmp_path),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            _wait_for_port(port)
            proc = subprocess.run(
                [sys.executable, "-B", str(client_driver)],
                capture_output=True, text=True, env=env, cwd=str(tmp_path),
                timeout=120,
            )
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=10)
        assert proc.returncode == 0, proc.stderr[-2000:]
        out = dict(
            ln.split(" ", 1) for ln in proc.stdout.splitlines() if " " in ln
        )
        # No token and a wrong token are both refused, with the challenge.
        assert out["NONE"].split()[0] == "401"
        assert out["NONE"].split()[1].lower() == "bearer"
        assert out["WRONG"].split()[0] == "401"
        assert out["WRONG"].split()[1].lower() == "bearer"
        # The good token is admitted past the guard (the endpoint no longer 401s).
        assert out["GOOD"] != "401"
        # No refusal echoes the presented or expected token.
        assert out["LEAK_NONE"] == "False"
        assert out["LEAK_WRONG"] == "False"
