"""Repository-scoped lane contract (SW-159P-REPO-001).

Proves the acceptance surface against three real, local (network-free)
repositories that deliberately share a product-repository name and lane branch
names:

1. A versioned ``RepositoryTarget`` contract carries repository id, canonical
   root, base ref, resolved full base SHA, controller branch and a bounded
   worktree root; a mutating lane references exactly one target.
2. The dispatcher resolves base and produced-candidate SHAs *inside the
   declared repository* and refuses a SHA that only resolves elsewhere — before
   any worktree is created.
3. ``GitWorktreeProvider`` instances and worktree paths are derived from the
   lane's repository target, never from one global product repository.

Self-contained ``sys.path`` handling and hermetic temp repositories, following
the convention of ``test_workspace.py`` and ``test_integration_eligibility.py``.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_src = Path(__file__).resolve().parent.parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from skillweave.workspace import (  # noqa: E402
    GitWorktreeProvider,
    RepositoryTarget,
    RepositoryTargetError,
    REPOSITORY_TARGET_SCHEMA_ID,
    REPOSITORY_TARGET_SCHEMA_VERSION,
    acquire_lane,
    controller_branch_for,
    lane_branch,
    plan_lane,
    provider_for,
    resolve_base,
    resolve_candidate,
    resolve_ref_in_repository,
    worktree_path_for,
)

RUN = "sw159p"
# The same branch name is used in every repository on purpose: the collision is
# the point of criterion 6-adjacent scoping, and it must never conflate repos.
SHARED_LANE_BRANCH = "ops/SW-159P-multi-repo"


# ── Hermetic fixtures: three repositories ────────────────────────────────────


def _make_repo(root: Path, *, product: str, content: str) -> str:
    """Create a one-commit git repo; return its full HEAD SHA."""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=str(root), check=True
    )
    subprocess.run(["git", "config", "user.name", "test"], cwd=str(root), check=True)
    (root / "product.txt").write_text(content)
    subprocess.run(["git", "add", "product.txt"], cwd=str(root), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=str(root), check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


class _World:
    """Three repositories, each with its own target and a shared branch name."""

    def __init__(self, tmp: str):
        self.collection = Path(tmp)
        self.roots = {
            # NOTE: two repositories deliberately share the id "skillweave",
            # living in different collections. They are distinct repositories.
            "a": self.collection / "collection-a" / "skillweave",
            "b": self.collection / "collection-b" / "skillweave",
            "c": self.collection / "collection-c" / "service",
        }
        self.repos = {
            "a": self.collection / "collection-a",
            "b": self.collection / "collection-b",
            "c": self.collection / "collection-c",
        }
        self.shas = {
            key: _make_repo(root, product=key, content=f"repo-{key}")
            for key, root in self.roots.items()
        }
        self.targets = {
            key: RepositoryTarget(
                repository_id=root.name,
                canonical_root=str(root),
                base_ref="HEAD",
                base_sha=self.shas[key],
                controller_branch=controller_branch_for(RUN),
                worktree_root=str(
                    self.repos[key] / ".worktrees" / root.name / RUN / "multi-repo"
                ),
            )
            for key, root in self.roots.items()
        }


@pytest.fixture
def world():
    with tempfile.TemporaryDirectory(prefix="sw-159p-mr-") as tmp:
        yield _World(tmp)


# ── Criterion 1: versioned RepositoryTarget contract ─────────────────────────


def test_target_contract_carries_the_required_facts(world):
    target = world.targets["a"]
    data = target.to_dict()
    assert data["schema_version"] == REPOSITORY_TARGET_SCHEMA_VERSION
    assert data["repository_id"] == "skillweave"
    assert data["canonical_root"] == str(world.roots["a"])
    assert data["base_ref"] == "HEAD"
    assert data["base_sha"] == world.shas["a"]
    assert data["controller_branch"] == "ops/sw159p-controller"
    assert data["worktree_root"]
    # Round-trips through JSON without loss.
    assert RepositoryTarget.from_dict(data) == target
    assert REPOSITORY_TARGET_SCHEMA_ID.endswith("/repository-target/v1")


def test_each_lane_references_exactly_one_target(world):
    # A lane is planned against one target only; the dispatch names exactly that
    # repository and no other.
    dispatch = plan_lane(world.targets["b"], "multi-repo")
    assert dispatch.repository_id == world.targets["b"].repository_id
    assert dispatch.base_sha == world.shas["b"]
    assert dispatch.branch == SHARED_LANE_BRANCH

    # Three distinct repositories produce three distinct dispatches. Two share
    # the repository id "skillweave" on purpose, so identity is carried by the
    # canonical root and the resolved base SHA, not by the name alone.
    dispatches = [plan_lane(t, "multi-repo") for t in world.targets.values()]
    assert len({d.base_sha for d in dispatches}) == 3
    assert len({t.canonical_root for t in world.targets.values()}) == 3


def test_target_rejects_an_unversioned_or_extra_field(world):
    data = world.targets["a"].to_dict()
    data["extra"] = "nope"
    with pytest.raises(RepositoryTargetError) as exc:
        RepositoryTarget.from_dict(data)
    assert exc.value.field == "extra"

    data = world.targets["a"].to_dict()
    data["schema_version"] = 999
    with pytest.raises(RepositoryTargetError) as exc:
        RepositoryTarget.from_dict(data)
    assert exc.value.field == "schema_version"


def test_target_rejects_worktree_root_inside_the_checkout(world):
    with pytest.raises(RepositoryTargetError) as exc:
        RepositoryTarget(
            repository_id="skillweave",
            canonical_root=str(world.roots["a"]),
            base_ref="HEAD",
            base_sha=world.shas["a"],
            controller_branch=controller_branch_for(RUN),
            worktree_root=str(world.roots["a"] / ".sw-worktrees"),
        )
    assert exc.value.field == "worktree_root"


# ── Criterion 2: resolve inside the declared repository ─────────────────────


def test_resolve_base_and_candidate_inside_declared_repository(world):
    target = world.targets["a"]
    assert resolve_base(target) == world.shas["a"]
    assert resolve_candidate(target, world.shas["a"]) == world.shas["a"]
    # A SHA reachable by the other repositories' objects is refused here.
    with pytest.raises(RepositoryTargetError) as exc:
        resolve_ref_in_repository(target, world.shas["b"])
    assert exc.value.field == "ref"


def test_cross_repository_sha_is_refused_before_worktree_creation(world):
    # world.shas["b"] resolves in repository b, never in repository a. The
    # plan (the pre-worktree gate) must refuse it, and no worktree must exist.
    target = world.targets["a"]
    with pytest.raises(RepositoryTargetError):
        plan_lane(target, "leak", base_ref=world.shas["b"])

    provider = provider_for(target)
    leaked = worktree_path_for(target, "leak")
    assert not leaked.exists()
    # The target repository still has no worktrees registered under the lane.
    listed = subprocess.run(
        ["git", "worktree", "list"], cwd=str(world.roots["a"]),
        capture_output=True, text=True, check=True,
    ).stdout
    assert str(leaked) not in listed


def test_resolution_happens_inside_this_repositorys_object_store(world):
    # The same short ref name ("HEAD") resolves to a different full SHA in each
    # repository, proving resolution is repository-scoped.
    resolved = {k: resolve_base(t) for k, t in world.targets.items()}
    assert resolved["a"] == world.shas["a"]
    assert resolved["b"] == world.shas["b"]
    assert resolved["c"] == world.shas["c"]
    assert len(set(resolved.values())) == 3


# ── Criterion 3: providers and paths derived from the target ────────────────


def test_provider_is_derived_from_the_target_not_a_global_repository(world):
    providers = {k: provider_for(t) for k, t in world.targets.items()}
    roots = {k: p.repo_root for k, p in providers.items()}
    assert roots["a"] == world.roots["a"].resolve()
    assert roots["b"] == world.roots["b"].resolve()
    assert roots["c"] == world.roots["c"].resolve()
    assert len(set(roots.values())) == 3


def test_worktree_paths_are_bounded_and_never_inside_a_checkout(world):
    for key, target in world.targets.items():
        path = worktree_path_for(target, "multi-repo")
        assert path == Path(target.worktree_root) / "multi-repo"
        # Bounded under the target's worktree root.
        assert Path(target.worktree_root) in path.parents
        # Never inside the primary checkout.
        assert world.roots[key] not in path.parents
        assert path != world.roots[key]


def test_acquire_lane_materialises_only_inside_its_own_repository(world):
    acquired = []
    try:
        for key, target in world.targets.items():
            ws = acquire_lane(target, "multi-repo")
            acquired.append(ws)
            # The worktree is at the derived path and carries this repo's product.
            assert ws.path == worktree_path_for(target, "multi-repo")
            assert (ws.path / "product.txt").read_text() == f"repo-{key}"
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=str(ws.path),
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            assert head == world.shas[key]
    finally:
        for ws in acquired:
            ws.release()


def test_colliding_branch_names_across_repos_do_not_conflate_providers(world):
    # The three repositories share one lane branch name. Each provider creates
    # it in its own repository; each repository's worktree HEAD is that
    # repository's base — never another repository's.
    acquired = []
    try:
        for key, target in world.targets.items():
            ws = acquire_lane(target, "multi-repo", branch=SHARED_LANE_BRANCH)
            acquired.append((key, ws))

        branch_shas = {}
        for key, ws in acquired:
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=str(ws.path),
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            branch_shas[key] = head

        # Same branch name everywhere; three different facts.
        assert branch_shas["a"] == world.shas["a"]
        assert branch_shas["b"] == world.shas["b"]
        assert branch_shas["c"] == world.shas["c"]
        assert len(set(branch_shas.values())) == 3

        # Each repository's branch list contains the shared name exactly once,
        # and each worktree lives inside its own repository's derived root.
        for key, ws in acquired:
            listed = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                cwd=str(world.roots[key]),
                capture_output=True, text=True, check=True,
            ).stdout
            assert str(ws.path) in listed
            for other, other_ws in acquired:
                if other != key:
                    assert str(other_ws.path) not in listed
    finally:
        for _, ws in acquired:
            ws.release()


def test_release_is_repository_scoped_and_idempotent(world):
    ws = acquire_lane(world.targets["a"], "multi-repo")
    path = ws.attestation.path
    provider = GitWorktreeProvider(str(world.roots["a"]))

    assert provider.release(ws.attestation) is True
    assert not Path(path).exists()
    # Repeated release is a no-op, not an error.
    assert provider.release(ws.attestation) is True
    # The other repositories never saw this lane's branch.
    for key in ("b", "c"):
        branches = subprocess.run(
            ["git", "branch", "--list", lane_branch("multi-repo")],
            cwd=str(world.roots[key]),
            capture_output=True, text=True, check=True,
        ).stdout
        assert branches.strip() == ""
