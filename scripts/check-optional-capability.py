#!/usr/bin/env python3
"""check-optional-capability - report an optional capability's availability.

SkillWeave's core imports even when an optional capability is absent: several
modules degrade instead of raising (``src/skillweave/mcp/server.py`` falls back
to an inert ``FastMCP`` stub when the ``mcp`` SDK is not installed, so the tool
*names* still exist but no protocol server can serve them). Degrading is the
right runtime behaviour; the failure mode it must not create is *silence* - an
operator who installs the base package and later expects MCP hosting has no way
to learn that the capability is missing and no way to learn how to add it.

This script is that missing diagnostic. It answers, for one capability (or all
declared capabilities):

    is the capability actually importable in THIS interpreter?

and, when it is not, prints an actionable install hint targeting the declared
pip extra - so the failure is a one-line fix rather than a puzzle.

Stdlib only; imports nothing but the capability it is asked about. It is safe
to run in a base install with no optionals present.

Usage::

    check-optional-capability.py [capability ...]

Exit 0 = every requested capability is available.
Exit 1 = at least one requested capability is missing.
Exit 2 = usage error (unknown capability name).

Output is a single JSON document on stdout, so a harness or a test can consume
it without scraping prose. Nothing here mutates the environment: the script
probes and reports, it does not install.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from typing import Any, Dict, Mapping

# capability name -> (import module, sentinel attribute, pip extra, why it is
# optional). The sentinel is the public symbol the code imports; checking the
# *module* alone would report a stub shim as available.
CAPABILITIES: Mapping[str, tuple] = {
    "mcp": (
        "mcp.server.fastmcp",
        "FastMCP",
        "mcp",
        "Host the SkillWeave tool surface over the Model Context Protocol. "
        "When absent, src/skillweave/mcp/server.py registers its tools against "
        "an inert stub and cannot serve protocol requests.",
    ),
}


def diagnose(capability: str) -> Dict[str, Any]:
    """Return a JSON-ready report for one declared optional capability."""
    module, sentinel, extra, why = CAPABILITIES[capability]
    available = False
    detail = ""
    module_path = None
    try:
        loaded = importlib.import_module(module)
    except ImportError as exc:  # capability genuinely absent
        detail = "cannot import %s: %s" % (module, exc)
    else:
        module_path = getattr(loaded, "__file__", None)
        if getattr(loaded, sentinel, None) is None:
            detail = "%s imported but exposes no %r" % (module, sentinel)
        else:
            available = True

    report = {
        "available": available,
        "status": "available" if available else "missing",
        "module": module,
        "sentinel": sentinel,
        "extra": extra,
        "module_path": module_path,
        "detail": detail,
        "why_optional": why,
        "install_hint": "" if available else "pip install 'skillweave[%s]'" % extra,
    }
    return report


def main(argv: Any = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check-optional-capability",
        description=(
            "Report whether an optional SkillWeave capability is importable in "
            "this interpreter, with an install hint when it is not."
        ),
    )
    parser.add_argument(
        "capabilities",
        nargs="*",
        default=None,
        metavar="CAPABILITY",
        help="capability name(s) to check; omit to check every declared capability",
    )
    args = parser.parse_args(argv)

    requested = list(args.capabilities) if args.capabilities else sorted(CAPABILITIES)
    unknown = [name for name in requested if name not in CAPABILITIES]
    if unknown:
        print(
            "check-optional-capability: unknown capability %r; declare one of: %s"
            % (unknown[0], ", ".join(sorted(CAPABILITIES))),
            file=sys.stderr,
        )
        return 2

    reports = {name: diagnose(name) for name in requested}
    document = {
        "python": ".".join(str(part) for part in sys.version_info[:3]),
        "executable": sys.executable,
        "capabilities": reports,
        "missing": sorted(name for name, r in reports.items() if not r["available"]),
    }
    print(json.dumps(document, indent=2, sort_keys=True))
    return 0 if not document["missing"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
