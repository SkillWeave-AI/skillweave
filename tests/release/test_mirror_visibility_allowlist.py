"""SW-CORE-ZR-020 — the narrow mirror allow-list in ``.forgejo/workflows/mirror.yml``.

The visibility gate of the Forgejo->GitHub mirror blocks planning/strategy
paths. Two public product assets of the shipped ``skillweave-blueprint`` skill
(``assets/prd.schema.json`` and ``references/prd-template.md``) shipped since
v1.5.9 and were false-flagged by the gate on real run #498. Mirror policy
part 2 resolves that with a **narrow allow-list**: ``ALLOWED_PATHS`` holds
exactly those two paths (no wildcard), and the gate loop skips a touched path
that equals an allowed path *before* the ``BLOCKED_PATHS`` match.

This test does not reimplement the gate. It reads ``mirror.yml``, extracts the
gate's real shell loop verbatim from the workflow YAML and runs it with ``bash``
against fixture path lists. The positive case, every negative control and a
widened-allow-list mutation are all executed through that extracted loop, so
the test would fail if the loop's allow logic were removed or loosened.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

# The two public product assets of the shipped skill that the allow-list names.
ALLOWED_ASSETS = (
    "skills/skillweave-blueprint/assets/prd.schema.json",
    "skills/skillweave-blueprint/references/prd-template.md",
)

# Every one of these must stay blocked; only the exact allowed paths above are
# skipped. Includes the ``.bak`` derived sibling of an allowed asset, which the
# end-anchored BLOCKED_PATHS globs (prd*.json) do not catch on their own.
NEGATIVE_CONTROLS = (
    ".skillweave/x",
    "docs/prd-foo.md",
    "skills/x/prd.md",
    "skills/skillweave-blueprint/assets/strategy.md",
    "skills/other/assets/prd.schema.json",
    "skills/skillweave-blueprint/assets/prd.schema.json.bak",
)

_LOOP_START = "IFS='|' read -ra GLOBS"
_LOOP_END = 'done < "${touched}"'


def _core_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _mirror_yaml() -> dict:
    text = (_core_root() / ".forgejo" / "workflows" / "mirror.yml").read_text(
        encoding="utf-8"
    )
    return yaml.safe_load(text)


def _gate_run_script(workflow: dict) -> str:
    """Return the shell body of the visibility-gate 'gate' step."""
    for step in workflow["jobs"]["visibility-gate"]["steps"]:
        if step.get("id") == "gate":
            return step["run"]
    raise AssertionError("visibility-gate has no step with id 'gate'")


def _extract_loop(run_script: str) -> str:
    """Slice the gate's blocking loop out of the real step script.

    Anchored on the loop's own first and last lines so the extracted text is
    the workflow's code, never a rewrite.
    """
    start = run_script.index(_LOOP_START)
    end = run_script.index(_LOOP_END, start) + len(_LOOP_END)
    loop = run_script[start:end]
    assert "BLOCKED_PATHS" in loop, "extracted loop is not the gate loop"
    assert "ALLOWED_PATHS" in loop, "allow-list is not consulted in the gate loop"
    return loop


def _run_gate(paths, *, allowed_override=None, tmpdir: Path):
    """Run the workflow's real gate loop over ``paths`` with bash.

    Returns ``(blocked_list, verdict)`` where verdict is ``"PASS"`` when the
    gate would publish (no blocked path) and ``"FAIL"`` otherwise.
    """
    workflow = _mirror_yaml()
    loop = _extract_loop(_gate_run_script(workflow))

    allowed = (
        allowed_override
        if allowed_override is not None
        else workflow["env"]["ALLOWED_PATHS"]
    )
    blocked_env = workflow["env"]["BLOCKED_PATHS"]

    touched = tmpdir / "touched.txt"
    touched.write_text("".join(f"{p}\n" for p in paths), encoding="utf-8")

    # Harness instrumentation only: the gate logic above is untouched; we print
    # the loop's own ``blocked`` accumulator.
    harness = loop + '\nprintf \'BLOCKED_OUT:%s\\n\' "${blocked}"\n'
    harness_file = tmpdir / "gate_loop.sh"
    harness_file.write_text(harness, encoding="utf-8")

    env = dict(os.environ)
    env["BLOCKED_PATHS"] = blocked_env
    env["ALLOWED_PATHS"] = allowed
    env["touched"] = str(touched)

    proc = subprocess.run(
        ["bash", "-eo", "pipefail", str(harness_file)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, (
        f"gate loop failed to run (rc={proc.returncode}):\n{proc.stderr}"
    )
    line = next(
        (ln for ln in proc.stdout.splitlines() if ln.startswith("BLOCKED_OUT:")),
        None,
    )
    assert line is not None, f"no BLOCKED_OUT in gate output:\n{proc.stdout}"
    blocked = line[len("BLOCKED_OUT:") :].split()
    return blocked, ("PASS" if not blocked else "FAIL")


def test_allowlist_positive_case_allows_the_two_product_assets(tmp_path):
    """The two exact public product assets pass the gate; nothing else does."""
    blocked, verdict = _run_gate(ALLOWED_ASSETS, tmpdir=tmp_path)
    assert blocked == [], f"allowed assets must not be blocked; got {blocked}"
    assert verdict == "PASS"
    # Each asset passes on its own, too (exact equality, not a group effect).
    for asset in ALLOWED_ASSETS:
        solo, solo_verdict = _run_gate([asset], tmpdir=tmp_path)
        assert solo == [], f"{asset} must not be blocked on its own; got {solo}"
        assert solo_verdict == "PASS"


def test_allowlist_negative_controls_stay_blocked(tmp_path):
    """Every non-allow-listed path is still reported blocked by the real loop."""
    for control in NEGATIVE_CONTROLS:
        blocked, verdict = _run_gate([control], tmpdir=tmp_path)
        assert control in blocked, (
            f"negative control {control!r} must be blocked; got {blocked}"
        )
        assert verdict == "FAIL", f"{control!r} must fail the gate"


def test_allowlist_negative_control_bak_sibling_is_blocked(tmp_path):
    """A derived sibling (``<allowed>.bak``) is not the allowed asset.

    The base BLOCKED_PATHS globs are end-anchored, so ``.bak`` is invisible to
    them; the allow-skip must not swallow it either. The gate must fail closed.
    """
    bak = "skills/skillweave-blueprint/assets/prd.schema.json.bak"
    blocked, verdict = _run_gate([bak, ALLOWED_ASSETS[0]], tmpdir=tmp_path)
    assert bak in blocked, f"derived sibling must be blocked; got {blocked}"
    assert ALLOWED_ASSETS[0] not in blocked, "the exact asset itself is allowed"
    assert verdict == "FAIL"


def test_widened_allowlist_mutation_lets_a_negative_control_through(tmp_path):
    """Teeth: widening the allow-list to ``skills/*/assets/*`` breaks a control.

    With the shipped narrow allow-list the foreign-skill asset is blocked. Under
    the mutation it is skipped *before* BLOCKED_PATHS, so the control's expected
    verdict (FAIL) no longer holds — the mutation is detected.
    """
    control = "skills/other/assets/prd.schema.json"

    blocked, verdict = _run_gate([control], tmpdir=tmp_path)
    assert control in blocked and verdict == "FAIL", (
        "baseline: the foreign-skill asset must be blocked"
    )

    widened, widened_verdict = _run_gate(
        [control], allowed_override="skills/*/assets/*", tmpdir=tmp_path
    )
    assert control not in widened, (
        "mutation must bypass the control (that is the defect the test catches)"
    )
    assert widened_verdict == "PASS", (
        "mutated gate publishes the control; the control's invariant fails"
    )
