"""REL-011: the top release notes address an external reader, not the tracker.

The release body of ``.forgejo/workflows/forgejo-release.yml`` is the CHANGELOG
entry for the tag being released, sliced out by the workflow's own extraction
step and then passed to the ops-engine ``release_notes_audience_gate.py`` with
``--ticket-prefixes`` built from the ``tracker_prefixes:`` line of ``.ops.yaml``.
A body that names an internal ticket reference addresses the author's tracker,
so the gate refuses it before any release object exists.

This test does NOT re-implement either block. It reads both inline Python blocks
out of ``.forgejo/workflows/forgejo-release.yml`` and executes them exactly as
the workflow does, so the test cannot drift from the workflow:

* ``<<'PY'`` extracts the notes for a tag from CHANGELOG.md.
* ``<<'PREFIXES_PY'`` extracts the organisation's tracker prefixes from
  ``.ops.yaml``.

It then applies the gate's own token shape ``[A-Z]{2,5}-[0-9]+`` restricted to
the declared prefixes and asserts the extracted top section names none of them.

A negative control injects a declared-prefix token and asserts it IS flagged,
and asserts a token whose prefix is NOT declared is NOT flagged — so an empty
or mis-parsed vocabulary cannot make the real assertion pass vacuously.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WORKFLOW_PATH = REPO_ROOT / ".forgejo" / "workflows" / "forgejo-release.yml"

# The shape shared by an internal ticket reference and an external identifier,
# copied verbatim from release_notes_audience_gate.py.
INTERNAL_TICKET_RE = re.compile(r"[A-Z]{2,5}-[0-9]+")


# ── Workflow block extraction (read from the workflow, never restated) ──────


def _workflow_text() -> str:
    assert WORKFLOW_PATH.exists(), f"missing workflow: {WORKFLOW_PATH}"
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _heredoc(workflow: str, marker: str) -> str:
    """Return the body of the ``<<'MARKER'`` heredoc, de-indented.

    The workflow writes its inline Python as a quoted heredoc on a run block
    that is itself indented; the interpreter receives the de-indented body.
    """
    m = re.search(
        r"<<'" + re.escape(marker) + r"'\n(.*?)\n\s*" + re.escape(marker) + r"\n",
        workflow,
        re.DOTALL,
    )
    assert m, f"heredoc <<'{marker}' not found in {WORKFLOW_PATH}"
    body = m.group(1)
    lines = body.split("\n")
    indents = [len(line) - len(line.lstrip()) for line in lines if line.strip()]
    cut = min(indents) if indents else 0
    return "\n".join(line[cut:] for line in lines)


def _run_block(marker: str, argv: list[str]) -> subprocess.CompletedProcess:
    """Execute a workflow inline-Python block the way the run step does."""
    body = _heredoc(_workflow_text(), marker)
    return subprocess.run(
        [sys.executable, "-", *argv],
        input=body,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def _top_heading_version() -> str:
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(r"^##\s+v?(\d+\.\d+\.\d+)", changelog, re.MULTILINE)
    assert m, "CHANGELOG.md has no version heading"
    return m.group(1)


def _extract_notes() -> str:
    """Run the workflow's own ``<<'PY'`` extraction for the top version."""
    version = _top_heading_version()
    proc = _run_block("PY", ["v" + version])
    assert proc.returncode == 0, "workflow extraction block failed:\n" + proc.stderr
    return proc.stdout


def _extract_prefixes(tmp_path: Path) -> list[str]:
    """Run the workflow's own ``<<'PREFIXES_PY'`` block against .ops.yaml."""
    out = tmp_path / "prefixes.txt"
    proc = _run_block("PREFIXES_PY", [str(out)])
    assert proc.returncode == 0, "workflow prefix block failed:\n" + proc.stderr
    return out.read_text(encoding="utf-8").split()


def _offending_tokens(notes: str, prefixes: set[str]) -> set[str]:
    found: set[str] = set()
    for line in notes.splitlines():
        for m in INTERNAL_TICKET_RE.finditer(line):
            if m.group(0).split("-", 1)[0] in prefixes:
                found.add(m.group(0))
    return found


@pytest.fixture(scope="module")
def notes() -> str:
    return _extract_notes()


@pytest.fixture(scope="module")
def prefixes(tmp_path_factory: pytest.TempPathFactory) -> set[str]:
    return set(_extract_prefixes(tmp_path_factory.mktemp("prefixes")))


# ── Tests ──────────────────────────────────────────────────────────────────


class TestReleaseNotesAudience:
    def test_extraction_block_returns_the_top_section(self, notes):
        assert notes.strip(), "workflow extraction returned empty notes"
        # The extraction block slices from the end of the heading line, so the
        # body carries the section prose and bullets without the heading, and
        # stops at the next version's heading.
        assert not notes.lstrip().startswith("## "), (
            "the extracted body must not begin with the heading line"
        )
        assert "## v1.5.9" not in notes, "the body must stop at the next heading"
        assert "### Features" in notes and "### Infrastructure" in notes

    def test_declared_vocabulary_is_non_trivial(self, prefixes):
        assert len(prefixes) >= 30, (
            f"only {len(prefixes)} tracker prefixes declared; the workflow "
            "prefix block may be mis-parsed"
        )
        assert all(re.fullmatch(r"[A-Z]{2,5}", p) for p in prefixes), prefixes
        assert "SW" in prefixes

    def test_top_release_section_names_no_internal_ticket(self, notes, prefixes):
        offending = _offending_tokens(notes, prefixes)
        assert not offending, (
            "the top release notes name internal ticket references, which the "
            "release-notes audience gate refuses: " + ", ".join(sorted(offending))
        )
        assert "SW-1510 recovery work" not in notes

    def test_negative_control_flags_a_declared_prefix_token(self, prefixes):
        # The scan has teeth: a line naming a declared-prefix token IS flagged.
        sample = sorted(prefixes)[0]
        injected = f"- see {sample}-9999 for details\n"
        assert _offending_tokens(injected, prefixes) == {f"{sample}-9999"}

    def test_negative_control_ignores_an_undeclared_prefix(self, prefixes):
        # The gate refuses only tokens whose prefix the organisation declared;
        # an undeclared shape (e.g. an external identifier) is left alone. This
        # proves the scan is prefix-driven and not a blanket shape match.
        undeclared = "ZZZZ-1"
        assert undeclared.split("-", 1)[0] not in prefixes
        injected = f"- see {undeclared} for details\n"
        assert _offending_tokens(injected, prefixes) == set()
