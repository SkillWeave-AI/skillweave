"""Integration tests for planning ticket handshake (SW-159-BP-TICKET-001).

Tests all four terminal states plus duplicate-title and concurrent-create
fixtures.  Uses ``tmp_path`` for isolated filesystem testing — never mutates
a real planning board.
"""

from __future__ import annotations

import sys
import time
import threading
from pathlib import Path

import pytest

_src = Path(__file__).resolve().parent.parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from skillweave.core.planning.planning_handshake import (
    HandshakeError,
    HandshakeEvidence,
    HandshakeResult,
    HandshakeTerminal,
    PlanningBoardInfo,
    _acquire_lock,
    _find_ticket_by_title,
    _release_lock,
    _scan_tickets,
    create_ticket_on_board,
    detect_planning_repository,
    link_ticket,
    perform_handshake,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def board_path(tmp_path: Path) -> Path:
    """Create a minimal beans-pattern planning board at ``tmp_path/.skillweave/planning/``."""
    bp = tmp_path / ".skillweave" / "planning"
    for sub in ("backlog", "doing", "done"):
        (bp / sub).mkdir(parents=True)
    return bp


@pytest.fixture
def populated_board(board_path: Path) -> Path:
    """A planning board with one existing ticket."""
    create_ticket_on_board(
        board_path,
        ticket_id="FEAT-001",
        title="Existing Feature",
        content="Some existing work.",
    )
    return board_path


# ---------------------------------------------------------------------------
# S1 — Bounded fan-out helpers (read-only, short)
# ---------------------------------------------------------------------------


def test_detect_planning_repository_returns_false_when_no_board(tmp_path: Path):
    """No .skillweave/planning/ → exists=False."""
    info = detect_planning_repository(tmp_path)
    assert info.exists is False
    assert info.path is None
    assert info.is_writable is False
    assert info.tickets == {}


def test_detect_planning_repository_returns_false_when_partial(tmp_path: Path):
    """Has .skillweave/planning/ but no beans dirs → exists=False."""
    pp = tmp_path / ".skillweave" / "planning"
    pp.mkdir(parents=True)
    info = detect_planning_repository(tmp_path)
    assert info.exists is False


def test_detect_planning_repository_detects_board(board_path: Path):
    """Has .skillweave/planning/ with backlog/doing/done → exists=True."""
    root = board_path.parent.parent
    info = detect_planning_repository(root)
    assert info.exists is True
    assert info.path == board_path
    assert info.is_writable is True


def test_detect_planning_repository_scans_tickets(populated_board: Path):
    """Existing tickets are discovered by scan."""
    root = populated_board.parent.parent
    info = detect_planning_repository(root)
    assert info.exists is True
    assert "FEAT-001" in info.tickets
    assert info.tickets["FEAT-001"] == "Existing Feature"


# ---------------------------------------------------------------------------
# S2 — Handshake terminal states
# ---------------------------------------------------------------------------


def test_handshake_not_applicable_when_no_board(tmp_path: Path):
    """No planning board → terminal=not_applicable."""
    result = perform_handshake("My PRD", project_root=tmp_path)
    assert result.terminal == HandshakeTerminal.NOT_APPLICABLE
    assert result.is_authoritative is False
    assert "No planning repository" in result.evidence.reason


def test_handshake_needs_authority_when_board_not_writable(
    board_path: Path, monkeypatch
):
    """Board exists but not writable → terminal=needs_authority."""
    root = board_path.parent.parent

    # Make board non-writable (r-x: traversable but not writable)
    original_mode = board_path.stat().st_mode
    board_path.chmod(0o555)  # r-x: readable + traversable, no write
    try:
        result = perform_handshake("My PRD", project_root=root)
        assert result.terminal == HandshakeTerminal.NEEDS_AUTHORITY
        assert result.is_authoritative is False
        assert "write authority" in result.evidence.reason
        assert str(board_path) in result.evidence.planning_board_path
    finally:
        board_path.chmod(original_mode)


def test_handshake_linked_when_duplicate_title(populated_board: Path):
    """Duplicate title → terminal=linked with existing ticket_id."""
    root = populated_board.parent.parent
    result = perform_handshake("Existing Feature", project_root=root)
    assert result.terminal == HandshakeTerminal.LINKED
    assert result.is_authoritative is True
    assert result.evidence.ticket_id == "FEAT-001"
    assert result.evidence.ticket_title == "Existing Feature"


def test_handshake_created_when_no_duplicate(board_path: Path):
    """Board writable, no duplicate → terminal=created."""
    root = board_path.parent.parent
    result = perform_handshake("Brand New PRD", project_root=root)
    assert result.terminal == HandshakeTerminal.CREATED
    assert result.is_authoritative is True
    assert result.evidence.ticket_title == "Brand New PRD"
    assert result.evidence.planning_board_path == str(board_path)


# ---------------------------------------------------------------------------
# S3 — Duplicate-title fixture
# ---------------------------------------------------------------------------


def test_find_ticket_by_title_exact_match(populated_board: Path):
    """Exact case-insensitive title match returns ticket ID."""
    tickets = _scan_tickets(populated_board)
    tid = _find_ticket_by_title(tickets, "Existing Feature")
    assert tid == "FEAT-001"


def test_find_ticket_by_title_case_insensitive(populated_board: Path):
    """Case difference still matches."""
    tickets = _scan_tickets(populated_board)
    tid = _find_ticket_by_title(tickets, "existing feature")
    assert tid == "FEAT-001"


def test_find_ticket_by_title_no_match(populated_board: Path):
    """No matching title returns None."""
    tickets = _scan_tickets(populated_board)
    tid = _find_ticket_by_title(tickets, "Nonexistent")
    assert tid is None


def test_find_ticket_by_title_returns_first_match(populated_board: Path):
    """Two tickets with same (case-different) title — first wins."""
    create_ticket_on_board(
        populated_board,
        ticket_id="FEAT-002",
        title="existing feature",
    )
    tickets = _scan_tickets(populated_board)
    tid = _find_ticket_by_title(tickets, "Existing Feature")
    # Should match the first one found (FEAT-001)
    assert tid == "FEAT-001"


# ---------------------------------------------------------------------------
# Concurrent-create fixture
# ---------------------------------------------------------------------------


def test_concurrent_create_lock_prevents_duplicate_handshake(board_path: Path):
    """Advisory lock prevents a second handshake while first is running."""
    lock = _acquire_lock(board_path, timeout_seconds=1.0)
    assert lock is not None

    # Second acquire should fail within timeout
    second_lock = _acquire_lock(board_path, timeout_seconds=0.5)
    assert second_lock is None

    _release_lock(lock)


def test_concurrent_create_lock_release(board_path: Path):
    """After release, another acquire succeeds."""
    lock = _acquire_lock(board_path)
    assert lock is not None
    _release_lock(lock)

    lock2 = _acquire_lock(board_path)
    assert lock2 is not None
    _release_lock(lock2)


def test_concurrent_create_stale_lock_cleared(board_path: Path):
    """A stale lock (>30 s mtime) is removed on acquire attempt."""
    stale = board_path / ".handshake.lock"
    stale.touch()
    # Set mtime to 60 seconds ago
    old_time = time.time() - 60
    os_util = __import__("os")
    os_util.utime(str(stale), (old_time, old_time))

    lock = _acquire_lock(board_path, timeout_seconds=1.0)
    assert lock is not None
    assert lock.exists()
    _release_lock(lock)


def test_perform_handshake_with_lock_contention(board_path: Path):
    """Handshake still returns a result even when lock is held."""
    root = board_path.parent.parent

    # Hold the lock externally
    external_lock = _acquire_lock(board_path, timeout_seconds=1.0)
    assert external_lock is not None

    # Handshake should still return (created with warning)
    result = perform_handshake("Contended PRD", project_root=root)
    assert result.terminal in (
        HandshakeTerminal.CREATED,
        HandshakeTerminal.LINKED,
    )
    # The reason should mention the lock issue
    assert "lock" in result.evidence.reason

    _release_lock(external_lock)


# ---------------------------------------------------------------------------
# Ticket creation and linkage
# ---------------------------------------------------------------------------


def test_create_ticket_on_board(board_path: Path):
    """Creates a ticket file with correct frontmatter."""
    path = create_ticket_on_board(
        board_path,
        ticket_id="FEAT-042",
        title="My Feature",
        content="## Description\nThis is a feature.",
    )
    assert path.exists()
    assert path.name == "FEAT-042.md"
    content = path.read_text()
    assert "id: FEAT-042" in content
    assert 'title: "My Feature"' in content
    assert "status: backlog" in content
    assert "This is a feature." in content


def test_create_ticket_on_board_raises_on_duplicate(board_path: Path):
    """Creating a ticket that already exists raises HandshakeError."""
    create_ticket_on_board(board_path, "FEAT-001", "First")
    with pytest.raises(HandshakeError, match="already exists"):
        create_ticket_on_board(board_path, "FEAT-001", "Second")


def test_create_ticket_on_board_raises_on_missing_dir(tmp_path: Path):
    """Creating a ticket on a non-existent board dir raises HandshakeError."""
    with pytest.raises(HandshakeError, match="does not exist"):
        create_ticket_on_board(tmp_path, "FEAT-001", "Missing")


def test_link_ticket_creates_marker(board_path: Path):
    """Linking a ticket creates a marker file."""
    link_file = link_ticket(board_path, "FEAT-001")
    assert link_file.exists()
    assert link_file.parent.name == ".links"
    content = link_file.read_text()
    assert "linked_ticket: FEAT-001" in content


def test_link_ticket_with_prd_path(board_path: Path, tmp_path: Path):
    """Link marker includes optional prd_path."""
    prd_path = tmp_path / "prd.json"
    prd_path.write_text("{}")
    link_file = link_ticket(board_path, "FEAT-001", prd_path=prd_path)
    content = link_file.read_text()
    assert str(prd_path) in content


# ---------------------------------------------------------------------------
# Evidence serialization round-trip
# ---------------------------------------------------------------------------


def test_handshake_evidence_to_from_dict():
    """HandshakeEvidence serializes and deserializes correctly."""
    evidence = HandshakeEvidence(
        terminal=HandshakeTerminal.CREATED,
        reason="Test reason",
        planning_board_path="/path/to/board",
        ticket_id="FEAT-001",
        ticket_title="Test",
    )
    d = evidence.to_dict()
    assert d["terminal"] == "created"
    assert d["ticket_id"] == "FEAT-001"

    restored = HandshakeEvidence.from_dict(d)
    assert restored.terminal == HandshakeTerminal.CREATED
    assert restored.reason == "Test reason"
    assert restored.ticket_id == "FEAT-001"


def test_handshake_result_to_dict():
    """HandshakeResult serializes correctly."""
    evidence = HandshakeEvidence(
        terminal=HandshakeTerminal.NOT_APPLICABLE,
        reason="No board.",
    )
    result = HandshakeResult(
        terminal=HandshakeTerminal.NOT_APPLICABLE,
        evidence=evidence,
        is_authoritative=False,
    )
    d = result.to_dict()
    assert d["terminal"] == "not_applicable"
    assert d["is_authoritative"] is False
    assert d["evidence"]["reason"] == "No board."


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_prd_title_handles_gracefully(board_path: Path):
    """Empty PRD title does not crash the handshake."""
    root = board_path.parent.parent
    result = perform_handshake("", project_root=root)
    assert result.terminal in (
        HandshakeTerminal.CREATED,
        HandshakeTerminal.LINKED,
    )


def test_special_characters_in_ticket_title(board_path: Path):
    """Special characters in ticket title are handled."""
    root = board_path.parent.parent
    title = "Feature: <Unlikely> & \"Edge\" Case!"
    result = perform_handshake(title, project_root=root)
    assert result.terminal == HandshakeTerminal.CREATED
    assert result.evidence.ticket_title == title
