"""SW-1510 composed native execution package: installed entry, protected triple,
and the real MCP transport, proven end to end.

This module closes the *composition* gap the source merge alone did not: the
authoritative installed native entry (``skillweave run``) and the MCP native
entry must drive the real durable streaming collector, and the shared
snapshot/replay view must agree across the CLI, MCP and a reconnect. It also
exercises the protected triple (authenticated worker + authenticated assay
actor + declared scope + a host-assigned attempt checkout + a durable store)
against a real child, and drives the actual product MCP server over a real
``stdio`` transport (initialize -> initialized -> tools/list -> call -> status),
not an in-process handler call against a stand-in server.

Host capability honesty
-----------------------
* The strongest write-isolation adapter this host offers is the *seal-only*
  BSD immutable-flag adapter (``uchg``); macOS Seatbelt (``sandbox-exec``)
  cannot apply inside this harness (``sandbox_apply: Operation not permitted``).
  A seal-only adapter locks the protected authority stores but does **not**
  confine an arbitrary process to the attempt checkout. Consequently a launch
  that *declares* a write scope must HOLD (SW-1510-SCOPE criterion 2), and the
  fully-confined positive branch is marked ``requires_confinement`` and runs
  only where such an adapter is available. The negative HOLD is asserted here.
* The real MCP transport is driven with the provisioned ``mcp`` interpreter
  (resolved from the environment, never from ambient ``sys.path``). When that
  provision or the accepted SDK is absent this module's relevant tests skip
  rather than fabricate a transport.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest  # noqa: E402

# The sequence root holding ``integrations/skillweave-sdk`` and the
# ``preflight/native-mcp-provision-*`` venv, derived from this file's location
# so the module carries no absolute machine paths.
_SEQ_ROOT = _SRC.parents[3]

try:  # noqa: SIM105 - a hard import error becomes a module-wide skip.
    from skillweave.runsvc import RunApplicationService, RunIntegrationError
    from skillweave.runtime.stream import DurableStreamStore
    from skillweave.runtime.snapshot import progress_frames

    _IMPORT_ERROR = None
except Exception as exc:  # noqa: BLE001
    _IMPORT_ERROR = exc

pytestmark = pytest.mark.skipif(
    _IMPORT_ERROR is not None,
    reason=f"native execution package imports unavailable: {_IMPORT_ERROR}",
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _assay_evidence import (  # noqa: E402
    genuine_artifact,
    reader_for,
    write_analysis_tree,
    write_source_tree,
)

#: A real, 40-hex subject commit the artifact proof and the source resolver
#: both bind to (SW-1510-VERIFY refuses a non-40-hex commit outright).
_COMMIT = "d34dbeefd34dbeefd34dbeefd34dbeefd34dbeef"


def _genuine_evidence(base, *, commit=_COMMIT):
    """A real artifact proof bound to an on-disk source tree + analysis bytes.

    SW-1510-VERIFY: the native Assay PASS consumer now requires artifact-specific
    semantics, so each assay positive control supplies a proof graph whose file
    and per-path analysis digests are recomputed from the bytes written here.
    """
    from skillweave.runtime.verify import FilesystemSourceResolver

    src = Path(base) / "evidence-src"
    ana = Path(base) / "evidence-analysis"
    files = write_source_tree(src)
    analysis = write_analysis_tree(ana, files)
    artifact = genuine_artifact(files, analysis, commit=commit)
    return {
        "artifact_evidence": [artifact],
        "artifact_json": json.dumps([artifact]),
        "source_resolver": FilesystemSourceResolver(src, commit=commit),
        "analysis_reader": reader_for(analysis),
        "commit": commit,
        "src": src,
        "ana": ana,
    }


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


@pytest.fixture(autouse=True)
def _scratch_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path), raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# Environment resolution: accepted SDK + provisioned real MCP interpreter
# ---------------------------------------------------------------------------


def _sdk_src():
    """The accepted SDK source root, or ``None`` when it is not present."""
    env = os.environ.get("SKILLWEAVE_SDK_SRC")
    candidates = ([Path(env)] if env else []) + [
        _SEQ_ROOT / "integrations" / "skillweave-sdk" / "src",
    ]
    for cand in candidates:
        if (cand / "skillweave_sdk").is_dir():
            return cand
    return None


def _mcp_python():
    """The provisioned interpreter that has the real ``mcp`` package."""
    env = os.environ.get("SKILLWEAVE_MCP_PY")
    if env and Path(env).exists():
        return env
    preflight = _SEQ_ROOT / "preflight"
    if preflight.is_dir():
        for provision in sorted(preflight.glob("native-mcp-provision-*")):
            for name in ("python3", "python"):
                cand = provision / "venv" / "bin" / name
                if cand.exists():
                    return str(cand)
    return None


def _real_adapter():
    from skillweave.runtime.worker_launcher import select_adapter

    return select_adapter()


requires_sdk = pytest.mark.skipif(
    _sdk_src() is None, reason="accepted skillweave_sdk not resolvable"
)
requires_mcp_venv = pytest.mark.skipif(
    _mcp_python() is None, reason="no provisioned real MCP interpreter"
)
_HAS_CHECKOUT_CONFINEMENT = bool(
    getattr(_real_adapter(), "confines_to_scope", False)
)
requires_confinement = pytest.mark.skipif(
    not _HAS_CHECKOUT_CONFINEMENT,
    reason="host adapter does not confine a process to the attempt checkout",
)


def _service(db_path, artifacts_path):
    from skillweave.runtime.journal import EventJournal
    from skillweave.runtime.registry import RawArtifactStore
    from skillweave.runtime.store import SQLiteRunStore

    store = SQLiteRunStore(db_path)
    return RunApplicationService(
        store, EventJournal(store), RawArtifactStore(artifacts_path)
    )


# ---------------------------------------------------------------------------
# 1. The installed native entry persists durable Assay/stream evidence, and the
#    shared replay view agrees with it.
# ---------------------------------------------------------------------------


def test_installed_cli_entry_persists_native_assay_and_replay_agrees(tmp_path, capsys):
    """``skillweave run`` (the installed native entry, exercised through the same
    ``main`` the console script dispatches to) must run the real bounded child
    under ``methodology=assay``, persist PIN/HYPOTHESIS/PROBE/MEASURE/CLASSIFY/
    outcome with run/task/attempt ids *and* durable per-attempt stream evidence,
    and agree with a later reconnect/replay.

    Before this lane's wiring fix the CLI called ``svc.execute`` with no stream
    store: the entry ran, but produced no ``frames.jsonl``/``facts.jsonl``/
    ``cursor.json`` at all. This asserts the durable files exist and that the
    ordered cursor replay reconstructs the same attempt.
    """
    from skillweave.cli import run as run_cli

    db = str(tmp_path / "cli.db")
    streams = str(tmp_path / "cli-streams")
    art = str(tmp_path / "cli-art")

    ev = _genuine_evidence(tmp_path / "cli-ev")
    evidence_file = tmp_path / "cli-evidence.json"
    evidence_file.write_text(ev["artifact_json"])

    rc = run_cli.main([
        "--tool", "opencode",
        "--model", "model-assay",
        "--subject-repo", "skillweave",
        "--subject-commit", ev["commit"],
        "--methodology", "assay",
        "--autonomy-policy", "unicorn",
        "--task-id", "task-native",
        "--attempt", "1",
        "--db-path", db,
        "--stream-root", streams,
        "--artifacts-path", art,
        "--artifact-evidence", str(evidence_file),
        "--source-root", str(ev["src"]),
        "--analysis-root", str(ev["ana"]),
        "--",
        sys.executable, "-c", "print('native-entry-ok')",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    run_id = payload["run_id"]

    assert payload["methodology"] == "assay"
    assert payload["assay"]["task_id"] == "task-native"
    assert payload["assay"]["outcome"] == "pass"
    # The genuine artifact proof is what the native entry admitted.
    assert payload["assay"]["artifact_evidence"]["admitted"] is True
    assert [p["phase"] for p in payload["assay"]["phases"]] == [
        "pin", "hypothesis", "probe", "measure", "classify", "artifact_verify", "pass",
    ]
    # The durable collector the CLI now drives left real evidence on disk.
    assert payload["stream"]["durable"] is True
    assert payload["stream"]["cursor"] >= 4
    root = Path(streams)
    for name in ("frames.jsonl", "facts.jsonl", "cursor.json", "meta.json"):
        assert (root / name).exists(), "missing durable evidence file %s" % name

    # A separate DurableStreamStore reopens the same evidence (reconnect) and
    # yields ordered, cursor-based progress over the same attempt.
    reopened = DurableStreamStore(root=streams)
    frames = progress_frames(reopened, run_id, task_id="task-native")
    cursors = [f.cursor for f in frames]
    assert cursors == sorted(cursors) and len(set(cursors)) == len(cursors)
    assert any(f.stream == "stdout" for f in frames), "no stdout chunk frames"
    assert any(f.kind == "fact" for f in frames), "no immutable fact frame"

    # The single shared snapshot the CLI/MCP/replay read agrees with the entry.
    snapshot = _service(db, art).run_snapshot(run_id)
    assert snapshot is not None
    assert snapshot["methodology"] == "assay"
    assert snapshot["task_id"] == "task-native"
    assert snapshot["outcome"] == "pass"
    assert [p["phase"] for p in snapshot["phases"]] == [
        p["phase"] for p in payload["assay"]["phases"]
    ]


# ---------------------------------------------------------------------------
# 2. The MCP native entry persists the same durable evidence and agrees with the
#    shared snapshot.
# ---------------------------------------------------------------------------


def test_mcp_native_entry_persists_durable_stream_and_agrees(tmp_path):
    mcp_server = pytest.importorskip("skillweave.mcp.server")

    db = str(tmp_path / "mcp.db")
    streams = str(tmp_path / "mcp-streams")
    art = str(tmp_path / "mcp-art")

    ev = _genuine_evidence(tmp_path / "mcp-ev")

    created = json.loads(mcp_server.create_run(
        tool="opencode",
        model="model-assay",
        subject_repo="skillweave",
        subject_commit=ev["commit"],
        # The MCP surface shlex-splits one shell-style string, so the probe is
        # expressed without inner quoting: it prints a token and exits 0.
        command="%s -c print(9001)" % sys.executable,
        db_path=db,
        artifacts_path=art,
        methodology="assay",
        autonomy_policy="unicorn",
        task_id="task-mcp-native",
        attempt=1,
        stream_root=streams,
        artifact_evidence=ev["artifact_json"],
        source_root=str(ev["src"]),
        analysis_root=str(ev["ana"]),
    ))
    assert created["methodology"] == "assay"
    assert created["stream"]["durable"] is True
    assert "assay" in created
    assert created["assay"]["artifact_evidence"]["admitted"] is True
    run_id = created["run_id"]

    root = Path(streams)
    assert (root / "frames.jsonl").exists()
    assert (root / "cursor.json").exists()

    # status_run reads the same shared snapshot the CLI replay reads.
    status = json.loads(mcp_server.status_run(run_id, db_path=db))
    assert status["methodology"] == "assay"
    assert status["task_id"] == "task-mcp-native"
    assert status["outcome"] == "pass"
    assert [p["phase"] for p in status["phases"]] == [
        p["phase"] for p in created["assay"]["phases"]
    ]

    reopened = DurableStreamStore(root=streams)
    frames = progress_frames(reopened, run_id, task_id="task-mcp-native")
    assert any(f.stream == "stdout" for f in frames)
    assert any(f.kind == "fact" for f in frames)


# ---------------------------------------------------------------------------
# 3. Protected triple: authenticated worker + assay actor + assigned checkout +
#    durable store, over a real child.
# ---------------------------------------------------------------------------


def _native_service(tmp_path):
    """A service whose authority DB sits in its own directory (sealable)."""
    from skillweave.runtime.journal import EventJournal
    from skillweave.runtime.registry import RawArtifactStore
    from skillweave.runtime.store import SQLiteRunStore

    db = tmp_path / "authority" / "runsvc.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteRunStore(str(db))
    stream = DurableStreamStore(root=str(tmp_path / "stream-root"))
    svc = RunApplicationService(
        store, EventJournal(store), RawArtifactStore(), stream_store=stream
    )
    return svc, store, db


@requires_sdk
def test_protected_triple_real_child_runs_in_assigned_checkout(tmp_path):
    """Genuine positive for the identity half of the triple on this host.

    A live host-verified worker credential is presented at ingress
    (``require_identity=True``); the host materializes the assigned attempt
    checkout, launches the real child *with that checkout as its working
    directory*, and seals the protected authority store. The child's authorized
    in-checkout write succeeds while its attempt to write the authority DB is
    denied at the kernel boundary.
    """
    from skillweave.runtime.authority_service import HostAuthorityService

    authority = HostAuthorityService()
    run_id, task_id, attempt = "run-triple", "t-1", 1
    svc, store, db = _native_service(tmp_path)
    checkout = tmp_path / "attempt-checkout"
    checkout.mkdir()
    token = authority.issue_attempt_credential(
        run_id=run_id, task_id=task_id, attempt=attempt,
        scope={"paths": [str(checkout / "out")], "mode": "rw"},
    )

    # The child writes an authorized file *inside* its cwd, then tries to append
    # to the sealed authority DB, reporting both outcomes as JSON on stdout (the
    # durable stream) rather than via an outside probe file.
    child = (
        "import json, os, pathlib, sys\n"
        "res = {}\n"
        "cwd = pathlib.Path.cwd()\n"
        "res['cwd'] = str(cwd)\n"
        "res['checkout_env'] = os.environ.get('SKILLWEAVE_ATTEMPT_CHECKOUT')\n"
        "out = cwd / 'out'\n"
        "out.mkdir(exist_ok=True)\n"
        "try:\n"
        "    (out / 'authorized.txt').write_text('authorized-inside')\n"
        "    res['authorized_write'] = 'wrote'\n"
        "except OSError as e:\n"
        "    res['authorized_write'] = 'denied:%s' % e.errno\n"
        "try:\n"
        "    open(sys.argv[1], 'a').write('x')\n"
        "    res['authority_write'] = 'wrote'\n"
        "except OSError as e:\n"
        "    res['authority_write'] = 'denied:%s' % e.errno\n"
        "print('PROTECTED-TRIPLE ' + json.dumps(res))\n"
    )
    out = svc.execute_streaming_native(
        [sys.executable, "-c", child, str(db)],
        run_id=run_id, tool="opencode", model="m",
        subject_repo="r", subject_commit="c",
        task_id=task_id, attempt=attempt,
        created_at="2026-10-05T00:00:00Z",
        authority_service=authority, worker_token=token,
        output_root=str(checkout),
        require_identity=True,
    )
    assert out["durable"] is True
    assert out["gate_state"] == "pass"

    retained = (
        svc.stream_store
        .spool(attempt, "stdout", run_id=run_id, task_id=task_id)
        .read_retained()
        .decode("utf-8", "replace")
    )
    line = next(
        ln for ln in retained.splitlines() if ln.startswith("PROTECTED-TRIPLE ")
    )
    report = json.loads(line[len("PROTECTED-TRIPLE "):])
    # The child ran in the host-assigned checkout, exported for the tool.
    assert report["cwd"] == str(checkout.resolve())
    assert report["checkout_env"] == str(checkout.resolve())
    # The authorized inside write succeeded...
    assert report["authorized_write"] == "wrote"
    assert (checkout / "out" / "authorized.txt").read_text() == "authorized-inside"
    # ...while the protected authority store write was denied by the kernel.
    assert report["authority_write"].startswith("denied")


@requires_sdk
def test_protected_triple_declared_scope_holds_or_confines(tmp_path):
    """The declared-scope half of the triple.

    Where the host offers real checkout confinement the full positive runs: the
    authorized inside write succeeds and the outside / symlink / traversal
    escapes plus the authority-store write are all refused. Where the host's
    strongest adapter only *seals* (this host), the launch must HOLD with the
    typed ``isolation_unavailable`` reason *before* the child exists - never an
    unconfined run.
    """
    from skillweave.runtime.authority_service import HostAuthorityService
    from skillweave.runtime.assay import AssayEnvelope
    from skillweave.core.proc.runner import ScopeEnforcementUnavailable

    authority = HostAuthorityService()
    run_id, task_id, attempt = "run-scope", "t-1", 1
    svc, store, db = _native_service(tmp_path)
    checkout = tmp_path / "attempt-checkout"
    checkout.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    token = authority.issue_attempt_credential(
        run_id=run_id, task_id=task_id, attempt=attempt,
        scope={"paths": [str(checkout / "out")], "mode": "rw"},
    )
    envelope = AssayEnvelope(
        deadline="2999-01-01T00:00:00Z", remaining_budget=50, require_actor=True
    )

    escaped = outside / "escaped.txt"
    ev = _genuine_evidence(tmp_path / "scope-ev")
    child = (
        "import json, os, pathlib, sys\n"
        "res = {}\n"
        "out = pathlib.Path.cwd() / 'out'\n"
        "out.mkdir(exist_ok=True)\n"
        "def attempt(label, fn):\n"
        "    try:\n"
        "        fn(); res[label] = 'wrote'\n"
        "    except OSError as e:\n"
        "        res[label] = 'denied:%s' % e.errno\n"
        "attempt('inside', lambda: (out / 'authorized.txt').write_text('ok'))\n"
        "attempt('outside', lambda: open(sys.argv[1], 'w').write('x'))\n"
        "attempt('authority', lambda: open(sys.argv[2], 'a').write('x'))\n"
        "link = out / 'link.txt'\n"
        "try:\n"
        "    if not link.exists():\n"
        "        link.symlink_to(sys.argv[1])\n"
        "except OSError:\n"
        "    pass\n"
        "attempt('symlink', lambda: open(link, 'w').write('x'))\n"
        "attempt('traversal', lambda: open(str(out / '..' / '..' / 'traverse.txt'), 'w').write('x'))\n"
        "print('SCOPE-REPORT ' + json.dumps(res))\n"
    )
    argv = [sys.executable, "-c", child, str(escaped), str(db)]
    call = lambda: svc.execute_streaming_native(  # noqa: E731
        argv,
        run_id=run_id, tool="opencode", model="m",
        subject_repo="r", subject_commit=ev["commit"],
        task_id=task_id, attempt=attempt,
        created_at="2026-10-05T00:00:00Z",
        methodology="assay", autonomy_policy="unicorn",
        authority_service=authority, worker_token=token,
        assay_actor_token=token, assay_envelope=envelope,
        declared_writes=["out"], output_root=str(checkout),
        require_identity=True,
        artifact_evidence=ev["artifact_evidence"],
        source_resolver=ev["source_resolver"],
        analysis_reader=ev["analysis_reader"],
    )

    if not _HAS_CHECKOUT_CONFINEMENT:
        with pytest.raises(ScopeEnforcementUnavailable) as exc:
            call()
        assert exc.value.to_hold()["reason"] == "isolation_unavailable"
        assert "checkout" in exc.value.reason
        # The refused launch never ran: no outside escape file exists.
        assert not escaped.exists()
        # The seal was released, so the host store is intact and readable.
        assert store.get_run(run_id) is not None
        return

    out = call()
    assert out["durable"] is True
    assert out["gate_state"] == "pass"
    assert out["assay"]["outcome"] == "pass"
    report = json.loads(
        next(
            ln for ln in out["result"].stdout_text.splitlines()
            if ln.startswith("SCOPE-REPORT ")
        )[len("SCOPE-REPORT "):]
    )
    assert report["inside"] == "wrote"
    assert report["outside"].startswith("denied")
    assert report["authority"].startswith("denied")
    assert report["symlink"].startswith("denied")
    assert report["traversal"].startswith("denied")
    assert not escaped.exists()


# ---------------------------------------------------------------------------
# 4. Real MCP transport: initialize -> initialized -> tools/list -> call ->
#    status, against the actual product server, over a real stdio pipe.
# ---------------------------------------------------------------------------

_MCP_DRIVER = r'''
import asyncio, hashlib, json, os
from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters

here = os.environ["MCP_WORK"]

# Build a real artifact proof over a real on-disk source tree + per-path
# analysis bytes, so the native MCP Assay PASS is admitted on genuine
# semantics (SW-1510-VERIFY) rather than an omitted/placeholder graph.
COMMIT = "d34dbeef" * 5
src = os.path.join(here, "ev-src")
ana = os.path.join(here, "ev-ana")
files = {
    "pkg/module.py": b"def real():\n    return 42\n",
    "pkg/util.py": b"VALUE = 'util'\n",
    "README.md": b"# readme\n",
}
analysis = {}
for rel, data in files.items():
    path = os.path.join(src, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").write(data)
    blob = ("analysis of %s: %d bytes" % (rel, len(data))).encode()
    analysis[rel] = blob
    apath = os.path.join(ana, rel)
    os.makedirs(os.path.dirname(apath), exist_ok=True)
    open(apath, "wb").write(blob)

def _sha(b):
    return hashlib.sha256(b).hexdigest()

coverage = [
    {"path": rel, "disposition": "analyzed",
     "content_sha256": _sha(files[rel]), "analysis_digest": _sha(analysis[rel])}
    for rel in files if not rel.endswith(".md")
]
proof = {
    "version": "evidence-receipt-v1", "analyzed": True, "coverage": coverage,
    "exclusions": [{"path": "README.md",
                    "justification": "documentation only, not executable source",
                    "bounded": "narrative paths (*.md)"}],
}
art = {
    "artifact_id": "art-1", "sha256": "0" * 64, "schema_version": "1",
    "producer_command": "analyze", "subject_repo": "skillweave",
    "subject_commit": COMMIT, "created_at": "2026-10-05T00:00:00Z",
    "evidence_type": "artifact", "purpose": "artifact analysis proof",
    "metadata": {"artifact_verifier": proof},
}
_body = json.dumps({k: v for k, v in art.items() if k != "sha256"},
                   sort_keys=True, separators=(",", ":")).encode()
art["sha256"] = hashlib.sha256(_body).hexdigest()
evidence = json.dumps([art])

server_env = dict(os.environ)
server_env["PYTHONPATH"] = os.environ["SKILLWEAVE_SRC"] + os.pathsep + os.environ["SDK_SRC"]
server_env["PYTHONUNBUFFERED"] = "1"
# SW-1510-MCP-AUTH: a local stdio session must name its explicit supervisor
# identity, exactly as a real operator's launcher does. Without it the server
# refuses to start (nil supervisor) and the handshake never completes.
server_env["SKILLWEAVE_MCP_SUPERVISOR"] = os.environ["MCP_SUPERVISOR"]
params = StdioServerParameters(
    command=os.environ["MCP_PY"],
    args=["-B", "-m", "skillweave.mcp.server"],
    env=server_env, cwd=here,
)

async def main():
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print("INIT", init.serverInfo.name, init.serverInfo.version)
            tools = await session.list_tools()
            print("TOOLS", ",".join(sorted(t.name for t in tools.tools)))
            call = await session.call_tool("create_run", {
                "tool": "opencode", "model": "m",
                "subject_repo": "skillweave", "subject_commit": COMMIT,
                "command": os.environ["MCP_PY"] + " -c \"print('mcp-transport-ok')\"",
                "db_path": os.path.join(here, "run.db"),
                "artifacts_path": os.path.join(here, "artifacts"),
                "methodology": "assay", "autonomy_policy": "unicorn",
                "task_id": "task-transport", "attempt": 1,
                "stream_root": os.path.join(here, "streams"),
                "artifact_evidence": evidence,
                "source_root": src,
                "analysis_root": ana,
            })
            payload = json.loads(call.content[0].text)
            print("CREATE", payload["run_id"], payload["gate_state"], payload["stream"]["durable"])
            st = await session.call_tool("status_run",
                {"run_id": payload["run_id"], "db_path": os.path.join(here, "run.db")})
            snap = json.loads(st.content[0].text)
            print("STATUS", snap["methodology"], snap["task_id"], snap["outcome"])
            streams = os.path.join(here, "streams")
            print("FILES", ",".join(sorted(os.listdir(streams))))
            cursors = []
            with open(os.path.join(streams, "frames.jsonl")) as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        cursors.append(json.loads(line)["cursor"])
            print("CURSORS", ",".join(str(c) for c in cursors))

asyncio.run(main())
'''


@requires_mcp_venv
def test_real_mcp_transport_initialize_call_and_status(tmp_path):
    """Drive the actual product MCP server over a real stdio transport.

    This is not an in-process handler call: a subprocess client speaks the MCP
    protocol (``initialize`` handshake, ``tools/list``, ``tools/call``) to a
    subprocess server launched as ``python -m skillweave.mcp.server``, and the
    call must persist durable stream evidence whose ordered cursors form a real
    replay.
    """
    mcp_py = _mcp_python()
    sdk = _sdk_src()
    if sdk is None:
        pytest.skip("accepted skillweave_sdk not resolvable")

    work = tmp_path / "mcp-work"
    work.mkdir()
    driver = tmp_path / "mcp_driver.py"
    driver.write_text(_MCP_DRIVER)

    env = dict(os.environ)
    env.update({
        "MCP_PY": mcp_py,
        "MCP_WORK": str(work),
        "SKILLWEAVE_SRC": str(_SRC),
        "SDK_SRC": str(sdk),
        # The explicit supervisor identity every real local stdio launcher must
        # now provide (SW-1510-MCP-AUTH criterion 3).
        "MCP_SUPERVISOR": "supervisor:ops-harness",
    })
    proc = subprocess.run(
        [mcp_py, "-B", str(driver)],
        capture_output=True, text=True, env=env, cwd=str(work), timeout=180,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    lines = {
        ln.split(" ", 1)[0]: ln.split(" ", 1)[1]
        for ln in proc.stdout.splitlines()
        if " " in ln
    }
    assert lines["INIT"] == "SkillWeave 1.26.0"
    assert lines["TOOLS"] == (
        "cancel_run,create_run,get_evidence,resume_run,review_run,status_run"
    )
    run_id, gate, durable = lines["CREATE"].split(" ")
    assert gate == "pass" and durable == "True"
    assert lines["STATUS"] == "assay task-transport pass"
    assert set(lines["FILES"].split(",")) >= {
        "frames.jsonl", "facts.jsonl", "cursor.json", "meta.json",
    }
    cursors = [int(c) for c in lines["CURSORS"].split(",") if c]
    assert len(cursors) >= 4
    assert cursors == sorted(cursors) and len(set(cursors)) == len(cursors)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
