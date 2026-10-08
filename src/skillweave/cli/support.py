"""The published CLI/ops **support table** and **shared state interpretation**
(SW-1510-CLI criterion 2).

Criterion 2 asks that the surfaces an operator can reach -- ``entry`` (which
*decides*), ``run``/``start`` (which *execute*), the ``dispatch`` legacy status
surface, and the MCP operations -- carry **an explicit versioned support table
and shared state interpretation** rather than each re-deriving its own meaning.

This module is that single table. It is versioned by
:data:`SUPPORT_TABLE_ID` (``skillweave.support/1``) and
:data:`SHARED_STATE_INTERPRETATION_ID` (``skillweave.run-state/1``). The state
section is *shared* in the strict sense: it does not copy the vocabulary, it
projects it from the one authority each part already lives in --

* canonical states and the terminal subset come from
  :class:`skillweave.runtime.store.RunStateModel`;
* the accepted legacy uppercase aliases come from
  :data:`skillweave.runtime.store.LEGACY_STATE_ALIASES`;
* the lifecycle projection (``running``/``succeeded``/``failed``/``killed``/
  ``unknown`` -> lane state) comes from
  :data:`skillweave.runlifecycle.store._LIFECYCLE_TO_LANE`;
* the persistent stop reasons come from
  :data:`skillweave.runtime.store.STOP_REASONS`.

so the table can never drift from those sources -- adding a state, an alias, a
lifecycle value or a stop reason there is reflected here without editing a
second copy. Nothing in this module invents a value: an unknown raw state is
reported as *unrecognized* (``recognized: false``), never coerced to a default.

Run-snapshot boundary (task ``boundaries[0]``): the versioned public contract
this surface speaks is ``run-snapshot-v1``. It is pinned by id in
:data:`RUN_SNAPSHOT_SCHEMA_ID`; the schema bytes themselves are owned by the
accepted SDK (frozen before implementation), so this module references the
version rather than re-declaring the schema. ``contracts/catalog.json`` is the
declared catalog home but is absent from this checkout; the id below is the
literal the task boundary names, kept here so the publish surface is explicit.

Exit semantics
--------------
The support surface is read-only and additive to the entry surface's codes::

    0  ok             the table (or one state interpretation) was emitted
    2  invalid input  unknown sub-option / argparse usage error
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

#: The versioned identity of this support table.
SUPPORT_TABLE_ID = "skillweave.support/1"
#: The versioned identity of the shared run-state interpretation.
SHARED_STATE_INTERPRETATION_ID = "skillweave.run-state/1"
#: The versioned public data contract this surface speaks (task boundary).
RUN_SNAPSHOT_SCHEMA_ID = "run-snapshot-v1"

#: A surface's role in the pipeline. ``decide`` names an intent; ``execute``
#: performs bounded work; ``operate`` inspects or stops a run; ``legacy`` is a
#: surface kept available only through an explicit compatibility path.
ROLE_DECIDE = "decide"
ROLE_EXECUTE = "execute"
ROLE_OPERATE = "operate"
ROLE_LEGACY = "legacy"

#: Support status. ``supported`` is a first-class surface; ``legacy`` is kept
#: available but is not the recommended path.
STATUS_SUPPORTED = "supported"
STATUS_LEGACY = "legacy"


@dataclass(frozen=True)
class SurfaceSupport:
    """One row of the support table: a nameable surface and its role/status."""

    name: str
    role: str
    status: str
    entry_point: str
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "status": self.status,
            "entry_point": self.entry_point,
            "description": self.description,
        }


#: The explicit support table. ``entry`` decides; ``run``/``start`` execute;
#: the async lifecycle + MCP operations operate; ``dispatch`` is the legacy
#: compatibility surface (its status remains readable through the same state
#: interpretation here).
SURFACES: tuple[SurfaceSupport, ...] = (
    SurfaceSupport(
        "entry", ROLE_DECIDE, STATUS_SUPPORTED,
        "skillweave.cli.main:run_entry",
        "Resolve a typed intent through the shared EntryService contract.",
    ),
    SurfaceSupport(
        "run", ROLE_EXECUTE, STATUS_SUPPORTED,
        "skillweave.cli.run:main",
        "Execute a single authoritative run command.",
    ),
    SurfaceSupport(
        "start", ROLE_EXECUTE, STATUS_SUPPORTED,
        "skillweave.runlifecycle.cli:main_start",
        "Start a command, optionally as a durable async run.",
    ),
    SurfaceSupport(
        "inspect", ROLE_OPERATE, STATUS_SUPPORTED,
        "skillweave.runlifecycle.cli:main_inspect",
        "Inspect a run's state and events (snapshot or follow).",
    ),
    SurfaceSupport(
        "kill", ROLE_OPERATE, STATUS_SUPPORTED,
        "skillweave.runlifecycle.cli:main_kill",
        "Kill a run by its ID and recorded PID.",
    ),
    SurfaceSupport(
        "cancel", ROLE_OPERATE, STATUS_SUPPORTED,
        "skillweave.runlifecycle.cli:main_cancel",
        "Cancel an owned run after verifying its process identity.",
    ),
    SurfaceSupport(
        "resume", ROLE_OPERATE, STATUS_SUPPORTED,
        "skillweave.runlifecycle.cli:main_resume",
        "Resume a run by launching a new identified attempt.",
    ),
    SurfaceSupport(
        "mcp", ROLE_OPERATE, STATUS_SUPPORTED,
        "skillweave.mcp.server",
        "MCP tools (create/status/cancel/resume/get_evidence/review) reading "
        "the same run snapshot as the CLI.",
    ),
    SurfaceSupport(
        "dispatch", ROLE_LEGACY, STATUS_LEGACY,
        "skillweave.dispatch.cli:main",
        "Legacy one-wave dispatch surface; status stays readable through the "
        "shared state interpretation.",
    ),
)

#: The router subcommands that are mounted as first-class surfaces.
ROUTER_SURFACES: tuple[str, ...] = (
    "entry", "dispatch", "run", "rework", "planning-sync", "onboard",
    "assess", "start", "inspect", "kill", "cancel", "resume", "support",
)


def lookup(name: str) -> Optional[SurfaceSupport]:
    """Return the support row for *name*, or ``None`` if unnamed."""
    for surface in SURFACES:
        if surface.name == name:
            return surface
    return None


def surface_help(name: str) -> Optional[str]:
    """Return the operator-facing description for *name*, or ``None``."""
    surface = lookup(name)
    return None if surface is None else surface.description


# ── Shared state interpretation ─────────────────────────────────────────────
#
# Every accessor below reads the *live* vocabulary from its one authority, so
# the interpretation is shared by construction rather than copied.


def canonical_states() -> tuple[str, ...]:
    """The canonical lowercase state vocabulary (RunStateModel's values)."""
    from skillweave.runtime.store import RunStateModel

    return tuple(member.value for member in RunStateModel)


def terminal_states() -> frozenset[str]:
    """The canonical terminal subset, projected from RunStateModel."""
    from skillweave.runtime.store import RunStateModel

    return RunStateModel.terminal_values()


def legacy_aliases() -> Mapping[str, str]:
    """The accepted legacy uppercase aliases -> canonical value."""
    from skillweave.runtime.store import LEGACY_STATE_ALIASES

    return dict(LEGACY_STATE_ALIASES)


def stop_reasons() -> tuple[str, ...]:
    """The persistent stop reasons recorded in ``metadata['stop_reason']``."""
    from skillweave.runtime.store import STOP_REASONS

    return tuple(STOP_REASONS)


def lifecycle_projection() -> Mapping[str, str]:
    """The async lifecycle state -> lane-state projection (one authority)."""
    from skillweave.runlifecycle.store import _LIFECYCLE_TO_LANE

    return dict(_LIFECYCLE_TO_LANE)


@dataclass(frozen=True)
class StateInterpretation:
    """The shared interpretation of one raw state string.

    ``recognized`` is false when the raw value matches no canonical value, no
    legacy alias and no lifecycle state -- an unknown value is reported as
    unknown, never defaulted.
    """

    raw: str
    canonical: Optional[str]
    is_legacy_alias: bool
    is_terminal: bool
    lifecycle: Optional[str]
    recognized: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "canonical": self.canonical,
            "is_legacy_alias": self.is_legacy_alias,
            "is_terminal": self.is_terminal,
            "lifecycle": self.lifecycle,
            "recognized": self.recognized,
        }


def interpret_state(raw: str) -> StateInterpretation:
    """Interpret *raw* through the one shared vocabulary.

    Resolution order: a canonical value first; else a legacy uppercase alias
    (mapped to its canonical value and flagged); else a lifecycle state (mapped
    to its lane projection). A value that matches none of these is returned
    with ``recognized=False`` and ``canonical=None`` -- no fabricated value.
    """
    text = raw if isinstance(raw, str) else ("" if raw is None else str(raw))
    canonical_values = set(canonical_states())
    aliases = legacy_aliases()
    lifecycle_map = lifecycle_projection()

    if text in canonical_values:
        return StateInterpretation(
            raw=text,
            canonical=text,
            is_legacy_alias=False,
            is_terminal=text in terminal_states(),
            lifecycle=text if text in lifecycle_map else None,
            recognized=True,
        )
    if text in aliases:
        mapped = aliases[text]
        return StateInterpretation(
            raw=text,
            canonical=mapped,
            is_legacy_alias=True,
            is_terminal=mapped in terminal_states(),
            lifecycle=mapped if mapped in lifecycle_map else None,
            recognized=True,
        )
    if text in lifecycle_map:
        return StateInterpretation(
            raw=text,
            canonical=None,
            is_legacy_alias=False,
            is_terminal=False,
            lifecycle=lifecycle_map[text],
            recognized=True,
        )
    return StateInterpretation(
        raw=text,
        canonical=None,
        is_legacy_alias=False,
        is_terminal=False,
        lifecycle=None,
        recognized=False,
    )


def state_interpretation_payload() -> dict[str, Any]:
    """The versioned, serializable shared state interpretation."""
    return {
        "schema_id": SHARED_STATE_INTERPRETATION_ID,
        "canonical": list(canonical_states()),
        "terminal": sorted(terminal_states()),
        "legacy_aliases": legacy_aliases(),
        "lifecycle_projection": lifecycle_projection(),
        "stop_reasons": list(stop_reasons()),
    }


def support_table() -> dict[str, Any]:
    """The versioned support table, with the shared state interpretation."""
    return {
        "schema_id": SUPPORT_TABLE_ID,
        "state_interpretation_id": SHARED_STATE_INTERPRETATION_ID,
        "run_snapshot_schema_id": RUN_SNAPSHOT_SCHEMA_ID,
        "surfaces": [surface.to_dict() for surface in SURFACES],
        "state": state_interpretation_payload(),
    }


# ── CLI surface ─────────────────────────────────────────────────────────────


def build_parser(prog: str = "skillweave support") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Print the versioned surface support table and the shared run-state "
            "interpretation. Read-only."
        ),
    )
    parser.add_argument(
        "--state",
        metavar="RAW",
        default=None,
        help="Interpret one raw state value instead of printing the table.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Force the JSON envelope."
    )
    return parser


def _render_human(table: Mapping[str, Any]) -> str:
    lines = [
        f"support table : {table['schema_id']}",
        f"state schema  : {table['state_interpretation_id']}",
        f"snapshot      : {table['run_snapshot_schema_id']}",
        "",
        "surfaces:",
    ]
    for row in table["surfaces"]:
        lines.append(
            f"  {row['name']:<14} {row['role']:<8} {row['status']:<10} "
            f"{row['entry_point']}"
        )
    state = table["state"]
    lines += [
        "",
        f"canonical states ({len(state['canonical'])}): "
        + ", ".join(state["canonical"]),
        "terminal        : " + ", ".join(state["terminal"]),
        "stop reasons    : " + ", ".join(state["stop_reasons"]),
        "lifecycle -> lane: "
        + ", ".join(f"{k}->{v}" for k, v in state["lifecycle_projection"].items()),
    ]
    return "\n".join(lines) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
        return 0 if code == 0 else 2

    if args.state is not None:
        payload: dict[str, Any] = {
            "schema_id": SHARED_STATE_INTERPRETATION_ID,
            "interpretation": interpret_state(args.state).to_dict(),
        }
    else:
        payload = support_table()

    if args.json:
        sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    elif args.state is not None:
        sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    else:
        sys.stdout.write(_render_human(payload))
    return 0


__all__ = [
    "ROLE_DECIDE",
    "ROLE_EXECUTE",
    "ROLE_LEGACY",
    "ROLE_OPERATE",
    "ROUTER_SURFACES",
    "RUN_SNAPSHOT_SCHEMA_ID",
    "SHARED_STATE_INTERPRETATION_ID",
    "STATUS_LEGACY",
    "STATUS_SUPPORTED",
    "SUPPORT_TABLE_ID",
    "SURFACES",
    "StateInterpretation",
    "SurfaceSupport",
    "build_parser",
    "canonical_states",
    "interpret_state",
    "legacy_aliases",
    "lifecycle_projection",
    "lookup",
    "main",
    "state_interpretation_payload",
    "stop_reasons",
    "support_table",
    "surface_help",
    "terminal_states",
]
