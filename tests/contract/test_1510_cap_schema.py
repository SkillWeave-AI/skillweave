"""SW-1510-CAP contract: the admission gate consumes the frozen v1 snapshot schema.

The native-tool admission gate (``skillweave.routing.harness_adapter``) does not
invent its own capability wire contract: it consumes the SDK-owned
``capability-snapshot-v1`` schema (SW-1510-CONTRACT), whose ``schema_version`` is
the frozen ``const "1.0.0"``. This contract ties the two together so the gate can
never silently drift to a private version or a private top-level shape:

1. The gate's declared schema version is exactly the frozen schema's ``const``.
2. A canonical snapshot carrying the schema's required top-level keys validates
   against the frozen schema.
3. The gate's fail-closed checks land on the schema's required top-level keys,
   so a snapshot the schema would reject is one the gate refuses.
4. A version the schema's ``const`` forbids is refused by the gate too.

The gate's *admission* semantics (declared vs detected vs run-attested, weak vs
bound evidence, child vs coordinator) live in the SDK schema's open
``capabilities`` object and cannot be expressed by the schema itself; those are
proven in ``tests/unit/test_1510_cap.py``. This file only binds the wire shape.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_src = Path(__file__).resolve().parent.parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

import pytest  # noqa: E402

from skillweave.routing.harness_adapter import (  # noqa: E402
    AdapterRequest,
    CapabilityRequirement,
    CAPABILITY_SNAPSHOT_SCHEMA_VERSION,
    NativeRequirements,
    OpenClaudeAdapter,
    admit_native_plan,
)

_SCHEMA_REL = Path(
    "integrations/skillweave-sdk/src/skillweave_sdk/schemas/execution/"
    "capability-snapshot-v1.schema.json"
)


def _find_schema():
    for parent in Path(__file__).resolve().parents:
        candidate = parent / _SCHEMA_REL
        if candidate.is_file():
            return candidate
    return None


SCHEMA_PATH = _find_schema()
pytestmark = pytest.mark.skipif(
    SCHEMA_PATH is None, reason="frozen capability-snapshot-v1 schema not present"
)


def _canonical_snapshot(**overrides):
    now = datetime.now(timezone.utc)
    snapshot = {
        "schema_version": "1.0.0",
        "snapshot_id": "cap-1510-contract",
        "revision": "1",
        "measured_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "subject": "openclaude",
        "issuer": "fixture-host-collector",
        "limits": {},
        "readiness": "unknown",
        "evidence_level": "run-attested",
        "capabilities": {},
    }
    snapshot.update(overrides)
    return snapshot


def _plan():
    adapter = OpenClaudeAdapter(executable="openclaude")
    return adapter.plan(
        AdapterRequest(
            run_id="cap-contract",
            role="ops",
            requirement=CapabilityRequirement(complexity=0, capabilities=("native-tool",)),
            work=b"/understand",
        )
    )


def _needs():
    return NativeRequirements(
        skill_name="understand",
        skill_digest="a" * 64,
        required_tools=("Agent", "Skill"),
        required_stages=("skill-expanded",),
    )


def test_frozen_schema_is_present_and_frozen():
    schema = json.loads(SCHEMA_PATH.read_text())
    assert schema["properties"]["schema_version"]["const"] == "1.0.0"


def test_the_gate_declares_the_frozen_version_not_a_private_one():
    schema = json.loads(SCHEMA_PATH.read_text())
    assert CAPABILITY_SNAPSHOT_SCHEMA_VERSION == schema["properties"]["schema_version"]["const"]


def test_a_canonical_snapshot_validates_against_the_frozen_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA_PATH.read_text())
    jsonschema.validate(_canonical_snapshot(), schema)
    # Every schema-required key is present in the canonical shape.
    assert set(schema["required"]) <= set(_canonical_snapshot())


@pytest.mark.parametrize("missing", ["measured_at", "expires_at", "subject", "issuer"])
def test_gate_checks_land_on_schema_required_top_level_keys(missing):
    # Each of these is a schema-required top-level key; the gate refuses on the
    # same key, so the two agree on the shape of a valid snapshot.
    schema = json.loads(SCHEMA_PATH.read_text())
    assert missing in schema["required"]
    snapshot = _canonical_snapshot()
    snapshot.pop(missing)
    admission = admit_native_plan(
        _plan(),
        capability_snapshot=snapshot,
        native_requirements=_needs(),
        host_binding={"binary_path": "/nonexistent", "child_worker_config": {}},
    )
    assert admission.admitted is False
    assert admission.asset and admission.asset.startswith(missing)


def test_a_version_the_schema_const_forbids_is_refused_by_the_gate():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA_PATH.read_text())
    snapshot = _canonical_snapshot(schema_version="99.0.0")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(snapshot, schema)
    admission = admit_native_plan(
        _plan(),
        capability_snapshot=snapshot,
        native_requirements=_needs(),
        host_binding={"binary_path": "/nonexistent", "child_worker_config": {}},
    )
    assert admission.admitted is False
    assert admission.asset == "schema_version"
