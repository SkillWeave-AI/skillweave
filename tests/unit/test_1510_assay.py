"""SW-1510-ASSAY: native Assay methodology wiring.

Criterion 1 -- an installed entry command dispatches a real bounded subprocess
  with ``methodology=assay`` and persists the ordered PIN/HYPOTHESIS/PROBE/
  MEASURE/CLASSIFY phases plus a terminal outcome, each carrying the
  run/task/attempt triple. Proven twice: through the in-process application
  service and through a *fresh installed wheel's* ``skillweave run`` console
  script (a real subprocess, an isolated venv, an offline wheel build).

Criterion 2 -- the same attempt and terminal verdict are visible through CLI,
  MCP and a journal replay; there is no script-owned parallel controller. All
  three read the single :meth:`RunApplicationService.run_snapshot` view.

Criterion 3 -- REX/Ralph/Gauntlet keep their declared compatible bindings and
  an Assay request can never silently run Ralph. An unknown methodology, an
  unknown autonomy policy, and an incompatible registered combination are each
  refused with a *named* reason at the ``methodology`` stage, before any run
  state is written.

Boundary discipline proven here: the PROBE phase records the child's *actual*
execution facts (pid/exit_code/signal/termination), MEASURE binds the captured
bytes by digest, and CLASSIFY carries the independent verifier's gate -- these
stay distinct, and the outcome phase is PASS only when that gate PASSes under a
policy that can author it. A probe that prints a pass-looking string cannot make
itself pass. Child-execution facts are never a verdict, and model text is never
terminal authority.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

_src = Path(__file__).resolve().parent.parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from skillweave.runsvc import RunApplicationService, RunIntegrationError  # noqa: E402
from skillweave.runtime.store import SQLiteRunStore  # noqa: E402
from skillweave.runtime.journal import EventJournal  # noqa: E402
from skillweave.runtime.registry import RawArtifactStore  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: The ordered Assay drill phases, in the append-only order the engine defines.
_DRILL_PHASES = ("pin", "hypothesis", "probe", "measure", "classify")

#: A real, bounded probe that exits 0 and writes non-empty stdout.
_PASS_PROBE = [sys.executable, "-c", "print('assay-probe-output')"]


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _service(db_path=":memory:", artifacts_dir=None):
    store = SQLiteRunStore(db_path)
    journal = EventJournal(db_path)
    raw = RawArtifactStore(None if artifacts_dir is None else str(artifacts_dir))
    return RunApplicationService(store, journal, raw), store, journal, raw


def _phase(assay_block, name):
    for payload in assay_block["phases"]:
        if payload["phase"] == name:
            return payload
    raise AssertionError(f"phase {name!r} missing from {list(assay_block['phases'])}")


def _phase_names(assay_block):
    return [p["phase"] for p in assay_block["phases"]]


def _run_assay(service, command=None, *, run_id="assay-1", task_id=None,
               attempt=1, policy="unicorn"):
    return service.execute(
        list(command or _PASS_PROBE),
        run_id=run_id,
        tool="opencode",
        model="model-assay",
        subject_repo="skillweave",
        subject_commit="deadbeef",
        created_at="2026-10-05T00:00:00Z",
        methodology="assay",
        autonomy_policy=policy,
        task_id=task_id,
        attempt=attempt,
    )


# ---------------------------------------------------------------------------
# Criterion 1: real bounded subprocess + persisted ordered phases
# ---------------------------------------------------------------------------

def test_assay_run_persists_ordered_phases_with_run_task_attempt():
    service, store, journal, raw = _service()
    result = _run_assay(service, run_id="assay-c1", task_id="task-77", attempt=3)

    assert result.methodology == "assay"
    assert result.assay is not None
    block = result.assay

    # The run/task/attempt triple is on the block and on every phase payload.
    assert block["task_id"] == "task-77"
    assert block["attempt"] == 3
    for payload in block["phases"]:
        assert payload["run_id"] == "assay-c1"
        assert payload["task_id"] == "task-77"
        assert payload["attempt"] == 3

    # The five drill phases appear in the append-only order, then a terminal.
    names = _phase_names(block)
    assert names[:5] == list(_DRILL_PHASES)
    assert names[5] in ("pass", "hold")
    assert block["outcome"] == "pass"
    assert names[5] == "pass"

    # The PROBE is a real bounded subprocess: a distinct pid and exit code.
    probe = _phase(block, "probe")
    assert isinstance(probe["pid"], int) and probe["pid"] > 0
    assert probe["exit_code"] == 0
    assert probe["termination"] == "exited"
    assert probe["command"] == list(_PASS_PROBE)

    # MEASURE binds the captured bytes by digest, matching the stored raw bytes.
    measure = _phase(block, "measure")
    assert measure["byte_length"] == len(result.raw_bytes)
    assert measure["stdout_digest"] == result.raw_digest
    assert raw.resolve(measure["stdout_digest"]) == result.raw_bytes

    # Journal is ordered and gap-free.
    assert journal.has_gaps("assay-c1") is False
    seqs = [p["sequence"] for p in block["phase_events"]]
    assert seqs == sorted(seqs)


def test_probe_facts_stay_distinct_from_verification_and_gate():
    service, store, journal, raw = _service()
    result = _run_assay(service, run_id="assay-distinct")

    block = result.assay
    probe = _phase(block, "probe")
    classify = _phase(block, "classify")

    # PROBE carries only execution facts -- it is not a grade.
    assert set(("pid", "exit_code", "signal", "termination")) <= set(probe)
    assert "grade" not in probe
    assert "gate_state" not in probe

    # CLASSIFY carries the independent verifier's decision, bound to the
    # producer receipt, with a *separate* verifier identity/receipt.
    assert classify["gate_state"] == result.gate_state
    assert classify["subject_artifact_id"] == result.receipt.artifact_id
    assert classify["grade"] == "high"

    verify_receipt = store.get_evidence("verify-runsvc-assay-distinct")
    assert verify_receipt is not None
    assert verify_receipt.artifact_id != result.receipt.artifact_id
    assert verify_receipt.metadata["verified_by"] == "verifier"
    assert verify_receipt.metadata["gate_state"] == "pass"


# ---------------------------------------------------------------------------
# Criterion 2: CLI / MCP / replay share one snapshot
# ---------------------------------------------------------------------------

def test_cli_then_snapshot_and_replay_agree(tmp_path, capsys):
    from skillweave.cli import run as run_cli

    db = str(tmp_path / "cli.db")
    art = str(tmp_path / "cli-art")

    rc = run_cli.main([
        "--tool", "opencode",
        "--model", "model-assay",
        "--subject-repo", "skillweave",
        "--subject-commit", "deadbeef",
        "--methodology", "assay",
        "--autonomy-policy", "unicorn",
        "--task-id", "task-cli",
        "--attempt", "2",
        "--db-path", db,
        "--artifacts-path", art,
        "--",
        *_PASS_PROBE,
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    run_id = payload["run_id"]
    assert payload["methodology"] == "assay"
    assert payload["assay"]["outcome"] == "pass"
    assert payload["assay"]["task_id"] == "task-cli"
    assert payload["assay"]["attempt"] == 2

    # A *separate* service instance reads the same persisted view (the replay).
    service, store, journal, raw = _service(db, art)
    snapshot = service.run_snapshot(run_id)
    assert snapshot is not None
    assert snapshot["methodology"] == "assay"
    assert snapshot["task_id"] == "task-cli"
    assert snapshot["attempt"] == 2
    assert snapshot["outcome"] == "pass"
    assert snapshot["gate_state"] == "pass"
    assert [p["phase"] for p in snapshot["phases"]] == _phase_names(payload["assay"])

    # journal.replay walks the same ordered events the snapshot collected.
    replayed = journal.replay(run_id, lambda e: (e.payload or {}).get("phase"))
    assert [p for p in replayed if p] == [p["phase"] for p in snapshot["phases"]]


def test_mcp_status_and_create_agree_with_cli(tmp_path):
    mcp_server = pytest.importorskip("skillweave.mcp.server")

    db = str(tmp_path / "mcp.db")
    art = str(tmp_path / "mcp-art")

    created = json.loads(mcp_server.create_run(
        tool="opencode",
        model="model-assay",
        subject_repo="skillweave",
        subject_commit="deadbeef",
        # The MCP surface takes one shell-style string and shlex-splits it, so
        # the probe is expressed without quoting: it prints 42 and exits 0.
        command=f"{sys.executable} -c print(42)",
        db_path=db,
        artifacts_path=art,
        methodology="assay",
        autonomy_policy="unicorn",
        task_id="task-mcp",
        attempt=1,
    ))
    assert created["methodology"] == "assay"
    assert created["assay"]["outcome"] == "pass"
    run_id = created["run_id"]

    status = json.loads(mcp_server.status_run(run_id, db_path=db))
    assert status["methodology"] == "assay"
    assert status["task_id"] == "task-mcp"
    assert status["outcome"] == "pass"
    assert status["gate_state"] == "pass"
    assert [p["phase"] for p in status["phases"]] == _phase_names(created["assay"])


# ---------------------------------------------------------------------------
# Criterion 3: declared bindings, never a silent downgrade to Ralph
# ---------------------------------------------------------------------------

def test_unknown_methodology_is_refused_by_name():
    service, store, journal, raw = _service()
    with pytest.raises(RunIntegrationError) as excinfo:
        service.execute(
            list(_PASS_PROBE),
            run_id="assay-unknown",
            tool="opencode",
            model="m",
            subject_repo="skillweave",
            subject_commit="deadbeef",
            methodology="warp-drive",
        )
    assert excinfo.value.stage == "methodology"
    assert "warp-drive" in excinfo.value.reason
    # Refused *before* any run row exists: no half-written run.
    assert store.get_run("assay-unknown") is None


def test_unknown_autonomy_policy_is_refused_not_downgraded():
    service, store, journal, raw = _service()
    with pytest.raises(RunIntegrationError) as excinfo:
        _run_assay(service, run_id="assay-badpol", policy="anarchic")
    assert excinfo.value.stage == "methodology"
    assert "anarchic" in excinfo.value.reason
    assert store.get_run("assay-badpol") is None


def test_incompatible_registered_binding_has_a_named_reason():
    from skillweave.runtime.methodology import (
        Methodology, TopologyType, PolicyType,
        check_compatibility, assert_compatible, MethodologyError,
    )

    # The exact combination the service refuses to construct must carry the
    # registry's named reason -- not a generic "unsupported".
    reason = check_compatibility(
        Methodology.ASSAY, TopologyType.SEQUENTIAL, PolicyType.APPROVAL_REQUIRED
    )
    assert reason is not None
    assert "Assay" in reason and "human approval" in reason

    with pytest.raises(MethodologyError) as excinfo:
        assert_compatible(
            Methodology.ASSAY, TopologyType.SEQUENTIAL, PolicyType.APPROVAL_REQUIRED
        )
    assert excinfo.value.reason == reason

    # And the binding the service *does* build is compatible.
    assert check_compatibility(
        Methodology.ASSAY, TopologyType.SEQUENTIAL, PolicyType.SUPERVISED
    ) is None


def test_assay_never_silently_runs_as_ralph_and_ralph_has_no_assay_block():
    service, store, journal, raw = _service()
    assay = _run_assay(service, run_id="assay-explicit")
    assert assay.methodology == "assay"
    assert assay.assay is not None
    assert store.get_run("assay-explicit").metadata["methodology"] == "assay"

    ralph = service.execute(
        list(_PASS_PROBE),
        run_id="ralph-explicit",
        tool="opencode",
        model="m",
        subject_repo="skillweave",
        subject_commit="deadbeef",
        methodology="ralph",
    )
    assert ralph.methodology == "ralph"
    assert ralph.assay is None
    assert store.get_run("ralph-explicit").metadata["methodology"] == "ralph"


def test_rex_and_gauntlet_keep_their_declared_bindings():
    # Criterion 3: REX and Gauntlet continue through *declared compatible*
    # bindings (their own registered topology/policy), not a silent default and
    # not an Assay drill. Each is validated against the registry.
    service, store, journal, raw = _service()

    rex = service.execute(
        list(_PASS_PROBE),
        run_id="rex-explicit",
        tool="opencode",
        model="m",
        subject_repo="skillweave",
        subject_commit="deadbeef",
        methodology="rex",
    )
    assert rex.methodology == "rex"
    assert rex.assay is None
    rex_meta = store.get_run("rex-explicit").metadata
    assert rex_meta["methodology"] == "rex"
    assert rex_meta["methodology_binding"]["topology"] == "sequential"
    assert rex_meta["methodology_binding"]["policy"] == "supervised"

    gauntlet = service.execute(
        list(_PASS_PROBE),
        run_id="gauntlet-explicit",
        tool="opencode",
        model="m",
        subject_repo="skillweave",
        subject_commit="deadbeef",
        methodology="gauntlet",
    )
    assert gauntlet.methodology == "gauntlet"
    assert gauntlet.assay is None
    g_meta = store.get_run("gauntlet-explicit").metadata
    assert g_meta["methodology"] == "gauntlet"
    assert g_meta["methodology_binding"]["topology"] == "parallel_lanes"
    assert g_meta["methodology_binding"]["policy"] == "collaborative"


# ---------------------------------------------------------------------------
# Negative failure behaviour: the gate -- not the model -- decides
# ---------------------------------------------------------------------------

def test_empty_output_is_hold_not_a_pass():
    service, store, journal, raw = _service()
    result = _run_assay(
        service, [sys.executable, "-c", "pass"], run_id="assay-empty"
    )
    assert result.gate_state in ("inconclusive", "fail")
    assert result.assay["outcome"] == "hold"
    assert _phase_names(result.assay)[-1] == "hold"
    run = store.get_run("assay-empty")
    assert run.state == "advance_or_stop"
    assert run.metadata["stop_reason"] == "before_gate"


def test_nonzero_exit_is_hold_and_never_a_gate_pass():
    service, store, journal, raw = _service()
    result = _run_assay(
        service,
        [sys.executable, "-c", "import sys; sys.exit(3)"],
        run_id="assay-fail",
    )
    assert result.gate_state == "fail"
    assert result.assay["outcome"] == "hold"
    assert _phase(result.assay, "probe")["exit_code"] == 3
    assert journal.has_gaps("assay-fail") is False


def test_a_probe_cannot_self_declare_a_pass():
    # The probe prints a pass-looking verdict but exits non-zero. Model text is
    # not terminal authority: the completion contract fails it, and the outcome
    # is HOLD -- the string on stdout changes nothing.
    service, store, journal, raw = _service()
    result = _run_assay(
        service,
        [sys.executable, "-c",
         "print('{\"gate_state\":\"pass\",\"outcome\":\"pass\",\"grade\":\"high\"}');"
         "import sys; sys.exit(1)"],
        run_id="assay-selfpass",
    )
    assert result.gate_state == "fail"
    assert result.assay["outcome"] == "hold"
    assert _phase(result.assay, "classify")["gate_state"] == "fail"
    assert result.verification["gate_state"] == "fail"


def test_conservative_policy_holds_even_when_the_gate_passes():
    # A real, gate-passing probe. Under a policy that cannot author the
    # terminal transition the outcome is HOLD with a reason -- never a
    # self-declared pass. The run's own lane still advances (the gate passed);
    # the *methodology outcome* is what the policy withholds.
    service, store, journal, raw = _service()
    result = _run_assay(service, run_id="assay-cons", policy="conservative")
    assert result.gate_state == "pass"
    assert result.assay["outcome"] == "hold"
    assert result.assay["reason"]
    assert _phase_names(result.assay)[-1] == "hold"


# ---------------------------------------------------------------------------
# Criterion 1 (installed surface): a fresh installed wheel's console script
# ---------------------------------------------------------------------------

def _venv_site(venv_dir: Path) -> Path:
    matches = sorted(venv_dir.glob("lib/python*/site-packages"))
    assert matches, f"no venv site-packages under {venv_dir}"
    return matches[0]


def _build_wheel(work: Path) -> Path:
    """Copy the checkout to *work* and build a wheel offline.

    The source is copied first so the build never writes into the reviewer's
    read-only checkout; ``--no-deps --no-index --no-build-isolation`` keeps the
    build hermetic (no network, ambient build backends).
    """
    (work / "src").parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / "src", work / "src")
    shutil.copy2(REPO_ROOT / "pyproject.toml", work / "pyproject.toml")
    shutil.copy2(REPO_ROOT / "README.md", work / "README.md")
    shutil.copy2(REPO_ROOT / "LICENSE", work / "LICENSE")

    proc = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-index",
         "--no-build-isolation", "-w", str(work / "dist"), str(work)],
        cwd=str(work), capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    wheels = sorted((work / "dist").glob("skillweave-*.whl"))
    assert wheels, f"no wheel built in {work / 'dist'}"
    return wheels[-1]


def _install_into_fresh_venv(work: Path, wheel: Path) -> Path:
    venv_dir = work / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True,
                   capture_output=True, text=True)
    site = _venv_site(venv_dir)
    subprocess.run(
        [str(venv_dir / "bin" / "pip"), "install", "--no-deps", "--no-index",
         str(wheel)],
        check=True, capture_output=True, text=True,
    )
    # The offline install cannot fetch PyYAML (a declared runtime dependency).
    # Make the dependency available to the isolated interpreter without
    # reaching the network or the source tree.
    import yaml  # noqa: PLC0415 - intentional late import

    ambient_yaml = Path(yaml.__file__).resolve().parent
    shutil.copytree(ambient_yaml, site / "yaml")
    extra = ambient_yaml.parent / "_yaml"
    if extra.exists():
        shutil.copytree(extra, site / "_yaml")
    return venv_dir


def test_installed_wheel_console_script_runs_native_assay(tmp_path):
    pytest.importorskip("yaml")

    work = tmp_path / "build"
    work.mkdir()
    wheel = _build_wheel(work)
    venv_dir = _install_into_fresh_venv(work, wheel)

    script = venv_dir / "bin" / "skillweave"
    db = tmp_path / "installed.db"
    art = tmp_path / "installed-art"

    env = dict(os.environ)
    # Prove the *installed* package is exercised: drop any source-tree path.
    env.pop("PYTHONPATH", None)
    env["TMPDIR"] = str(tmp_path / "tmp")
    (tmp_path / "tmp").mkdir(exist_ok=True)

    proc = subprocess.run(
        [
            str(script), "run",
            "--tool", "opencode",
            "--model", "model-assay",
            "--subject-repo", "skillweave",
            "--subject-commit", "deadbeef",
            "--methodology", "assay",
            "--autonomy-policy", "unicorn",
            "--task-id", "task-installed",
            "--attempt", "2",
            "--db-path", str(db),
            "--artifacts-path", str(art),
            "--",
            sys.executable, "-c", "print('installed-assay-probe')",
        ],
        cwd=str(tmp_path), env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)

    assert payload["methodology"] == "assay"
    assert payload["assay"]["outcome"] == "pass"
    assert payload["assay"]["task_id"] == "task-installed"
    assert payload["assay"]["attempt"] == 2
    assert [p["phase"] for p in payload["assay"]["phases"]][:5] == list(_DRILL_PHASES)
    assert payload["assay"]["child_execution"]["exit_code"] == 0
    run_id = payload["run_id"]

    # The run and its phases are durably persisted by the installed entry
    # command, readable straight from SQLite with no skillweave import.
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT metadata FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        assert row is not None
        metadata = json.loads(row[0])
        assert metadata["methodology"] == "assay"
        assert metadata["task_id"] == "task-installed"
        assert metadata["attempt"] == 2

        phase_rows = conn.execute(
            "SELECT payload FROM events WHERE run_id = ? ORDER BY sequence",
            (run_id,),
        ).fetchall()
        phases = [json.loads(r[0]).get("phase") for r in phase_rows]
        phases = [p for p in phases if p]
        assert phases[:5] == list(_DRILL_PHASES)
        assert phases[-1] == "pass"
    finally:
        conn.close()


def test_installed_wheel_refuses_unknown_methodology(tmp_path):
    pytest.importorskip("yaml")

    work = tmp_path / "build"
    work.mkdir()
    wheel = _build_wheel(work)
    venv_dir = _install_into_fresh_venv(work, wheel)
    script = venv_dir / "bin" / "skillweave"

    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["TMPDIR"] = str(tmp_path / "tmp")
    (tmp_path / "tmp").mkdir(exist_ok=True)

    proc = subprocess.run(
        [
            str(script), "run",
            "--tool", "opencode", "--model", "m",
            "--subject-repo", "skillweave", "--subject-commit", "deadbeef",
            "--methodology", "warp-drive",
            "--db-path", str(tmp_path / "installed2.db"),
            "--artifacts-path", str(tmp_path / "installed2-art"),
            "--",
            sys.executable, "-c", "print('never')",
        ],
        cwd=str(tmp_path), env=env, capture_output=True, text=True,
    )
    # The declared-methodology surface refuses an unregistered name at
    # argument parsing: an explicit non-zero exit, the name echoed, and no
    # run is executed (there is no fall-through to a default methodology).
    assert proc.returncode == 2
    combined = proc.stdout + proc.stderr
    assert "warp-drive" in combined
    assert "invalid choice" in combined


# ---------------------------------------------------------------------------
# Standalone
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
