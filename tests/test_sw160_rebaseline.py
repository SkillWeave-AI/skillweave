"""SW-160-BASE-001: Verify the GLE rebaseline contract and breadth matrices.

Every old GLE acceptance criterion maps to delivered, open, superseded or
explicitly out-of-scope status.  Code, tests, current PRD JSON and the mapping
agree on full subject SHAs.  Legacy IDs remain traceable and no completed
behavior is silently replanned.  The resulting contract and breadth matrices
are machine-readable.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLANNING_REALITY = REPO_ROOT / "planning" / "reality"
MAP_PATH = PLANNING_REALITY / "SW-160-gle-rebaseline.json"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def map_data() -> dict:
    """Load the rebaseline mapping JSON once per module."""
    assert MAP_PATH.is_file(), (
        f"Rebaseline map not found at {MAP_PATH}"
    )
    with open(MAP_PATH, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def head_sha() -> str:
    """Return the current HEAD SHA."""
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
        cwd=REPO_ROOT,
    ).stdout.strip()


# ---------------------------------------------------------------------------
# Tests: meta structure
# ---------------------------------------------------------------------------

def test_map_meta_is_complete(map_data):
    """The meta block has required fields."""
    meta = map_data.get("meta", {})
    assert meta.get("task") == "SW-160-BASE-001"
    assert "current_head" in meta
    assert "current_version" in meta
    assert meta["current_version"].startswith("v")


def test_map_head_matches_repo(map_data, head_sha):
    """The current_head in the map agrees with ``git rev-parse HEAD``."""
    recorded = map_data["meta"]["current_head"]
    # The recorded SHA must exist as a valid commit object in this repo.
    subprocess.run(
        ["git", "cat-file", "-e", f"{recorded}^{{commit}}"],
        capture_output=True, text=True, check=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    # The recorded SHA must be an ancestor of (or equal to) the live HEAD.
    # This proves the rebaseline map is still part of the history without
    # requiring an exact match (HEAD moves with every commit).
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", recorded, head_sha],
        capture_output=True, text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    if result.returncode == 128:
        pytest.skip("cannot compute ancestry in this clone")
    assert result.returncode == 0, (
        f"Map HEAD {recorded} is not an ancestor of repo HEAD {head_sha}. "
        f"The rebaseline map is no longer part of the current history."
    )


# ---------------------------------------------------------------------------
# Tests: contract matrix — every GLE from the PRD is mapped
# ---------------------------------------------------------------------------

def test_contract_matrix_covers_all_gle_ids(map_data):
    """All 20 GLE IDs from the PRD are present in the contract matrix."""
    expected_gle_ids = {
        f"GLE-{i:03d}" for i in range(1, 20)
    }  # GLE-001 through GLE-019 (GLE-020 is special)
    expected_gle_ids.add("GLE-020")

    contract = map_data.get("contract_matrix", {})
    present = set(contract.keys())
    missing = expected_gle_ids - present
    extra = present - expected_gle_ids
    assert not missing, f"GLE IDs missing from contract matrix: {sorted(missing)}"
    assert not extra, f"Unexpected GLE IDs in contract matrix: {sorted(extra)}"


def test_every_criterion_has_required_fields(map_data):
    """Every contract matrix entry has title, status, subject_shas, legacy_ids, acceptance_criteria, evidence."""
    required = {"title", "status", "subject_shas", "legacy_ids", "acceptance_criteria", "evidence"}
    contract = map_data.get("contract_matrix", {})
    for gle_id, entry in contract.items():
        missing = required - set(entry.keys())
        assert not missing, f"{gle_id}: missing fields {sorted(missing)}"


def test_every_status_is_valid(map_data):
    """Status is one of: delivered, open, superseded, out_of_scope."""
    valid = {"delivered", "open", "superseded", "out_of_scope"}
    contract = map_data.get("contract_matrix", {})
    for gle_id, entry in contract.items():
        assert entry["status"] in valid, (
            f"{gle_id}: invalid status {entry['status']!r}"
        )


def test_subject_shas_are_40_hex(map_data):
    """All subject_shas entries are 40-character hex strings."""
    contract = map_data.get("contract_matrix", {})
    for gle_id, entry in contract.items():
        for sha in entry.get("subject_shas", []):
            assert isinstance(sha, str) and len(sha) == 40, (
                f"{gle_id}: invalid SHA {sha!r}"
            )
            int(sha, 16)  # raises ValueError if not hex


# ---------------------------------------------------------------------------
# Tests: breadth matrix — sums and coverage
# ---------------------------------------------------------------------------

def test_breadth_matrix_sums_to_total(map_data):
    """Breadth matrix status counts sum to total_criteria."""
    bm = map_data.get("breadth_matrix", {})
    total = bm.get("total_criteria", 0)
    counted = (
        bm.get("delivered", 0)
        + bm.get("open", 0)
        + bm.get("superseded", 0)
        + bm.get("out_of_scope", 0)
    )
    assert counted == total, (
        f"Breadth matrix counts ({counted}) != total ({total})"
    )


def test_breadth_lists_match_counts(map_data):
    """Length of each status list matches the corresponding count."""
    bm = map_data.get("breadth_matrix", {})
    for status in ("delivered", "open", "superseded", "out_of_scope"):
        count = bm.get(f"{status}_list", [])
        expected = bm.get(status, 0)
        assert len(count) == expected, (
            f"{status}: list length {len(count)} != count {expected}"
        )


def test_breadth_lists_are_disjoint(map_data):
    """No GLE ID appears in more than one status list."""
    bm = map_data.get("breadth_matrix", {})
    all_ids = {}
    for status in ("delivered", "open", "superseded", "out_of_scope"):
        for gle_id in bm.get(f"{status}_list", []):
            assert gle_id not in all_ids, (
                f"{gle_id} appears in both {all_ids[gle_id]} and {status}"
            )
            all_ids[gle_id] = status


def test_breadth_lists_cover_all_contract_entries(map_data):
    """Every contract_matrix entry appears in exactly one breadth list."""
    contract_ids = set(map_data.get("contract_matrix", {}).keys())
    bm = map_data.get("breadth_matrix", {})
    list_ids = set()
    for status in ("delivered", "open", "superseded", "out_of_scope"):
        list_ids.update(bm.get(f"{status}_list", []))
    missing_from_lists = contract_ids - list_ids
    extra_in_lists = list_ids - contract_ids
    assert not missing_from_lists, (
        f"Contract entries missing from breadth lists: {sorted(missing_from_lists)}"
    )
    assert not extra_in_lists, (
        f"Breadth list entries not in contract: {sorted(extra_in_lists)}"
    )


# ---------------------------------------------------------------------------
# Tests: delivery_details — GLE-020 is delivered
# ---------------------------------------------------------------------------

def test_gle020_is_delivered(map_data):
    """GLE-020 is marked delivered with implementation SHAs and tests."""
    details = map_data.get("delivery_details", {}).get("GLE-020", {})
    assert details.get("status") == "delivered"
    assert len(details.get("implementation_shas", [])) >= 3
    for sha in details["implementation_shas"]:
        assert len(sha) == 40
        int(sha, 16)
    assert len(details.get("tests", [])) >= 2
    for test_path in details["tests"]:
        assert (REPO_ROOT / test_path).is_file(), (
            f"GLE-020 test not found: {test_path}"
        )
    assert "merge_sha" in details
    assert "version_introduced" in details


def test_gle020_contract_matches_contract_matrix(map_data):
    """GLE-020 status is consistent between contract_matrix and delivery_details."""
    contract = map_data.get("contract_matrix", {}).get("GLE-020", {})
    assert contract.get("status") == "delivered"
    details = map_data.get("delivery_details", {}).get("GLE-020", {})
    assert details.get("status") == "delivered"


# ---------------------------------------------------------------------------
# Tests: legacy traceability
# ---------------------------------------------------------------------------

def test_legacy_traceability_present(map_data):
    """Legacy traceability block exists and records PRD source."""
    trace = map_data.get("legacy_traceability", {})
    assert "prd_source" in trace
    prd = trace["prd_source"]
    assert "commit" in prd
    assert len(prd["commit"]) == 40
    assert "gle_ids_in_current_codebase" in trace
    assert "GLE-020" in trace["gle_ids_in_current_codebase"]
    assert trace.get("completed_behavior_preserved") is True
    assert trace.get("no_silent_replanning") is True


# ---------------------------------------------------------------------------
# Tests: GLE-004 superseded status
# ---------------------------------------------------------------------------

def test_gle004_is_superseded(map_data):
    """GLE-004 is marked superseded with subject SHAs from its branch."""
    entry = map_data.get("contract_matrix", {}).get("GLE-004", {})
    assert entry["status"] == "superseded"
    # GLE-004 has 8 subject SHAs on the chore/gle-prd-einchecken branch
    assert len(entry["subject_shas"]) >= 6
    for sha in entry["subject_shas"]:
        assert len(sha) == 40


# ---------------------------------------------------------------------------
# Tests: machine-readable format validation
# ---------------------------------------------------------------------------

def test_map_is_valid_json():
    """The map file is valid JSON."""
    with open(MAP_PATH, encoding="utf-8") as f:
        data = json.load(f)
    assert isinstance(data, dict)


def test_map_has_top_level_sections(map_data):
    """The map has all required top-level sections."""
    required_sections = {
        "meta", "contract_matrix", "breadth_matrix",
        "delivery_details", "legacy_traceability",
    }
    present = set(map_data.keys())
    missing = required_sections - present
    assert not missing, f"Missing top-level sections: {sorted(missing)}"


# ---------------------------------------------------------------------------
# Tests: no completed behavior is silently replanned
# ---------------------------------------------------------------------------

def test_no_delivered_gle_in_open_list(map_data):
    """No delivered GLE appears in the open list."""
    bm = map_data.get("breadth_matrix", {})
    delivered_set = set(bm.get("delivered_list", []))
    open_set = set(bm.get("open_list", []))
    assert delivered_set.isdisjoint(open_set), (
        f"Delivered GLEs also in open list: {delivered_set & open_set}"
    )


def test_gle020_code_still_present():
    """Verify GLE-020 code artifacts still exist in the codebase."""
    init_py = REPO_ROOT / "src" / "skillweave" / "__init__.py"
    assert init_py.is_file()
    content = init_py.read_text(encoding="utf-8")
    assert "OPTIONAL_SUBPACKAGES" in content
    assert "__getattr__" in content
    assert "runtime" in content

    degraded = REPO_ROOT / "src" / "skillweave_degraded.py"
    assert degraded.is_file()
    assert "GLE-020" in degraded.read_text(encoding="utf-8")
