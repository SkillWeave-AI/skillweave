"""Offline license-scanner availability probe (SW-LIC-001 criterion 1).

Probes, with NO network, for any third-party license scanner already present
in this lane: Python modules, executables on PATH, and npm globals. It prints
literal results and a final ``SCANNER_AVAILABLE=<bool>`` line. An absent
scanner is recorded as an explicit setup hold, never as a fabricated scan.

Run: ``python3 -B tests/packaging/probe_scanners.py``
"""
import importlib.util
import shutil
import subprocess
import sys

MODULES = ["scancode", "licensecheck", "license_expression", "askalono",
           "licensee", "reuse", "pip_licenses", "license_scanner"]
EXECUTABLES = ["scancode", "scancode-toolkit", "askalono", "licensee",
               "reuse", "pip-licenses", "licensecheck", "fossology"]


def probe():
    """Return (found_any, report_lines)."""
    lines = ["python: %s" % sys.version.split()[0], "-- modules --"]
    found_any = False
    for m in MODULES:
        ok = importlib.util.find_spec(m) is not None
        found_any = found_any or ok
        lines.append("  %-18s %s" % (m, "PRESENT" if ok else "absent"))
    lines.append("-- executables --")
    for e in EXECUTABLES:
        p = shutil.which(e)
        found_any = found_any or bool(p)
        lines.append("  %-18s %s" % (e, p or "absent"))
    lines.append("-- npm globals --")
    try:
        npm = shutil.which("npm")
        if npm:
            r = subprocess.run([npm, "ls", "-g", "--depth=0"],
                               capture_output=True, text=True)
            hits = [l for l in r.stdout.splitlines() if "license" in l.lower()]
            lines.append("  " + ("; ".join(hits) if hits
                                 else "no license-related global"))
        else:
            lines.append("  npm absent")
    except Exception as exc:  # pragma: no cover
        lines.append("  npm probe error: %s" % exc)
    return found_any, lines


def main():
    found_any, lines = probe()
    for line in lines:
        print(line)
    print("SCANNER_AVAILABLE=%s" % found_any)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
