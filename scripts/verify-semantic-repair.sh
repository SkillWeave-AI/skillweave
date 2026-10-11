#!/usr/bin/env bash
# SW-159-REPAIR-001 S3 verification harness.
#
# Runs, under `set -eo pipefail`, the two required grounded auto-repairs
# (nonexistent modifies path, wrong-language source path) plus the four holds
# (repeated fingerprint, target drift, exhausted attempt/budget, authority
# expansion). Every case is asserted; a single failure aborts the run.
#
# Evidence is persisted next to this script's output directory.
set -eo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
export PYTHONPATH="$REPO_ROOT/src"

EVIDENCE_DIR="${EVIDENCE_DIR:-$REPO_ROOT/tests_output/semantic-repair}"
mkdir -p "$EVIDENCE_DIR"
LOG="$EVIDENCE_DIR/s3-verify.log"
: > "$LOG"

log() { echo "$@" | tee -a "$LOG"; }

log "== SW-159-REPAIR-001 S3 verification =="
log "repo=$REPO_ROOT"
log "head=$(git rev-parse HEAD)"
log "branch=$(git branch --show-current)"

# The harness itself is the reproduction: exit non-zero on any assertion.
python3 - "$EVIDENCE_DIR" <<'PY' 2>&1 | tee -a "$LOG"
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.getcwd(), "src"))

from skillweave.runtime.journal import EventJournal
from skillweave.runtime.preflight import (
    FailureClass, PreflightResult, Retryability, digest_target,
)
from skillweave.runtime.semantic_repair import (
    BoundedRepairer, HoldReason, detect_surface_failure,
    repair_plan_from_grounding,
)

evidence_dir = Path(sys.argv[1])
results = {}


def record(name, payload):
    results[name] = payload
    print(f"  [{name}] {json.dumps(payload, sort_keys=True)}")


def yes(role, capability, failure):
    return True


def no(role, capability, failure):
    return False


def make_repo():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "src").mkdir()
    (tmp / "src" / "real_module.py").write_text("x = 1\n", encoding="utf-8")
    (tmp / "src" / "notes.md").write_text("# notes\n", encoding="utf-8")
    return tmp


repo = make_repo()

# ---- required auto-repair 1: nonexistent modifies path ---------------------
plan1 = {"id": "T-1", "acceptanceCriteria": ["intent"], "lane": {"modifies": ["src/missing.py"], "tests": ["tests/t.py"]}}
t1 = digest_target(plan1)
f1 = detect_surface_failure(plan1, repo, target_digest=t1)
assert f1.failure_class == FailureClass.NONEXISTENT_MODIFIES_PATH.value, f1
o1 = repair_plan_from_grounding(plan1, repo, role="ops", current_target_digest=t1, authorize=yes,
                                redispatch=lambda p: "dispatch:T-1:1")
assert o1.repaired, o1
assert o1.repaired_plan["lane"]["modifies"] == ["src/real_module.py"]
assert o1.repaired_plan["acceptanceCriteria"] == ["intent"]
assert o1.repaired_plan["lane"]["tests"] == ["tests/t.py"]
record("repair_nonexistent_modifies_path", {
    "failure_class": f1.failure_class,
    "implicated_fields": f1.implicated_fields,
    "before": plan1["lane"]["modifies"],
    "after": o1.repaired_plan["lane"]["modifies"],
    "intent_preserved": True,
    "dispatch_identity": o1.dispatch_identity,
})

# ---- required auto-repair 2: wrong-language source path --------------------
plan2 = {"id": "T-2", "acceptanceCriteria": ["c"], "language": "Python", "lane": {"modifies": ["src/notes.md"]}}
t2 = digest_target(plan2)
f2 = detect_surface_failure(plan2, repo, target_digest=t2)
assert f2.failure_class == FailureClass.WRONG_LANGUAGE_SOURCE_PATH.value, f2
o2 = repair_plan_from_grounding(plan2, repo, role="ops", current_target_digest=t2, authorize=yes,
                                redispatch=lambda p: "dispatch:T-2:1")
assert o2.repaired, o2
assert o2.repaired_plan["lane"]["modifies"] == ["src/real_module.py"]
record("repair_wrong_language_source_path", {
    "failure_class": f2.failure_class,
    "expected_language": "Python",
    "before": plan2["lane"]["modifies"],
    "after": o2.repaired_plan["lane"]["modifies"],
    "dispatch_identity": o2.dispatch_identity,
})

# ---- hold 1: repeated fingerprint -----------------------------------------
plan3 = {"id": "T-3", "lane": {"modifies": ["src/missing.py"]}}
t3 = digest_target(plan3)
f3 = detect_surface_failure(plan3, repo, target_digest=t3)
rep = BoundedRepairer(max_attempts=5, authorize=yes)
first = rep.repair(plan3, f3, role="ops", current_target_digest=t3, evidence=f3.evidence,
                   redispatch=lambda p: "dispatch:T-3:1")
assert first.repaired, first
second = rep.repair(plan3, f3, role="ops", current_target_digest=t3, evidence=f3.evidence)
assert second.hold_reason == HoldReason.REPEATED_FINGERPRINT.value, second
record("hold_repeated_fingerprint", {
    "fingerprint": f3.fingerprint, "hold_reason": second.hold_reason,
})

# ---- hold 2: target drift --------------------------------------------------
rep2 = BoundedRepairer(max_attempts=5, authorize=yes)
drift = rep2.repair(plan3, f3, role="ops",
                    current_target_digest=digest_target({"moved": True}),
                    evidence=f3.evidence)
assert drift.hold_reason == HoldReason.TARGET_DRIFT.value, drift
record("hold_target_drift", {
    "declared_digest": f3.target_digest, "observed_digest": digest_target({"moved": True}),
    "hold_reason": drift.hold_reason,
})

# ---- hold 3: exhausted attempt/budget -------------------------------------
from skillweave.runtime.semantic_repair import RepairAttempt
placeholder = RepairAttempt(attempt=1, failure_fingerprint="prior", failure_class="prior",
                            implicated_fields=[], before_digest="x", after_digest="x",
                            command="promptchain-validate", exit_code=1, outcome="held")
rep3 = BoundedRepairer(max_attempts=1, authorize=yes)
rep3.attempts.append(placeholder)
att = rep3.repair(plan3, f3, role="ops", current_target_digest=t3, evidence=f3.evidence)
assert att.hold_reason == HoldReason.ATTEMPTS_EXHAUSTED.value, att

rep4 = BoundedRepairer(max_attempts=10, max_correction_rounds=1, authorize=yes)
rep4.attempts.append(placeholder)
bud = rep4.repair(plan3, f3, role="ops", current_target_digest=t3, evidence=f3.evidence)
assert bud.hold_reason == HoldReason.BUDGET_EXHAUSTED.value, bud
record("hold_attempt_budget_exhausted", {
    "attempts_hold": att.hold_reason, "budget_hold": bud.hold_reason,
})

# ---- hold 4: authority expansion ------------------------------------------
rep5 = BoundedRepairer(max_attempts=5, authorize=no)
auth = rep5.repair(plan3, f3, role="reviewer", current_target_digest=t3, evidence=f3.evidence)
assert auth.hold_reason == HoldReason.AUTHORITY_EXPANSION.value, auth
record("hold_authority_expansion", {
    "role": "reviewer", "required_capability": f3.authority_requirement,
    "hold_reason": auth.hold_reason,
})

# ---- append-only ledger: before/after digests, command/exit, dispatch ------
journal = EventJournal(":memory:")
plan6 = {"id": "T-6", "lane": {"modifies": ["src/missing.py"]}}
t6 = digest_target(plan6)
o6 = repair_plan_from_grounding(plan6, repo, role="ops", current_target_digest=t6, authorize=yes,
                                redispatch=lambda p: "dispatch:T-6:1",
                                journal=journal, journal_run_id="s3-run")
assert o6.repaired, o6
rec = o6.attempts[0]
assert rec.before_digest != rec.after_digest and rec.exit_code == 0
events = journal.get_events("s3-run")
assert len(events) == 1
record("append_only_ledger", {
    "before_digest": rec.before_digest, "after_digest": rec.after_digest,
    "command": rec.command, "exit_code": rec.exit_code,
    "dispatch_identity": rec.dispatch_identity, "journal_events": len(events),
})

# ---- revalidate against unchanged target digest ---------------------------
seen = {}
o7 = repair_plan_from_grounding(plan6, make_repo(), role="ops", current_target_digest=t6,
                                authorize=yes,
                                revalidate=lambda p: seen.setdefault("revalidated", p["lane"]["modifies"]) and PreflightResult(passed=True),
                                redispatch=lambda p: "dispatch:T-6:revalidated")
assert o7.repaired, o7
assert seen["revalidated"] == ["src/real_module.py"], seen
record("revalidate_unchanged_target_digest", {
    "revalidated": seen["revalidated"], "target_digest": o7.target_digest,
})

(evidence_dir / "s3-results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
print("ALL S3 CASES PASSED")
PY

rc=$?
log "== S3 harness exit: $rc =="
exit $rc
