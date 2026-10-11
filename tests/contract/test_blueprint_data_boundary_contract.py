"""Contract tests for the Blueprint PRD data-boundary contract
(SW-159-BP-CONTRACT-001).

A PRD task that crosses an architectural boundary must define an exact
versioned data contract for that crossing. These tests pin the four failure
directions the contract must refuse, and the one compatibility direction it
must preserve:

* an **unknown boundary kind** is refused, never classified by default;
* a **required contract field that is undefined** (absent or blank) is refused
  with a task-specific diagnostic naming the field;
* a **prose-only boundary** — a contract that is a bare string, or absent — is
  refused, because a described crossing is not a defined interface;
* the **schema** carries the same contract, so an unmodified new field or a
  missing required key fails schema validation too;
* a task **with no data-boundary change** declares no ``boundaries`` and
  validates exactly as before.

The module consumes the integrated ``WorkContract`` subject vocabulary
(SW-159-WORK-001) by parity; ``TestIntegratedWorkContractVocabulary`` proves the
two tuples are identical, so a boundary can never name a subject kind the work
contract does not know.
"""

import json
from pathlib import Path

import jsonschema
import pytest

from skillweave.blueprint import data_boundary_contract as dbc
from skillweave.dispatch.work_contract import SUBJECT_KINDS as DISPATCH_SUBJECT_KINDS

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCHEMA = _REPO_ROOT / "skills" / "skillweave-blueprint" / "assets" / "prd.schema.json"
_FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "prd-schema"


def _load_schema():
    return json.loads(_SCHEMA.read_text())


def _validator():
    return jsonschema.Draft202012Validator(_load_schema())


def _errors(doc):
    return list(_validator().iter_errors(doc))


def _fixture(name):
    return json.loads((_FIXTURE_DIR / name).read_text())


def _task(**overrides):
    task = {
        "id": "BOUND-001",
        "title": "Cross a boundary",
        "description": "A task that crosses one architectural boundary.",
        "acceptanceCriteria": ["The crossing is contracted"],
        "priority": "high",
        "points": 3,
        "dependsOn": [],
        "type": "feature",
        "passes": False,
    }
    task.update(overrides)
    return task


def _contract(**overrides):
    contract = {
        "artifact": "session record",
        "versioning": "schema v2; v1 readable",
        "producer": "auth service",
        "consumer": "request middleware",
        "compatibility": "backward compatible",
    }
    contract.update(overrides)
    return contract


def _boundary(kind="storage", contract=None, **overrides):
    boundary = {"kind": kind, "data_contract": _contract() if contract is None else contract}
    boundary.update(overrides)
    return boundary


class TestBoundaryClassification:
    """The five architectural boundaries are contract-requiring; nothing else."""

    def test_all_five_kinds_are_contract_requiring(self):
        for kind in ("storage", "process", "adapter", "telemetry", "public-api"):
            assert dbc.is_contract_requiring(kind) is True, kind
            assert kind in dbc.BOUNDARY_KINDS

    def test_public_api_alias_is_normalised(self):
        assert dbc.normalize_boundary_kind("public_api") == "public-api"
        assert dbc.normalize_boundary_kind("public api") == "public-api"

    def test_known_kind_task_is_contract_requiring(self):
        task = _task(boundaries=[_boundary("storage")])
        assert dbc.classify_task_boundary(task) == dbc.CONTRACT_REQUIRING

    def test_unknown_boundary_kind_is_refused(self):
        task = _task(boundaries=[_boundary("database")])
        with pytest.raises(dbc.UnknownBoundaryKindError) as exc:
            dbc.classify_task_boundary(task)
        assert exc.value.task_id == "BOUND-001"
        assert "unknown boundary kind" in str(exc.value)
        assert exc.value.field == "boundaries[0].kind"

    def test_blank_boundary_kind_is_refused(self):
        task = _task(boundaries=[_boundary("")])
        with pytest.raises(dbc.UnknownBoundaryKindError):
            dbc.classify_task_boundary(task)


class TestRequiredContractFields:
    """Every one of the five contract fields must be defined, per task and field."""

    @pytest.mark.parametrize(
        "missing", ["artifact", "versioning", "producer", "consumer", "compatibility"]
    )
    def test_absent_required_field_is_refused(self, missing):
        contract = _contract()
        del contract[missing]
        task = _task(boundaries=[_boundary("storage", contract=contract)])
        with pytest.raises(dbc.UndefinedContractFieldError) as exc:
            dbc.classify_task_boundary(task)
        assert exc.value.task_id == "BOUND-001"
        assert exc.value.field == f"boundaries.data_contract.{missing}"
        assert missing in str(exc.value)

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_required_field_is_refused(self, blank):
        task = _task(boundaries=[_boundary("telemetry", contract=_contract(producer=blank))])
        with pytest.raises(dbc.UndefinedContractFieldError) as exc:
            dbc.classify_task_boundary(task)
        assert exc.value.field == "boundaries.data_contract.producer"

    def test_unknown_contract_field_is_refused(self):
        task = _task(
            boundaries=[_boundary("adapter", contract=_contract(owner="nobody"))]
        )
        with pytest.raises(dbc.InvalidDataContractError) as exc:
            dbc.classify_task_boundary(task)
        assert exc.value.field == "boundaries.data_contract.owner"
        assert "undefined field" in str(exc.value)

    def test_required_field_list_is_exactly_five(self):
        assert dbc.REQUIRED_DATA_CONTRACT_FIELDS == (
            "artifact",
            "versioning",
            "producer",
            "consumer",
            "compatibility",
        )


class TestProseOnlyRefusal:
    """A prose-only crossing is a description, not a contract."""

    def test_bare_string_contract_is_refused(self):
        task = _task(boundaries=[_boundary("storage", contract="the object store")])
        with pytest.raises(dbc.ProseOnlyBoundaryError) as exc:
            dbc.classify_task_boundary(task)
        assert exc.value.task_id == "BOUND-001"
        assert "prose" in str(exc.value)
        assert "artifact" in str(exc.value)

    def test_absent_contract_is_refused(self):
        task = _task(boundaries=[{"kind": "process"}])
        with pytest.raises(dbc.ProseOnlyBoundaryError) as exc:
            dbc.classify_task_boundary(task)
        assert exc.value.task_id == "BOUND-001"
        assert exc.value.field == "boundaries[0].data_contract"

    def test_null_contract_is_refused(self):
        task = _task(boundaries=[{"kind": "process", "data_contract": None}])
        with pytest.raises(dbc.ProseOnlyBoundaryError):
            dbc.classify_task_boundary(task)

    def test_diagnostic_names_the_task_and_the_crossing(self):
        task = _task(id="DATA-042", boundaries=[_boundary("public-api", contract="the docs")])
        with pytest.raises(dbc.ProseOnlyBoundaryError) as exc:
            dbc.classify_task_boundary(task)
        assert "DATA-042" in str(exc.value)
        assert "public-api" in str(exc.value)


class TestFixtureSchemaValidation:
    """The schema and the module agree on positive and negative fixtures."""

    def test_no_boundary_fixture_validates_and_classifies_clean(self):
        doc = _fixture("task-no-boundary.json")
        assert _errors(doc) == []
        assert dbc.validate_prd_data_boundaries(doc) == {"COMP-001": dbc.NO_DATA_BOUNDARY}

    def test_exact_contract_fixture_validates(self):
        doc = _fixture("task-exact-contract.json")
        assert _errors(doc) == []
        dispositions = dbc.validate_prd_data_boundaries(doc)
        assert dispositions == {
            "BOUND-STORAGE-001": dbc.CONTRACT_REQUIRING,
            "BOUND-MULTI-001": dbc.CONTRACT_REQUIRING,
        }

    def test_prose_only_fixture_is_rejected_by_schema_and_module(self):
        doc = _fixture("task-prose-only-boundary.json")
        assert _errors(doc), "schema must reject a string data_contract"
        with pytest.raises(dbc.ProseOnlyBoundaryError):
            dbc.validate_prd_data_boundaries(doc)

    def test_schema_rejects_missing_required_contract_field(self):
        doc = _fixture("task-exact-contract.json")
        del doc["tasks"][0]["boundaries"][0]["data_contract"]["compatibility"]
        errs = _errors(doc)
        assert len(errs) == 1
        assert "compatibility" in str(errs[0])

    def test_schema_rejects_unknown_boundary_kind(self):
        doc = _fixture("task-exact-contract.json")
        doc["tasks"][0]["boundaries"][0]["kind"] = "database"
        errs = _errors(doc)
        assert len(errs) == 1
        assert list(errs[0].path)[-1] == "kind"

    def test_schema_rejects_unknown_key_inside_a_boundary(self):
        doc = _fixture("task-exact-contract.json")
        doc["tasks"][0]["boundaries"][0]["notes"] = "not allowed"
        errs = _errors(doc)
        assert errs and "notes" in str(errs[0])

    def test_schema_boundary_kinds_match_the_module(self):
        schema = _load_schema()
        boundaries = schema["properties"]["tasks"]["items"]["properties"]["boundaries"]
        enum = boundaries["items"]["properties"]["kind"]["enum"]
        assert sorted(enum) == sorted(dbc.BOUNDARY_KINDS)

    def test_schema_marks_all_five_contract_fields_required(self):
        schema = _load_schema()
        contract = schema["properties"]["tasks"]["items"]["properties"]["boundaries"][
            "items"
        ]["properties"]["data_contract"]
        assert sorted(contract["required"]) == sorted(dbc.REQUIRED_DATA_CONTRACT_FIELDS)


class TestCompatibility:
    """A task with no data-boundary change validates exactly as before."""

    def test_task_without_boundaries_is_no_data_boundary(self):
        assert dbc.classify_task_boundary(_task()) == dbc.NO_DATA_BOUNDARY

    def test_empty_boundaries_is_no_data_boundary(self):
        assert dbc.classify_task_boundary(_task(boundaries=[])) == dbc.NO_DATA_BOUNDARY

    def test_prd_without_tasks_is_valid_and_empty(self):
        assert dbc.validate_prd_data_boundaries({"projectName": "P"}) == {}

    def test_production_prds_still_validate_and_carry_no_boundaries(self):
        for name in ("ops-002-mirror-rollout.json", "forgejo-first.json"):
            doc = _fixture(name)
            assert _errors(doc) == [], name
            dispositions = dbc.validate_prd_data_boundaries(doc)
            assert dispositions, name
            assert set(dispositions.values()) == {dbc.NO_DATA_BOUNDARY}, name

    def test_corrected_build_fixture_still_validates(self):
        doc = _fixture("corrected-build-format.json")
        assert _errors(doc) == []
        assert set(dbc.validate_prd_data_boundaries(doc).values()) == {
            dbc.NO_DATA_BOUNDARY
        }


class TestIntegratedWorkContractVocabulary:
    """The boundary subject vocabulary is identical to the integrated work
    contract's, so a boundary can never name a subject the work contract does
    not know — and this module stays free of a dispatch/runtime import."""

    def test_subject_kinds_match_work_contract(self):
        assert dbc.WORK_CONTRACT_SUBJECT_KINDS == DISPATCH_SUBJECT_KINDS

    def test_schema_subject_kind_enum_matches_work_contract(self):
        schema = _load_schema()
        contract = schema["properties"]["tasks"]["items"]["properties"]["boundaries"][
            "items"
        ]["properties"]["data_contract"]["properties"]["subject_kind"]
        assert sorted(contract["enum"]) == sorted(DISPATCH_SUBJECT_KINDS)

    def test_valid_subject_kind_is_accepted(self):
        task = _task(
            boundaries=[_boundary("storage", contract=_contract(subject_kind="deployment"))]
        )
        assert dbc.classify_task_boundary(task) == dbc.CONTRACT_REQUIRING

    def test_unknown_subject_kind_is_refused(self):
        task = _task(
            boundaries=[_boundary("storage", contract=_contract(subject_kind="vm"))]
        )
        with pytest.raises(dbc.InvalidDataContractError) as exc:
            dbc.classify_task_boundary(task)
        assert exc.value.field == "boundaries.data_contract.subject_kind"
