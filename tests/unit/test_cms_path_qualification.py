"""CMS process-qualification contract (SW-1511-CMS-FLOWS).

Every test here fails on the base SHA 8ff4befb (the module and this file do not
exist there). The contract is the frozen declaration of the five initial CMS
paths and the fail-closed ``cms-process-qualification-v1`` record an
independent observer fills in. No test launches a controller, a provider or a
sandbox: the live run is NOT PERFORMED and this file proves only the mechanism
and the named refusals.
"""

from __future__ import annotations

import json
import pytest

from skillweave.blueprint.cms_path_qualification import (
    CMS_PATHS_BY_ID,
    CmsPath,
    CmsPathQualificationError,
    CmsProcessQualification,
    DECLARATION_PRIVATE_FORK,
    DECLARATION_SPECIALIZATION,
    DISPOSITION_NOT_PERFORMED,
    DISPOSITION_PERFORMED,
    GenericPathMutationRefused,
    INITIAL_CMS_PATHS,
    IncompleteQualificationError,
    InvalidQualificationError,
    PATH_CONTENT_SEO,
    PATH_GENERIC,
    PATH_INCIDENT_ROLLBACK,
    PATH_IDS,
    PATH_LANDING_PAGE,
    PATH_RECURRING_MAINTENANCE,
    PrewrittenReceiptRefused,
    PrivateRuntimeForkRefused,
    REQUIRED_OUTCOMES,
    REQUIRED_PERFORMED_PINS,
    REQUIRED_QUALIFICATION_FIELDS,
    SCHEMA_VERSION,
    SPECIALIZED_PATH_IDS,
    SUBJECT_KIND,
    UnknownDeclarationKindError,
    declaration_from_mapping,
)
from skillweave.blueprint.data_boundary_contract import (
    DataContract,
    UndefinedContractFieldError,
    WORK_CONTRACT_SUBJECT_KINDS,
)


def _performed_mapping(**overrides):
    """A well-formed performed record mapping, with overrides for negatives."""
    payload = {
        "artifact": SCHEMA_VERSION,
        "versioning": "v1 draft; frozen at the baseline task",
        "producer": "independent scenario observer",
        "consumer": "release assessor",
        "compatibility": "additive opt-in; unsupported versions fail closed",
        "subject_kind": SUBJECT_KIND,
        "disposition": DISPOSITION_PERFORMED,
        "pins": {
            "controller": "native-controller-1",
            "source_digest": "sha256:" + "a" * 64,
            "artifact_digest": "sha256:" + "b" * 64,
        },
        "readback_verified": True,
        "restore_verified": True,
        "measured_outcomes": list(REQUIRED_OUTCOMES),
    }
    payload.update(overrides)
    return payload


# --- criterion 1: the five paths are declared, in order, on one contract -------


def test_declares_exactly_the_five_initial_paths_in_order():
    assert PATH_IDS == (
        PATH_GENERIC,
        PATH_LANDING_PAGE,
        PATH_CONTENT_SEO,
        PATH_INCIDENT_ROLLBACK,
        PATH_RECURRING_MAINTENANCE,
    )
    assert tuple(p.path_id for p in INITIAL_CMS_PATHS) == PATH_IDS
    assert len(INITIAL_CMS_PATHS) == 5
    assert set(CMS_PATHS_BY_ID) == set(PATH_IDS)


def test_each_declared_path_labels_the_generic_and_landing_paths_verbatim():
    labels = {p.path_id: p.label for p in INITIAL_CMS_PATHS}
    assert labels[PATH_GENERIC] == "Generic content request"
    assert labels[PATH_LANDING_PAGE] == "Landing-page change"
    assert labels[PATH_CONTENT_SEO] == "Content/SEO change"
    assert labels[PATH_INCIDENT_ROLLBACK] == "Incident/rollback"
    assert labels[PATH_RECURRING_MAINTENANCE] == "Recurring maintenance"


def test_every_declared_path_speaks_the_work_contract_subject_vocabulary():
    # The boundary contract and this module must not drift: every declared
    # subject kind is a member of the integrated WorkContract vocabulary.
    for path in INITIAL_CMS_PATHS:
        assert path.subject_kind in WORK_CONTRACT_SUBJECT_KINDS
    assert SUBJECT_KIND in WORK_CONTRACT_SUBJECT_KINDS


def test_incident_path_names_the_incident_subject_kind():
    # The one path whose subject is not content: an incident/rollback is an
    # incident, and the record is honest about that.
    assert CMS_PATHS_BY_ID[PATH_INCIDENT_ROLLBACK].subject_kind == "incident"


# --- criterion 2: specializations, not forks; generic path mutates nothing -----


def test_four_specializations_descend_from_the_generic_path():
    assert SPECIALIZED_PATH_IDS == PATH_IDS[1:]
    assert len(SPECIALIZED_PATH_IDS) == 4
    for path_id in SPECIALIZED_PATH_IDS:
        path = CMS_PATHS_BY_ID[path_id]
        assert path.specialization_of == PATH_GENERIC
        assert path.declaration_kind == DECLARATION_SPECIALIZATION


def test_generic_path_requires_no_source_code_and_no_git_mutation():
    generic = CMS_PATHS_BY_ID[PATH_GENERIC]
    assert generic.requires_source_code_mutation is False
    assert generic.requires_git_mutation is False
    assert generic.specialization_of == ""


def test_private_runtime_fork_is_refused_by_name():
    with pytest.raises(PrivateRuntimeForkRefused) as exc:
        declaration_from_mapping(
            {
                "path_id": PATH_LANDING_PAGE,
                "label": "Landing-page change",
                "subject_kind": "content",
                "declaration_kind": DECLARATION_PRIVATE_FORK,
            }
        )
    assert exc.value.path_id == PATH_LANDING_PAGE
    assert exc.value.field == "paths.declaration_kind"
    assert "private runtime fork" in str(exc.value)


def test_unknown_declaration_kind_is_refused_not_coerced():
    with pytest.raises(UnknownDeclarationKindError):
        declaration_from_mapping(
            {
                "path_id": PATH_CONTENT_SEO,
                "label": "Content/SEO change",
                "subject_kind": "content",
                "declaration_kind": "capability-based",
            }
        )


def test_specialization_outside_the_generic_path_is_refused():
    # A specialized path that descends from a sibling is a private fork in
    # disguise: the same controller/policy contract no longer applies.
    with pytest.raises(PrivateRuntimeForkRefused):
        declaration_from_mapping(
            {
                "path_id": PATH_CONTENT_SEO,
                "label": "Content/SEO change",
                "subject_kind": "content",
                "specialization_of": PATH_LANDING_PAGE,
            }
        )


@pytest.mark.parametrize(
    "field", ["requires_source_code_mutation", "requires_git_mutation"]
)
def test_generic_path_mutation_is_refused(field):
    payload = {
        "path_id": PATH_GENERIC,
        "label": "Generic content request",
        "subject_kind": "content",
        field: True,
    }
    with pytest.raises(GenericPathMutationRefused) as exc:
        declaration_from_mapping(payload)
    assert exc.value.path_id == PATH_GENERIC


def test_generic_path_cannot_specialize_another_path():
    with pytest.raises(InvalidQualificationError) as exc:
        declaration_from_mapping(
            {
                "path_id": PATH_GENERIC,
                "label": "Generic content request",
                "subject_kind": "content",
                "specialization_of": PATH_LANDING_PAGE,
            }
        )
    assert exc.value.field == "paths.specialization_of"


def test_declared_boolean_must_be_a_real_boolean_not_a_truthy_string():
    # The declaration is operator-supplied, so "false" must not read as True
    # and "true" must not read as False by spelling.
    with pytest.raises(InvalidQualificationError) as exc:
        declaration_from_mapping(
            {
                "path_id": PATH_GENERIC,
                "label": "Generic content request",
                "subject_kind": "content",
                "requires_git_mutation": "false",
            }
        )
    assert exc.value.field == "requires_git_mutation"


# --- criterion 3: honest disposition; no prewritten receipt -------------------


def test_not_performed_record_is_the_honest_default():
    record = CmsProcessQualification.not_performed(
        producer="ops lane lane-sw-1511-cms-flows"
    )
    assert record.disposition == DISPOSITION_NOT_PERFORMED
    assert record.is_performed is False
    assert record.readback_verified is False
    assert record.restore_verified is False
    assert record.measured_outcomes == ()
    assert record.limitations, "a not-performed record must state its limitation"
    assert record.paths == INITIAL_CMS_PATHS


def test_not_performed_record_cannot_be_read_as_a_qualification():
    record = CmsProcessQualification.not_performed(producer="observer")
    payload = record.to_dict()
    # Round-tripping the honest record preserves the honest disposition; it
    # does not acquire a performed claim by being serialised.
    again = CmsProcessQualification.from_mapping(payload)
    assert again.disposition == DISPOSITION_NOT_PERFORMED
    assert again.is_performed is False


def test_performed_record_without_pins_is_refused():
    payload = _performed_mapping(pins={})
    with pytest.raises(PrewrittenReceiptRefused) as exc:
        CmsProcessQualification.from_mapping(payload)
    assert exc.value.field == "pins"
    for pin in REQUIRED_PERFORMED_PINS:
        assert pin in str(exc.value) or pin == "controller"


def test_performed_record_without_readback_is_refused():
    payload = _performed_mapping(readback_verified=False)
    with pytest.raises(PrewrittenReceiptRefused) as exc:
        CmsProcessQualification.from_mapping(payload)
    assert exc.value.field == "readback_verified"


def test_performed_record_without_restore_is_refused():
    payload = _performed_mapping(restore_verified=False)
    with pytest.raises(PrewrittenReceiptRefused):
        CmsProcessQualification.from_mapping(payload)


def test_performed_record_missing_a_required_outcome_is_refused():
    payload = _performed_mapping(
        measured_outcomes=["success", "wait", "failure", "cancel", "resume"]
    )
    with pytest.raises(PrewrittenReceiptRefused) as exc:
        CmsProcessQualification.from_mapping(payload)
    assert exc.value.field == "measured_outcomes"
    assert "publication-refusal" in str(exc.value)


def test_performed_record_measuring_an_unknown_outcome_is_refused():
    payload = _performed_mapping(
        measured_outcomes=list(REQUIRED_OUTCOMES) + ["published"]
    )
    with pytest.raises(InvalidQualificationError) as exc:
        CmsProcessQualification.from_mapping(payload)
    assert exc.value.field == "measured_outcomes"


def test_complete_performed_record_is_accepted_and_reports_itself_performed():
    record = CmsProcessQualification.from_mapping(_performed_mapping())
    assert record.is_performed is True
    assert record.readback_verified is True
    assert record.restore_verified is True
    assert set(record.measured_outcomes) == set(REQUIRED_OUTCOMES)
    assert set(record.pins) >= set(REQUIRED_PERFORMED_PINS)


def test_unknown_disposition_is_refused_not_downgraded():
    payload = _performed_mapping(disposition="probably-worked")
    with pytest.raises(InvalidQualificationError) as exc:
        CmsProcessQualification.from_mapping(payload)
    assert exc.value.field == "disposition"


# --- the path set is exactly the five declared paths -------------------------


def test_record_missing_a_path_is_refused():
    payload = _performed_mapping(paths=list(PATH_IDS[:-1]))
    with pytest.raises(IncompleteQualificationError) as exc:
        CmsProcessQualification.from_mapping(payload)
    assert exc.value.field == "paths"
    assert PATH_RECURRING_MAINTENANCE in str(exc.value)


def test_record_with_a_duplicated_path_is_refused():
    payload = _performed_mapping(
        paths=list(PATH_IDS[:-1]) + [PATH_GENERIC]
    )
    with pytest.raises(IncompleteQualificationError) as exc:
        CmsProcessQualification.from_mapping(payload)
    assert "more than once" in str(exc.value)


def test_record_with_an_undeclared_path_is_refused():
    payload = _performed_mapping(paths=list(PATH_IDS) + ["cms-marketing-blast"])
    with pytest.raises(InvalidQualificationError) as exc:
        CmsProcessQualification.from_mapping(payload)
    assert "not one of the five declared CMS paths" in str(exc.value)


# --- fail-closed mapping contract, mirroring the blueprint DataContract ------


@pytest.mark.parametrize("field", REQUIRED_QUALIFICATION_FIELDS)
def test_blank_required_field_is_refused_by_name(field):
    payload = _performed_mapping(**{field: "   "})
    with pytest.raises(InvalidQualificationError) as exc:
        CmsProcessQualification.from_mapping(payload)
    assert exc.value.field == f"data_contract.{field}"


def test_prose_only_record_is_refused():
    with pytest.raises(InvalidQualificationError) as exc:
        CmsProcessQualification.from_mapping("all five paths passed, trust me")
    assert exc.value.field == "data_contract"


def test_non_mapping_record_is_refused():
    with pytest.raises(InvalidQualificationError):
        CmsProcessQualification.from_mapping(["cms-generic-request"])


def test_unknown_record_key_is_refused():
    payload = _performed_mapping(verdict="PASS")
    with pytest.raises(InvalidQualificationError) as exc:
        CmsProcessQualification.from_mapping(payload)
    assert "verdict" in str(exc.value)


def test_wrong_subject_kind_is_refused():
    payload = _performed_mapping(subject_kind="deployment")
    with pytest.raises(InvalidQualificationError) as exc:
        CmsProcessQualification.from_mapping(payload)
    assert exc.value.field == "data_contract.subject_kind"


def test_record_refusals_mirror_the_boundary_contract_refusal_shape():
    # The blueprint boundary contract refuses a blank required field with a
    # named UndefinedContractFieldError; this record refuses with the same
    # attributable shape (a typed error carrying the field path).
    with pytest.raises(UndefinedContractFieldError) as boundary:
        DataContract.from_mapping(
            {"artifact": "x", "versioning": "", "producer": "p",
             "consumer": "c", "compatibility": "k"}
        )
    with pytest.raises(CmsPathQualificationError) as qualification:
        CmsProcessQualification.from_mapping(_performed_mapping(artifact=""))
    assert boundary.value.field == "boundaries.data_contract.versioning"
    assert qualification.value.field == "data_contract.artifact"
    assert isinstance(qualification.value, ValueError)


# --- the record is machine-readable and round-trips --------------------------


def test_record_round_trips_through_json():
    record = CmsProcessQualification.from_mapping(_performed_mapping())
    again = CmsProcessQualification.from_mapping(
        json.loads(json.dumps(record.to_dict()))
    )
    assert again.to_dict() == record.to_dict()


def test_not_performed_record_declares_all_five_paths_but_no_measurement():
    record = CmsProcessQualification.not_performed(producer="observer")
    payload = record.to_dict()
    assert [p["path_id"] for p in payload["paths"]] == list(PATH_IDS)
    assert payload["disposition"] == DISPOSITION_NOT_PERFORMED
    assert payload["measured_outcomes"] == []


def test_declared_path_dict_is_the_declaration_not_a_live_observation():
    # The five paths are declared with adapter capabilities; the declaration is
    # a statement about which capabilities are required, never a claim that a
    # run happened.
    for path in INITIAL_CMS_PATHS:
        payload = path.to_dict()
        assert payload["path_id"] in PATH_IDS
        assert isinstance(payload["adapter_capabilities"], list)
        assert "disposition" not in payload


def test_cms_path_is_frozen():
    with pytest.raises(Exception):
        CMS_PATHS_BY_ID[PATH_GENERIC].label = "changed"  # type: ignore[misc]


def test_initial_cms_paths_is_immutable_tuple_of_cms_paths():
    assert isinstance(INITIAL_CMS_PATHS, tuple)
    assert all(isinstance(p, CmsPath) for p in INITIAL_CMS_PATHS)
