"""A packaged, pinned MCP server (SW-COUNCIL-003 criterion 1/2 fixture).

This is a *real installed artifact*, not the in-file fake: it is the package body
of a distribution named ``sw-council-pinned-mcp`` at version ``9.9.9`` with a
matching ``.dist-info`` directory and an entry-point console script. A resolver
that verifies a registration against *installed distribution metadata + a real
console script* can therefore resolve it with no network and no floating
install -- exactly the "verified installed binary" rung of criterion 2.
"""

__version__ = "9.9.9"
