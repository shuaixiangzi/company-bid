"""Independent reviews cannot repair OCR status or invent source facts."""
import copy

import pytest

from company_bid.material_source_review import (
    MaterialSourceReviewFailure,
    build_material_source_review,
    material_source_review_note,
    validate_material_source_review,
)
from company_bid.storage import digest


def example():
    return {
        "identity": {"source_id": "SOURCE-1", "source_sha256": "1" * 64,
                     "selection_sha256": "2" * 64, "image_sha256": "3" * 64},
        "ocr": {"run_id": "4" * 32, "node_key": "N3", "unit_id": "material-ocr-1",
                "response_sha256": "5" * 64, "status": "failed",
                "error_code": "MATERIAL_OCR_NUMERIC_REFERENCE_MISMATCH"},
        "reviewer": {"review_id": "review-area-1", "review_method": "agent_source_image_review",
                     "evidence_sha256": "6" * 64},
        "field": {"field_id": "restoration-area", "scope": {"image_region_normalized": [0.1, 0.2, 0.8, 0.9],
                  "table_cell": {"table_index": 0, "row": 1, "column": 3}},
                  "disposition": "machine_error", "source_observation": "clear_value",
                  "recognized_text": "740 m³", "source_text": "740 m²"},
    }


def test_review_records_original_values_and_keeps_failure_qualification_and_inputs():
    value = example()
    before = copy.deepcopy(value)
    record = build_material_source_review(value)
    assert value == before
    assert record["ocr"]["status"] == "failed"
    assert record["field"]["recognized_text"] == "740 m³"
    assert record["field"]["source_text"] == "740 m²"
    assert record["qualification_result"] == "not_determined"
    assert record["reading_completeness"] == "not_established"
    for flag in ("ocr_response_modified", "ocr_status_modified", "automatic_numeric_adoption",
                 "automatic_fact_adoption", "automatic_assembly_allowed", "node_completion_authorized"):
        assert record[flag] is False
    assert record["record_sha256"] == digest({k: v for k, v in record.items() if k != "record_sha256"})
    assert validate_material_source_review(record) == record
    note = material_source_review_note(record)
    assert "740 m²" in note and "740 m³" in note and "机器识别错误" in note
    assert "状态保持失败" in note and "未自动替换" in note and "资格仍待核" in note


@pytest.mark.parametrize("parent,key", [("identity", "source_id"), ("identity", "source_sha256"),
    ("identity", "selection_sha256"), ("identity", "image_sha256"), ("ocr", "response_sha256"),
    ("reviewer", "review_id"), ("reviewer", "review_method"), ("reviewer", "evidence_sha256"),
    ("field", "scope")])
def test_missing_source_response_or_independent_review_binding_rejected(parent, key):
    value = example()
    del value[parent][key]
    with pytest.raises(MaterialSourceReviewFailure, match="^MATERIAL_SOURCE_REVIEW_INVALID$"):
        build_material_source_review(value)


@pytest.mark.parametrize("change", [
    {"source_text": None}, {"source_text": ""}, {"source_text": "   "}, {"recognized_text": ""},
    {"recognized_text": "740 m²"}, {"recognized_text": "７４０ｍ2"},
    {"source_observation": "ambiguous_or_unreadable"}, {"disposition": "material_missing"},
    {"source_text": "confidential\nvalue"},
])
def test_invalid_conflict_or_missing_material_inference_rejected_without_content_leak(change):
    value = example()
    value["field"].update(change)
    with pytest.raises(MaterialSourceReviewFailure) as caught:
        build_material_source_review(value)
    assert str(caught.value) == "MATERIAL_SOURCE_REVIEW_INVALID"
    assert "740" not in str(caught.value) and "confidential" not in str(caught.value)


def test_ambiguous_source_preserves_machine_value_but_does_not_guess_source_value():
    value = example()
    value["field"].update(disposition="source_ambiguous", source_observation="ambiguous_or_unreadable", source_text=None)
    record = build_material_source_review(value)
    note = material_source_review_note(record)
    assert "来源模糊" in note and "不猜填" in note and "740 m³" in note
    assert record["field"]["source_text"] is None
    value["field"]["source_text"] = "740 m²"
    with pytest.raises(MaterialSourceReviewFailure):
        build_material_source_review(value)


@pytest.mark.parametrize("recognized", [None, "", "740 m³"])
def test_independently_reviewed_blank_is_distinct_from_no_ocr_text(recognized):
    value = example()
    value["field"].update(disposition="source_blank", source_observation="independently_confirmed_blank",
                          recognized_text=recognized, source_text="")
    record = build_material_source_review(value)
    note = material_source_review_note(record)
    assert "原文留空" in note and "不据此认定整页空白或公司缺少" in note
    assert record["material_presence_conclusion"] == "not_determined"


@pytest.mark.parametrize("change", [{"source_observation": "clear_value"},
    {"source_observation": "ambiguous_or_unreadable"}, {"source_text": None},
    {"source_text": " "}, {"source_text": "740 m²"}])
def test_missing_machine_text_does_not_establish_source_blank(change):
    value = example()
    value["field"].update(disposition="source_blank", source_observation="independently_confirmed_blank",
                          recognized_text="", source_text="")
    value["field"].update(change)
    with pytest.raises(MaterialSourceReviewFailure):
        build_material_source_review(value)


@pytest.mark.parametrize("scope", [{"table_cell": {"table_index": 0, "row": 1, "column": 3}},
    {"image_region_normalized": [0.2, 0.2, 0.2, 0.8]}, {"image_region_normalized": [0., 0., 1.1, 1.]},
    {"image_region_normalized": [0., 0., float("nan"), 1.]},
    {"image_region_normalized": [0., 0., 1., 1.], "table_cell": {"table_index": True, "row": 1, "column": 3}},
])
def test_missing_or_invalid_source_region_is_not_replaced_by_machine_table_scope(scope):
    value = example()
    value["field"]["scope"] = scope
    with pytest.raises(MaterialSourceReviewFailure):
        build_material_source_review(value)


@pytest.mark.parametrize("status,error", [("failed", None), ("succeeded", "OCR_FAILURE"),
                                          ("pending", None)])
def test_inconsistent_or_unknown_ocr_execution_state_rejected(status, error):
    value = example()
    value["ocr"].update(status=status, error_code=error)
    with pytest.raises(MaterialSourceReviewFailure):
        build_material_source_review(value)


def test_successful_ocr_is_not_upgraded_to_field_accuracy():
    value = example()
    value["ocr"].update(status="succeeded", error_code=None)
    note = material_source_review_note(build_material_source_review(value))
    assert "执行成功（不代表字段准确）" in note


@pytest.mark.parametrize("mutation", ["image", "response", "source_text", "qualification", "hash", "extra", "bool_type"])
def test_tampered_stored_review_cannot_be_rendered(mutation):
    record = build_material_source_review(example())
    if mutation == "image":
        record["identity"]["image_sha256"] = "a" * 64
    elif mutation == "response":
        record["ocr"]["response_sha256"] = "a" * 64
    elif mutation == "source_text":
        record["field"]["source_text"] = "741 m²"
    elif mutation == "qualification":
        record["qualification_result"] = "satisfied"
    elif mutation == "hash":
        record["record_sha256"] = "b" * 64
    elif mutation == "bool_type":
        record["automatic_fact_adoption"] = 0
    else:
        record["fact"] = "invented"
    with pytest.raises(MaterialSourceReviewFailure, match="MATERIAL_SOURCE_REVIEW_RECORD_INVALID"):
        material_source_review_note(record)


def test_supplied_fact_or_adoption_flag_is_not_accepted_in_input():
    value = example()
    value["automatic_fact_adoption"] = True
    with pytest.raises(MaterialSourceReviewFailure):
        build_material_source_review(value)
