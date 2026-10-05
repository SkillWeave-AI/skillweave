"""SW-1510-ASSAY-SEMANTICS: the three acceptance rows enforced by the *native
Assay PASS consumer itself*, proven from an installed wheel.

The controller's central finding was that the artifact verifier existed but no
native Assay execution consumer called it: the installed proof exercised a
never-called helper and the legacy omission bypass remained. This module closes
that gap at the seam that actually decides a native Assay PASS --
``RunApplicationService._run_assay_drill`` (reached through both
``execute`` and ``execute_streaming_native``) -- and proves it end to end from a
freshly *installed* wheel.

Criterion 1 -- empty/narrative-only proofs, README-only graphs over populated
  sources, unsupported analyzed dispositions and substituted source fields fail
  artifact-specific verifiers *at the native consumer*.
Criterion 2 -- a source-coverage entry must bind file/blob/analysis evidence or
  a justified exclusion; a git ``ls-tree`` enumeration is not analysis.
Criterion 3 -- N/A is explicit/bounded/independently justified; an omitted proof
  graph, an empty graph and a partial graph never become a PASS by schema repair
  or placeholder creation.

Every refusal is asserted on the *blocking* gate state (the completion contract
itself PASSes, so the withhold is provably the artifact-semantics layer) and on
the raw refusal reason -- never on a repaired or fabricated PASS.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from skillweave.runsvc import RunApplicationService  # noqa: E402
from skillweave.runtime.store import SQLiteRunStore  # noqa: E402
from skillweave.runtime.journal import EventJournal  # noqa: E402
from skillweave.runtime.registry import RawArtifactStore  # noqa: E402
from skillweave.runtime.stream import DurableStreamStore  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _assay_evidence import (  # noqa: E402
    VERSION,
    artifact,
    coverage_entry,
    genuine_artifact,
    genuine_proof,
    reader_for,
    readme_only_artifact,
    sha,
    write_analysis_tree,
    write_source_tree,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: A real, 40-hex commit the artifact proof and the source resolver both bind.
COMMIT = "a724d32a724d32a724d32a724d32a724d32a724d"

#: A real bounded probe that exits 0 with non-empty stdout, so the *completion*
#: gate PASSes and any refusal below is provably the artifact-semantics layer.
_PASS_PROBE = [sys.executable, "-c", "print('sem-probe-output')"]


@pytest.fixture(autouse=True)
def _scratch_tmp(tmp_path, monkeypatch):
    """Keep every scratch file off the host TMPDIR."""

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path), raising=False)
    yield tmp_path


def _service(db_path=":memory:"):
    store = SQLiteRunStore(db_path)
    # The protected native entry demands a *durable* stream store (it refuses a
    # memory-only stand-in), so the scratch root is a real on-disk directory
    # under the per-test tmp (tempfile.tempdir is re-pointed by the fixture).
    stream_root = Path(tempfile.mkdtemp(prefix="sem-stream-")) / "streams"
    artifacts = Path(tempfile.mkdtemp(prefix="sem-art-"))
    return RunApplicationService(
        store,
        EventJournal(db_path),
        RawArtifactStore(str(artifacts)),
        DurableStreamStore(root=str(stream_root)),
    )


def _bundle(base):
    src = Path(base) / "src-tree"
    ana = Path(base) / "analysis"
    files = write_source_tree(src)
    analysis = write_analysis_tree(ana, files)
    return files, analysis, src, ana


def _run_native(base, artifacts, *, src, analysis, run_id="sem-1"):
    """Drive the real in-process native consumer with the given proof."""
    from skillweave.runtime.verify import FilesystemSourceResolver

    svc = _service()
    resolver = (
        FilesystemSourceResolver(src, commit=COMMIT) if src is not None else None
    )
    reader = reader_for(analysis) if analysis is not None else None
    out = svc.execute_streaming_native(
        list(_PASS_PROBE),
        run_id=run_id,
        tool="opencode",
        model="m",
        subject_repo="skillweave",
        subject_commit=COMMIT,
        methodology="assay",
        autonomy_policy="unicorn",
        task_id="t-sem",
        attempt=1,
        created_at="2026-10-05T00:00:00Z",
        artifact_evidence=artifacts,
        source_resolver=resolver,
        analysis_reader=reader,
    )
    return svc, out


def _block(out):
    return out["assay"]


# ---------------------------------------------------------------------------
# Criterion 3: omission is itself a refusal -- the legacy bypass is closed.
# ---------------------------------------------------------------------------

def test_omitted_artifact_evidence_refuses_native_pass(tmp_path):
    """A completion-gate PASS with *no* artifact evidence is refused.

    This is the exact bypass the controller found: a native Assay worker could
    formerly omit the proof fields and still get a PASS. Now omission withholds
    the native PASS with the raw ``artifact_evidence_missing`` reason.
    """
    files, analysis, src, _ = _bundle(tmp_path)
    _, out = _run_native(tmp_path, None, src=src, analysis=analysis, run_id="omit")
    block = _block(out)

    # The completion contract passed -- so the withhold is the semantics layer.
    assert block["completion_gate_state"] == "pass"
    assert block["artifact_evidence"]["admitted"] is False
    assert block["outcome"] == "hold"
    assert any(
        "artifact_evidence_missing" in r for r in block["artifact_evidence"]["reasons"]
    )
    assert block["phases"][-1]["phase"] == "hold"
    # No artifact_verify admission was fabricated.
    verify = next(p for p in block["phases"] if p["phase"] == "artifact_verify")
    assert verify["artifact_admitted"] is False


def test_empty_graph_over_populated_source_refuses_native_pass(tmp_path):
    """An empty proof graph over a populated source is refused by the consumer."""
    files, analysis, src, _ = _bundle(tmp_path)
    empty = artifact({"version": VERSION, "analyzed": True, "coverage": []},
                     commit=COMMIT)
    _, out = _run_native(tmp_path, [empty], src=src, analysis=analysis, run_id="empty")
    block = _block(out)

    assert block["completion_gate_state"] == "pass"
    assert block["artifact_evidence"]["admitted"] is False
    assert block["outcome"] == "hold"
    joined = " ".join(block["artifact_evidence"]["reasons"])
    assert "no source coverage" in joined or "empty graph" in joined


# ---------------------------------------------------------------------------
# Criterion 3: a partial graph / an unjustified N/A never becomes a PASS.
# ---------------------------------------------------------------------------

def test_partial_graph_is_not_repaired_into_a_native_pass(tmp_path):
    files, analysis, src, _ = _bundle(tmp_path)
    proof = genuine_proof(files, analysis)
    proof["coverage"] = proof["coverage"][:1]  # drop pkg/util.py
    partial = artifact(proof, commit=COMMIT)

    _, out = _run_native(tmp_path, [partial], src=src, analysis=analysis,
                         run_id="partial")
    block = _block(out)
    assert block["artifact_evidence"]["admitted"] is False
    assert block["outcome"] == "hold"
    joined = " ".join(block["artifact_evidence"]["reasons"])
    assert "neither analyzed nor justified" in joined
    assert "schema repair" not in joined  # never repaired into a pass


def test_na_omission_without_justification_never_becomes_a_native_pass(tmp_path):
    """N/A is only bound when it is explicit + independently justified.

    Marking a real source file N/A with no justification must not become a
    native PASS; the raw reason names the unjustified file.
    """
    files, analysis, src, _ = _bundle(tmp_path)
    proof = genuine_proof(files, analysis)
    # pkg/util.py is covered positionally; replace it with a bare N/A (no
    # justification / no bounding scope).
    proof["coverage"] = [
        coverage_entry("pkg/module.py", files, analysis),
        {"path": "pkg/util.py", "disposition": "not_applicable"},
    ]
    unjustified = artifact(proof, commit=COMMIT)

    _, out = _run_native(tmp_path, [unjustified], src=src, analysis=analysis,
                         run_id="na")
    block = _block(out)
    assert block["artifact_evidence"]["admitted"] is False
    assert block["outcome"] == "hold"
    joined = " ".join(block["artifact_evidence"]["reasons"])
    assert "N/A" in joined and "justification" in joined


# ---------------------------------------------------------------------------
# Criterion 1 + 2: substituted fields, README-only, git enumeration, and a
# missing source binding all refuse at the native consumer.
# ---------------------------------------------------------------------------

def test_substituted_source_field_refuses_native_pass(tmp_path):
    files, analysis, src, _ = _bundle(tmp_path)
    proof = genuine_proof(files, analysis)
    proof["coverage"][0]["content_sha256"] = sha(b"substituted bytes")
    falsified = artifact(proof, commit=COMMIT)

    _, out = _run_native(tmp_path, [falsified], src=src, analysis=analysis,
                         run_id="sub")
    block = _block(out)
    assert block["artifact_evidence"]["admitted"] is False
    assert block["outcome"] == "hold"
    assert any(
        "substituted source field" in r for r in block["artifact_evidence"]["reasons"]
    )


def test_readme_only_graph_refuses_native_pass(tmp_path):
    files, analysis, src, _ = _bundle(tmp_path)
    readme_only = readme_only_artifact(files, analysis, commit=COMMIT)

    _, out = _run_native(tmp_path, [readme_only], src=src, analysis=analysis,
                         run_id="readme")
    block = _block(out)
    assert block["artifact_evidence"]["admitted"] is False
    assert block["outcome"] == "hold"


def test_git_ls_tree_enumeration_is_not_analysis_at_the_consumer(tmp_path):
    files, analysis, src, _ = _bundle(tmp_path)
    proof = genuine_proof(files, analysis)
    # A ``git ls-tree`` line is an enumeration, not a per-file binding.
    proof["coverage"] = [
        {"path": "100644 blob aaaa\tpkg/module.py", "disposition": "analyzed"}
    ]
    enumerated = artifact(proof, commit=COMMIT)

    _, out = _run_native(tmp_path, [enumerated], src=src, analysis=analysis,
                         run_id="git-enum")
    block = _block(out)
    assert block["artifact_evidence"]["admitted"] is False
    assert block["outcome"] == "hold"
    assert any(
        "git ls-tree" in r for r in block["artifact_evidence"]["reasons"]
    )


def test_missing_source_binding_refuses_native_pass(tmp_path):
    """A genuine artifact with no source root never becomes a PASS."""
    files, analysis, src, _ = _bundle(tmp_path)
    genuine = genuine_artifact(files, analysis, commit=COMMIT)

    _, out = _run_native(tmp_path, [genuine], src=None, analysis=analysis,
                         run_id="no-src")
    block = _block(out)
    assert block["artifact_evidence"]["admitted"] is False
    assert block["outcome"] == "hold"
    assert any(
        "no source resolver" in r for r in block["artifact_evidence"]["reasons"]
    )


def test_genuine_artifact_admits_native_pass(tmp_path):
    """The positive control: a genuine proof is admitted and the PASS stands."""
    files, analysis, src, _ = _bundle(tmp_path)
    genuine = genuine_artifact(files, analysis, commit=COMMIT)

    svc, out = _run_native(tmp_path, [genuine], src=src, analysis=analysis,
                           run_id="good")
    block = _block(out)
    assert block["completion_gate_state"] == "pass"
    assert block["artifact_evidence"]["admitted"] is True
    assert block["artifact_evidence"]["reasons"] == []
    assert block["outcome"] == "pass"
    assert block["phases"][-1]["phase"] == "pass"

    # The raw verdict is persisted as its own addressable receipt, so the
    # decision is durable and reconstructable (not only returned in memory).
    stored = svc.store.get_evidence("artifact-verify-good")
    assert stored is not None
    assert stored.metadata["admitted"] is True


# ---------------------------------------------------------------------------
# The installed wheel: the *same* positive and negative controls driven through
# the real installed native entry, never the source tree.
# ---------------------------------------------------------------------------

_DRIVER = r'''
import json, sys
from skillweave.runsvc.service import RunApplicationService
from skillweave.runtime.store import SQLiteRunStore
from skillweave.runtime.journal import EventJournal
from skillweave.runtime.registry import RawArtifactStore
from skillweave.runtime.stream import DurableStreamStore
from skillweave.runtime.verify import FilesystemSourceResolver

cfg = json.load(open(sys.argv[1]))
ev = json.load(open(sys.argv[2]))

artifacts = ev.get("artifacts")
analysis = ev.get("analysis")
reader = (
    (lambda p: analysis[p].encode() if p in analysis else None)
    if analysis is not None else None
)
src = ev.get("source_root") or None
resolver = FilesystemSourceResolver(src, commit=ev["commit"]) if src else None

store = SQLiteRunStore(cfg["db"])
svc = RunApplicationService(
    store, EventJournal(cfg["db"]), RawArtifactStore(cfg["art"]),
    DurableStreamStore(root=cfg["streams"]),
)
out = svc.execute_streaming_native(
    [sys.executable, "-c", "print('installed-sem')"],
    run_id=cfg["run_id"], tool="opencode", model="m",
    subject_repo="skillweave", subject_commit=ev["commit"],
    methodology="assay", autonomy_policy="unicorn", task_id="t-1", attempt=1,
    created_at="2026-10-05T00:00:00Z",
    artifact_evidence=artifacts, source_resolver=resolver, analysis_reader=reader,
)
block = out["assay"]
print(json.dumps({
    "outcome": block["outcome"],
    "admitted": block["artifact_evidence"]["admitted"],
    "reasons": block["artifact_evidence"]["reasons"],
    "gate_state": block["completion_gate_state"],
}))
'''


def _venv_site(venv_dir: Path) -> Path:
    matches = sorted(venv_dir.glob("lib/python*/site-packages"))
    assert matches, f"no venv site-packages under {venv_dir}"
    return matches[0]


def _build_wheel(work: Path) -> Path:
    (work / "src").parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / "src", work / "src")
    shutil.copy2(REPO_ROOT / "pyproject.toml", work / "pyproject.toml")
    shutil.copy2(REPO_ROOT / "README.md", work / "README.md")
    shutil.copy2(REPO_ROOT / "LICENSE", work / "LICENSE")

    proc = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-index",
         "--no-build-isolation", "-w", str(work / "dist"), str(work)],
        cwd=str(work), capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    wheels = sorted((work / "dist").glob("skillweave-*.whl"))
    assert wheels, f"no wheel built in {work / 'dist'}"
    return wheels[-1]


def _install_into_fresh_venv(work: Path, wheel: Path) -> Path:
    venv_dir = work / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True,
                   capture_output=True, text=True)
    site = _venv_site(venv_dir)
    subprocess.run(
        [str(venv_dir / "bin" / "pip"), "install", "--no-deps", "--no-index",
         str(wheel)],
        check=True, capture_output=True, text=True,
    )
    import yaml  # noqa: PLC0415 - intentional late import

    ambient_yaml = Path(yaml.__file__).resolve().parent
    shutil.copytree(ambient_yaml, site / "yaml")
    extra = ambient_yaml.parent / "_yaml"
    if extra.exists():
        shutil.copytree(extra, site / "_yaml")
    return venv_dir


def _run_installed(venv_dir: Path, tmp_path: Path, name: str, ev: dict) -> dict:
    case = tmp_path / f"case-{name}"
    case.mkdir()
    cfg_file = case / "cfg.json"
    out_root = tmp_path / f"out-{name}"
    out_root.mkdir()
    cfg_file.write_text(json.dumps({
        "db": str(out_root / "run.db"),
        "art": str(out_root / "art"),
        "streams": str(out_root / "streams"),
        "run_id": f"inst-{name}",
    }))
    ev_file = case / "ev.json"
    ev_file.write_text(json.dumps(ev))

    env = dict(os.environ)
    env.pop("PYTHONPATH", None)  # prove the *installed* package is exercised
    env["TMPDIR"] = str(case / "tmp")
    (case / "tmp").mkdir(exist_ok=True)

    proc = subprocess.run(
        [str(venv_dir / "bin" / "python"), "-c", _DRIVER,
         str(cfg_file), str(ev_file)],
        cwd=str(case), env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _installed_evidence(tmp_path):
    """On-disk source + analysis trees plus the serializable evidence bundle."""
    src = tmp_path / "inst-src"
    ana = tmp_path / "inst-analysis"
    files = write_source_tree(src)
    analysis = write_analysis_tree(ana, files)
    return files, analysis, src, ana


def test_installed_wheel_native_consumer_enforces_semantics(tmp_path):
    pytest.importorskip("yaml")

    work = tmp_path / "build"
    work.mkdir()
    wheel = _build_wheel(work)
    venv_dir = _install_into_fresh_venv(work, wheel)

    files, analysis, src, ana = _installed_evidence(tmp_path)
    analysis_text = {k: v.decode() for k, v in analysis.items()}

    def case(name, artifacts, *, source=src, analysis_map=analysis_text):
        return _run_installed(venv_dir, tmp_path, name, {
            "artifacts": artifacts,
            "analysis": analysis_map,
            "source_root": str(source) if source is not None else "",
            "commit": COMMIT,
        })

    # -- positive: a genuine proof is admitted by the *installed* consumer.
    genuine = genuine_artifact(files, analysis, commit=COMMIT)
    good = case("good", [genuine])
    assert good["gate_state"] == "pass"
    assert good["admitted"] is True
    assert good["outcome"] == "pass"
    assert good["reasons"] == []

    # -- omission: no artifact evidence at all -> refusal by the consumer.
    omitted = case("omit", None)
    assert omitted["gate_state"] == "pass"
    assert omitted["admitted"] is False
    assert omitted["outcome"] == "hold"
    assert any("artifact_evidence_missing" in r for r in omitted["reasons"])

    # -- empty / narrative-only graph over a populated source -> refusal.
    empty = case("empty", [artifact(
        {"version": VERSION, "analyzed": True, "coverage": []}, commit=COMMIT
    )])
    assert empty["admitted"] is False and empty["outcome"] == "hold"

    # -- falsified / substituted source field -> refusal.
    sub_proof = genuine_proof(files, analysis)
    sub_proof["coverage"][0]["content_sha256"] = sha(b"substituted bytes")
    falsified = case("falsified", [artifact(sub_proof, commit=COMMIT)])
    assert falsified["admitted"] is False and falsified["outcome"] == "hold"
    assert any("substituted source field" in r for r in falsified["reasons"])

    # -- README-only graph over the same populated tree -> refusal.
    readme = case("readme", [readme_only_artifact(files, analysis, commit=COMMIT)])
    assert readme["admitted"] is False and readme["outcome"] == "hold"

    # -- partial graph (a real file left neither analyzed nor justified).
    partial_proof = genuine_proof(files, analysis)
    partial_proof["coverage"] = partial_proof["coverage"][:1]
    partial = case("partial", [artifact(partial_proof, commit=COMMIT)])
    assert partial["admitted"] is False and partial["outcome"] == "hold"
    assert any("neither analyzed nor justified" in r for r in partial["reasons"])

    # -- N/A omission: a real file marked N/A without justification -> refusal.
    na_proof = genuine_proof(files, analysis)
    na_proof["coverage"] = [
        coverage_entry("pkg/module.py", files, analysis),
        {"path": "pkg/util.py", "disposition": "not_applicable"},
    ]
    na = case("na", [artifact(na_proof, commit=COMMIT)])
    assert na["admitted"] is False and na["outcome"] == "hold"
    assert any("N/A" in r and "justification" in r for r in na["reasons"])

    # -- missing source binding: a genuine proof with no source -> refusal.
    no_src = case("no-src", [genuine], source=None)
    assert no_src["admitted"] is False and no_src["outcome"] == "hold"
    assert any("no source resolver" in r for r in no_src["reasons"])

