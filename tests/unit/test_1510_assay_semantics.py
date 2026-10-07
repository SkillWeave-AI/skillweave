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
verdicts = block["artifact_evidence"].get("verdicts") or []
print(json.dumps({
    "outcome": block["outcome"],
    "admitted": block["artifact_evidence"]["admitted"],
    "reasons": block["artifact_evidence"]["reasons"],
    "gate_state": block["completion_gate_state"],
    "schema_authority": verdicts[0].get("schema_authority") if verdicts else None,
}))
'''


def _venv_site(venv_dir: Path) -> Path:
    matches = sorted(venv_dir.glob("lib/python*/site-packages"))
    assert matches, f"no venv site-packages under {venv_dir}"
    return matches[0]


def _clean_env() -> dict:
    """A subprocess environment with ``PYTHONPATH`` removed.

    The lane runs the tests with ``PYTHONPATH`` pointing at the accepted-SDK
    site-packages, which *also* carries a ``skillweave`` install. An inherited
    ``PYTHONPATH`` makes ``pip`` report "already installed with the same
    version" and skip the freshly-built wheel, leaving the new venv without
    ``skillweave``. Build/install/verify subprocesses therefore run with
    ``PYTHONPATH`` cleared, proving the *installed* wheel is exercised.
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

    env = _clean_env()  # prove the *installed* package (and its bundled SDK) is used
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
    # Validated against the pinned SDK schema, never the structural fallback.
    assert good["schema_authority"] == "sdk"

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

    # -- false bounded N/A: a real executable file excluded as N/A with a
    #    plausible narrative justification but a bound that does not cover it
    #    (README.md on pkg/module.py) -> refusal. A non-empty justification
    #    string is not an independent justification; the bound must cover the
    #    excluded path. This is the reviewed defect, refused end-to-end by the
    #    *installed* native consumer under SDK schema authority.
    false_bound_proof = {
        "version": VERSION,
        "analyzed": True,
        "coverage": [coverage_entry("pkg/util.py", files, analysis)],
        "exclusions": [
            {"path": "pkg/module.py",
             "justification": "Narrative documentation only; no executable code",
             "bounded": "README.md"},
            {"path": "README.md", "justification": "documentation only",
             "bounded": "narrative paths (*.md)"},
        ],
    }
    false_bound = case("false-bound", [artifact(false_bound_proof, commit=COMMIT)])
    assert false_bound["admitted"] is False and false_bound["outcome"] == "hold"
    assert any("does not cover" in r for r in false_bound["reasons"])

    # -- the reviewed counterexample, end to end through the installed native
    #    consumer: ``settings.py`` with real executable bytes (``LIMIT = 7``),
    #    excluded as ``not_applicable`` under a narrative justification and a
    #    bound that *correctly* names its own path. Matching the path does not
    #    establish the reason -- the installed consumer refutes the narrative
    #    claim against the real source bytes and withholds the PASS.
    #    A real, populated tree gaining one executable file.
    files["settings.py"] = b"LIMIT = 7\n"
    (src / "settings.py").write_bytes(files["settings.py"])
    analysis["settings.py"] = b"analysis of settings.py"
    (ana / "settings.py").write_bytes(analysis["settings.py"])
    analysis_text["settings.py"] = analysis["settings.py"].decode()
    contradicted_proof = {
        "version": VERSION,
        "analyzed": True,
        "coverage": [coverage_entry("settings.py", files, analysis)],
        "exclusions": [
            {"path": "settings.py",
             "justification": "Narrative documentation only; no executable code",
             "bounded": "settings.py"},
        ],
    }
    contradicted = case("contradicted", [artifact(contradicted_proof, commit=COMMIT)])
    assert contradicted["admitted"] is False and contradicted["outcome"] == "hold"
    assert any(
        "contradicted exclusion reason" in r for r in contradicted["reasons"]
    ), contradicted["reasons"]

    # -- positive counterpart from the same counterexample: a *genuine* code
    #    analysis of the very same executable file is admitted by the same
    #    installed consumer under SDK authority, so the fix refuses the
    #    contradicted exclusion without refusing legitimate analysis.
    code_proof = {
        "version": VERSION,
        "analyzed": True,
        "coverage": [
            coverage_entry("pkg/module.py", files, analysis),
            coverage_entry("pkg/util.py", files, analysis),
            coverage_entry("settings.py", files, analysis),
        ],
        "exclusions": [
            {"path": "README.md",
             "justification": "documentation only, not executable source",
             "bounded": "narrative paths (*.md)"},
        ],
    }
    code_positive = case("code-positive", [artifact(code_proof, commit=COMMIT)])
    assert code_positive["admitted"] is True and code_positive["outcome"] == "pass"
    assert code_positive["reasons"] == []
    assert code_positive["schema_authority"] == "sdk"

    # -- positive: a legitimate bounded exclusion of a *non-code* file is
    #    admitted (the fix is not a blanket refusal of all exclusions). A real
    #    data file is excluded with a source-bound reason and a bound naming its
    #    own path; it is independently proved non-executable by its real bytes
    #    (a non-code name and a data body), while a code file in the same tree
    #    is genuinely analyzed.
    data_src = tmp_path / "data-src"
    data_files = {
        "config/settings.json": b'{"limit": 7}\n',
        "pkg/util.py": b"VALUE = 'util'\n",
    }
    write_source_tree(data_src, data_files)
    data_analysis = write_analysis_tree(tmp_path / "data-analysis", data_files)
    legit_proof = {
        "version": VERSION,
        "analyzed": True,
        "coverage": [coverage_entry("pkg/util.py", data_files, data_analysis)],
        "exclusions": [
            {"path": "config/settings.json",
             "justification": "documentation only, not executable source",
             "bounded": "config/settings.json"},
        ],
    }
    legit = _run_installed(venv_dir, tmp_path, "legit-bound", {
        "artifacts": [artifact(legit_proof, commit=COMMIT)],
        "analysis": {k: v.decode() for k, v in data_analysis.items()},
        "source_root": str(data_src),
        "commit": COMMIT,
    })
    assert legit["admitted"] is True and legit["outcome"] == "pass"
    assert legit["reasons"] == []

    # -- an *unsupported* exclusion authority: a generated/vendored claim with
    #    no independent provenance is refused even when its bound covers the
    #    path, because the reason itself is not independently established.
    generated_proof = {
        "version": VERSION,
        "analyzed": True,
        "coverage": [coverage_entry("pkg/util.py", files, analysis)],
        "exclusions": [
            {"path": "pkg/module.py",
             "justification": "generated constant table; vendored",
             "bounded": "pkg/*.py"},
            {"path": "README.md", "justification": "documentation only",
             "bounded": "narrative paths (*.md)"},
        ],
    }
    generated = case("generated", [artifact(generated_proof, commit=COMMIT)])
    assert generated["admitted"] is False and generated["outcome"] == "hold"
    assert any("unsupported exclusion authority" in r
               for r in generated["reasons"]), generated["reasons"]

    # -- a *generic* justification that is not source-bound is refused: a
    #    non-empty string is not an independent reason, and the real source
    #    (an executable ``.py`` file) refutes the exclusion regardless of the
    #    wording.
    generic_proof = {
        "version": VERSION,
        "analyzed": True,
        "coverage": [coverage_entry("pkg/util.py", files, analysis)],
        "exclusions": [
            {"path": "pkg/module.py",
             "justification": "not needed for this analysis",
             "bounded": "pkg/module.py"},
            {"path": "README.md", "justification": "documentation only",
             "bounded": "narrative paths (*.md)"},
        ],
    }
    generic = case("generic", [artifact(generic_proof, commit=COMMIT)])
    assert generic["admitted"] is False and generic["outcome"] == "hold"
    assert any("contradicted exclusion reason" in r
               for r in generic["reasons"]), generic["reasons"]

    # -- positive: the wording-independence the review demanded. A genuinely
    #    narrative OVERVIEW.md excluded with "Project overview text for readers"
    #    -- a phrasing *without* any "documentation" keyword -- is admitted
    #    because the verdict is the file's real bytes, not the justification's
    #    vocabulary. A plain-text licence is admitted under the same rule.
    wording_src = tmp_path / "wording-src"
    wording_files = {
        "pkg/util.py": b"VALUE = 'util'\n",
        "OVERVIEW.md": b"# Overview\n\nWhat the project does, in prose.\n",
        "LICENSE": b"MIT License\n\nPermission is hereby granted, free of charge.\n",
    }
    write_source_tree(wording_src, wording_files)
    wording_analysis = write_analysis_tree(tmp_path / "wording-analysis", wording_files)
    wording_analysis_text = {k: v.decode() for k, v in wording_analysis.items()}
    for name, path, justification in (
        ("wording-overview", "OVERVIEW.md", "Project overview text for readers"),
        ("wording-licence", "LICENSE", "release notes for humans, non-code narrative"),
    ):
        wording_proof = {
            "version": VERSION,
            "analyzed": True,
            "coverage": [coverage_entry("pkg/util.py", wording_files, wording_analysis)],
            "exclusions": [
                {"path": path, "justification": justification, "bounded": path},
            ],
        }
        admitted = _run_installed(venv_dir, tmp_path, name, {
            "artifacts": [artifact(wording_proof, commit=COMMIT)],
            "analysis": wording_analysis_text,
            "source_root": str(wording_src),
            "commit": COMMIT,
        })
        assert admitted["admitted"] is True, (name, admitted["reasons"])
        assert admitted["outcome"] == "pass"
        assert admitted["reasons"] == []

    # -- negative: a doc-suffix file whose *bytes* are an executable script, and
    #    code without a recognised code suffix, are both refuted from the real
    #    source even under a plausible narrative justification.
    bypass_src = tmp_path / "bypass-src"
    bypass_files = {
        "pkg/util.py": b"VALUE = 'util'\n",
        "notes.md": b"#!/bin/sh\nexec rm -rf /\n",
        "settings": b"LIMIT = 7\n",
    }
    write_source_tree(bypass_src, bypass_files)
    bypass_analysis = write_analysis_tree(tmp_path / "bypass-analysis", bypass_files)
    bypass_analysis_text = {k: v.decode() for k, v in bypass_analysis.items()}
    for name, path in (("bypass-doc-script", "notes.md"),
                       ("bypass-nosuffix", "settings")):
        bypass_proof = {
            "version": VERSION,
            "analyzed": True,
            "coverage": [coverage_entry("pkg/util.py", bypass_files, bypass_analysis)],
            "exclusions": [
                {"path": path,
                 "justification": "Narrative documentation only; no executable code",
                 "bounded": path},
            ],
        }
        refused = _run_installed(venv_dir, tmp_path, name, {
            "artifacts": [artifact(bypass_proof, commit=COMMIT)],
            "analysis": bypass_analysis_text,
            "source_root": str(bypass_src),
            "commit": COMMIT,
        })
        assert refused["admitted"] is False, (name, refused["reasons"])
        assert refused["outcome"] == "hold"
        assert any("contradicted exclusion reason" in r
                   for r in refused["reasons"]), (name, refused["reasons"])

    # -- missing source binding: a genuine proof with no source -> refusal.
    no_src = case("no-src", [genuine], source=None)
    assert no_src["admitted"] is False and no_src["outcome"] == "hold"
    assert any("no source resolver" in r for r in no_src["reasons"])

