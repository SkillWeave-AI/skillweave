"""SW-1511A-INTEGRATION-001: the cumulative, role-safe integration gate.

One adversarial test per lane criterion. Every test drives the *real production
entry point* -- ``skillweave.github_integration.release_gate``'s
``ReleaseReadinessGate.check_integration_gate``, ``evaluate`` and ``run_cli`` --
with fakes supplied through its seam, so the gate run is what derives the
cumulative manifest, binds the result to the exact candidate and proves the
review read-only. Nothing here launches a suite, opens a socket, names a
provider or reads a credential; the environment digest and suite totals are the
caller's observed facts, injected.

The red proof: on the frozen base the gate has no ``check_integration_gate`` and
no ``integration`` seam, so every criterion test fails at the first call -- the
cumulative manifest is never derived and no ``integration-gate-result/v1`` is
ever produced.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest

_GATE_MODULE = "skillweave.github_integration.release_gate"
_CONTRACT_MODULE = "skillweave.gates.integration_gate_result"

_CANDIDATE = "a" * 40
_BASE = "b" * 40
_OTHER = "c" * 40
_ENV_DIGEST = "d" * 64


def _gate():
    """Import the production entry point, naming the wiring on the base."""
    return importlib.import_module(_GATE_MODULE)


def _contract():
    """Import the deliverable contract, naming the module on the base."""
    return importlib.import_module(_CONTRACT_MODULE)


def _candidate_tree(tmp_path: Path) -> Path:
    """A per-test candidate worktree with a realistic unit-suite surface."""
    root = tmp_path / "candidate"
    unit = root / "tests" / "unit"
    unit.mkdir(parents=True)
    # A file the diff never touches: only a cumulative (base-derived) manifest
    # keeps it, which is exactly the file the diff-only list silently dropped.
    (unit / "test_untouched_surface.py").write_text("def test_ok():\n    assert True\n")
    # The file the candidate diff changes.
    (unit / "test_changed_module.py").write_text("def test_ok():\n    assert True\n")
    # The file a prior accepted surface implies.
    (unit / "test_prior_surface.py").write_text("def test_ok():\n    assert True\n")
    (root / "src").mkdir()
    (root / "src" / "changed_module.py").write_text("VALUE = 2\n")
    return root


def _fake_totals(passed: int = 12, skipped: int = 1):
    contract = _contract()
    return contract.SuiteTotals(passed=passed, failed=0, skipped=skipped)


# ── criterion 1 ──────────────────────────────────────────────────────────────


def test_criterion1_cumulative_manifest_and_exact_candidate_refusal(tmp_path):
    """Criterion 1 through the real gate entry point.

    The gate (a) derives the *cumulative* manifest from the frozen base, the
    candidate diff and the prior accepted surfaces, and (b) refuses a
    final-head integration when the attested suite result names a *different*
    candidate than the one under integration.

    Red on the base: ``check_integration_gate`` does not exist.
    """
    gate_mod = _gate()
    contract = _contract()
    root = _candidate_tree(tmp_path)
    gate = gate_mod.ReleaseReadinessGate(repo_root=str(root))

    # (a) cumulative derivation: the diff names only the changed module, yet the
    # untouched base file and the prior-surface file are both in the manifest --
    # a diff-only list would have dropped the untouched file.
    manifest = contract.derive_test_manifest(
        candidate_root=root,
        base_root=root,
        diff_paths=["src/changed_module.py"],
        prior_surfaces=["skillweave/prior_surface.py"],
    )
    assert "tests/unit/test_changed_module.py" in manifest
    assert "tests/unit/test_untouched_surface.py" in manifest
    assert "tests/unit/test_prior_surface.py" in manifest

    # (b) the gate refuses a result for another candidate and passes for the
    # exact candidate with an attested complete-suite result.
    check, result = gate.check_integration_gate(
        candidate_sha=_CANDIDATE,
        base_sha=_BASE,
        expected_candidate_sha=_OTHER,  # a result for a different SHA
        source_suite=_fake_totals(),
        installed_wheel=_fake_totals(passed=9, skipped=0),
        environment_digest=_ENV_DIGEST,
        diff_paths=["src/changed_module.py"],
        prior_surfaces=["skillweave/prior_surface.py"],
    )
    assert check.passed is False
    assert result["verdict"] == contract.VERDICT_REFUSED
    assert any("source-binding" in r for r in result["refusals"])

    check_ok, result_ok = gate.check_integration_gate(
        candidate_sha=_CANDIDATE,
        base_sha=_BASE,
        expected_candidate_sha=_CANDIDATE,
        source_suite=_fake_totals(),
        installed_wheel=_fake_totals(passed=9, skipped=0),
        environment_digest=_ENV_DIGEST,
        diff_paths=["src/changed_module.py"],
        prior_surfaces=["skillweave/prior_surface.py"],
    )
    assert check_ok.passed, check_ok.detail
    assert result_ok["candidate_sha"] == _CANDIDATE
    assert result_ok["verdict"] == contract.VERDICT_PASS

    # An attested *complete-suite* result is required: omitting either suite is
    # a refusal, never a defaulted pass.
    check_missing, result_missing = gate.check_integration_gate(
        candidate_sha=_CANDIDATE,
        base_sha=_BASE,
        environment_digest=_ENV_DIGEST,
        diff_paths=["src/changed_module.py"],
    )
    assert check_missing.passed is False
    assert result_missing["verdict"] == contract.VERDICT_REFUSED
    assert any("source-suite" in r for r in result_missing["refusals"])
    assert any("installed-wheel" in r for r in result_missing["refusals"])


# ── criterion 2 ──────────────────────────────────────────────────────────────


def test_criterion2_review_is_read_only_and_red_proof_is_disposable(tmp_path):
    """Criterion 2 through the real gate entry point.

    Review is read-only: the gate digests the candidate (and the evidence
    artifacts) around the review action and refuses if either changed. The
    red-proof/destructive check runs in a *disposable* worktree that never
    touches the candidate and is always removed.

    Red on the base: no ``review_probe`` seam and no read-only proof exist.
    """
    gate_mod = _gate()
    contract = _contract()
    root = _candidate_tree(tmp_path)
    artifacts = tmp_path / "evidence-artifacts"
    artifacts.mkdir()
    (artifacts / "receipt.json").write_text("{}\n")
    gate = gate_mod.ReleaseReadinessGate(repo_root=str(root))

    before_candidate = contract.tree_digest(root)
    before_artifacts = contract.tree_digest(artifacts)

    def read_only_review():
        # A real review just reads; the red-proof belongs elsewhere.
        _ = (root / "src" / "changed_module.py").read_text()
        with contract.disposable_worktree(parent=tmp_path) as scratch:
            # The destructive check writes here, never into the candidate.
            (scratch / "destroyed.txt").write_text("boom\n")
            assert (scratch / "destroyed.txt").is_file()

    check, result = gate.check_integration_gate(
        candidate_sha=_CANDIDATE,
        base_sha=_BASE,
        expected_candidate_sha=_CANDIDATE,
        source_suite=_fake_totals(),
        installed_wheel=_fake_totals(passed=9, skipped=0),
        environment_digest=_ENV_DIGEST,
        review_probe=read_only_review,
        artifacts_root=artifacts,
    )
    assert check.passed, check.detail
    assert result["review_read_only"] is True
    # The candidate and its evidence artifacts are byte-identical after review.
    assert contract.tree_digest(root) == before_candidate
    assert contract.tree_digest(artifacts) == before_artifacts
    # The disposable worktree was removed; nothing leaked into the candidate.
    assert not any(p.name == "destroyed.txt" for p in tmp_path.rglob("destroyed.txt"))

    # A review that mutates the candidate is refused, never silently accepted.
    def mutating_review():
        (root / "src" / "changed_module.py").write_text("VALUE = 999\n")

    check_bad, result_bad = gate.check_integration_gate(
        candidate_sha=_CANDIDATE,
        base_sha=_BASE,
        source_suite=_fake_totals(),
        installed_wheel=_fake_totals(passed=9, skipped=0),
        environment_digest=_ENV_DIGEST,
        review_probe=mutating_review,
        artifacts_root=artifacts,
    )
    assert check_bad.passed is False
    assert result_bad["review_read_only"] is False
    assert any("read-only" in r or "digest changed" in r for r in result_bad["refusals"])

    # ``disposable_worktree`` removes the directory even when the body raises.
    scratch_path = None
    with pytest.raises(RuntimeError):
        with contract.disposable_worktree(parent=tmp_path) as scratch:
            scratch_path = scratch
            (scratch / "partial.txt").write_text("x")
            raise RuntimeError("destructive check failed")
    assert scratch_path is not None and not scratch_path.exists()


# ── criterion 3 ──────────────────────────────────────────────────────────────


def test_criterion3_regression_records_source_and_wheel_in_v1(tmp_path, monkeypatch):
    """Criterion 3 through the real *CLI* entry point (``run_cli``).

    The regression scenario proves cross-file state cleanup: two gate runs over
    distinct per-test roots are reproducible and leave no residue in the
    candidate, and the written ``integration-gate-result/v1`` records the
    source-suite and installed-wheel totals, their skips, the environment
    digest and the candidate SHA -- read back from the gate's own artifact.

    Red on the base: ``run_cli`` takes no ``integration`` seam, so the
    ``integration_gate_result`` key is never written.
    """
    gate_mod = _gate()
    contract = _contract()
    root = _candidate_tree(tmp_path)

    def source_surface_digest() -> str:
        # The candidate *source surface* only. The gate legitimately writes its
        # own declared artifact (release-gate-data.json) at the gate root; the
        # cross-file-cleanup claim is that no residue reaches the product source.
        return contract.tree_digest(root / "src") + contract.tree_digest(root / "tests")

    before = source_surface_digest()

    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    # The environment seam is not under test here: skip building it so the run
    # touches no interpreter, index or credential.
    monkeypatch.setenv("SKILLWEAVE_ATTEST_ENV", "0")

    integration = {
        "candidate_sha": _CANDIDATE,
        "base_sha": _BASE,
        "expected_candidate_sha": _CANDIDATE,
        "source_suite": {"passed": 30, "failed": 0, "skipped": 2},
        "installed_wheel": {"passed": 28, "failed": 0, "skipped": 1},
        "environment_digest": _ENV_DIGEST,
        "diff_paths": ["src/changed_module.py"],
        "prior_surfaces": ["skillweave/prior_surface.py"],
        "tasks": ["SW-1511A-LEDGER-001", "SW-1511A-ENV-001"],
    }
    repo_cwd = os.getcwd()
    try:
        monkeypatch.chdir(root)  # run the gate over the candidate tree
        rc = gate_mod.run_cli(["--release-version", "0.0.0"], integration=integration)
    finally:
        monkeypatch.chdir(repo_cwd)

    assert rc == 0
    data = json.loads((root / "release-gate-data.json").read_text(encoding="utf-8"))
    result = data["integration_gate_result"]

    # The versioned product schema, replacing the interim controller shape.
    assert result["schema"] == contract.INTEGRATION_GATE_RESULT_SCHEMA_ID
    contract.verify_result(result)

    # Source-suite plus installed-wheel results, totals, skips, environment
    # digest and candidate SHA are all recorded.
    assert result["source_suite"] == {
        "passed": 30, "failed": 0, "skipped": 2, "total": 32
    }
    assert result["installed_wheel"] == {
        "passed": 28, "failed": 0, "skipped": 1, "total": 29
    }
    assert result["test_totals"] == {
        "source_suite": 32, "installed_wheel": 29, "combined": 61
    }
    assert result["skips"] == {"source_suite": 2, "installed_wheel": 1, "combined": 3}
    assert result["environment_digest"] == _ENV_DIGEST
    assert result["candidate_sha"] == _CANDIDATE
    assert result["verdict"] == contract.VERDICT_PASS

    # Cross-file state cleanup: the candidate source surface is unchanged by the
    # gate run, and a second run over the same tree is reproducible (no residue,
    # no fixed id in shared temp).
    assert source_surface_digest() == before
    monkeypatch.chdir(root)
    try:
        gate_mod.run_cli(["--release-version", "0.0.0"], integration=integration)
    finally:
        monkeypatch.chdir(repo_cwd)
    second = json.loads((root / "release-gate-data.json").read_text(encoding="utf-8"))
    assert second["integration_gate_result"] == result
    assert source_surface_digest() == before

    # The result round-trips through its own durable store, valid, and the
    # shipped schema is the v1 product schema.
    written = contract.write_result(tmp_path / "igr.json", contract.IntegrationGateResult.from_dict(result))
    assert contract.read_result(written).to_dict() == result
    assert contract.load_schema()["$id"].endswith("/integration-gate-result/v1")


if __name__ == "__main__":  # pragma: no cover - manual runner
    raise SystemExit(pytest.main([__file__, "-q"]))
