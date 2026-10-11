"""Read-only multi-repository workspace inventory (SW-155-HEALTH-001).

Covers the acceptance surface of the workspace *inventory* that sits on top of
the SW-155 / SW-WORKSPACE-001 manifest contract (consumed read-only):

* every inventory row reports a **finite classification plus reason** and
  covers: worktree registration, filesystem existence, dirty/clean,
  branch/detached, ahead/behind, commit reachability, lease, active-process
  evidence, session evidence and disk use;
* unknown / unavailable evidence is a **finite, explicit** state and never
  silently treated as safe;
* present-but-unregistered workspace-like paths and legacy
  (``.sw-worktrees`` / ``wt-*``) locations are surfaced, never moved;
* the inventory is read-only: it creates no directory and mutates no
  git/filesystem state.

All fixtures live under ``tempfile.TemporaryDirectory``; the repository's real
worktrees are never touched.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_src = Path(__file__).resolve().parent.parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from skillweave.repo_health.worktrees import (  # noqa: E402
    Classification,
    DirtyState,
    DiskState,
    EvidenceState,
    ExistenceState,
    HeadState,
    LeaseState,
    LocationKind,
    ReachabilityState,
    RegistrationState,
    UpstreamState,
    WorkspaceInventory,
    WorkspaceRow,
    inventory_workspaces,
)
from skillweave.routing.workspace import Lease, WorkspaceManifest  # noqa: E402

NOW = "2026-09-24T00:00:00+00:00"
FUTURE = "2030-01-01T00:00:00+00:00"
PAST = "2020-01-01T00:00:00+00:00"


# --------------------------------------------------------------------------- #
# git helpers (fixtures only - the inventory under test never shells out to a
# mutating command)
# --------------------------------------------------------------------------- #
def _run(*args, cwd=None, check=True):
    return subprocess.run(
        list(args),
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=check,
    )


def _git(repo, *args, check=True):
    return _run("git", *args, cwd=repo, check=check)


def _identity(repo):
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")


def _commit(repo, name, message):
    (Path(repo) / name).write_text(message + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _init_repo(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    _run("git", "init", "-q", "-b", "main", str(path))
    _identity(path)
    return _commit(path, "seed.txt", "init")


def _origin_fixture(tmp):
    """Bare origin seeded with one commit; returns (origin, collection, sha)."""
    tmp = Path(tmp)
    origin = tmp / "origin.git"
    _run("git", "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp / "seed"
    sha = _init_repo(seed)
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "-u", "origin", "main")
    collection = tmp / "collection"
    collection.mkdir()
    return origin, collection, sha


def _manifest(base_sha, head_sha, *, lease_until=FUTURE, heartbeat=NOW):
    return WorkspaceManifest(
        repo="repo",
        base_sha=base_sha,
        head_sha=head_sha,
        branch="feat",
        run="run1",
        lane="lane1",
        session="sess-1",
        write_scope=["src/"],
        lease=Lease(lease_until=lease_until, owner="host"),
        heartbeat=heartbeat,
        state="active",
        retention="temporary",
    )


def _inventory(collection, *, manifests=None, processes=None, now=NOW):
    return inventory_workspaces(
        str(collection), manifests=manifests, processes=processes, now=now
    )


def _snapshot(root):
    """Structural snapshot: relative path, size and mode of every entry."""
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(dirnames) + sorted(filenames):
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            entries.append((os.path.relpath(full, root), st.st_size, st.st_mode))
    return sorted(entries)


# --------------------------------------------------------------------------- #
# 1. every required dimension, finite classification + reason
# --------------------------------------------------------------------------- #
def test_row_reports_all_required_dimensions_and_is_healthy():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        collection = tmp / "collection"
        primary = collection / "repo"
        head = _init_repo(primary)

        wt = collection / ".worktrees" / "repo" / "run1" / "lane1"
        _git(primary, "worktree", "add", "-q", "-b", "feat", str(wt))

        report = _inventory(
            collection,
            manifests={str(wt): _manifest(head, head)},
            processes={str(wt): [os.getpid()]},
        )

        assert isinstance(report, WorkspaceInventory)
        row = report.row_for(str(wt))
        assert isinstance(row, WorkspaceRow)

        # each dimension is a finite enum member (never a bare None sentinel)
        assert row.registration is RegistrationState.REGISTERED
        assert row.existence is ExistenceState.PRESENT
        assert row.dirtiness is DirtyState.CLEAN
        assert row.head is HeadState.BRANCH
        assert row.upstream is UpstreamState.NO_UPSTREAM
        assert row.reachability is ReachabilityState.REACHABLE
        assert row.lease is LeaseState.ACTIVE
        assert row.process is EvidenceState.PRESENT
        assert row.session is EvidenceState.PRESENT
        assert row.disk is DiskState.MEASURED
        assert isinstance(row.disk_bytes, int) and row.disk_bytes > 0

        assert row.classification is Classification.HEALTHY
        assert row.reason
        assert row.evidence_available is True


def test_dirty_worktree_is_flagged_dirty():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        collection = tmp / "collection"
        primary = collection / "repo"
        head = _init_repo(primary)
        wt = collection / ".worktrees" / "repo" / "run1" / "lane1"
        _git(primary, "worktree", "add", "-q", "-b", "feat", str(wt))
        (wt / "scratch.txt").write_text("uncommitted\n")

        row = _inventory(
            collection,
            manifests={str(wt): _manifest(head, head)},
            processes={str(wt): [os.getpid()]},
        ).row_for(str(wt))

        assert row.dirtiness is DirtyState.DIRTY
        assert row.classification is Classification.DIRTY
        assert "dirty" in row.reason.lower()


def test_detached_head_is_flagged():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        collection = tmp / "collection"
        primary = collection / "repo"
        head = _init_repo(primary)
        wt = collection / ".worktrees" / "repo" / "run1" / "lane1"
        _git(primary, "worktree", "add", "-q", "-b", "feat", str(wt))
        _git(wt, "checkout", "-q", "--detach")

        row = _inventory(
            collection,
            manifests={str(wt): _manifest(head, head)},
            processes={str(wt): [os.getpid()]},
        ).row_for(str(wt))

        assert row.head is HeadState.DETACHED
        assert row.classification is Classification.DETACHED


def test_registered_but_missing_worktree_is_stale():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        collection = tmp / "collection"
        primary = collection / "repo"
        head = _init_repo(primary)
        wt = collection / ".worktrees" / "repo" / "run1" / "lane1"
        _git(primary, "worktree", "add", "-q", "-b", "feat", str(wt))

        # Remove the materialised directory WITHOUT pruning the registration.
        shutil.rmtree(wt)

        row = _inventory(
            collection,
            manifests={str(wt): _manifest(head, head)},
            processes={str(wt): [os.getpid()]},
        ).row_for(str(wt))

        assert row.registration is RegistrationState.REGISTERED
        assert row.existence is ExistenceState.MISSING
        assert row.classification is Classification.STALE


def test_unreachable_commit_is_orphaned():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        collection = tmp / "collection"
        primary = collection / "repo"
        _init_repo(primary)
        wt = collection / ".worktrees" / "repo" / "run1" / "lane1"
        _git(primary, "worktree", "add", "-q", "-b", "feat", str(wt))

        bogus = "0" * 40
        row = _inventory(
            collection,
            manifests={str(wt): _manifest(bogus, bogus)},
            processes={str(wt): [os.getpid()]},
        ).row_for(str(wt))

        assert row.reachability is ReachabilityState.UNREACHABLE
        assert row.classification is Classification.ORPHANED


# --------------------------------------------------------------------------- #
# 2. unknown evidence is explicit and never silently safe
# --------------------------------------------------------------------------- #
def test_unknown_process_evidence_blocks_a_safe_classification():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        collection = tmp / "collection"
        primary = collection / "repo"
        head = _init_repo(primary)
        wt = collection / ".worktrees" / "repo" / "run1" / "lane1"
        _git(primary, "worktree", "add", "-q", "-b", "feat", str(wt))

        # No process probe supplied -> process evidence is UNKNOWN, not ABSENT.
        row = _inventory(
            collection, manifests={str(wt): _manifest(head, head)}, processes=None
        ).row_for(str(wt))

        assert row.process is EvidenceState.UNKNOWN
        assert row.classification is Classification.UNKNOWN
        assert row.evidence_available is False
        assert "process" in row.reason.lower()


def test_unparseable_lease_is_unknown_not_absent():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        collection = tmp / "collection"
        primary = collection / "repo"
        head = _init_repo(primary)
        wt = collection / ".worktrees" / "repo" / "run1" / "lane1"
        _git(primary, "worktree", "add", "-q", "-b", "feat", str(wt))

        manifest = _manifest(head, head)
        manifest.lease = Lease(lease_until="not-a-timestamp", owner="host")

        row = _inventory(
            collection,
            manifests={str(wt): manifest},
            processes={str(wt): [os.getpid()]},
        ).row_for(str(wt))

        assert row.lease is LeaseState.UNKNOWN
        assert row.classification is Classification.UNKNOWN


def test_clean_worktree_with_unavailable_upstream_is_unknown():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        collection = tmp / "collection"
        primary = collection / "repo"
        head = _init_repo(primary)
        wt = collection / ".worktrees" / "repo" / "run1" / "lane1"
        _git(primary, "worktree", "add", "-q", "-b", "feat", str(wt))

        # Configure upstream to a non-existent remote ref so that @{upstream}
        # resolves as a symbolic name but the tracking ref does not exist,
        # making upstream evidence genuinely unavailable.
        _git(wt, "remote", "add", "origin", "http://nonexistent/repo.git")
        _git(wt, "config", "branch.feat.remote", "origin")
        _git(wt, "config", "branch.feat.merge", "refs/heads/feat")

        row = _inventory(
            collection,
            manifests={str(wt): _manifest(head, head)},
            processes={str(wt): [os.getpid()]},
        ).row_for(str(wt))

        # Clean tree -> no DIRTY finding; genuinely unavailable upstream
        # evidence -> UNKNOWN classification.
        assert row.dirtiness is DirtyState.CLEAN
        assert row.upstream is UpstreamState.UNKNOWN
        assert row.classification is Classification.UNKNOWN
        assert "upstream" in row.reason


# --------------------------------------------------------------------------- #
# locale regression: git messages must be locale-independent
# --------------------------------------------------------------------------- #
def test_german_locale_upstream_is_no_upstream_not_unknown():
    """Regression: _git_read forces LC_ALL=C so git messages never depend on
    the user's locale.  Running under German locale must still detect
    NO_UPSTREAM correctly (not fall through to UNKNOWN)."""
    old_lc = os.environ.get("LC_ALL")
    old_lang = os.environ.get("LANGUAGE")
    os.environ["LC_ALL"] = "de_DE.UTF-8"
    os.environ["LANGUAGE"] = "de"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            collection = tmp / "collection"
            primary = collection / "repo"
            head = _init_repo(primary)

            wt = collection / ".worktrees" / "repo" / "run1" / "lane1"
            _git(primary, "worktree", "add", "-q", "-b", "feat", str(wt))

            row = _inventory(
                collection,
                manifests={str(wt): _manifest(head, head)},
                processes={str(wt): [os.getpid()]},
            ).row_for(str(wt))

            # Even with German locale in the process environment, the git
            # subprocess inside _git_read forces LC_ALL=C, so upstream
            # detection works correctly.
            assert row.upstream is UpstreamState.NO_UPSTREAM, (
                f"expected NO_UPSTREAM, got {row.upstream}"
            )
            assert row.classification is Classification.HEALTHY, (
                f"expected HEALTHY, got {row.classification}: {row.reason}"
            )
    finally:
        if old_lc is not None:
            os.environ["LC_ALL"] = old_lc
        else:
            os.environ.pop("LC_ALL", None)
        if old_lang is not None:
            os.environ["LANGUAGE"] = old_lang
        else:
            os.environ.pop("LANGUAGE", None)


def test_stale_session_evidence_is_stale_not_healthy():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        collection = tmp / "collection"
        primary = collection / "repo"
        head = _init_repo(primary)
        wt = collection / ".worktrees" / "repo" / "run1" / "lane1"
        _git(primary, "worktree", "add", "-q", "-b", "feat", str(wt))

        manifest = _manifest(head, head, heartbeat=PAST)
        row = _inventory(
            collection,
            manifests={str(wt): manifest},
            processes={str(wt): [os.getpid()]},
        ).row_for(str(wt))

        assert row.session is EvidenceState.ABSENT
        assert row.classification is Classification.STALE


def test_expired_lease_is_stale():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        collection = tmp / "collection"
        primary = collection / "repo"
        head = _init_repo(primary)
        wt = collection / ".worktrees" / "repo" / "run1" / "lane1"
        _git(primary, "worktree", "add", "-q", "-b", "feat", str(wt))

        row = _inventory(
            collection,
            manifests={str(wt): _manifest(head, head, lease_until=PAST)},
            processes={str(wt): [os.getpid()]},
        ).row_for(str(wt))

        assert row.lease is LeaseState.EXPIRED
        assert row.classification is Classification.STALE


def test_absent_manifest_is_unmanaged_not_healthy():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        collection = tmp / "collection"
        primary = collection / "repo"
        _init_repo(primary)
        wt = collection / ".worktrees" / "repo" / "run1" / "lane1"
        _git(primary, "worktree", "add", "-q", "-b", "feat", str(wt))

        row = _inventory(
            collection, processes={str(wt): [os.getpid()]}
        ).row_for(str(wt))

        assert row.lease is LeaseState.ABSENT
        assert row.classification is Classification.UNMANAGED
        assert row.classification is not Classification.HEALTHY


# --------------------------------------------------------------------------- #
# 3. unregistered / legacy locations are surfaced, never moved
# --------------------------------------------------------------------------- #
def test_unregistered_workspace_like_path_is_orphaned_and_reported():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        collection = tmp / "collection"
        primary = collection / "repo"
        _init_repo(primary)

        # A git worktree working directory that was never registered.
        stray = collection / ".worktrees" / "repo" / "run9" / "lane9"
        stray.mkdir(parents=True)
        _run("git", "init", "-q", "-b", "main", str(stray))
        _identity(stray)
        _commit(stray, "x.txt", "stray")

        report = _inventory(collection)
        row = report.row_for(str(stray))

        assert row.registration is RegistrationState.UNREGISTERED
        assert row.classification is Classification.ORPHANED
        assert str(stray) in report.unregistered_paths


def test_legacy_locations_are_surfaced_and_left_in_place():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        collection = tmp / "collection"
        primary = collection / "repo"
        _init_repo(primary)

        legacy_in_repo = primary / ".sw-worktrees" / "old"
        legacy_wt = collection / "wt-152"
        legacy_wt_dot = collection / ".worktrees" / "wt-x"
        for path in (legacy_in_repo, legacy_wt, legacy_wt_dot):
            path.mkdir(parents=True)

        report = _inventory(collection)
        kinds = {row.kind for row in report.rows}
        assert LocationKind.LEGACY_SW_WORKTREES in kinds
        assert LocationKind.LEGACY_WT in kinds

        # surfaced paths must still exist afterwards - nothing moved/deleted
        for path in (legacy_in_repo, legacy_wt, legacy_wt_dot):
            assert path.exists(), f"legacy location {path} was removed"


# --------------------------------------------------------------------------- #
# 4. ahead / behind / diverged
# --------------------------------------------------------------------------- #
def test_upstream_up_to_date_then_ahead():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        origin, collection, _ = _origin_fixture(tmp)
        primary = collection / "repo"
        _run("git", "clone", "-q", str(origin), str(primary))
        _identity(primary)

        wt = collection / ".worktrees" / "repo" / "run1" / "lane1"
        _git(primary, "worktree", "add", "-q", "-b", "feat", str(wt))
        _git(wt, "branch", "--set-upstream-to=origin/main")

        row = _inventory(collection).row_for(str(wt))
        assert row.upstream is UpstreamState.UP_TO_DATE

        _commit(wt, "a.txt", "local work")
        assert _inventory(collection).row_for(str(wt)).upstream is UpstreamState.AHEAD


def test_upstream_behind_and_diverged():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        origin, collection, _ = _origin_fixture(tmp)
        primary = collection / "repo"
        _run("git", "clone", "-q", str(origin), str(primary))
        _identity(primary)

        # Advance the origin from a second clone.
        other = tmp / "other"
        _run("git", "clone", "-q", str(origin), str(other))
        _identity(other)
        _commit(other, "b.txt", "remote work")
        _git(other, "push", "-q", "origin", "main")
        _git(primary, "fetch", "-q", "origin")

        wt = collection / ".worktrees" / "repo" / "run1" / "lane1"
        _git(primary, "worktree", "add", "-q", "-b", "feat", str(wt))
        _git(wt, "branch", "--set-upstream-to=origin/main")

        assert _inventory(collection).row_for(str(wt)).upstream is UpstreamState.BEHIND

        _commit(wt, "c.txt", "concurrent work")
        assert _inventory(collection).row_for(str(wt)).upstream is UpstreamState.DIVERGED


# --------------------------------------------------------------------------- #
# 5. multi-repository inventory + read-only guarantee
# --------------------------------------------------------------------------- #
def test_inventory_covers_multiple_repositories():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        collection = tmp / "collection"
        for name in ("alpha", "beta"):
            primary = collection / name
            _init_repo(primary)
            _git(
                primary,
                "worktree",
                "add",
                "-q",
                "-b",
                "feat",
                str(collection / ".worktrees" / name / "run1" / "lane1"),
            )

        report = _inventory(collection)
        assert {row.repo for row in report.rows} == {"alpha", "beta"}
        assert len(report.rows) == 2


def test_inventory_creates_no_directory_and_mutates_no_state():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        collection = tmp / "collection"
        primary = collection / "repo"
        head = _init_repo(primary)
        wt = collection / ".worktrees" / "repo" / "run1" / "lane1"
        _git(primary, "worktree", "add", "-q", "-b", "feat", str(wt))

        before_tree = _snapshot(tmp)
        before_worktrees = _git(primary, "worktree", "list", "--porcelain").stdout
        before_status = _git(wt, "status", "--porcelain").stdout

        _inventory(
            collection,
            manifests={str(wt): _manifest(head, head)},
            processes={str(wt): [os.getpid()]},
        )

        assert _snapshot(tmp) == before_tree
        assert _git(primary, "worktree", "list", "--porcelain").stdout == before_worktrees
        assert _git(wt, "status", "--porcelain").stdout == before_status
