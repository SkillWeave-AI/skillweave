#!/usr/bin/env python3
"""Source-tree wrapper for the packaged optional-capability diagnostic.

The real implementation lives inside the distribution at
``src/skillweave/scripts/check_optional_capability.py`` so that the documented

    python -m skillweave.scripts.check_optional_capability

invocation resolves from a fresh wheel. This file only exists for a source
checkout that is run *without* installing: it puts ``src`` on the path and
delegates, so there is exactly one implementation and the two can never drift.

Exit codes and output are identical to the packaged module:
    0 = every requested capability is available
    1 = at least one requested capability is missing (actionable hint printed)
    2 = usage error (unknown capability name)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "src"))

from skillweave.scripts.check_optional_capability import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
