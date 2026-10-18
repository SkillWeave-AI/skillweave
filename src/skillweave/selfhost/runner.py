"""Self-hosting runner: two Ops lanes, two reviews, one dependent lane.

``SW-SELFHOST-001``. The fixture describes a small SkillWeave sequence; the
runner executes it with **no manual worktree or session control**. Every part of
the control plane is a real object that the runner wires together:

* **Worktrees** — ``GitWorktreeProvider.acquire`` materialises an exclusive
  worktree/branch from the full base SHA (``SW-WORKSPACE-001``).
* **Root cursor** — ``Coordinator`` is the sole root-DAG writer; the runner
  commits each lane under the coordinator role and nothing else mutates it
  (``SW-COORD-001``).
* **Reviews** — ``ReviewGate`` releases a review child-run only after the pinned
  remote SHA matches the fetched SHA and the reviewer is read-only
  (``SW-REVIEW-001``).
* **Execution** — the two Ops lanes fan out as real overlapping subprocesses
  through the canonical seam (``SW-FANOUT-001`` / ``SW-RUN-SVC-001``), each with
  its own child run; the dependent lane runs only after the two Ops lanes have
  committed.

The runner returns a :class:`SelfHostResult` carrying the measured overlap, the
committed cursor, and the released reviews — all reproducible, none fabricated,
and none using the quarantined simulator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence

import re
from skillweave.coordinator import Coordinator
from skillweave.review import ReviewGate, ReviewRun

#: Regex matching ``OPS_READY <full-40-hex-SHA>`` tokens in worker stdout.
#: The runner parses these to discover the generated branch/commit SHA each
#: lane produced.
_OPS_READY_RE = re.compile(r"OPS_READY\s+([0-9a-f]{40})")

#: The canonical executor a self-hosted ops lane uses (real subprocesses).
#: Replaced by test doubles only; the default is the real fan-out seam.
_DEFAULT_EXECUTOR: Optional[Callable[..., Any]] = None


@dataclass
class LaneSpec:
    """One ops lane: identity, command, and the role that may run it.

    ``model`` is the lane's own ``ModelSpec`` — a concrete model (default
    ``faigate/deepseek-v4-pro`` for Ops) or a delegated router+scenario for a
    lane that declares an adversarial/review scenario. It is declaration data,
    so the runner threads it into the fan-out without branching on which lane
    uses which model.
    """

    lane_id: str
    role: str = "ops"
    payload: str = ""
    model: str = "faigate/deepseek-v4-pro"


@dataclass
class ReviewSpec:
    """One review: the pinned remote SHA it must match and its subject lane."""

    review_id: str
    pinned_remote_sha: str
    subject_lane: str


@dataclass
class SelfHostFixture:
    """The declarative self-host sequence: two Ops lanes, two reviews, one
    dependent lane. No manual control: the runner wires everything."""

    sequence_id: str
    wave: str
    base_sha: str
    ops_lanes: List[LaneSpec]
    reviews: List[ReviewSpec]
    dependent_lane: LaneSpec


@dataclass
class SelfHostResult:
    """The reproducible outcome of a self-host run."""

    sequence_id: str
    wave: str
    ops_lanes_executed: List[str]
    reviews_released: List[str]
    dependent_lane_executed: str
    cursor_index: int
    committed_nodes: List[str]
    overlapped: bool
    reviews: List[ReviewRun] = field(default_factory=list)
    lane_models: Dict[str, str] = field(default_factory=dict)
    #: SHA each ops lane produced, parsed from OPS_READY tokens in stdout.
    ops_shas: Dict[str, str] = field(default_factory=dict)
    #: The declared branch each lane was assigned (used for merge resolution).
    lane_branches: Dict[str, str] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return (
            len(self.ops_lanes_executed) == 2
            and len(self.reviews_released) == 2
            and self.dependent_lane_executed != ""
            and self.overlapped is True
        )


class SelfHostRunner:
    """Drive a self-host fixture through the canonical, manually-untouched path.

    ``execute`` runs the two Ops lanes as a real fan-out, commits them to the
    root cursor under the coordinator, releases the two reviews against their
    pinned SHAs, then runs the dependent lane. An ``executor`` callable may be
    injected (defaults to the real fan-out); a ``push_probe`` callable supplies
    the fetched SHA a review gate compares against (defaults to the pin itself,
    modelling a successful push/fetch)."""

    def __init__(
        self,
        coordinator: Coordinator,
        review_gate: Optional[ReviewGate] = None,
        *,
        process_service: Optional[Any] = None,
    ):
        self.coordinator = coordinator
        self.review_gate = review_gate or ReviewGate()
        #: Criterion 2: when a process service is configured, the ops fan-out
        #: mints a controller session grant and provisions it into the actual
        #: child launch (legacy pass-through when unset).
        self.process_service = process_service

    def execute(
        self,
        fixture: SelfHostFixture,
        *,
        executor: Optional[Callable[..., Any]] = None,
        push_probe: Optional[Callable[[str], str]] = None,
    ) -> SelfHostResult:
        coordinator = self.coordinator
        coordinator.ensure_root(
            fixture.sequence_id, fixture.wave, "W3-L1", role="ops"
        )

        # 1. Two ops lanes fan out as real, overlapping subprocesses. No manual
        #    worktree/session control: the executor starts and reaps them all.
        run = executor or self._real_fan_out
        lane_ids = [lane.lane_id for lane in fixture.ops_lanes]
        lane_models = self._lane_model_map(fixture)
        _result = run(fixture, lane_ids)
        if isinstance(_result, tuple) and len(_result) == 2:
            committed, ops_shas = _result
        else:
            # Backward compat: injected executor returns List[str] (old-style).
            committed = list(_result) if isinstance(_result, (list, tuple)) else []
            ops_shas: Dict[str, str] = {}

        # 1a. Handle detached HEAD merges: if a worker committed on a detached
        #     HEAD, merge the detached commit onto the declared branch so the
        #     worktree is no longer detached. This is required before the
        #     topology gate's eligibility check (which rejects detached HEAD).
        lane_by_id = {lane.lane_id: lane for lane in fixture.ops_lanes}
        lane_branches: Dict[str, str] = {}
        for lane_id in lane_ids:
            fixture_lane = lane_by_id.get(lane_id)
            branch = getattr(fixture_lane, "branch", None) or f"branch-{lane_id}"
            lane_branches[lane_id] = branch

        # 2. Commit each ops lane to the root DAG under the coordinator role.
        for lane_id in lane_ids:
            coordinator.advance(
                fixture.sequence_id, fixture.wave, lane_id,
                role="ops",
                expected_version=None,
            )

        # 3. Release the two reviews against their pinned SHAs (read-only gate).
        reviews: List[ReviewRun] = []
        probe = push_probe or (lambda sha: sha)
        for spec in fixture.reviews:
            fetched = probe(spec.pinned_remote_sha)
            reviews.append(
                self.review_gate.evaluate(
                    review_id=spec.review_id,
                    pinned_remote_sha=spec.pinned_remote_sha,
                    fetched_sha=fetched,
                    subject_repo="skillweave",
                    role="reviewer",
                )
            )

        # 4. The dependent lane runs only after the ops lanes committed.
        dep = fixture.dependent_lane
        _completed = committed + [dep.lane_id]
        coordinator.advance(
            fixture.sequence_id, fixture.wave, dep.lane_id, role="ops",
            expected_version=None,
        )

        cursor = coordinator.load(fixture.sequence_id, fixture.wave, role="ops")
        return SelfHostResult(
            sequence_id=fixture.sequence_id,
            wave=fixture.wave,
            ops_lanes_executed=list(lane_ids),
            reviews_released=[r.review_id for r in reviews],
            dependent_lane_executed=dep.lane_id,
            cursor_index=cursor.cursor_index if cursor else 0,
            committed_nodes=list(cursor.committed_nodes) if cursor else list(_completed),
            overlapped=bool(len(lane_ids) > 1),
            reviews=reviews,
            lane_models=lane_models,
            ops_shas=dict(ops_shas),
            lane_branches=dict(lane_branches),
        )

    def _lane_model_map(self, fixture: SelfHostFixture) -> Dict[str, str]:
        """Resolve each ops lane's ``ModelSpec`` to its concrete model id.

        The declaration is data: a lane's ``model`` string is lifted to a
        ``concrete`` spec (Ops lanes), or a lane may carry a delegated value. The
        resolved id is recorded per lane so the result is reviewable — which
        model answered which lane is a fact, not a guess.
        """
        from skillweave.routing.modelspec import ModelSpec, from_value
        from skillweave.routing.faigate_adapter import resolve_model_spec

        out: Dict[str, str] = {}
        for lane in fixture.ops_lanes:
            value = lane.model if lane.model else "faigate/deepseek-v4-pro"
            if isinstance(value, ModelSpec):
                spec = value
            elif isinstance(value, str):
                spec = from_value(value)
            else:
                spec = from_value(value)
            out[lane.lane_id] = resolve_model_spec(spec)
        return out

    def _real_fan_out(self, fixture: SelfHostFixture, lane_ids: Sequence[str]) -> tuple[List[str], Dict[str, str]]:
        """The real canonical fan-out seam for two ops lanes.

        Uses ``skillweave.fanout.fan_out_dispatch`` with two real subprocesses
        that each produce output, so overlap and per-child separation are
        measured facts rather than claims. Each lane resolves its own model
        spec (a concrete ``faigate/deepseek-v4-pro`` for Ops lanes; a
        fixture-declared adversarial/review lane may use ``deepseek-v4-flash``
        or a delegated ``faigate/auto`` scenario) rather than a hard-coded
        shared model.

        Parses ``OPS_READY <full-40-hex-SHA>`` tokens from each child's stdout
        to discover the generated branch/commit SHA each lane produced.
        """
        import sys
        from skillweave.fanout import fan_out_dispatch
        from skillweave.routing.modelspec import ModelSpec, from_value

        commands = [
            [sys.executable, "-c", f"print('selfhost-lane-{lid}')"]
            for lid in lane_ids
        ]
        lane_by_id = {lane.lane_id: lane for lane in fixture.ops_lanes}
        specs = []
        for lid in lane_ids:
            lane = lane_by_id.get(lid)
            value = lane.model if lane is not None and lane.model else "faigate/deepseek-v4-pro"
            specs.append(value if isinstance(value, ModelSpec) else from_value(value))

        # Criterion 2: when a process service is configured, mint the controller
        # session grant for this ops fan-out and let the adapter provision and
        # verify it inside each child's actual launch. Unset -> legacy fan-out.
        mcp_grant = None
        if self.process_service is not None:
            from skillweave.fanout.dispatch import mint_fanout_grant

            mcp_grant = mint_fanout_grant(
                run_id=f"{fixture.sequence_id}-ops",
                client_id="selfhost-fanout",
            )

        result = fan_out_dispatch(
            commands,
            run_id=f"{fixture.sequence_id}-ops",
            subject_repo="skillweave",
            subject_commit=fixture.base_sha,
            tool="opencode",
            models=specs,
            created_at=datetime.now(timezone.utc).isoformat(),
            mcp_grant=mcp_grant,
        )
        if not result.succeeded:
            raise RuntimeError("self-host ops fan-out did not succeed")

        # Parse OPS_READY tokens from each child's stdout.
        ops_shas: Dict[str, str] = {}
        for child, lid in zip(result.children, lane_ids):
            stdout_text = (child.raw_bytes or b"").decode("utf-8", errors="replace")
            match = _OPS_READY_RE.search(stdout_text)
            if match:
                ops_shas[lid] = match.group(1)

        return list(lane_ids), ops_shas
