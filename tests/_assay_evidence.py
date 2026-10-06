"""Shared, real (never faked) artifact-proof builders for the SW-1510-VERIFY
native Assay semantics tests.

Every builder here writes *real* bytes to a real source/analysis tree and
computes *real* digests, so a positive control is genuinely bound evidence and
a negative control is a genuine refusal -- not a mocked verdict. The builders
are imported by the unit and integration suites so the CLI, MCP, in-process
service and installed-wheel paths all verify against the identical proof.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

#: The artifact-semantics proof version the verifier accepts.
VERSION = "evidence-receipt-v1"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def default_files() -> dict[str, bytes]:
    """A small, populated source tree with one non-narrative README."""
    return {
        "pkg/module.py": b"def real():\n    return 42\n",
        "pkg/util.py": b"VALUE = 'util'\n",
        "README.md": b"# A readme\n",
    }


def write_source_tree(root, files=None) -> dict[str, bytes]:
    files = dict(default_files() if files is None else files)
    for rel, data in files.items():
        target = Path(root) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return files


def write_git_source_tree(root, files=None) -> tuple[dict[str, bytes], str]:
    """Write a real source tree and commit it to a real git repo.

    Returns ``(files, commit)`` where *commit* is the real 40-hex HEAD of the
    fresh repository, so a ``GitSourceResolver`` (the ``git ls-tree``-backed
    enumerator) resolves the same commit the proof graph binds. The enumeration
    is deliberately *not* analysis -- the proof must still bind per-path
    analysis artifacts.
    """
    import subprocess

    files = write_source_tree(root, files)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"}
    for args in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "source"],
    ):
        subprocess.run(args, cwd=str(root), check=True, capture_output=True, text=True, env=env)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(root), check=True,
        capture_output=True, text=True, env=env,
    ).stdout.strip()
    return files, head


def write_analysis_tree(root, files) -> dict[str, bytes]:
    """One distinct real analysis artifact per source file, on disk."""
    analysis = {
        rel: f"analysis of {rel}: {len(data)} bytes".encode()
        for rel, data in files.items()
    }
    for rel, data in analysis.items():
        target = Path(root) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return analysis


def artifact(proof, *, commit, artifact_id: str = "art-1") -> dict:
    """A schema-shaped evidence receipt whose self-digest is real."""
    from skillweave.runtime.verify import artifact_sha256

    art = {
        "artifact_id": artifact_id,
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
    art["sha256"] = artifact_sha256(art)
    return art


def coverage_entry(rel, files, analysis, **extra) -> dict:
    entry = {
        "path": rel,
        "disposition": "analyzed",
        "content_sha256": sha(files[rel]),
        "analysis_digest": sha(analysis[rel]),
    }
    entry.update(extra)
    return entry


def genuine_proof(files, analysis) -> dict:
    """A proof graph that genuinely analyzes every non-narrative source file."""
    return {
        "version": VERSION,
        "analyzed": True,
        "coverage": [
            coverage_entry(rel, files, analysis)
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


def genuine_artifact(files, analysis, *, commit) -> dict:
    return artifact(genuine_proof(files, analysis), commit=commit)


def readme_only_artifact(files, analysis, *, commit) -> dict:
    return artifact(
        {
            "version": VERSION,
            "analyzed": True,
            "coverage": [coverage_entry("README.md", files, analysis)],
        },
        commit=commit,
    )


def reader_for(analysis):
    return lambda path: analysis.get(path)
