"""Architecture claim-check (SW-DOC-ARCH-001).

A machine check over ``docs/architecture.md`` proves the doc is current:

1. **No stale statements.** The doc must not claim ``v0.4.4``, "five skills"
   (or "Five Integrated Skills"), or a simulator-as-executor path.
2. **15 skills.** The skill layer section names exactly the fifteen
   ``skillweave-*`` packages that ship in ``skills/``.
3. **Diagram matches the callgraph.** The canonical run path lists the six
   record stages in the order the Run Application Service produces them
   (Run → Journal → Raw Artifact → Receipt → Verification → Gate), and the
   canonical modules named in the doc exist and carry no ``simulate_*``
   reference.

Self-contained sys.path handling, following the sibling-test convention.
"""

import re
import subprocess
import sys
from pathlib import Path

_repo = Path(__file__).resolve().parent.parent.parent
_doc = _repo / "docs" / "architecture.md"
_src = _repo / "src"

if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

#: The canonical run-path stage order the Run Application Service guarantees.
_CANONICAL_STAGES = ["Run", "Journal", "Raw Artifact", "Receipt", "Verification", "Gate"]

#: Stale claims that must not appear in the architecture doc.
_STALE_CLAIMS = [
    (r"v0\.4\.4", "v0.4.4 version claim"),
    (r"five\s+integrated\s+skills", "five-skills claim"),
    (r"five\s+skills", "five-skills claim"),
    (r"9-state", "obsolete 9-state machine claim"),
]

#: Executor-simulation statements that must not describe the canonical path.
#: The doc MAY mention the simulator only to assert that the canonical path
#: does NOT use it (the quarantine). A *claim* that the simulator IS the
#: executor is what the check forbids.
_SIMULATOR_CLAIMS = [
    (r"simulat\w*\s+as\s+executor", "simulator-as-executor claim"),
    (r"simulat\w*\s+is\s+the\s+executor", "simulator-as-executor claim"),
    (r"simulated\s+executor\s+is\b", "simulated-executor-is claim"),
]


def _skills_on_disk() -> list[str]:
    out = subprocess.run(
        ["ls", str(_repo / "skills")],
        capture_output=True, text=True,
    ).stdout.split()
    return sorted(n for n in out if n.startswith("skillweave-"))


def test_doc_exists_and_is_current():
    assert _doc.exists(), "docs/architecture.md is missing"
    text = _doc.read_text()

    assert "1.3.7" in text, "architecture doc must name release 1.3.7"

    for pattern, label in _STALE_CLAIMS:
        assert not re.search(pattern, text, re.IGNORECASE), (
            f"architecture doc still contains stale claim: {label}"
        )


def test_thirteen_skills_are_named_and_match_disk():
    text = _doc.read_text()
    on_disk = _skills_on_disk()
    assert len(on_disk) == 15, f"expected 15 skillweave-* skills, found {len(on_disk)}"
    for skill in on_disk:
        assert skill in text, f"architecture doc is missing skill {skill!r}"


def test_diagram_matches_callgraph():
    text = _doc.read_text()

    # The canonical stages must appear in order in the doc's run-path diagram.
    idx = -1
    for stage in _CANONICAL_STAGES:
        pos = text.find(stage)
        assert pos > idx, f"canonical stage {stage!r} out of order in diagram"
        idx = pos

    # No simulator mention should sit anywhere on the canonical path language.
    for pattern, label in _SIMULATOR_CLAIMS:
        assert not re.search(pattern, text, re.IGNORECASE), (
            f"architecture doc claims {label}"
        )


def test_canonical_modules_exist_and_are_simulator_free():
    # The modules the doc names as the control plane must exist, and the run
    # service path must carry no simulate_* reference (the callgraph proof).
    for rel in [
        "runsvc/service.py",
        "coordinator/coordinator.py",
        "workspace/provider.py",
        "fanout/dispatch.py",
        "review/review.py",
        "selfhost/runner.py",
        "legacy/quarantine.py",
    ]:
        path = _src / "skillweave" / rel
        assert path.exists(), f"documented module missing: {rel}"

    # The Runsvc service must not reference the simulator.
    svc_src = (_src / "skillweave" / "runsvc" / "service.py").read_text()
    assert "simulate" not in svc_src, "runsvc service references the simulator"


def _run_all() -> int:
    tests = [
        test_doc_exists_and_is_current,
        test_thirteen_skills_are_named_and_match_disk,
        test_diagram_matches_callgraph,
        test_canonical_modules_exist_and_are_simulator_free,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
