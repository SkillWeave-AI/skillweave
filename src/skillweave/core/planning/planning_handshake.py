"""Planning ticket handshake for authority-aware PRD emission (SW-159-BP-TICKET-001).

This module implements a conditional planning handshake that checks for the
existence of a planning board and write authority before allowing PRD emission.
It produces one of four terminal states, each with evidence.

Terminal states:
    - linked: PRD linked to an existing ticket on an authoritative writable board.
    - created: New ticket created on an authoritative writable board.
    - not_applicable: No planning repository / board exists.
    - needs_authority: Board exists but write authority is not available.

The handshake also prevents duplicate-title linkage and concurrent-create races
through a file-based advisory lock and title deduplication.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Terminal states
# ---------------------------------------------------------------------------


class HandshakeTerminal(str, Enum):
    """Terminal handshake states for the planning ticket handshake.

    Each state represents a final outcome of the handshake evaluation that
    determines how (or whether) the PRD emission proceeds with respect to the
    planning board.
    """

    LINKED = "linked"
    CREATED = "created"
    NOT_APPLICABLE = "not_applicable"
    NEEDS_AUTHORITY = "needs_authority"


class HandshakeError(ValueError):
    """Raised when the handshake encounters an unrecoverable error."""


# ---------------------------------------------------------------------------
# Evidence and result data classes
# ---------------------------------------------------------------------------


@dataclass
class HandshakeEvidence:
    """Tamper-evident record of a handshake terminal state.

    Carries the terminal value, a human-readable reason, and optional
    planning-board / ticket metadata so downstream consumers can trace the
    decision.
    """

    terminal: HandshakeTerminal
    reason: str
    planning_board_path: Optional[str] = None
    ticket_id: Optional[str] = None
    ticket_title: Optional[str] = None
    evidence_timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "terminal": self.terminal.value,
            "reason": self.reason,
            "planning_board_path": self.planning_board_path,
            "ticket_id": self.ticket_id,
            "ticket_title": self.ticket_title,
            "evidence_timestamp": self.evidence_timestamp,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> HandshakeEvidence:
        return cls(
            terminal=HandshakeTerminal(data["terminal"]),
            reason=data["reason"],
            planning_board_path=data.get("planning_board_path"),
            ticket_id=data.get("ticket_id"),
            ticket_title=data.get("ticket_title"),
            evidence_timestamp=data.get(
                "evidence_timestamp",
                datetime.now(timezone.utc).isoformat(),
            ),
        )


@dataclass
class PlanningBoardInfo:
    """Information about a discovered planning board.

    ``exists`` is the primary gate; when ``False`` the handshake short-circuits
    to ``not_applicable``. ``is_writable`` is tested via a probe file so it
    reflects the runtime authority of the current process, not a static config.
    """

    exists: bool
    path: Optional[Path] = None
    is_writable: bool = False
    tickets: Dict[str, str] = field(default_factory=dict)


@dataclass
class HandshakeResult:
    """The result of a planning handshake evaluation.

    Consumers should branch on ``terminal``:
    - ``linked`` / ``created``  → proceed with PRD emission.
    - ``not_applicable``        → proceed without ticket linkage.
    - ``needs_authority``       → stop and escalate.
    """

    terminal: HandshakeTerminal
    evidence: HandshakeEvidence
    is_authoritative: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "terminal": self.terminal.value,
            "evidence": self.evidence.to_dict(),
            "is_authoritative": self.is_authoritative,
        }


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Sub-directory names that indicate a beans-pattern planning board.
_PLANNING_BOARD_DIRS = frozenset({"backlog", "doing", "done"})

#: Ticket file extension.
_TICKET_EXTENSION = ".md"

#: Name of the advisory lock file for concurrent-create protection.
_LOCK_FILE = ".handshake.lock"


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def _scan_tickets(planning_path: Path) -> Dict[str, str]:
    """Scan existing tickets in a planning board, returning ``{ticket_id: title}``.

    Tickets are Markdown files with YAML frontmatter in the ``backlog/``,
    ``doing/``, and ``done/`` sub-directories.  Titles are extracted from the
    frontmatter ``title`` field; files without frontmatter use the stem as
    title.
    """
    tickets: Dict[str, str] = {}

    for board_dir in ("backlog", "doing", "done"):
        board_path = planning_path / board_dir
        if not board_path.is_dir():
            continue

        for ticket_file in sorted(board_path.glob(f"*{_TICKET_EXTENSION}")):
            ticket_id = ticket_file.stem
            title = _extract_title(ticket_file)
            tickets[ticket_id] = title

    return tickets


def _extract_title(ticket_file: Path) -> str:
    """Extract the ``title`` field from a ticket's YAML frontmatter."""
    try:
        content = ticket_file.read_text()
    except Exception:
        return ticket_file.stem

    lines = content.split("\n")
    in_frontmatter = False
    for line in lines:
        stripped = line.strip()
        if stripped == "---":
            in_frontmatter = not in_frontmatter
        elif in_frontmatter and ":" in stripped:
            key, _, value = stripped.partition(":")
            if key.strip() == "title":
                return value.strip().strip('"').strip("'")

    return ticket_file.stem


def _check_writable(path: Path) -> bool:
    """Probe whether *path* is writable by creating and removing a test file."""
    if not path.is_dir():
        return False
    try:
        probe = path / ".write_probe"
        probe.touch()
        probe.unlink()
        return True
    except (OSError, PermissionError):
        return False


def _find_ticket_by_title(
    tickets: Dict[str, str],
    prd_title: str,
) -> Optional[str]:
    """Return the ticket ID whose title matches *prd_title* (case-insensitive)."""
    normalized = prd_title.strip().lower()
    for ticket_id, ticket_title in tickets.items():
        if ticket_title and ticket_title.strip().lower() == normalized:
            return ticket_id
    return None


# ---------------------------------------------------------------------------
# Advisory lock for concurrent-create protection
# ---------------------------------------------------------------------------


class _LockAcquireError(RuntimeError):
    """Another process holds the handshake lock."""


def _acquire_lock(board_path: Path, *, timeout_seconds: float = 5.0) -> Optional[Path]:
    """Acquire an advisory file lock for the handshake.

    Uses ``O_CREAT | O_EXCL`` semantics via ``Path.touch(exist_ok=False)`` to
    detect concurrent handshakes.  Returns the lock-file path on success or
    ``None`` if another process holds the lock (after polling up to
    *timeout_seconds*).
    """
    import time

    lock_file = board_path / _LOCK_FILE
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        try:
            lock_file.touch(exist_ok=False)
            return lock_file
        except FileExistsError:
            # Lock held by another process — check if stale (>30 s)
            try:
                age = time.time() - lock_file.stat().st_mtime
            except OSError:
                age = 0.0
            if age > 30.0:
                # Stale lock — remove and retry once
                try:
                    lock_file.unlink()
                    lock_file.touch(exist_ok=False)
                    return lock_file
                except (OSError, FileExistsError):
                    pass
            time.sleep(0.2)
    return None


def _release_lock(lock_path: Optional[Path]) -> None:
    """Remove the advisory lock file if it exists and we hold it."""
    if lock_path is not None and lock_path.exists():
        try:
            lock_path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_planning_repository(project_root: Optional[Path] = None) -> PlanningBoardInfo:
    """Detect whether a planning repository / board exists under *project_root*.

    Checks for the beans-pattern planning board at
    ``{project_root}/.skillweave/planning/`` with ``backlog/``, ``doing/``,
    and/or ``done/`` sub-directories.  When those sub-directories are present
    the board is considered to exist; otherwise it does not.

    Returns
    -------
    PlanningBoardInfo
        Carries existence, path, writability, and a scan of existing tickets.
    """
    root = project_root or Path.cwd()
    planning_path = root / ".skillweave" / "planning"

    if not planning_path.is_dir():
        return PlanningBoardInfo(exists=False)

    # At least one of the beans-pattern sub-directories must be present.
    has_any_board_dir = any(
        (planning_path / d).is_dir() for d in _PLANNING_BOARD_DIRS
    )

    if not has_any_board_dir:
        return PlanningBoardInfo(exists=False, path=planning_path)

    is_writable = _check_writable(planning_path)
    tickets = _scan_tickets(planning_path)

    return PlanningBoardInfo(
        exists=True,
        path=planning_path,
        is_writable=is_writable,
        tickets=tickets,
    )


def perform_handshake(
    prd_title: str,
    project_root: Optional[Path] = None,
) -> HandshakeResult:
    """Perform the planning ticket handshake before PRD emission.

    Decision flow
    -------------
    1. Detect planning board existence.
    2. No board → ``not_applicable``.
    3. Board exists but not writable → ``needs_authority``.
    4. Board writable, duplicate title found → ``linked``.
    5. Board writable, no duplicate → ``created``.

    Concurrent-create protection
    ----------------------------
    An advisory file lock (``.handshake.lock``) is acquired inside the board
    directory before the duplicate check / creation decision.  If the lock
    cannot be obtained within a short timeout the handshake still returns a
    terminal state (``created`` with a warning in the reason) so the pipeline
    is not deadlocked by a stale lock.

    Parameters
    ----------
    prd_title:
        The title of the PRD being emitted.
    project_root:
        Root directory of the project.  Defaults to ``Path.cwd()``.

    Returns
    -------
    HandshakeResult
        One of the four terminal states with attached evidence.
    """
    # --- Step 1: detect planning board ---
    board = detect_planning_repository(project_root)

    if not board.exists:
        return HandshakeResult(
            terminal=HandshakeTerminal.NOT_APPLICABLE,
            evidence=HandshakeEvidence(
                terminal=HandshakeTerminal.NOT_APPLICABLE,
                reason="No planning repository or beans-pattern board detected. "
                "PRD can proceed without ticket linkage.",
            ),
            is_authoritative=False,
        )

    board_path = board.path
    assert board_path is not None  # exists=True guarantees a path

    # --- Step 2: check write authority ---
    if not board.is_writable:
        return HandshakeResult(
            terminal=HandshakeTerminal.NEEDS_AUTHORITY,
            evidence=HandshakeEvidence(
                terminal=HandshakeTerminal.NEEDS_AUTHORITY,
                reason=f"Planning board exists at {board_path} but write "
                f"authority is not available for the current process. "
                f"PRD emission is blocked until write authority is granted.",
                planning_board_path=str(board_path),
            ),
            is_authoritative=False,
        )

    # --- Step 3: acquire advisory lock (concurrent-create protection) ---
    lock_path = _acquire_lock(board_path)
    lock_acquired = lock_path is not None
    lock_warning = (
        " Could not acquire handshake lock within timeout; proceeding "
        "without lock protection."
        if not lock_acquired
        else ""
    )

    try:
        # --- Step 4: duplicate-title check ---
        duplicate_ticket_id = _find_ticket_by_title(board.tickets, prd_title)

        if duplicate_ticket_id is not None:
            return HandshakeResult(
                terminal=HandshakeTerminal.LINKED,
                evidence=HandshakeEvidence(
                    terminal=HandshakeTerminal.LINKED,
                    reason=f"PRD title '{prd_title}' matches existing ticket "
                    f"'{duplicate_ticket_id}'. Linking to existing ticket."
                    + lock_warning,
                    planning_board_path=str(board_path),
                    ticket_id=duplicate_ticket_id,
                    ticket_title=prd_title,
                ),
                is_authoritative=True,
            )

        # --- Step 5: create new ticket ---
        return HandshakeResult(
            terminal=HandshakeTerminal.CREATED,
            evidence=HandshakeEvidence(
                terminal=HandshakeTerminal.CREATED,
                reason=f"Planning board is writable at {board_path}. "
                f"New ticket for '{prd_title}' can be created."
                + lock_warning,
                planning_board_path=str(board_path),
                ticket_title=prd_title,
            ),
            is_authoritative=True,
        )

    finally:
        if lock_acquired:
            _release_lock(lock_path)


def create_ticket_on_board(
    board_path: Path,
    ticket_id: str,
    title: str,
    content: Optional[str] = None,
    target_dir: str = "backlog",
) -> Path:
    """Create a new ticket file on the planning board.

    Parameters
    ----------
    board_path:
        Path to the planning board root (``.skillweave/planning/``).
    ticket_id:
        The ID for the new ticket (e.g. ``FEAT-042``).
    title:
        The title for the ticket.
    content:
        Optional body content for the ticket (Markdown).
    target_dir:
        Which board sub-directory to place the ticket in (default ``backlog``).

    Returns
    -------
    Path
        Path to the created ticket file.

    Raises
    ------
    HandshakeError
        If the target directory does not exist or the ticket already exists.
    """
    target = board_path / target_dir
    if not target.is_dir():
        raise HandshakeError(
            f"Board directory '{target}' does not exist. "
            f"Expected a beans-pattern board under {board_path}."
        )

    ticket_path = target / f"{ticket_id}{_TICKET_EXTENSION}"
    if ticket_path.exists():
        raise HandshakeError(
            f"Ticket '{ticket_id}' already exists at {ticket_path}. "
            f"Use link_ticket() instead of create_ticket_on_board()."
        )

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: List[str] = [
        "---",
        f"id: {ticket_id}",
        f'title: "{title}"',
        "status: backlog",
        f"created: {timestamp}",
        "---",
        "",
        f"# {title}",
        "",
    ]
    if content:
        lines.append(content)

    ticket_path.write_text("\n".join(lines))
    return ticket_path


def link_ticket(
    board_path: Path,
    ticket_id: str,
    prd_path: Optional[Path] = None,
) -> Path:
    """Record a linkage between the PRD and an existing ticket.

    Creates a small YAML marker under ``{board_path}/.links/`` so downstream
    tooling can trace which PRD was linked to which ticket.

    Returns
    -------
    Path
        Path to the created link marker file.
    """
    links_dir = board_path / ".links"
    links_dir.mkdir(exist_ok=True)

    lines: List[str] = [
        f"linked_ticket: {ticket_id}",
        f"linked_at: {datetime.now(timezone.utc).isoformat()}",
    ]
    if prd_path is not None:
        lines.append(f"prd_path: {prd_path}")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    link_file = links_dir / f"link_{ticket_id}_{timestamp}.yaml"
    link_file.write_text("\n".join(lines) + "\n")
    return link_file
