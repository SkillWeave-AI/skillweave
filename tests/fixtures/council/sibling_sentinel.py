"""A sentinel process that must survive a neighbour's teardown (SW-COUNCIL-003).

Started by the test itself (never by the client under test), it records its own
pid and sleeps. If the client's timeout/cancel teardown reached beyond its own
process group, this process would be killed too; the test asserts it still runs.
"""

import os
import sys
import time

_PATH = sys.argv[1]
with open(_PATH, "w") as handle:
    handle.write(str(os.getpid()))
time.sleep(3600)
