"""Packaged product defaults.

Shipped with the distribution; never read from a substrate path, and never
resolved relative to a caller working directory. See
``skillweave/core/catalogue/__init__.py`` and
``skillweave/design_thinking.py`` for the consumers. The ``schemas/`` subtree
holds the Core runtime/dispatch schemas the installed package loads at runtime
(``skillweave.gates.integration_gate_result`` and its siblings); they ship as
package data so a wheel resolves them from inside the package rather than
reaching for a repository-root ``schemas/`` directory that no longer exists
once installed.
"""

from pathlib import Path

__all__ = ["assets_root", "packaged_schema_path", "SCHEMAS_SUBDIR"]


SCHEMAS_SUBDIR = "schemas"


def assets_root() -> Path:
    """Return the shipped ``skillweave/assets`` root, anchored to this package.

    Anchoring to ``__file__`` means the lookup resolves identically from a
    source checkout and from an installed wheel, regardless of the caller's
    working directory.
    """
    return Path(__file__).resolve().parent


def packaged_schema_path(filename: str) -> Path:
    """Absolute path to a shipped package-data schema ``assets/schemas/<filename>``.

    A missing schema raises :class:`FileNotFoundError` naming the resolved
    absolute path, so a packaging regression surfaces as an explicit,
    path-carrying error rather than silently resolving another checkout or a
    caller-relative directory.
    """
    path = assets_root() / SCHEMAS_SUBDIR / filename
    if not path.is_file():
        raise FileNotFoundError(
            f"packaged schema {filename!r} is not installed at {path}; the "
            "distribution must ship skillweave/assets/schemas/"
        )
    return path
