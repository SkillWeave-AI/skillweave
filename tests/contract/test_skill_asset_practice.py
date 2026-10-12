"""Contract tests tying SkillWeave skill *assets* to the format the ecosystem
actually produces.

This file exists because skill assets describe structures and, left
unchecked, silently describe structures nobody produces any more. Two assets
were measured stale (SW-SKILL-001):

- ``skills/skillweave-blueprint/assets/prd.schema.json`` claimed tasks carry
  ``acceptance_criteria`` plus an ``estimated_minutes`` time field, while the
  ecosystem produces ``acceptanceCriteria`` and Fibonacci ``points`` and has
  no time field.
- ``skills/skillweave-promptchain-validate`` required a twelve-section
  topic contract for every sequence, while the build format the ecosystem
  ships is ``phases``/``parallel_lanes``/``mutual_exclusion``/
  ``gate_pass_requires``/``session_boundary``.

The two named production PRDs (ops-002 mirror-rollout, Forgejo-first) are
treated as fixtures under ``tests/fixtures/`` as flat
``blueprint-schema-*.json`` files. They are real
production artifacts with their structure preserved intact and their prose
redacted; do not simplify them to make tests pass — flattening that structure
destroys the only check that catches a stale schema.
"""

from pathlib import Path

import jsonschema

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCHEMA = _REPO_ROOT / "skills" / "skillweave-blueprint" / "assets" / "prd.schema.json"
_FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"

# regen-sequence.py reads at most this many acceptance criteria per dispatch.
# See planning/scripts/regen-sequence.py (MAX_CRITERIA_PER_DISPATCH).
_MAX_CRITERIA_PER_DISPATCH = 3

FIBONACCI = (1, 2, 3, 5, 8, 13, 21)


def _load_schema():
    import json

    return json.loads(_SCHEMA.read_text())


def _validator():
    return jsonschema.Draft202012Validator(_load_schema())


def _errors(doc):
    return list(_validator().iter_errors(doc))


def _fixture(name):
    import json

    return json.loads((_FIXTURE_DIR / f"blueprint-schema-{name}").read_text())


# Keys regen-sequence.py's ``_sequence`` and ``_briefs`` read unconditionally
# from the ``sequence`` block (``cfg[key]``). Their absence makes the real
# generator raise ``KeyError`` before a single brief is written, so a PRD that
# omits any of them fails closed with zero dispatches regardless of task shape.
# See planning/scripts/regen-sequence.py (_sequence/_briefs).
_SEQUENCE_REQUIRED_KEYS = (
    "sequence_id",
    "sequence_type",
    "execution_mode",
    "worktree",
    "branch",
    "base",
    "state_file",
    "runner_shell",
)


def _sequence_fail_closed(prd):
    """True if the real generator fails before writing any dispatch brief.

    Mirrors regen-sequence.py: ``load`` aborts without a ``sequence`` block,
    and ``_sequence``/``_briefs`` read these keys directly, so any missing key
    raises ``KeyError`` before a brief exists.
    """
    cfg = prd.get("sequence")
    if not isinstance(cfg, dict):
        return True
    return any(key not in cfg for key in _SEQUENCE_REQUIRED_KEYS)


def _tasks_with_dispatch_order(doc):
    return [t for t in doc["tasks"] if (t.get("lane") or {}).get("dispatch_order")]


def _dispatch_order_errors(prd):
    """Exact-once errors in every task's ``dispatch_order``.

    This is a *structural* check, not a generation proof: for each task that
    declares ``dispatch_order``, the criteria it covers must be exactly
    ``1..len(acceptanceCriteria)`` with no missing, duplicate or out-of-range
    entry. It shadows regen-sequence.py ``validate_dispatch_orders`` so the
    exact-once contract is pinned in the product tests independently of a
    live generator run.
    """
    errors = []
    for task in prd["tasks"]:
        lane = task.get("lane")
        if not (lane and lane.get("dispatch_order")):
            continue
        seen = []
        for dispatch in lane["dispatch_order"]:
            if not dispatch.get("criteria"):
                errors.append(
                    f"{task['id']}: dispatch_order group is empty; every dispatch"
                    f" must name at least one criterion")
            else:
                seen.extend(dispatch["criteria"])
        n = len(task["acceptanceCriteria"])
        if len(seen) != n or sorted(seen) != list(range(1, n + 1)):
            missing = [c for c in range(1, n + 1) if c not in seen]
            dupes = sorted({c for c in seen if seen.count(c) > 1})
            out = sorted({c for c in seen if c < 1 or c > n})
            errors.append(
                f"{task['id']}: dispatch_order must cover criteria 1..{n} exactly once"
                f" (missing {missing}, duplicates {dupes}, out-of-range {out})")
    return errors


def _structural_dispatch_estimate(prd):
    """Structural dispatch estimate — a local helper, never a generation proof.

    Replica/chunk arithmetic is used ONLY to bound the synthetic red/corrected
    regression pair below. It is not presented as evidence that a production
    PRD generates briefs; realise the real generator or run it instead. For a
    lane with explicit ``dispatch_order`` one brief is emitted per entry; for a
    lane-less task criteria are chunked at ``MAX_CRITERIA_PER_DISPATCH``. A task
    without ``acceptanceCriteria`` (the red, snake_case shape) fails closed with
    zero, as does a ``sequence`` block missing any generator-required key.
    """
    if _sequence_fail_closed(prd):
        return 0
    total = 0
    for task in prd["tasks"]:
        lane = task.get("lane") or {}
        if lane.get("dispatch_order"):
            total += len(lane["dispatch_order"])
            continue
        try:
            n_acs = len(task["acceptanceCriteria"])
        except KeyError:
            return 0
        total += -(-n_acs // _MAX_CRITERIA_PER_DISPATCH)
    return total


class TestSchemaAcceptsProducedFormat:
    """Criterion 1: the shipped schema accepts the produced build format."""

    def test_no_time_estimate_field_is_required(self):
        doc = _fixture("corrected-build-format.json")
        assert _errors(doc) == []

    def test_produced_keys_are_allowed(self):
        schema = _load_schema()
        props = schema["properties"]
        assert "sequence" in props
        assert "totals" in props
        task = schema["properties"]["tasks"]["items"]
        for key in ("acceptanceCriteria", "points", "dependsOn", "lane"):
            assert key in task["properties"], key

    def test_blueprint_schema_has_no_time_field_at_all(self):
        schema = _load_schema()
        # No required key mentions time.
        required = [r for r in schema.get("required", []) if "time" in r]
        assert required == []
        # No task property is a time estimate.
        task_schema = schema["properties"]["tasks"]["items"]["properties"]
        assert "estimated_minutes" not in task_schema
        assert "estimated_tokens" not in task_schema

    def test_points_constrained_to_fibonacci(self):
        schema = _load_schema()
        points = schema["properties"]["tasks"]["items"]["properties"]["points"]
        assert sorted(points["enum"]) == sorted(FIBONACCI)


class TestSchemaValidatesProductionPRDs:
    """Criterion 2: the schema validates the two unchanged production PRDs
    and rejects non-Fibonacci points."""

    PROD_PRDS = ("ops-002-mirror-rollout.json", "forgejo-first.json")

    def test_ops_002_and_forgejo_first_pass_unchanged(self):
        for name in self.PROD_PRDS:
            assert _errors(_fixture(name)) == [], name

    def test_non_fibonacci_points_are_rejected(self):
        for name in self.PROD_PRDS:
            doc = _fixture(name)
            doc["tasks"][0]["points"] = 4
            errs = _errors(doc)
            assert len(errs) == 1, name
            assert list(errs[0].path) == ["tasks", 0, "points"], name


class TestValidateDetectsBuildSequence:
    """Criteria 3-4: promptchain-validate recognises the build format and
    validates its own keys instead of the twelve-section topic contract."""

    def test_skill_makes_build_keys_explicit(self):
        skill = (
            _REPO_ROOT / "skills" / "skillweave-promptchain-validate" / "SKILL.md"
        ).read_text()
        for key in (
            "phases",
            "parallel_lanes",
            "mutual_exclusion",
            "gate_pass_requires",
            "session_boundary",
        ):
            assert key in skill, key

    def test_skill_names_the_consuming_flow_for_each_format(self):
        skill = (
            _REPO_ROOT / "skills" / "skillweave-promptchain-validate" / "SKILL.md"
        ).read_text()
        # Both formats are supported, so each must name who consumes it.
        assert "regen-sequence" in skill or "regen-sequence.py" in skill
        assert "promptchain-execute" in skill


class TestExactOnceDispatchCoverage:
    """Criterion: every task with ``dispatch_order`` in both production PRDs
    covers its acceptance criteria exactly once, and missing / duplicate /
    out-of-range coverage is a focused, rejected mutation."""

    PROD_PRDS = ("ops-002-mirror-rollout.json", "forgejo-first.json")

    def test_prod_prds_have_exact_once_coverage(self):
        for name in self.PROD_PRDS:
            assert _dispatch_order_errors(_fixture(name)) == [], name

    def test_missing_criterion_is_rejected(self):
        for name in self.PROD_PRDS:
            doc = _fixture(name)
            task = _tasks_with_dispatch_order(doc)[0]
            disp = task["lane"]["dispatch_order"]
            # Drop one criterion from the final dispatch so coverage is short.
            crit = list(disp[-1]["criteria"])
            dropped = crit.pop()
            disp[-1] = {"criteria": crit, "focus": "removed one criterion"}
            errs = _dispatch_order_errors(doc)
            assert len(errs) == 1, name
            assert task["id"] in errs[0], name
            assert "missing" in errs[0] and str(dropped) in errs[0], name

    def test_duplicate_criterion_is_rejected(self):
        for name in self.PROD_PRDS:
            doc = _fixture(name)
            task = _tasks_with_dispatch_order(doc)[0]
            dup = task["lane"]["dispatch_order"][-1]["criteria"][0]
            task["lane"]["dispatch_order"].append(
                {"criteria": [dup], "focus": "duplicate on purpose"})
            errs = _dispatch_order_errors(doc)
            assert len(errs) == 1, name
            assert task["id"] in errs[0], name
            assert "duplicates" in errs[0], name

    def test_out_of_range_criterion_is_rejected(self):
        for name in self.PROD_PRDS:
            doc = _fixture(name)
            task = _tasks_with_dispatch_order(doc)[0]
            n = len(task["acceptanceCriteria"])
            task["lane"]["dispatch_order"].append(
                {"criteria": [n + 99], "focus": "out of range on purpose"})
            errs = _dispatch_order_errors(doc)
            assert len(errs) == 1, name
            assert task["id"] in errs[0], name
            assert "out-of-range" in errs[0], name

    def test_schema_rejects_empty_criteria_group(self):
        for name in self.PROD_PRDS:
            doc = _fixture(name)
            task = _tasks_with_dispatch_order(doc)[0]
            task["lane"]["dispatch_order"].append(
                {"criteria": [], "focus": "empty group on purpose"})
            errs = _errors(doc)
            assert len(errs) == 1, name
            assert list(errs[0].path) == [
                "tasks", doc["tasks"].index(task), "lane", "dispatch_order",
                len(task["lane"]["dispatch_order"]) - 1, "criteria"], name

    def test_helper_rejects_empty_group_despite_remaining_exact_once(self):
        for name in self.PROD_PRDS:
            doc = _fixture(name)
            task = _tasks_with_dispatch_order(doc)[0]
            # Append an empty group: the remaining nonempty groups still cover
            # every criterion exactly once, so the exact-once check alone would
            # pass. The empty group must still be rejected explicitly.
            task["lane"]["dispatch_order"].append(
                {"criteria": [], "focus": "empty group on purpose"})
            errs = _dispatch_order_errors(doc)
            assert len(errs) == 1, name
            assert task["id"] in errs[0], name
            assert "empty" in errs[0], name

    def test_all_nonempty_regrouping_stays_accepted(self):
        for name in self.PROD_PRDS:
            doc = _fixture(name)
            task = _tasks_with_dispatch_order(doc)[0]
            lane = task["lane"]
            merged = [c for d in lane["dispatch_order"] for c in d["criteria"]]
            n = len(task["acceptanceCriteria"])
            assert sorted(merged) == list(range(1, n + 1)), name
            # Redistribute the same exact-once coverage into fewer, all-nonempty
            # groups; original group boundaries are not immutable.
            midpoint = (len(merged) + 1) // 2
            lane["dispatch_order"] = [
                {"criteria": merged[:midpoint], "focus": "first regrouped group"},
                {"criteria": merged[midpoint:], "focus": "second regrouped group"},
            ]
            assert _errors(doc) == [], name
            assert _dispatch_order_errors(doc) == [], name


class TestRedAndCorrectedFixtures:
    """Criterion 5: a PRD valid under the previous schema has no structurally
    dispatchable lane; the corrected fixture does. Structural helper only —
    it does not claim a production PRD generates briefs."""

    def test_red_fixture_produces_zero_dispatches(self):
        doc = _fixture("red-old-format.json")
        assert _structural_dispatch_estimate(doc) == 0

    def test_corrected_fixture_produces_at_least_one_brief(self):
        doc = _fixture("corrected-build-format.json")
        assert _errors(doc) == []
        assert _structural_dispatch_estimate(doc) >= 1
        assert _dispatch_order_errors(doc) == []
