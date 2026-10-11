"""SW-1510-CLI: repair the installed CLI entry and publish command semantics.

Every criterion is proven against the **actual installed console entry** -- a
fresh offline wheel built from this checkout, installed into an isolated venv,
invoked as the real ``skillweave`` subprocess -- not only against individual
parser helpers (the task criterion is explicit: "Test the actual console entry
point in a fresh wheel environment rather than only individual parser
helpers").

Criterion 1 -- the installed top-level ``start`` REMAINDER positional no longer
  collides with the router's ``dest="command"``, so ``start``/``inspect``/
  ``kill`` reach their own parsers, each help surface exits 0, and a real
  subprocess invocation of the installed script starts, inspects and kills a
  durable run. A dest collision would make the installed script print
  top-level help and never dispatch (the pre-fix defect).

Criterion 2 -- the installed script publishes an explicit *versioned* support
  table (``skillweave.support/1``) and a shared run-state interpretation
  (``skillweave.run-state/1``): ``entry`` decides, ``run``/``start`` execute,
  the async lifecycle + MCP operate, and the legacy ``dispatch`` status stays
  readable. The published vocabulary is *shared*, i.e. projected from the live
  authorities (``RunStateModel``/``LEGACY_STATE_ALIASES``/``STOP_REASONS`` and
  the lifecycle projection) rather than copied -- and the table's rows match
  the router's actually-mounted subcommands, with no fabricated surface.

Criterion 3 -- invalid run IDs, duplicate run IDs, unsupported methodologies,
  a missing source binding and killed-running inconsistencies each produce a
  meaningful error on the installed entry and a nonzero exit code.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: A real, 40-hex commit the assay artifact/source resolver both bind to.
COMMIT = "a724d32a724d32a724d32a724d32a724d32a724d"


# ---------------------------------------------------------------------------
# Fresh installed-wheel fixture (build once, reused by the whole module)
# ---------------------------------------------------------------------------

def _venv_site(venv_dir: Path) -> Path:
    matches = sorted(venv_dir.glob("lib/python*/site-packages"))
    assert matches, f"no venv site-packages under {venv_dir}"
    return matches[0]


def _clean_env() -> dict:
    """Subprocess env with ``PYTHONPATH`` removed.

    The lane exports ``PYTHONPATH`` (the accepted SDK / source tree). An
    inherited value makes ``pip`` skip the freshly-built wheel and can leak the
    source tree into the "installed" proof, so build/install/run subprocesses
    clear it.
    """
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("SKILLWEAVE_ACCEPTED_SDK", None)
    return env


def _build_wheel(work: Path) -> Path:
    """Copy the checkout into *work* and build a wheel offline (hermetic).

    The source is copied first so the build never writes into the reviewer's
    read-only checkout; ``--no-deps --no-index --no-build-isolation`` keeps the
    build offline and free of ambient build backends.
    """
    (work / "src").parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / "src", work / "src")
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(REPO_ROOT / name, work / name)
    if (REPO_ROOT / "NOTICE").exists():
        shutil.copy2(REPO_ROOT / "NOTICE", work / "NOTICE")

    work_tmp = work / "tmp"
    work_tmp.mkdir(exist_ok=True)
    env = _clean_env()
    env["TMPDIR"] = str(work_tmp)
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-index",
         "--no-build-isolation", "-w", str(work / "dist"), str(work)],
        cwd=str(work), capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    wheels = sorted((work / "dist").glob("skillweave-*.whl"))
    assert wheels, f"no wheel built in {work / 'dist'}"
    return wheels[-1]


def _install_into_fresh_venv(work: Path, wheel: Path) -> Path:
    venv_dir = work / "venv"
    env = _clean_env()
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True,
                   capture_output=True, text=True, env=env)
    site = _venv_site(venv_dir)
    subprocess.run(
        [str(venv_dir / "bin" / "pip"), "install", "--no-deps", "--no-index",
         str(wheel)],
        check=True, capture_output=True, text=True, env=env,
    )
    # The offline install cannot fetch PyYAML (a declared runtime dep). Make it
    # available to the isolated interpreter without reaching the network.
    import yaml  # noqa: PLC0415 - intentional late import

    ambient_yaml = Path(yaml.__file__).resolve().parent
    shutil.copytree(ambient_yaml, site / "yaml")
    extra = ambient_yaml.parent / "_yaml"
    if extra.exists():
        shutil.copytree(extra, site / "_yaml")
    return venv_dir


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    """A fresh installed ``skillweave`` console script (built once)."""
    pytest.importorskip("yaml")
    work = tmp_path_factory.mktemp("sw1510cli-build")
    wheel = _build_wheel(work)
    venv_dir = _install_into_fresh_venv(work, wheel)
    script = venv_dir / "bin" / "skillweave"
    assert script.exists(), f"console script not installed: {script}"
    return {"work": work, "venv": venv_dir, "script": script,
            "python": venv_dir / "bin" / "python"}


def _run(installed, args, *, cwd, extra_env=None):
    """Invoke the *installed* console script as a real subprocess."""
    env = _clean_env()
    tmp = Path(cwd) / "tmp"
    tmp.mkdir(exist_ok=True)
    env["TMPDIR"] = str(tmp)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(installed["script"]), *args],
        cwd=str(cwd), env=env, capture_output=True, text=True,
    )


# ---------------------------------------------------------------------------
# Criterion 1 -- installed entry dispatches start/inspect/kill
# ---------------------------------------------------------------------------

def test_installed_entry_lists_every_subcommand(installed, tmp_path):
    proc = _run(installed, ["--help"], cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    # The router reaches one parser per mounted surface; a colliding REMAINDER
    # positional (or a colliding option dest) would drop ``start`` and make the
    # installed top-level help miss it.
    for name in ("start", "inspect", "kill", "cancel", "resume", "support"):
        assert name in proc.stdout, f"{name!r} missing from installed help"


@pytest.mark.parametrize("sub", ["start", "inspect", "kill"])
def test_installed_help_surfaces_exit_zero_and_are_subcommand_specific(
    installed, tmp_path, sub
):
    proc = _run(installed, [sub, "--help"], cwd=tmp_path)
    assert proc.returncode == 0, (sub, proc.stderr)
    # The parser that answered is the *sub*, not the top-level router: the usage
    # line names ``skillweave <sub>``. Before the fix the colliding positional
    # made ``start`` print the top-level help instead.
    assert f"skillweave {sub}" in proc.stdout


def test_installed_start_help_no_longer_collides(installed, tmp_path):
    proc = _run(installed, ["start", "--help"], cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    # The REMAINDER positional is ``start_argv`` (not ``command``): the option
    # dest ``command`` belongs to the router's subparser action, and reusing it
    # here is exactly the defect this criterion repairs.
    assert "start_argv" in out
    assert "--async" in out
    assert "--run-id" in out


def test_installed_start_inspect_kill_roundtrip(installed, tmp_path):
    run_id = "cli-roundtrip"
    py = str(installed["python"])

    started = _run(
        installed,
        ["start", "--async", "--run-id", run_id, "--", py, "-c",
         "import time; time.sleep(30)"],
        cwd=tmp_path,
    )
    assert started.returncode == 0, started.stderr
    handle = json.loads(started.stdout)
    assert handle["run_id"] == run_id
    assert isinstance(handle["pid"], int) and handle["pid"] > 0

    snapshot = _run(installed, ["inspect", run_id], cwd=tmp_path)
    assert snapshot.returncode == 0, snapshot.stderr
    state = json.loads(snapshot.stdout.strip().splitlines()[0])
    assert state["run_id"] == run_id
    assert state["state"] == "running"
    assert state["pid"] == handle["pid"]

    killed = _run(installed, ["kill", run_id, str(handle["pid"])], cwd=tmp_path)
    assert killed.returncode == 0, killed.stderr
    outcome = json.loads(killed.stdout)
    # A live run was really signalled (graceful or forced) -- not a no-op.
    assert outcome["result"] in ("graceful_kill", "forced_kill")
    assert outcome["state"] == "killed"

    after = _run(installed, ["inspect", run_id], cwd=tmp_path)
    assert after.returncode == 0, after.stderr
    final = json.loads(after.stdout.strip().splitlines()[0])
    assert final["state"] == "killed"


# ---------------------------------------------------------------------------
# Criterion 2 -- versioned support table + shared state interpretation
# ---------------------------------------------------------------------------

def test_installed_support_table_is_versioned_and_classifies_surfaces(
    installed, tmp_path
):
    proc = _run(installed, ["support", "--json"], cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    table = json.loads(proc.stdout)

    assert table["schema_id"] == "skillweave.support/1"
    assert table["state_interpretation_id"] == "skillweave.run-state/1"
    assert table["run_snapshot_schema_id"] == "run-snapshot-v1"

    rows = {row["name"]: row for row in table["surfaces"]}
    # entry decides; run/start execute; the lifecycle + mcp operations operate;
    # the legacy dispatch status surface stays explicitly legacy.
    assert rows["entry"]["role"] == "decide"
    assert rows["run"]["role"] == "execute"
    assert rows["start"]["role"] == "execute"
    assert rows["start"]["status"] == "supported"
    assert rows["mcp"]["role"] == "operate"
    assert rows["dispatch"]["role"] == "legacy"
    assert rows["dispatch"]["status"] == "legacy"
    for name in ("inspect", "kill", "cancel", "resume"):
        assert rows[name]["role"] == "operate"


def test_installed_support_state_matches_the_live_authorities(installed, tmp_path):
    """The published vocabulary is *shared*, not a second copy."""
    from skillweave.runtime.store import (
        LEGACY_STATE_ALIASES,
        STOP_REASONS,
        RunStateModel,
    )
    from skillweave.runlifecycle.store import _LIFECYCLE_TO_LANE

    proc = _run(installed, ["support", "--json"], cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    state = json.loads(proc.stdout)["state"]
    assert state["schema_id"] == "skillweave.run-state/1"

    assert set(state["canonical"]) == {m.value for m in RunStateModel}
    assert set(state["terminal"]) == set(RunStateModel.terminal_values())
    assert state["legacy_aliases"] == dict(LEGACY_STATE_ALIASES)
    assert state["lifecycle_projection"] == dict(_LIFECYCLE_TO_LANE)
    assert state["stop_reasons"] == list(STOP_REASONS)


def test_installed_support_state_interpretation_is_honest(installed, tmp_path):
    legacy = _run(installed, ["support", "--state", "FAILED"], cwd=tmp_path)
    assert legacy.returncode == 0, legacy.stderr
    interp = json.loads(legacy.stdout)["interpretation"]
    assert interp["recognized"] is True
    assert interp["canonical"] == "failed"
    assert interp["is_legacy_alias"] is True
    assert interp["is_terminal"] is True

    unknown = _run(installed, ["support", "--state", "not-a-state"], cwd=tmp_path)
    assert unknown.returncode == 0, unknown.stderr
    interp = json.loads(unknown.stdout)["interpretation"]
    # An unrecognized value is reported as unrecognized -- never defaulted.
    assert interp["recognized"] is False
    assert interp["canonical"] is None


def test_support_rows_match_the_router_mounted_subcommands(installed, tmp_path):
    """The table's router rows equal the actually-mounted subcommands."""
    import skillweave.cli.support as support_mod
    from skillweave.cli.main import _build_parser

    parser = _build_parser()
    choices = set(parser._subparsers._group_actions[0].choices)
    assert set(support_mod.ROUTER_SURFACES) == choices


# ---------------------------------------------------------------------------
# Criterion 3 -- meaningful errors and nonzero exit codes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "argv",
    [
        ["inspect", "no-such-run"],
        ["kill", "no-such-run", "999999"],
        ["resume", "no-such-run"],
        ["cancel", "no-such-run"],
    ],
)
def test_installed_invalid_run_id_is_a_meaningful_error(installed, tmp_path, argv):
    proc = _run(installed, argv, cwd=tmp_path)
    assert proc.returncode != 0, (argv, proc.stdout)
    combined = (proc.stdout + proc.stderr).lower()
    assert "no run" in combined or "not found" in combined, (argv, combined)


def test_installed_duplicate_run_id_is_a_reservation_refusal(installed, tmp_path):
    py = str(installed["python"])
    run_id = "cli-dup"
    first = _run(
        installed,
        ["start", "--async", "--run-id", run_id, "--", py, "-c",
         "import time; time.sleep(5)"],
        cwd=tmp_path,
    )
    assert first.returncode == 0, first.stderr
    pid = json.loads(first.stdout)["pid"]

    second = _run(
        installed,
        ["start", "--async", "--run-id", run_id, "--", py, "-c", "print(1)"],
        cwd=tmp_path,
    )
    # A duplicate bootstrap is a *reservation* refusal, not an uncaught
    # traceback: a meaningful error and a nonzero exit. The message names the
    # collision ("run already exists") rather than leaking the raw CAS numbers
    # ("Version conflict ... expected 1, got 6").
    assert second.returncode != 0, second.stdout
    assert f"run already exists: {run_id}" in second.stderr, second.stderr
    assert "expected 1" not in second.stderr, second.stderr

    _run(installed, ["kill", run_id, str(pid)], cwd=tmp_path)


def test_installed_unsupported_methodology_is_refused(installed, tmp_path):
    proc = _run(
        installed,
        ["run", "--tool", "opencode", "--model", "m",
         "--subject-repo", "skillweave", "--subject-commit", COMMIT,
         "--methodology", "warp-drive",
         "--db-path", str(tmp_path / "m.db"),
         "--artifacts-path", str(tmp_path / "m-art"),
         "--", sys.executable, "-c", "print('never')"],
        cwd=tmp_path,
    )
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    combined = proc.stdout + proc.stderr
    assert "warp-drive" in combined and "invalid choice" in combined


def test_installed_missing_source_binding_withholds_the_assay_pass(
    installed, tmp_path
):
    proc = _run(
        installed,
        ["run", "--tool", "opencode", "--model", "m",
         "--subject-repo", "skillweave", "--subject-commit", COMMIT,
         "--methodology", "assay", "--autonomy-policy", "unicorn",
         "--db-path", str(tmp_path / "a.db"),
         "--artifacts-path", str(tmp_path / "a-art"),
         # No --artifact-evidence / --source-root: the native PASS is withheld.
         "--", sys.executable, "-c", "print('probe')"],
        cwd=tmp_path,
    )
    # A withheld PASS is a meaningful non-success: the full result JSON is still
    # emitted, but the exit code is nonzero so a wrapper cannot read it as PASS.
    assert proc.returncode != 0, proc.stdout
    result = json.loads(proc.stdout)
    assert result["assay"]["outcome"] == "hold"
    assert "withheld" in proc.stderr.lower(), proc.stderr


def test_installed_killed_running_inconsistency_is_nonzero(installed, tmp_path):
    py = str(installed["python"])
    run_id = "cli-already-terminal"
    started = _run(
        installed,
        ["start", "--async", "--run-id", run_id, "--", py, "-c",
         "import time; time.sleep(0.05)"],
        cwd=tmp_path,
    )
    assert started.returncode == 0, started.stderr
    pid = json.loads(started.stdout)["pid"]

    # Let it finish, so the recorded state is terminal while the caller still
    # asks to kill a run they believe is live: the inconsistency must not read
    # as an unqualified success.
    import time as _time

    deadline = _time.monotonic() + 10.0
    state = None
    while _time.monotonic() < deadline:
        snap = _run(installed, ["inspect", run_id], cwd=tmp_path)
        state = json.loads(snap.stdout.strip().splitlines()[0])["state"]
        if state != "running":
            break
        _time.sleep(0.1)
    assert state not in (None, "running"), state

    killed = _run(installed, ["kill", run_id, str(pid)], cwd=tmp_path)
    assert killed.returncode == 3, (killed.returncode, killed.stdout, killed.stderr)
    result = json.loads(killed.stdout)
    assert result["result"] in ("already_terminal", "already_dead")
    assert "not running" in killed.stderr.lower()


# ---------------------------------------------------------------------------
# Criterion 2/3 -- controller-finding regressions on the installed entry
# ---------------------------------------------------------------------------

# ``-leading`` is deliberately excluded: argparse itself interprets a leading
# dash as an option, so it never reaches the shared run-id validator. The
# shapes below all reach the validator and must be rejected as invalid input.
INVALID_RUN_IDS = ["../x", "a b", "x/y", "", "..", ".", "a" * 300, ".hidden"]


def test_installed_support_table_publishes_run_id_rule_and_exit_codes(
    installed, tmp_path
):
    """The run-id rule and exit-code table are published, not merely enforced."""
    proc = _run(installed, ["support", "--json"], cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    table = json.loads(proc.stdout)
    assert table["run_id_rule"] == "[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
    codes = table["exit_codes"]
    assert codes["invalid_input"] == 2
    assert codes["command_not_found"] == 127
    assert codes["not_executable"] == 126


@pytest.mark.parametrize("run_id", INVALID_RUN_IDS)
def test_installed_invalid_run_id_is_rejected_with_code_2(installed, tmp_path, run_id):
    proc = _run(
        installed,
        ["start", "--async", "--run-id", run_id, "--", sys.executable, "-c",
         "print('never')"],
        cwd=tmp_path,
    )
    # An invalid run id is a usage error (documented code 2), never a path
    # hazard or a downstream traceback.
    assert proc.returncode == 2, (run_id, proc.returncode, proc.stderr)
    assert "run id" in proc.stderr.lower(), (run_id, proc.stderr)


@pytest.mark.parametrize("argv0", ["inspect", "kill", "cancel", "resume"])
def test_installed_invalid_run_id_is_rejected_on_every_async_surface(
    installed, tmp_path, argv0
):
    argv = {
        "inspect": ["inspect", "../x"],
        "kill": ["kill", "../x", "1"],
        "cancel": ["cancel", "../x"],
        "resume": ["resume", "../x"],
    }[argv0]
    proc = _run(installed, argv, cwd=tmp_path)
    assert proc.returncode == 2, (argv0, proc.returncode, proc.stderr)


def test_installed_auto_generated_run_id_stays_valid(installed, tmp_path):
    """The default (uuid hex) id still passes the published rule."""
    py = str(installed["python"])
    started = _run(
        installed,
        ["start", "--async", "--", py, "-c", "import time; time.sleep(5)"],
        cwd=tmp_path,
    )
    assert started.returncode == 0, started.stderr
    run_id = json.loads(started.stdout)["run_id"]
    import re

    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id), run_id
    # Stop the child so the test leaves no live process behind.
    pid = json.loads(started.stdout)["pid"]
    _run(installed, ["kill", run_id, str(pid)], cwd=tmp_path)


def test_installed_start_missing_command_is_a_meaningful_error(installed, tmp_path):
    for argv in (["start", "--", "/nonexistent/x"],
                 ["start", "--async", "--run-id", "nf-sync", "--", "/nonexistent/x"]):
        proc = _run(installed, argv, cwd=tmp_path)
        assert proc.returncode == 127, (argv, proc.returncode, proc.stdout, proc.stderr)
        assert "not found" in proc.stderr.lower(), (argv, proc.stderr)
        # No raw Python traceback leaks to the operator.
        assert "Traceback (most recent call last)" not in proc.stderr, proc.stderr


def test_installed_async_missing_command_records_a_failed_run(installed, tmp_path):
    """--async must not leave a silent running record for a command it cannot launch."""
    proc = _run(
        installed,
        ["start", "--async", "--run-id", "nf-async", "--", "/nonexistent/x"],
        cwd=tmp_path,
    )
    assert proc.returncode == 127, (proc.returncode, proc.stdout, proc.stderr)
    snap = _run(installed, ["inspect", "nf-async"], cwd=tmp_path)
    assert snap.returncode == 0, snap.stderr
    state = json.loads(snap.stdout.strip().splitlines()[0])
    assert state["state"] == "failed", state


def test_installed_duplicate_run_id_reports_already_exists(installed, tmp_path):
    py = str(installed["python"])
    run_id = "cli-dup-2"
    first = _run(
        installed,
        ["start", "--async", "--run-id", run_id, "--", py, "-c",
         "import time; time.sleep(5)"],
        cwd=tmp_path,
    )
    assert first.returncode == 0, first.stderr
    pid = json.loads(first.stdout)["pid"]

    second = _run(
        installed,
        ["start", "--async", "--run-id", run_id, "--", py, "-c", "print(1)"],
        cwd=tmp_path,
    )
    assert second.returncode != 0, second.stdout
    assert f"run already exists: {run_id}" in second.stderr, second.stderr
    # Not the raw CAS message.
    assert "expected 1" not in second.stderr, second.stderr

    _run(installed, ["kill", run_id, str(pid)], cwd=tmp_path)


def test_installed_inspect_lane_state_agrees_with_the_shared_interpretation(
    installed, tmp_path
):
    """inspect must apply the shared lifecycle->lane projection (finding 1).

    After a kill, ``inspect`` reported ``state=killed`` (finished) but
    ``lane_states={'<id>':'running'}`` while ``support`` mapped ``killed ->
    advance_or_stop``: a contradiction. Every shared-interpretation path must
    now agree.
    """
    py = str(installed["python"])
    run_id = "cli-lane-consistency"
    started = _run(
        installed,
        ["start", "--async", "--run-id", run_id, "--", py, "-c",
         "import time; time.sleep(60)"],
        cwd=tmp_path,
    )
    assert started.returncode == 0, started.stderr
    pid = json.loads(started.stdout)["pid"]

    killed = _run(installed, ["kill", run_id, str(pid)], cwd=tmp_path)
    assert killed.returncode == 0, killed.stderr
    assert json.loads(killed.stdout)["result"] in ("graceful_kill", "forced_kill")

    # The support table's one authority for this lifecycle value.
    support = _run(installed, ["support", "--json"], cwd=tmp_path)
    projection = json.loads(support.stdout)["state"]["lifecycle_projection"]
    expected_lane = projection["killed"]

    snap = _run(installed, ["inspect", run_id], cwd=tmp_path)
    assert snap.returncode == 0, snap.stderr
    snapshot = json.loads(snap.stdout.strip().splitlines()[0])
    assert snapshot["state"] == "killed"
    assert snapshot["finished_at"]
    # The lifecycle and the lane state never contradict: not a stale "running".
    assert snapshot["lane_states"].get(run_id) == expected_lane, snapshot
    assert snapshot["lane_states"].get(run_id) != "running", snapshot
    assert snapshot.get("lane_state") == expected_lane, snapshot

    # cancel reports the same lane state for the same run (a second shared path).
    cancelled = _run(installed, ["cancel", run_id], cwd=tmp_path)
    if cancelled.returncode == 0:
        assert json.loads(cancelled.stdout)["lane_state"] == expected_lane


# ---------------------------------------------------------------------------
# Standalone
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
