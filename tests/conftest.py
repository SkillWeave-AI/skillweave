import os
import sys
from pathlib import Path

_src = Path(__file__).resolve().parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

# Deterministic independent-host recipe hook. When SKILLWEAVE_ACCEPTED_SDK is
# set (the accepted ``skillweave_sdk`` package dir, or its parent
# site-packages), put it ahead of any ambient install and drop any already
# resolved ambient module, so *both* the source-tree imports
# (``skillweave.runtime.authority_service``) and the installed-wheel proofs
# validate against the pinned SDK authority rather than ambient 0.2.0. Unset
# (the default) leaves the host's own resolution untouched.
_accepted = os.environ.get("SKILLWEAVE_ACCEPTED_SDK")
if _accepted:
    _p = Path(_accepted)
    _site = _p.parent if _p.name == "skillweave_sdk" else _p
    if str(_site) not in sys.path:
        sys.path.insert(0, str(_site))
    # Export the pin so child processes spawned by tests inherit the same SDK.
    _existing = [e for e in os.environ.get("PYTHONPATH", "").split(os.pathsep)
                 if e and e != str(_site)]
    os.environ["PYTHONPATH"] = os.pathsep.join([str(_site), *_existing])
    for _mod in [m for m in sys.modules
                 if m == "skillweave_sdk" or m.startswith("skillweave_sdk.")]:
        del sys.modules[_mod]
