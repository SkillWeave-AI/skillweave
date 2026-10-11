"""Grounding module — bounded, deterministic, digest-bearing scans.

Produces a ``GroundingManifest`` that freezes a repository baseline before
Blueprint finalization.  Scans are read-only, bounded, and produce a content
digest so that subsequent readers can detect drift.
"""

from .manifest import (
    GapKind,
    GroundingGap,
    GroundingManifest,
    LanguageScan,
    NeighborRepo,
    RequestedSymbol,
    RevisionPin,
)
from .scanner import GroundingScanner, ScanLimit, ScanResult

__all__ = [
    "GapKind",
    "GroundingGap",
    "GroundingManifest",
    "GroundingScanner",
    "LanguageScan",
    "NeighborRepo",
    "RequestedSymbol",
    "RevisionPin",
    "ScanLimit",
    "ScanResult",
]
