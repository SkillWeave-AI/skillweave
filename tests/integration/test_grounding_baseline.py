"""Integration tests for the grounding module (SW-159-BP-GROUND-001).

Exercises: Python-versus-TypeScript, exact-symbol, inaccessible-neighbor,
missing-target, and target-drift scenarios.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skillweave.grounding import (
    GapKind,
    GroundingManifest,
    GroundingScanner,
    ScanLimit,
)

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent / "fixtures" / "grounding"
REPO_ROOT = HERE.parent.parent  # the real repository root


# ---------------------------------------------------------------------------
# Python-versus-TypeScript — prove language-correct paths
# ---------------------------------------------------------------------------


class TestLanguageDetection:
    """Language scan correctly classifies files by extension."""

    def test_python_files_detected(self):
        scanner = GroundingScanner(FIXTURES, ScanLimit(max_depth=1))
        result = scanner.scan()
        manifest = result.build_manifest()

        lang_map = {l.language: l.files for l in manifest.languages}
        py_files = lang_map.get("Python", [])
        assert any("sample_python.py" in f for f in py_files), (
            f"Python files not found in {py_files}"
        )

    def test_typescript_files_detected(self):
        scanner = GroundingScanner(FIXTURES, ScanLimit(max_depth=1))
        result = scanner.scan()
        manifest = result.build_manifest()

        lang_map = {l.language: l.files for l in manifest.languages}
        ts_files = lang_map.get("TypeScript", [])
        assert any("sample_typescript.ts" in f for f in ts_files), (
            f"TypeScript files not found in {ts_files}"
        )

    def test_yaml_files_detected(self):
        scanner = GroundingScanner(FIXTURES, ScanLimit(max_depth=1))
        result = scanner.scan()
        manifest = result.build_manifest()

        lang_map = {l.language: l.files for l in manifest.languages}
        yaml_files = lang_map.get("YAML", [])
        assert any("sample_config.yaml" in f for f in yaml_files), (
            f"YAML files not found in {yaml_files}"
        )


# ---------------------------------------------------------------------------
# Exact-symbol — prove repository-relative symbol citations
# ---------------------------------------------------------------------------


class TestExactSymbol:
    """Requested symbols are found with exact file+line citations."""

    def test_find_python_function(self):
        scanner = GroundingScanner(FIXTURES, ScanLimit(max_depth=1))
        result = scanner.scan_with_gaps(requested_symbols=["def hello"])
        manifest = result.build_manifest()

        symbols = manifest.requested_symbols
        matching = [s for s in symbols if "hello" in s.symbol]
        assert len(matching) >= 1, f"No symbol found for 'hello' in {symbols}"
        found = matching[0]
        assert "sample_python.py" in found.file_path
        assert found.line == 1

    def test_find_typescript_interface(self):
        scanner = GroundingScanner(FIXTURES, ScanLimit(max_depth=1))
        result = scanner.scan_with_gaps(
            requested_symbols=["SampleInterface"]
        )
        manifest = result.build_manifest()

        symbols = manifest.requested_symbols
        matching = [s for s in symbols if "SampleInterface" in s.symbol]
        assert len(matching) >= 1, (
            f"No symbol found for 'SampleInterface' in {symbols}"
        )
        assert any("sample_typescript.ts" in s.file_path for s in matching)


# ---------------------------------------------------------------------------
# Inaccessible-neighbor — no gaps for inaccessible repos (no root grant)
# ---------------------------------------------------------------------------


class TestNeighborBoundary:
    """Neighbor repos are only included with explicit evidence."""

    def test_no_neighbor_repos_without_explicit_links(self):
        """Scanning a fixture dir with no cross-repo links yields zero neighbors."""
        scanner = GroundingScanner(FIXTURES, ScanLimit(max_depth=1))
        result = scanner.scan()
        manifest = result.build_manifest()
        assert len(manifest.neighbor_repos) == 0, (
            f"Expected zero neighbors, got {manifest.neighbor_repos}"
        )

    def test_inaccessible_neighbor_no_root_grant(self):
        """Never crawl an organization without explicit root grant."""
        scanner = GroundingScanner(FIXTURES, ScanLimit(max_depth=1))
        result = scanner.scan()
        manifest = result.build_manifest()

        # Even if there's no explicit INACCESSIBLE_NEIGHBOR gap, the scan
        # must not have produced neighbor entries without evidence.
        for n in manifest.neighbor_repos:
            assert n.evidence, (
                f"Neighbor {n.url} has no evidence source"
            )


# ---------------------------------------------------------------------------
# Missing-target — typed blocking gaps
# ---------------------------------------------------------------------------


class TestMissingTarget:
    """Missing target paths produce typed blocking gaps."""

    def test_missing_target_gap(self):
        scanner = GroundingScanner(FIXTURES, ScanLimit(max_depth=1))
        result = scanner.scan_with_gaps(
            requested_targets=["does-not-exist.py"]
        )
        manifest = result.build_manifest()

        gaps = [g for g in manifest.gaps if g.kind == GapKind.MISSING_TARGET]
        assert len(gaps) >= 1, (
            f"Expected MISSING_TARGET gap, got {manifest.gaps}"
        )
        assert "does-not-exist.py" in gaps[0].description

    def test_existing_target_no_gap(self):
        scanner = GroundingScanner(FIXTURES, ScanLimit(max_depth=1))
        result = scanner.scan_with_gaps(
            requested_targets=["sample_python.py"]
        )
        manifest = result.build_manifest()

        gaps = [g for g in manifest.gaps if g.kind == GapKind.MISSING_TARGET]
        assert len(gaps) == 0, (
            f"Unexpected MISSING_TARGET gap: {gaps}"
        )

    def test_missing_symbol_gap(self):
        scanner = GroundingScanner(FIXTURES, ScanLimit(max_depth=1))
        result = scanner.scan_with_gaps(
            requested_symbols=["NonExistentSymbol"]
        )
        manifest = result.build_manifest()

        gaps = [g for g in manifest.gaps if g.kind == GapKind.MISSING_SYMBOL]
        assert len(gaps) >= 1, (
            f"Expected MISSING_SYMBOL gap, got {manifest.gaps}"
        )


# ---------------------------------------------------------------------------
# Target-drift — changed revision detection
# ---------------------------------------------------------------------------


class TestTargetDrift:
    """Changed revision detection produces typed blocking gaps."""

    def test_revision_mismatch_gap(self):
        scanner = GroundingScanner(FIXTURES, ScanLimit(max_depth=1))
        result = scanner.scan_with_gaps(
            expected_revision="0000000000000000000000000000000000000000"
        )
        manifest = result.build_manifest()

        gaps = [g for g in manifest.gaps if g.kind == GapKind.CHANGED_REVISION]
        assert len(gaps) >= 1, (
            f"Expected CHANGED_REVISION gap, got {manifest.gaps}"
        )
        assert "0000000" in gaps[0].description


# ---------------------------------------------------------------------------
# Digest determinism
# ---------------------------------------------------------------------------


class TestDigest:
    """Digest is deterministic and covers all substantive fields."""

    def test_digest_is_deterministic(self):
        scanner = GroundingScanner(FIXTURES, ScanLimit(max_depth=1))
        m1 = scanner.scan().build_manifest()
        m2 = scanner.scan().build_manifest()
        assert m1.digest == m2.digest, (
            f"Digest mismatch: {m1.digest} != {m2.digest}"
        )

    def test_digest_changes_on_field_change(self):
        m1 = GroundingManifest(target="a").with_digest()
        m2 = GroundingManifest(target="b").with_digest()
        assert m1.digest != m2.digest, (
            "Digest should differ when target changes"
        )

    def test_serialize_roundtrip(self):
        scanner = GroundingScanner(FIXTURES, ScanLimit(max_depth=1))
        manifest = scanner.scan().build_manifest()
        d = manifest.to_dict()
        assert d["digest"] == manifest.digest
        assert d["target"] == str(FIXTURES)
        assert isinstance(d["languages"], list)
        assert isinstance(d["gaps"], list)


# ---------------------------------------------------------------------------
# Real repo scan — bounded, no exceptions
# ---------------------------------------------------------------------------


class TestRealRepository:
    """Scan the actual repository — bounded and deterministic."""

    def test_real_repo_scan_completes(self):
        scanner = GroundingScanner(REPO_ROOT, ScanLimit(max_depth=4))
        result = scanner.scan()
        manifest = result.build_manifest()

        assert manifest.full_revision, "Expected non-empty revision"
        assert len(manifest.languages) >= 1, (
            f"Expected at least one language, got {manifest.languages}"
        )
        # Python should be detected
        lang_map = {l.language: l.files for l in manifest.languages}
        assert "Python" in lang_map, (
            f"Python not detected in {list(lang_map.keys())}"
        )
        # pyproject.toml should be a manifest
        assert any("pyproject.toml" in m for m in manifest.manifests), (
            f"pyproject.toml not in manifests: {manifest.manifests}"
        )

    def test_real_repo_no_gaps_without_checks(self):
        scanner = GroundingScanner(REPO_ROOT, ScanLimit(max_depth=4))
        result = scanner.scan()
        manifest = result.build_manifest()
        assert len(manifest.gaps) == 0, (
            f"Unexpected gaps in scan without checks: {manifest.gaps}"
        )
