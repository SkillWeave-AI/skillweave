"""Grounding manifest data model — typed data classes for repository baseline."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional


class GapKind(str, Enum):
    """Typed blocking gap kinds."""

    MISSING_TARGET = "missing_target"
    MISSING_SYMBOL = "missing_symbol"
    MISSING_NEIGHBOR = "missing_neighbor"
    CHANGED_REVISION = "changed_revision"
    INACCESSIBLE_NEIGHBOR = "inaccessible_neighbor"


@dataclass(frozen=True)
class GroundingGap:
    """A typed blocking gap that prevents grounding finalization."""

    kind: GapKind
    description: str
    detail: str = ""


@dataclass(frozen=True)
class RevisionPin:
    """A pinned SHA revision for a repository."""

    target: str
    full_sha: str


@dataclass(frozen=True)
class LanguageScan:
    """Language scan result — paths with exact repository-relative citations."""

    language: str
    files: List[str]  # repository-relative paths


@dataclass(frozen=True)
class RequestedSymbol:
    """A requested symbol with its exact repository-relative citation."""

    symbol: str
    file_path: str  # repository-relative path
    line: int


@dataclass(frozen=True)
class NeighborRepo:
    """A neighbor repository referenced by an explicit link, manifest, shared
    namespace, or planning-to-product convention, with evidence."""

    url: str
    evidence: str  # short description of the evidence source
    source_file: str = ""  # repository-relative path to evidence


@dataclass(frozen=True)
class GroundingManifest:
    """Versioned grounding manifest for a repository baseline.

    All fields are frozen and digest-bearing.  Any mutation produces a new
    manifest with a different ``digest``.
    """

    schema_version: str = "1.0"

    # Target identification
    target: str = ""
    full_revision: str = ""

    # Scanner identity
    requesting_model: str = ""
    resolving_model: str = ""
    answering_model: str = ""

    # Scans
    languages: List[LanguageScan] = field(default_factory=list)
    manifests: List[str] = field(default_factory=list)
    roots: List[str] = field(default_factory=list)
    commands: List[str] = field(default_factory=list)

    # Symbol references
    requested_symbols: List[RequestedSymbol] = field(default_factory=list)

    # Neighbor references (bounded, evidence-backed)
    neighbor_repos: List[NeighborRepo] = field(default_factory=list)

    # Blocking gaps
    gaps: List[GroundingGap] = field(default_factory=list)

    # Content digest over all substantive fields
    digest: str = ""

    def compute_digest(self) -> str:
        """Return a deterministic SHA-256 digest over all substantive fields."""
        h = hashlib.sha256()
        h.update(f"schema_version={self.schema_version}\n".encode())
        h.update(f"target={self.target}\n".encode())
        h.update(f"full_revision={self.full_revision}\n".encode())
        h.update(
            f"requesting_model={self.requesting_model}\n".encode()
        )
        h.update(
            f"resolving_model={self.resolving_model}\n".encode()
        )
        h.update(
            f"answering_model={self.answering_model}\n".encode()
        )

        for lang in sorted(self.languages, key=lambda x: x.language):
            h.update(f"language={lang.language}\n".encode())
            for f in sorted(lang.files):
                h.update(f"  {f}\n".encode())

        for m in sorted(self.manifests):
            h.update(f"manifest={m}\n".encode())

        for r in sorted(self.roots):
            h.update(f"root={r}\n".encode())

        for c in sorted(self.commands):
            h.update(f"command={c}\n".encode())

        for s in sorted(
            self.requested_symbols, key=lambda x: (x.symbol, x.file_path)
        ):
            h.update(
                f"symbol={s.symbol} path={s.file_path} line={s.line}\n".encode()
            )

        for n in sorted(self.neighbor_repos, key=lambda x: x.url):
            h.update(f"neighbor={n.url} evidence={n.evidence}\n".encode())

        for g in sorted(self.gaps, key=lambda x: x.kind.value):
            h.update(f"gap={g.kind.value} desc={g.description}\n".encode())

        return h.hexdigest()

    def with_digest(self) -> GroundingManifest:
        """Return a new manifest with the digest field set."""
        d = self.compute_digest()
        return GroundingManifest(
            schema_version=self.schema_version,
            target=self.target,
            full_revision=self.full_revision,
            requesting_model=self.requesting_model,
            resolving_model=self.resolving_model,
            answering_model=self.answering_model,
            languages=self.languages,
            manifests=self.manifests,
            roots=self.roots,
            commands=self.commands,
            requested_symbols=self.requested_symbols,
            neighbor_repos=self.neighbor_repos,
            gaps=self.gaps,
            digest=d,
        )

    @property
    def has_blocking_gaps(self) -> bool:
        """True when at least one blocking gap exists."""
        return len(self.gaps) > 0

    def to_dict(self) -> Dict:
        """Serialise to a JSON-compatible dict."""
        return {
            "schema_version": self.schema_version,
            "target": self.target,
            "full_revision": self.full_revision,
            "requesting_model": self.requesting_model,
            "resolving_model": self.resolving_model,
            "answering_model": self.answering_model,
            "languages": [
                {"language": l.language, "files": sorted(l.files)}
                for l in sorted(self.languages, key=lambda x: x.language)
            ],
            "manifests": sorted(self.manifests),
            "roots": sorted(self.roots),
            "commands": sorted(self.commands),
            "requested_symbols": [
                {"symbol": s.symbol, "file_path": s.file_path, "line": s.line}
                for s in sorted(
                    self.requested_symbols,
                    key=lambda x: (x.symbol, x.file_path),
                )
            ],
            "neighbor_repos": [
                {"url": n.url, "evidence": n.evidence, "source_file": n.source_file}
                for n in sorted(self.neighbor_repos, key=lambda x: x.url)
            ],
            "gaps": [
                {"kind": g.kind.value, "description": g.description, "detail": g.detail}
                for g in sorted(self.gaps, key=lambda x: x.kind.value)
            ],
            "digest": self.digest,
        }
