"""SW-1510-VERIFY: artifact-specific semantics before an Assay PASS.

Three facts are separate: process completion (a clean exit), artifact
integrity (the bytes hash to the claimed digest), and *semantic* verification
(the artifact actually analyzed the source it claims). This suite pins the
third at the real PASS consumer -- :class:`ArtifactEvidenceVerifier` in
``skillweave.runtime.verify`` -- and proves that a falsified analysis proof is
refused while a genuine one, bound to real file/blob/analysis bytes, is
admitted.

Criterion 1 -- empty/narrative-only proofs, README-only graphs over a populated
  source, an unsupported ``analyzed`` flag, an unsupported disposition and a
  substituted source field each fail artifact-specific verification.

Criterion 2 -- a source-coverage entry must bind *both* the real file content
  digest *and* a non-placeholder analysis artifact, or be a justified bounded
  exclusion. Enumerating files (a ``git ls-tree`` line) is not analyzing them.

Criterion 3 -- an ``N/A`` is explicit, bounded and independently justified;
  missing input, a timeout and a partial graph are never repaired into a PASS,
  and no placeholder coverage entry is ever fabricated.

The same verdict is proven through a *fresh installed wheel's* own interpreter
(a real subprocess with the source tree off ``sys.path``), so the installed
native consumer -- not a source-tree shim -- rejects the falsified artifact and
admits the genuine one, preserving the raw refusal reasons and the exact
source/commit pins.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_src = Path(__file__).resolve().parent.parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from skillweave.runtime.verify import (  # noqa: E402
    ArtifactEvidenceVerifier,
    FilesystemSourceResolver,
    GateState,
    artifact_sha256,
    verify_artifact_evidence,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: A real 40-hex commit the source and the artifact both bind to.
COMMIT = "a724d32a724d32a724d32a724d32a724d32a724d"

#: The artifact-semantics proof version this verifier accepts.
VERSION = "evidence-receipt-v1"


# ---------------------------------------------------------------------------
# Genuine positive-control builders (real bytes, real digests -- never faked)
# ---------------------------------------------------------------------------

def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _source_tree(root: Path) -> dict[str, bytes]:
    """Write a real, populated source tree and return path -> bytes."""
    files = {
        "pkg/module.py": b"def real():\n    return 42\n",
        "pkg/util.py": b"VALUE = 'util'\n",
        "README.md": b"# A readme\n",
    }
    for rel, data in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return files


def _analysis_for(files: dict[str, bytes]) -> dict[str, bytes]:
    """A distinct real analysis artifact per source file."""
    return {rel: f"analysis of {rel}: {len(data)} bytes".encode() for rel, data in files.items()}


def _artifact(proof: dict, *, commit: str = COMMIT, sha256: str | None = None) -> dict:
    """A schema-shaped evidence receipt whose self-digest is real.

    ``sha256`` is computed over the canonical artifact (its ``sha256`` field
    excluded), so a genuine artifact carries its own true self-content digest.
    """
    artifact = {
        "artifact_id": "art-1",
        "sha256": "0" * 64,
        "schema_version": "1",
        "producer_command": "analyze",
        "subject_repo": "skillweave",
        "subject_commit": commit,
        "created_at": "2026-10-05T00:00:00Z",
        "evidence_type": "artifact",
        "purpose": "artifact analysis proof",
        "metadata": {"artifact_verifier": proof},
    }
    artifact["sha256"] = sha256 if sha256 is not None else artifact_sha256(artifact)
    return artifact


def _coverage_entry(rel: str, files: dict[str, bytes], analysis: dict[str, bytes], **extra) -> dict:
    """A coverage entry binding the real file digest and the real analysis digest."""
    entry = {
        "path": rel,
        "disposition": "analyzed",
        "content_sha256": _sha(files[rel]),
        "analysis_digest": _sha(analysis[rel]),
    }
    entry.update(extra)
    return entry


def _genuine_proof(files: dict[str, bytes], analysis: dict[str, bytes]) -> dict:
    """A proof graph that genuinely analyzes every non-narrative source file."""
    return {
        "version": VERSION,
        "analyzed": True,
        "coverage": [
            _coverage_entry(rel, files, analysis)
            for rel in files
            if not rel.endswith(".md")
        ],
        "exclusions": [
            {
                "path": "README.md",
                "justification": "documentation only, not executable source",
                "bounded": "narrative paths (*.md)",
            }
        ],
    }


def _reader(analysis: dict[str, bytes]):
    return lambda path: analysis.get(path)


def _verify(tmp_root: Path, proof, *, files=None, analysis=None, artifact=None, commit=COMMIT):
    """Convenience: build a real source tree + resolver and verify *proof*."""
    files = _source_tree(tmp_root) if files is None else files
    analysis = _analysis_for(files) if analysis is None else analysis
    artifact = _artifact(proof) if artifact is None else artifact
    resolver = FilesystemSourceResolver(tmp_root, commit=commit)
    result = ArtifactEvidenceVerifier().verify(
        artifact, resolver=resolver, analysis_reader=_reader(analysis)
    )
    return result, files, analysis


# ---------------------------------------------------------------------------
# Positive control: a genuine artifact is admitted and binds real evidence
# ---------------------------------------------------------------------------

def test_genuine_artifact_is_admitted_and_binds_real_analysis(tmp_path):
    files = _source_tree(tmp_path)
    analysis = _analysis_for(files)
    proof = _genuine_proof(files, analysis)
    result, _, _ = _verify(tmp_path, proof, files=files, analysis=analysis)

    assert result.admitted is True
    assert result.gate_state == GateState.PASS
    assert result.reasons == []
    # Exactly the non-narrative files were analyzed; README is a bounded
    # exclusion, not silently counted as analyzed.
    assert result.analyzed_files == ["pkg/module.py", "pkg/util.py"]
    assert result.exclusions == {"README.md": "documentation only, not executable source"}
    assert result.uncovered == [] and result.phantom == []
    assert result.schema_authority in ("sdk", "structural")


def test_sdk_schema_authority_is_used_when_importable(tmp_path):
    pytest.importorskip("skillweave_sdk")
    files = _source_tree(tmp_path)
    analysis = _analysis_for(files)
    result, _, _ = _verify(tmp_path, _genuine_proof(files, analysis),
                           files=files, analysis=analysis)
    # When the pinned SDK is importable the verifier validates against it --
    # never a copied or re-authored schema.
    assert result.schema_authority == "sdk"
    assert result.admitted is True


def test_genuine_artifact_receipt_is_separate_and_provenance_bound(tmp_path):
    files = _source_tree(tmp_path)
    analysis = _analysis_for(files)
    result, _, _ = _verify(tmp_path, _genuine_proof(files, analysis),
                           files=files, analysis=analysis)
    receipt = result.to_receipt(digest="d" * 64)
    assert receipt["artifact_id"].startswith("artifact-verify-")
    assert receipt["subject_artifact_id"] == result.artifact_digest
    assert receipt["verified_by"] == "artifact-evidence-verifier"
    assert receipt["sha256"] == "d" * 64
    assert receipt["gate_state"] == GateState.PASS


# ---------------------------------------------------------------------------
# Criterion 1: empty / narrative-only / README-only / unsupported flags fail
# ---------------------------------------------------------------------------

def test_empty_graph_over_populated_source_fails(tmp_path):
    files = _source_tree(tmp_path)
    analysis = _analysis_for(files)
    proof = {"version": VERSION, "analyzed": True, "coverage": []}
    result, _, _ = _verify(tmp_path, proof, files=files, analysis=analysis)
    assert result.admitted is False
    assert result.gate_state == GateState.FAIL
    assert any("no source coverage" in r for r in result.reasons)


def test_narrative_only_graph_over_populated_source_fails(tmp_path):
    files = _source_tree(tmp_path)
    analysis = _analysis_for(files)
    # Only the README is "analyzed"; the populated source is untouched.
    proof = {
        "version": VERSION,
        "analyzed": True,
        "coverage": [_coverage_entry("README.md", files, analysis)],
    }
    result, _, _ = _verify(tmp_path, proof, files=files, analysis=analysis)
    assert result.admitted is False
    assert result.gate_state == GateState.FAIL
    # The populated source files are reported uncovered, so the reason is
    # attributable -- not a generic "narrative" refusal.
    assert set(result.uncovered) == {"pkg/module.py", "pkg/util.py"}


def test_readme_only_graph_is_refused_even_with_full_exclusions(tmp_path):
    files = _source_tree(tmp_path)
    analysis = _analysis_for(files)
    proof = {
        "version": VERSION,
        "analyzed": True,
        "coverage": [_coverage_entry("README.md", files, analysis)],
        "exclusions": [
            {"path": p, "justification": f"excluded {p}", "bounded": "*.py"}
            for p in ("pkg/module.py", "pkg/util.py")
        ],
    }
    result, _, _ = _verify(tmp_path, proof, files=files, analysis=analysis)
    # Even with every populated file "excluded", no real source was analyzed:
    # a narrative-only graph never passes. (Under independently-checked
    # exclusions, excluding the executable files is itself refused, so the
    # refusal may surface at the exclusion layer or the population layer.)
    assert result.admitted is False
    assert result.gate_state == GateState.FAIL


def test_unsupported_analyzed_flag_fails(tmp_path):
    files = _source_tree(tmp_path)
    analysis = _analysis_for(files)
    for bad in ("yes", "true", 1, None, ["analyzed"]):
        proof = {"version": VERSION, "analyzed": bad,
                 "coverage": [_coverage_entry("pkg/module.py", files, analysis)]}
        result, _, _ = _verify(tmp_path, proof, files=files, analysis=analysis)
        assert result.admitted is False, f"analyzed={bad!r} must not be a flag"
        assert any("analyzed flag" in r for r in result.reasons)


def test_unsupported_analyzed_disposition_fails(tmp_path):
    files = _source_tree(tmp_path)
    analysis = _analysis_for(files)
    proof = {
        "version": VERSION,
        "analyzed": True,
        "coverage": [_coverage_entry("pkg/module.py", files, analysis, disposition="skimmed")],
    }
    result, _, _ = _verify(tmp_path, proof, files=files, analysis=analysis)
    assert result.admitted is False
    assert any("unsupported analyzed disposition" in r for r in result.reasons)


def test_substituted_content_digest_fails(tmp_path):
    files = _source_tree(tmp_path)
    analysis = _analysis_for(files)
    # The declared file digest is a real digest -- of *different* bytes.
    proof = {
        "version": VERSION,
        "analyzed": True,
        "coverage": [
            _coverage_entry("pkg/module.py", files, analysis,
                            content_sha256=_sha(b"not the real bytes")),
        ],
    }
    result, _, _ = _verify(tmp_path, proof, files=files, analysis=analysis)
    assert result.admitted is False
    assert any("substituted source field" in r for r in result.reasons)


def test_unsupported_version_is_refused(tmp_path):
    files = _source_tree(tmp_path)
    analysis = _analysis_for(files)
    proof = _genuine_proof(files, analysis)
    proof["version"] = "evidence-receipt-v9"
    result, _, _ = _verify(tmp_path, proof, files=files, analysis=analysis)
    assert result.admitted is False
    assert any("unsupported artifact-evidence version" in r for r in result.reasons)


def test_artifact_with_tampered_self_digest_is_refused(tmp_path):
    files = _source_tree(tmp_path)
    analysis = _analysis_for(files)
    artifact = _artifact(_genuine_proof(files, analysis))
    artifact["purpose"] = "tampered after the digest was computed"
    result, _, _ = _verify(tmp_path, None, files=files, analysis=analysis, artifact=artifact)
    assert result.admitted is False
    assert any("does not match its canonical content" in r for r in result.reasons)


# ---------------------------------------------------------------------------
# Criterion 2: coverage binds file/blob/analysis evidence, or a justified N/A
# ---------------------------------------------------------------------------

def test_git_ls_tree_enumeration_is_not_analysis(tmp_path):
    files = _source_tree(tmp_path)
    analysis = _analysis_for(files)
    # A coverage "path" that is really a git ls-tree output line.
    proof = {
        "version": VERSION,
        "analyzed": True,
        "coverage": [
            {"path": "100644 blob a1b2c3\tpkg/module.py", "disposition": "analyzed"},
        ],
    }
    result, _, _ = _verify(tmp_path, proof, files=files, analysis=analysis)
    assert result.admitted is False
    assert any("git ls-tree line" in r for r in result.reasons)


def test_coverage_without_analysis_digest_fails(tmp_path):
    files = _source_tree(tmp_path)
    analysis = _analysis_for(files)
    entry = _coverage_entry("pkg/module.py", files, analysis)
    del entry["analysis_digest"]
    proof = {"version": VERSION, "analyzed": True, "coverage": [entry]}
    result, _, _ = _verify(tmp_path, proof, files=files, analysis=analysis)
    assert result.admitted is False
    assert any("binds no analysis_digest" in r for r in result.reasons)


def test_coverage_with_wrong_analysis_digest_fails(tmp_path):
    files = _source_tree(tmp_path)
    analysis = _analysis_for(files)
    entry = _coverage_entry("pkg/module.py", files, analysis,
                            analysis_digest=_sha(b"a different analysis artifact"))
    proof = {"version": VERSION, "analyzed": True, "coverage": [entry]}
    result, _, _ = _verify(tmp_path, proof, files=files, analysis=analysis)
    assert result.admitted is False
    assert any("analysis_digest does not match" in r for r in result.reasons)


def test_coverage_without_content_digest_fails(tmp_path):
    files = _source_tree(tmp_path)
    analysis = _analysis_for(files)
    entry = _coverage_entry("pkg/module.py", files, analysis)
    del entry["content_sha256"]
    proof = {"version": VERSION, "analyzed": True, "coverage": [entry]}
    result, _, _ = _verify(tmp_path, proof, files=files, analysis=analysis)
    assert result.admitted is False
    assert any("content digest" in r for r in result.reasons)


def test_uncovered_source_file_fails(tmp_path):
    files = _source_tree(tmp_path)
    analysis = _analysis_for(files)
    # Only one populated file is covered; the other is neither analyzed nor
    # justified, so the coverage is partial.
    entry = _coverage_entry("pkg/module.py", files, analysis)
    proof = {"version": VERSION, "analyzed": True, "coverage": [entry]}
    result, _, _ = _verify(tmp_path, proof, files=files, analysis=analysis)
    assert result.admitted is False
    # The other populated file -- and the unexcused README -- are uncovered.
    assert "pkg/util.py" in result.uncovered
    assert "README.md" in result.uncovered

    # And a full, genuine graph over the same tree is admitted -- proving the
    # refusal above is about coverage, not an un-analyzable tree.
    ok, _, _ = _verify(tmp_path, _genuine_proof(files, analysis), files=files, analysis=analysis)
    assert ok.admitted is True


def test_phantom_covered_path_fails(tmp_path):
    files = _source_tree(tmp_path)
    analysis = _analysis_for(files)
    phantom = {"path": "pkg/ghost.py", "disposition": "analyzed",
               "content_sha256": _sha(b"ghost"), "analysis_digest": _sha(b"ghost analysis")}
    proof = {"version": VERSION, "analyzed": True, "coverage": [
        _coverage_entry("pkg/module.py", files, analysis),
        _coverage_entry("pkg/util.py", files, analysis),
        phantom,
    ]}
    result, _, _ = _verify(tmp_path, proof, files=files, analysis=analysis)
    assert result.admitted is False
    assert result.phantom == ["pkg/ghost.py"]


# ---------------------------------------------------------------------------
# Criterion 3: bounded, justified N/A; missing input / timeout / partial graph
# ---------------------------------------------------------------------------

def _narrative_tree(root: Path) -> dict[str, bytes]:
    """A populated source tree with two independently-checkable narrative files."""
    files = _source_tree(root)
    files["docs/guide.md"] = b"# Guide\n\nHow to use the module.\n"
    target = root / "docs" / "guide.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(files["docs/guide.md"])
    return files


def test_justified_bounded_exclusion_is_admitted(tmp_path):
    """Legitimate narrative exclusions, when the reason is independently
    established (a real narrative file with a narrative reason and a covering
    bound), are admitted -- the fix is not a blanket refusal of exclusions."""
    files = _narrative_tree(tmp_path)
    analysis = _analysis_for(files)
    proof = {
        "version": VERSION,
        "analyzed": True,
        "coverage": [
            _coverage_entry("pkg/module.py", files, analysis),
            _coverage_entry("pkg/util.py", files, analysis),
        ],
        "exclusions": [
            {"path": "docs/guide.md",
             "justification": "documentation only, not executable source",
             "bounded": "docs/guide.md"},
            {"path": "README.md",
             "justification": "narrative documentation, no executable code",
             "bounded": "narrative paths (*.md)"},
        ],
    }
    result, _, _ = _verify(tmp_path, proof, files=files, analysis=analysis)
    assert result.admitted is True
    assert result.exclusions == {
        "docs/guide.md": "documentation only, not executable source",
        "README.md": "narrative documentation, no executable code",
    }


def test_na_without_justification_fails(tmp_path):
    files = _source_tree(tmp_path)
    analysis = _analysis_for(files)
    proof = {
        "version": VERSION,
        "analyzed": True,
        "coverage": [_coverage_entry("pkg/module.py", files, analysis)],
        "exclusions": [
            {"path": "pkg/util.py", "bounded": "generated/*.py"},
            {"path": "README.md", "justification": "documentation only",
             "bounded": "narrative paths"},
        ],
    }
    result, _, _ = _verify(tmp_path, proof, files=files, analysis=analysis)
    assert result.admitted is False
    assert any("no justification" in r for r in result.reasons)


def test_na_with_placeholder_justification_fails(tmp_path):
    files = _source_tree(tmp_path)
    analysis = _analysis_for(files)
    for placeholder in ("n/a", "none", "TBD", "unknown", "-"):
        proof = {
            "version": VERSION,
            "analyzed": True,
            "coverage": [_coverage_entry("pkg/module.py", files, analysis)],
            "exclusions": [
                {"path": "pkg/util.py", "justification": placeholder,
                 "bounded": "generated/*.py"},
                {"path": "README.md", "justification": "documentation only",
                 "bounded": "narrative paths"},
            ],
        }
        result, _, _ = _verify(tmp_path, proof, files=files, analysis=analysis)
        assert result.admitted is False, f"placeholder {placeholder!r} must not justify N/A"


def test_na_without_bounding_scope_fails(tmp_path):
    files = _source_tree(tmp_path)
    analysis = _analysis_for(files)
    proof = {
        "version": VERSION,
        "analyzed": True,
        "coverage": [_coverage_entry("pkg/module.py", files, analysis)],
        "exclusions": [
            {"path": "pkg/util.py", "justification": "generated constant table"},
            {"path": "README.md", "justification": "documentation only",
             "bounded": "narrative paths"},
        ],
    }
    result, _, _ = _verify(tmp_path, proof, files=files, analysis=analysis)
    assert result.admitted is False
    assert any("not bounded" in r for r in result.reasons)


def test_false_bounded_exclusion_of_executable_file_fails(tmp_path):
    """Reproduction of the reviewed defect: an executable source file is
    excluded as ``not_applicable`` with a narrative-sounding justification,
    but the declared bound (``README.md``) does not cover it. A non-empty
    justification string is not an independent justification -- the bound
    must actually cover the excluded path, and the source bytes refute the
    narrative reason (``pkg/util.py`` contains executable ``VALUE = 'util'``).
    """
    files = _source_tree(tmp_path)
    analysis = _analysis_for(files)
    proof = {
        "version": VERSION,
        "analyzed": True,
        "coverage": [_coverage_entry("pkg/module.py", files, analysis)],
        "exclusions": [
            {"path": "pkg/util.py",
             "justification": "Narrative documentation only; no executable code",
             "bounded": "README.md"},
            {"path": "README.md", "justification": "documentation only",
             "bounded": "narrative paths"},
        ],
    }
    result, _, _ = _verify(tmp_path, proof, files=files, analysis=analysis)
    assert result.admitted is False
    assert result.gate_state == GateState.FAIL
    assert any("does not cover" in r for r in result.reasons), result.reasons
    # The executable file is not silently accepted as bounded-excluded.
    assert "pkg/util.py" not in result.exclusions


def test_bound_not_covering_a_sibling_glob_fails(tmp_path):
    """A bound that names one file never covers a *different* file, even when
    the justification text is plausible. Only a genuinely-covering bound
    (literal path, directory prefix, matching glob or a matching predicate
    descriptor) is an independent justification."""
    files = _source_tree(tmp_path)
    analysis = _analysis_for(files)
    proof = {
        "version": VERSION,
        "analyzed": True,
        "coverage": [_coverage_entry("pkg/module.py", files, analysis)],
        "exclusions": [
            {"path": "pkg/util.py", "justification": "generated constant table",
             "bounded": "pkg/module.py"},
            {"path": "README.md", "justification": "documentation only",
             "bounded": "narrative paths"},
        ],
    }
    result, _, _ = _verify(tmp_path, proof, files=files, analysis=analysis)
    assert result.admitted is False
    assert any("does not cover" in r for r in result.reasons), result.reasons


def test_genuinely_covering_glob_bound_is_admitted(tmp_path):
    """Positive control: a narrative file excluded with a bound that genuinely
    covers it (a ``docs/*.md`` glob family) and a reason the bytes confirm is
    admitted -- the fix refuses only false/unsupported exclusions."""
    files = _narrative_tree(tmp_path)
    analysis = _analysis_for(files)
    proof = {
        "version": VERSION,
        "analyzed": True,
        "coverage": [
            _coverage_entry("pkg/module.py", files, analysis),
            _coverage_entry("pkg/util.py", files, analysis),
        ],
        "exclusions": [
            {"path": "docs/guide.md",
             "justification": "documentation only, not executable source",
             "bounded": "docs/*.md"},
            {"path": "README.md",
             "justification": "narrative documentation, no executable code",
             "bounded": "*.md"},
        ],
    }
    result, _, _ = _verify(tmp_path, proof, files=files, analysis=analysis)
    assert result.admitted is True
    assert set(result.exclusions) == {"docs/guide.md", "README.md"}


def test_literal_path_bound_is_admitted(tmp_path):
    """Positive control: a bound that names the exact excluded narrative path
    covers it, and the reason is established by the file's real bytes."""
    files = _narrative_tree(tmp_path)
    analysis = _analysis_for(files)
    proof = {
        "version": VERSION,
        "analyzed": True,
        "coverage": [
            _coverage_entry("pkg/module.py", files, analysis),
            _coverage_entry("pkg/util.py", files, analysis),
        ],
        "exclusions": [
            {"path": "docs/guide.md",
             "justification": "documentation only, not executable source",
             "bounded": "docs/guide.md"},
            {"path": "README.md",
             "justification": "narrative documentation, no executable code",
             "bounded": "narrative paths (*.md)"},
        ],
    }
    result, _, _ = _verify(tmp_path, proof, files=files, analysis=analysis)
    assert result.admitted is True


def test_independently_justified_na_with_contradictory_code_fails(tmp_path):
    """The reviewed defect, at its tightest: ``settings.py`` contains real
    executable code (``LIMIT = 7``), is excluded as ``not_applicable`` with a
    narrative-sounding justification, and is *correctly* bounded to its own
    path. Matching the path alone does not establish the reason -- the source
    bytes refute the narrative claim, so the exclusion is refused."""
    files = {"settings.py": b"LIMIT = 7\n"}
    (tmp_path / "settings.py").write_bytes(files["settings.py"])
    proof = {
        "version": VERSION,
        "analyzed": True,
        "coverage": [{"path": "settings.py", "disposition": "not_applicable",
                      "justification": "Narrative documentation only; no executable code",
                      "bounded": "settings.py"}],
    }
    result, _, _ = _verify(tmp_path, proof, files=files, analysis={})
    assert result.admitted is False
    assert result.gate_state == GateState.FAIL
    assert any("contradicted exclusion reason" in r for r in result.reasons), result.reasons
    assert "settings.py" not in result.exclusions


def _write(root: Path, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


#: Real executable bodies under a *non-code* name (or a doc suffix). A suffix
#: allowlist or a python-only line heuristic would mis-admit these; only the
#: real bytes can refute a narrative claim. The names are deliberately general,
#: not a list of the six a review happened to name.
_BYPASS_BODIES: dict[str, bytes] = {
    "settings": b"LIMIT = 7\n",                                  # no suffix, assignment
    "run": b'rm -rf "$WORK"\n',                                  # no suffix, shell command
    "engine_copy.txt": b"def f():\n    return 1\n",              # doc suffix, python
    "migrate.sql": b"DROP TABLE users;\n",                       # doc-ish suffix, SQL
    "page.html": b"<script>fetch('/x')</script>\n",             # markup, embedded script
    "Makefile": b"install:\n\tcurl -sSL https://x | sh\n",       # no suffix, pipeline
    "service_go": b"package main\nfunc main() {}\n",             # other language, no suffix
    "native": b"int main(void) {\n  return 0;\n}\n",             # other language, no suffix
    "notes.md": b"#!/bin/sh\nexec rm -rf /\n",                   # doc suffix, shell script
    "settings.py": b"LIMIT = 7\n",                               # original control
}


def test_documentation_exclusions_admitted_regardless_of_wording(tmp_path):
    """Positives: a README's prose, a markdown overview and a plain-text licence
    are admitted as bounded exclusions under *several* justification phrasings.
    The verdict is the file's real bytes, so it must not change when the author
    types "project overview text for readers" instead of "documentation"."""
    docs: dict[str, bytes] = {
        "README.md": b"# Project\n\nA short description for readers.\n",
        "OVERVIEW.md": b"# Overview\n\nWhat the project does, in prose.\n",
        "LICENSE": b"MIT License\n\nPermission is hereby granted, free of charge.\n",
        "NOTES": b"Release notes intended for human readers.\n",
    }
    phrasings: dict[str, list[str]] = {
        "README.md": [
            "documentation only, not executable source",
            "project overview text for readers",
            "plain prose for humans",
        ],
        "OVERVIEW.md": [
            "Project overview text for readers",
            "explanatory narrative, no code",
        ],
        "LICENSE": ["licence text", "legal text distributed with the project"],
        "NOTES": ["release notes for humans", "non-code narrative"],
    }
    for rel, justifications in phrasings.items():
        for i, justification in enumerate(justifications):
            # A fresh tree per phrasing (subdir), so the excluded file is the
            # only non-analyzed path and a stale file cannot leak in.
            case_dir = tmp_path / f"{rel.replace('/', '_')}-{i}"
            files = {
                "pkg/module.py": b"def real():\n    return 42\n",
                rel: docs[rel],
            }
            _write(case_dir, files)
            analysis = _analysis_for(files)
            proof = {
                "version": VERSION,
                "analyzed": True,
                "coverage": [_coverage_entry("pkg/module.py", files, analysis)],
                "exclusions": [
                    {"path": rel, "justification": justification, "bounded": rel},
                ],
            }
            result, _, _ = _verify(case_dir, proof, files=files, analysis=analysis)
            assert result.admitted is True, (rel, justification, result.reasons)
            assert result.exclusions == {rel: justification}


def test_non_literal_code_bodies_are_refused_as_documentation(tmp_path):
    """Negatives: executable content under a non-code name -- or under a doc
    suffix whose bytes are a script -- is refused as a narrative exclusion even
    with a plausible justification and a correct bound. Every body in
    ``_BYPASS_BODIES`` (the six the review named, other-language code without a
    code suffix, a doc-suffix script, and the original ``settings.py`` control)
    is refuted from the real source, not from the justification wording."""
    for rel, body in _BYPASS_BODIES.items():
        case_dir = tmp_path / rel.replace("/", "_")
        files = {
            "pkg/module.py": b"def real():\n    return 42\n",
            rel: body,
        }
        _write(case_dir, files)
        analysis = _analysis_for(files)
        proof = {
            "version": VERSION,
            "analyzed": True,
            "coverage": [_coverage_entry("pkg/module.py", files, analysis)],
            "exclusions": [
                {"path": rel,
                 "justification": "Narrative documentation only; no executable code",
                 "bounded": rel},
            ],
        }
        result, _, _ = _verify(case_dir, proof, files=files, analysis=analysis)
        assert result.admitted is False, (rel, result.reasons)
        assert any("contradicted exclusion reason" in r for r in result.reasons), (
            rel, result.reasons,
        )
        assert rel not in result.exclusions


def test_generated_na_without_provenance_fails(tmp_path):
    """A generated/vendored justification makes a specific claim about the
    file's provenance. Nothing in the source independently establishes that
    provenance, so the exclusion is unsupported and refused."""
    files = _source_tree(tmp_path)
    analysis = _analysis_for(files)
    proof = {
        "version": VERSION,
        "analyzed": True,
        "coverage": [_coverage_entry("pkg/module.py", files, analysis)],
        "exclusions": [
            {"path": "pkg/util.py", "justification": "generated constant table",
             "bounded": "pkg/util.py"},
            {"path": "README.md", "justification": "documentation only",
             "bounded": "narrative paths (*.md)"},
        ],
    }
    result, _, _ = _verify(tmp_path, proof, files=files, analysis=analysis)
    assert result.admitted is False
    assert any("unsupported exclusion authority" in r for r in result.reasons), result.reasons


def test_generic_na_reason_fails_closed(tmp_path):
    """A generic reason ("not needed here") is not independently checkable
    source-bound evidence: it asserts nothing the source can confirm, so it
    fails closed even when the bound genuinely covers the path."""
    files = _source_tree(tmp_path)
    analysis = _analysis_for(files)
    # The excluded file is executable source: regardless of the generic wording
    # the exclusion is refused because the source bytes/name refuse it -- the
    # verdict comes from the source, not the justification's phrasing.
    proof = {
        "version": VERSION,
        "analyzed": True,
        "coverage": [_coverage_entry("pkg/module.py", files, analysis)],
        "exclusions": [
            {"path": "pkg/util.py", "justification": "not needed for this analysis",
             "bounded": "pkg/util.py"},
            {"path": "README.md", "justification": "documentation only",
             "bounded": "narrative paths (*.md)"},
        ],
    }
    result, _, _ = _verify(tmp_path, proof, files=files, analysis=analysis)
    assert result.admitted is False
    assert any("contradicted exclusion reason" in r for r in result.reasons), result.reasons


def test_na_without_resolvable_source_bytes_fails(tmp_path):
    """When the excluded path's bytes cannot be resolved the narrative claim
    cannot be independently established, so the exclusion fails closed rather
    than being trusted on the strength of a non-empty justification string."""
    files = _source_tree(tmp_path)
    analysis = _analysis_for(files)
    proof = {
        "version": VERSION,
        "analyzed": True,
        "coverage": [_coverage_entry("pkg/module.py", files, analysis)],
        "exclusions": [
            {"path": "absent/settings.py",
             "justification": "Narrative documentation only; no executable code",
             "bounded": "absent/settings.py"},
            {"path": "README.md", "justification": "documentation only",
             "bounded": "narrative paths (*.md)"},
        ],
    }
    result, _, _ = _verify(tmp_path, proof, files=files, analysis=analysis)
    assert result.admitted is False
    assert any(
        "content for the excluded path is unavailable" in r for r in result.reasons
    ), result.reasons


def test_missing_source_input_fails(tmp_path):
    files = _source_tree(tmp_path)
    analysis = _analysis_for(files)
    # The coverage entry addresses a file that is absent from the source.
    entry = {"path": "pkg/vanished.py", "disposition": "analyzed",
             "content_sha256": _sha(b"present once"), "analysis_digest": _sha(b"a")}
    proof = {"version": VERSION, "analyzed": True, "coverage": [entry]}
    result, _, _ = _verify(tmp_path, proof, files=files, analysis=analysis)
    assert result.admitted is False
    # A missing input is a refusal, never a PASS and never a placeholder.
    assert result.phantom == ["pkg/vanished.py"]


def test_no_source_resolver_never_reaches_a_pass(tmp_path):
    files = _source_tree(tmp_path)
    analysis = _analysis_for(files)
    artifact = _artifact(_genuine_proof(files, analysis))
    result = ArtifactEvidenceVerifier().verify(artifact, resolver=None,
                                               analysis_reader=_reader(analysis))
    assert result.admitted is False
    assert any("no source resolver" in r for r in result.reasons)


def test_partial_graph_is_not_repaired_into_a_pass(tmp_path):
    files = _source_tree(tmp_path)
    analysis = _analysis_for(files)
    # A graph truncated by a timeout: the flag is set but coverage is absent.
    proof = {"version": VERSION, "analyzed": True}
    result, _, _ = _verify(tmp_path, proof, files=files, analysis=analysis)
    assert result.admitted is False
    assert result.gate_state == GateState.FAIL
    # No placeholder coverage was fabricated to force a PASS.
    assert result.analyzed_files == []
    assert result.uncovered == []


def test_artifact_without_any_proof_graph_fails(tmp_path):
    files = _source_tree(tmp_path)
    analysis = _analysis_for(files)
    artifact = _artifact({"version": VERSION, "analyzed": True,
                          "coverage": [_coverage_entry("pkg/module.py", files, analysis)]})
    artifact["metadata"] = {}  # proof graph stripped
    artifact["sha256"] = artifact_sha256(artifact)
    result, _, _ = _verify(tmp_path, None, files=files, analysis=analysis, artifact=artifact)
    assert result.admitted is False
    assert any("no analysis proof graph" in r for r in result.reasons)


# ---------------------------------------------------------------------------
# Canonical digest helpers pin the integrity contract
# ---------------------------------------------------------------------------

def test_self_digest_excludes_only_the_sha256_field():
    artifact = _artifact({"version": VERSION, "analyzed": True, "coverage": []})
    assert artifact["sha256"] == artifact_sha256(artifact)
    # Key order and whitespace do not change the digest.
    reordered = {k: artifact[k] for k in reversed(list(artifact))}
    assert artifact_sha256(reordered) == artifact_sha256(artifact)
    # Mutating any other field changes it.
    changed = dict(artifact)
    changed["purpose"] = "different"
    assert artifact_sha256(changed) != artifact_sha256(artifact)


# ---------------------------------------------------------------------------
# Installed native consumer: a fresh wheel's interpreter, source off sys.path
# ---------------------------------------------------------------------------

_INSTALLED_DRIVER = r'''
import json, sys
from skillweave.runtime.verify import ArtifactEvidenceVerifier, FilesystemSourceResolver

art_path, root, commit, amap_path = sys.argv[1:5]
artifact = json.load(open(art_path))
amap = json.load(open(amap_path))

resolver = FilesystemSourceResolver(root, commit=commit)
reader = lambda p: amap[p].encode() if p in amap else None
verifier = ArtifactEvidenceVerifier(expected_commit=commit)
result = verifier.verify(artifact, resolver=resolver, analysis_reader=reader)
print(json.dumps(result.to_dict(), sort_keys=True))
'''


def _venv_site(venv_dir: Path) -> Path:
    matches = sorted(venv_dir.glob("lib/python*/site-packages"))
    assert matches, f"no venv site-packages under {venv_dir}"
    return matches[0]


def _clean_env() -> dict:
    """A subprocess environment with ``PYTHONPATH`` removed.

    The lane runs the tests with ``PYTHONPATH`` pointing at the accepted-SDK
    site-packages, which *also* carries a ``skillweave`` install. An inherited
    ``PYTHONPATH`` therefore makes ``pip`` report "already installed with the
    same version" and skip the freshly-built wheel, leaving the new venv
    without ``skillweave``. Build/install/verify subprocesses therefore run
    with ``PYTHONPATH`` cleared, proving the *installed* wheel is exercised.
    """
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return env


def _accepted_sdk_dir() -> Path | None:
    """The accepted ``skillweave_sdk`` package dir, or ``None``.

    Only an SDK whose ``validator.py`` exports ``AuthenticatedServiceContext``
    is the accepted authority -- the ambient 0.2.0 lacks it. The accepted
    location may be pinned explicitly with ``SKILLWEAVE_ACCEPTED_SDK`` (the
    package dir or its parent ``site-packages``) for a deterministic
    independent-host recipe; otherwise it is discovered via ``PYTHONPATH``
    (the accepted-SDK site-packages the lane runs under), the interpreter's
    own site-packages, or the sequence-relative ``core-execution`` venv. The
    installed venv bundles these files so the verifier validates against the
    pinned SDK rather than silently degrading to the structural fallback.
    """
    cands: list[Path] = []

    def _consider(raw: str | os.PathLike[str]) -> None:
        p = Path(raw)
        if p.name == "skillweave_sdk":
            cands.append(p.parent)
        else:
            cands.append(p)

    override = os.environ.get("SKILLWEAVE_ACCEPTED_SDK")
    if override:
        _consider(override)
    for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep):
        if entry:
            cands.append(Path(entry))
    for site in Path(sys.prefix).glob("lib/python*/site-packages"):
        cands.append(site)
    # Sequence-relative: the accepted ``core-execution`` venv under the
    # checkout's own sequence (the ancestor that holds ``checkouts/``). A
    # reviewer lane can reach this; an OS sandbox that hides ``preflight``
    # simply yields no candidate (fails closed).
    for up in Path(__file__).resolve().parents:
        if (up / "checkouts").is_dir():
            try:
                for sdk in sorted(up.glob(
                        "preflight/*/venv/lib/python*/site-packages/skillweave_sdk")):
                    cands.append(sdk.parent)
            except OSError:
                pass
            break
    cands += [
        Path("/opt/homebrew/lib/python3.11/site-packages"),
        Path("/opt/homebrew/lib/python3.14/site-packages"),
    ]
    for cand in cands:
        validator = cand / "skillweave_sdk" / "validator.py"
        if not validator.is_file():
            continue
        try:
            if "AuthenticatedServiceContext" in validator.read_text():
                return cand / "skillweave_sdk"
        except OSError:
            continue
    return None


def _require_accepted_sdk() -> Path:
    """The accepted SDK, or a *skip* when this host genuinely lacks it.

    These installed-wheel tests cannot fabricate a native PASS without the
    pinned SDK authority; a host without it (ambient 0.2.0 only) must skip
    rather than fail, matching ``pytest.importorskip`` used for the other
    native paths. An explicit ``SKILLWEAVE_ACCEPTED_SDK`` that is set but
    invalid is a hard error, not a silent skip.
    """
    accepted = _accepted_sdk_dir()
    if accepted is None:
        if os.environ.get("SKILLWEAVE_ACCEPTED_SDK"):
            pytest.fail(
                "SKILLWEAVE_ACCEPTED_SDK is set but does not resolve to an SDK "
                "whose validator exports AuthenticatedServiceContext"
            )
        pytest.skip(
            "no accepted skillweave_sdk (validator exporting "
            "AuthenticatedServiceContext) on this host; set "
            "SKILLWEAVE_ACCEPTED_SDK to run the installed-wheel native proofs"
        )
    return accepted


def _build_wheel(work: Path) -> Path:
    """Copy the checkout and build a wheel offline (never writes the checkout)."""
    (work / "src").parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / "src", work / "src")
    shutil.copy2(REPO_ROOT / "pyproject.toml", work / "pyproject.toml")
    shutil.copy2(REPO_ROOT / "README.md", work / "README.md")
    shutil.copy2(REPO_ROOT / "LICENSE", work / "LICENSE")

    proc = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-index",
         "--no-build-isolation", "-w", str(work / "dist"), str(work)],
        cwd=str(work), capture_output=True, text=True, env=_clean_env(),
    )
    assert proc.returncode == 0, proc.stderr
    wheels = sorted((work / "dist").glob("skillweave-*.whl"))
    assert wheels, f"no wheel built in {work / 'dist'}"
    return wheels[-1]


def _install_into_fresh_venv(work: Path, wheel: Path) -> Path:
    venv_dir = work / "venv"
    env = _clean_env()
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True,
                   capture_output=True, text=True, env=env)
    site = _venv_site(venv_dir)
    subprocess.run(
        [str(venv_dir / "bin" / "pip"), "install", "--no-deps", "--no-index",
         str(wheel)],
        check=True, capture_output=True, text=True, env=env,
    )
    # The offline install cannot fetch PyYAML; make it available without
    # reaching the network or the source tree.
    import yaml  # noqa: PLC0415 - intentional late import

    ambient_yaml = Path(yaml.__file__).resolve().parent
    shutil.copytree(ambient_yaml, site / "yaml")
    extra = ambient_yaml.parent / "_yaml"
    if extra.exists():
        shutil.copytree(extra, site / "_yaml")
    # Bundle the accepted SDK so the installed verifier validates against the
    # pinned schema authority (never the structural fallback, never ambient).
    accepted = _require_accepted_sdk()
    shutil.copytree(accepted, site / "skillweave_sdk")
    return venv_dir


def _run_installed(venv_dir: Path, tmp_path: Path, artifact: dict,
                   root: Path, analysis: dict) -> dict:
    art_file = tmp_path / "artifact.json"
    art_file.write_text(json.dumps(artifact))
    amap_file = tmp_path / "amap.json"
    amap_file.write_text(json.dumps({k: v.decode() for k, v in analysis.items()}))

    env = _clean_env()  # prove the *installed* package (and its bundled SDK) is used
    env["TMPDIR"] = str(tmp_path / "tmp")
    (tmp_path / "tmp").mkdir(exist_ok=True)

    proc = subprocess.run(
        [str(venv_dir / "bin" / "python"), "-c", _INSTALLED_DRIVER,
         str(art_file), str(root), COMMIT, str(amap_file)],
        cwd=str(tmp_path), env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_installed_wheel_rejects_falsified_and_admits_genuine(tmp_path):
    pytest.importorskip("yaml")

    work = tmp_path / "build"
    work.mkdir()
    wheel = _build_wheel(work)
    venv_dir = _install_into_fresh_venv(work, wheel)

    root = tmp_path / "src-tree"
    files = _source_tree(root)
    analysis = _analysis_for(files)

    genuine = _artifact(_genuine_proof(files, analysis))
    genuine_result = _run_installed(venv_dir, tmp_path, genuine, root, analysis)
    assert genuine_result["admitted"] is True
    assert genuine_result["gate_state"] == "pass"
    # The installed verifier validated against the *pinned SDK schema*, never
    # the structural fallback -- the real authority is exercised.
    assert genuine_result["schema_authority"] == "sdk"
    # Exact source pins and raw diagnostics are preserved end to end.
    assert genuine_result["artifact_digest"] == genuine["sha256"]
    assert genuine_result["analyzed_files"] == ["pkg/module.py", "pkg/util.py"]
    assert genuine_result["reasons"] == []

    # A falsified artifact: real digests, but bound to substituted content.
    falsified_proof = _genuine_proof(files, analysis)
    falsified_proof["coverage"][0]["content_sha256"] = _sha(b"substituted bytes")
    falsified = _artifact(falsified_proof)
    falsified_result = _run_installed(venv_dir, tmp_path, falsified, root, analysis)
    assert falsified_result["admitted"] is False
    assert falsified_result["gate_state"] == "fail"
    assert any("substituted source field" in r for r in falsified_result["reasons"])

    # A README-only graph over the same populated tree is also refused.
    readme_only = _artifact({
        "version": VERSION,
        "analyzed": True,
        "coverage": [_coverage_entry("README.md", files, analysis)],
    })
    readme_result = _run_installed(venv_dir, tmp_path, readme_only, root, analysis)
    assert readme_result["admitted"] is False

    # -- the reviewed counterexample, end to end through the installed wheel:
    #    ``settings.py`` with real executable code, excluded as
    #    ``not_applicable`` under a narrative justification and *correctly*
    #    bounded to its own path. Matching the path alone must not admit it --
    #    the installed native consumer refutes the reason against real bytes.
    (root / "settings.py").write_bytes(b"LIMIT = 7\n")
    settings_proof = {
        "version": VERSION,
        "analyzed": True,
        "coverage": [{"path": "settings.py", "disposition": "not_applicable",
                      "justification": "Narrative documentation only; no executable code",
                      "bounded": "settings.py"}],
    }
    settings_result = _run_installed(
        venv_dir, tmp_path, _artifact(settings_proof), root, analysis
    )
    assert settings_result["admitted"] is False
    assert settings_result["gate_state"] == "fail"
    assert any(
        "contradicted exclusion reason" in r for r in settings_result["reasons"]
    ), settings_result["reasons"]

    # -- the positive counterpart in the *same* tree: a genuine code-analysis
    #    proof is admitted by the same installed consumer, so the fix refuses
    #    the contradicted exclusion without refusing legitimate analysis.
    code_tree = {
        "settings.py": b"LIMIT = 7\n",
        "pkg/module.py": b"def real():\n    return 42\n",
        "pkg/util.py": b"VALUE = 'util'\n",
        "README.md": b"# A readme\n",
    }
    code_analysis = {rel: f"analysis of {rel}".encode() for rel in code_tree}
    positive_proof = {
        "version": VERSION,
        "analyzed": True,
        "coverage": [
            _coverage_entry("settings.py", code_tree, code_analysis),
            _coverage_entry("pkg/module.py", code_tree, code_analysis),
            _coverage_entry("pkg/util.py", code_tree, code_analysis),
        ],
        "exclusions": [
            {"path": "README.md",
             "justification": "documentation only, not executable source",
             "bounded": "narrative paths (*.md)"},
        ],
    }
    positive_result = _run_installed(
        venv_dir, tmp_path, _artifact(positive_proof), root, code_analysis
    )
    assert positive_result["admitted"] is True
    assert positive_result["gate_state"] == "pass"
    assert positive_result["reasons"] == []


def test_clean_nonempty_output_is_still_the_only_completion_pass():
    # The completion contract (process completion) is untouched by the
    # artifact-semantics layer: exit 0 with empty stdout stays inconclusive and
    # the genuine artifact layer composes on top without weakening it.
    from skillweave.runtime.verify import CompletionContract

    contract = CompletionContract()
    assert contract.evaluate(exit_code=0, signal=None, termination="exited",
                             stdout=b"ok") == GateState.PASS
    assert contract.evaluate(exit_code=0, signal=None, termination="exited",
                             stdout=b"") == GateState.INCONCLUSIVE


# ---------------------------------------------------------------------------
# Gate reconciliation: artifact semantics are mandatory before a PASS
# ---------------------------------------------------------------------------

def _fake_registry(total=3):
    class _Registry:
        def count_by_type(self):
            return {"test": total} if total else {}

        def get_findings(self):
            return []

    return _Registry()


def _fake_observer():
    class _Observer:
        def state(self):
            return type("S", (), {"outputs": []})()

    return _Observer()


def test_gate_legacy_path_reconciles_without_artifact_evidence():
    # The explicit legacy adapter: a caller carrying no artifact proof keeps the
    # pre-SW-1510 flow, so no message that predates this feature is broken.
    from skillweave.runtime.gate_reconciliation import reconcile_gate

    result = reconcile_gate("G", _fake_registry(), _fake_observer(), None)
    assert result.reconciled is True
    assert result.evidence_weight == "sufficient"


def test_gate_admits_genuine_artifact_evidence(tmp_path):
    from skillweave.runtime.gate_reconciliation import reconcile_gate

    files = _source_tree(tmp_path)
    analysis = _analysis_for(files)
    artifact = _artifact(_genuine_proof(files, analysis))
    resolver = FilesystemSourceResolver(tmp_path, commit=COMMIT)
    result = reconcile_gate(
        "G", _fake_registry(), _fake_observer(), None,
        artifact_evidence=[artifact], source_resolver=resolver,
        analysis_reader=_reader(analysis),
    )
    assert result.reconciled is True
    assert result.evidence_weight == "sufficient"
    assert "verified against bound source" in result.authority_statement


def test_gate_refuses_falsified_artifact_evidence(tmp_path):
    from skillweave.runtime.gate_reconciliation import reconcile_gate

    files = _source_tree(tmp_path)
    analysis = _analysis_for(files)
    proof = _genuine_proof(files, analysis)
    proof["coverage"][0]["content_sha256"] = _sha(b"substituted bytes")
    artifact = _artifact(proof)
    resolver = FilesystemSourceResolver(tmp_path, commit=COMMIT)
    result = reconcile_gate(
        "G", _fake_registry(), _fake_observer(), None,
        artifact_evidence=[artifact], source_resolver=resolver,
        analysis_reader=_reader(analysis),
    )
    assert result.reconciled is False
    assert result.evidence_weight == "artifact_evidence_refused"
    # The raw refusal diagnostic is preserved on the authority statement,
    # labelled with the exact artifact that was refused.
    assert "substituted source field" in result.authority_statement
    assert "art-1" in result.authority_statement


def test_gate_refuses_readme_only_artifact_evidence(tmp_path):
    from skillweave.runtime.gate_reconciliation import reconcile_gate

    files = _source_tree(tmp_path)
    analysis = _analysis_for(files)
    artifact = _artifact({
        "version": VERSION,
        "analyzed": True,
        "coverage": [_coverage_entry("README.md", files, analysis)],
    })
    resolver = FilesystemSourceResolver(tmp_path, commit=COMMIT)
    result = reconcile_gate(
        "G", _fake_registry(), _fake_observer(), None,
        artifact_evidence=[artifact], source_resolver=resolver,
        analysis_reader=_reader(analysis),
    )
    assert result.reconciled is False
    assert result.evidence_weight == "artifact_evidence_refused"


def test_gate_refuses_when_source_binding_requested_but_no_artifacts(tmp_path):
    from skillweave.runtime.gate_reconciliation import reconcile_gate

    resolver = FilesystemSourceResolver(tmp_path, commit=COMMIT)
    # A source resolver was supplied, so the gate demands artifact-semantic
    # proof -- and there is none. A missing proof is never a PASS.
    result = reconcile_gate(
        "G", _fake_registry(), _fake_observer(), None,
        source_resolver=resolver,
    )
    assert result.reconciled is False
    assert result.evidence_weight == "artifact_evidence_refused"


def test_gate_insufficient_evidence_still_blocks():
    from skillweave.runtime.gate_reconciliation import reconcile_gate

    result = reconcile_gate("G", _fake_registry(total=1), _fake_observer(), None)
    assert result.reconciled is False
    assert result.evidence_weight == "insufficient"


def test_reconciliation_result_has_no_override_field():
    # A gate decision cannot be flipped by a caller-supplied field; the closed
    # shape is what keeps artifact refusal non-overridable.
    from skillweave.runtime.gate_reconciliation import ReconciliationResult

    assert set(ReconciliationResult.__dataclass_fields__) == {
        "reconciled", "evidence_weight", "observer_verdict",
        "authority_statement", "gate_name", "timestamp",
    }


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
