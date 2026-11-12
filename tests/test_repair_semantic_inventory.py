"""A bounded repair preserves each source item's business route and valid labels."""
import copy
import json

import pytest

from company_bid.diagnostics import inspect_diagnostics
from company_bid.extraction import (
    _index, _model_window, _windows, source_selected_output_contract, validate_live_window,
)
from test_source_selected_citations import block, fixture, live_candidate
from test_structured_validation_repair import artifacts, response, setup_run


BAD_LABEL = "不存在的证书标签CANARY"


def semantic_source(tmp_path):
    manifest, parsed = fixture(tmp_path)
    block(parsed, "TABLE", "", kind="table", rows=[
        ["事项", "要求"], ["资格", "资质证书、原件扫描件、承诺书"], ["商务", "按约支付服务费。"]],
        merges=[], column_widths=[1, 3], header_rows=[0])
    block(parsed, "CERT", "项目负责人应提交CP2证书及培训证书。")
    block(parsed, "OTHER", "其他事项另行安排；资质证书、原件扫描件、承诺书仅为本合成样本的同名词。")
    blocks, _, documents = _index(parsed, manifest)
    window = _windows(documents, blocks, {})[0][0]
    raw = live_candidate({"window": window}, {"block_ids": ["TABLE"]})
    raw["requirements"] = [
        {"citation": {"block_ids": ["TABLE"]}, "category": "qualification", "response_mode": "attachment",
         "format_status": "not_prescribed", "attachment_keywords": ["资质证书", "原件扫描件"]},
        {"citation": {"block_ids": ["TABLE"]}, "category": "commercial", "response_mode": "procedure",
         "format_status": "not_prescribed", "mandatory": True, "confidence": "high"},
        {"citation": {"block_ids": ["CERT"]}, "category": "qualification", "response_mode": "attachment",
         "format_status": "not_prescribed", "attachment_keywords": ["CP2证书", "培训证书"]},
    ]
    return window, raw


def run_repair(tmp_path, monkeypatch, window, first, second, *, expected_calls=2):
    originals = copy.deepcopy((first, second))
    executor, rid, _, payload, _, sent, validated, replies = setup_run(
        tmp_path / "runtime", monkeypatch, max_input_chars=50000,
        validate=lambda value, _: validate_live_window(value, window),
        transport=lambda _, count: response(first if count == 1 else second))
    payload.update(window=_model_window(window), output_contract=source_selected_output_contract())
    frozen_payload = copy.deepcopy(payload)
    state = executor.execute(rid)
    assert len(sent) == len(replies) == expected_calls
    assert (first, second) == originals
    assert payload == frozen_payload
    captured = artifacts(executor, state)
    actual = [json.loads(value["choices"][0]["message"]["content"])
              for record, value in captured if record["name"].startswith("response-")]
    assert actual == [first, second][:expected_calls]
    report = inspect_diagnostics(tmp_path / "runtime", rid)
    assert report["actual_call_attempts"] == expected_calls and not report["gaps"]
    relation = next((value for record, value in captured if record["name"].startswith("validation-repair-result-")), None)
    feedback = json.loads(sent[1]["messages"][-1]["content"])["validation_feedback"] if expected_calls == 2 else None
    return state, validated, relation, feedback


@pytest.mark.parametrize("correction", ["delete_one", "replace_one"])
def test_only_the_reported_label_may_be_deleted_or_replaced_and_raw_responses_stay_original(
        tmp_path, monkeypatch, correction):
    window, second = semantic_source(tmp_path)
    first = copy.deepcopy(second)
    first["requirements"][0]["attachment_keywords"] = ["资质证书", BAD_LABEL, "原件扫描件"]
    if correction == "replace_one":
        second["requirements"][0]["attachment_keywords"] = ["资质证书", "承诺书", "原件扫描件"]
    state, validated, relation, feedback = run_repair(tmp_path, monkeypatch, window, first, second)
    assert state["status"] == "succeeded" and relation["status"] == "completed"
    assert validated == [first, second]
    assert feedback["code"] == "EXTRACTION_LABEL_NOT_GROUNDED"
    assert feedback["context"] == {"candidate_type": "requirement", "candidate_index": 0,
                                    "field": "attachment_keywords", "label_index": 1}
    assert BAD_LABEL not in json.dumps(feedback, ensure_ascii=False)


@pytest.mark.parametrize("damage", [
    "merge_same_source_and_pad_count", "change_category", "change_response_mode",
    "delete_correct_certificate_labels", "replace_correct_certificate_label",
    "delete_error_field_prefix", "delete_error_field_suffix", "swap_same_source_business_items",
    "change_default_mandatory", "change_default_confidence", "change_format_status", "change_format_title",
    "replace_repeated_source_entry", "change_root_unresolved", "change_root_format", "add_unrelated_source_block",
])
def test_label_repair_cannot_trade_business_items_labels_or_source_multiplicity_for_a_valid_response(
        tmp_path, monkeypatch, damage):
    window, second = semantic_source(tmp_path)
    if damage == "replace_repeated_source_entry":
        second["requirements"][1] = copy.deepcopy(second["requirements"][0])
    first = copy.deepcopy(second)
    first["requirements"][0]["attachment_keywords"] = ["资质证书", BAD_LABEL, "原件扫描件"]
    requirements = second["requirements"]
    if damage == "merge_same_source_and_pad_count":
        requirements[0].update(response_mode="factual_response", attachment_keywords=[])
        requirements[1] = {"citation": {"block_ids": ["OTHER"]}, "category": "procedure",
                           "response_mode": "procedure", "format_status": "not_prescribed"}
    elif damage == "change_category":
        requirements[2]["category"] = "commercial"
    elif damage == "change_response_mode":
        requirements[2]["response_mode"] = "factual_response"
    elif damage == "delete_correct_certificate_labels":
        requirements[2]["attachment_keywords"] = []
    elif damage == "replace_correct_certificate_label":
        requirements[2]["attachment_keywords"] = ["项目负责人", "培训证书"]
    elif damage == "delete_error_field_prefix":
        requirements[0]["attachment_keywords"] = ["原件扫描件"]
    elif damage == "delete_error_field_suffix":
        requirements[0]["attachment_keywords"] = ["资质证书"]
    elif damage == "swap_same_source_business_items":
        requirements[0], requirements[1] = requirements[1], requirements[0]
    elif damage == "change_default_mandatory":
        requirements[2]["mandatory"] = False
    elif damage == "change_default_confidence":
        requirements[2]["confidence"] = "high"
    elif damage == "change_format_status":
        requirements[2]["format_status"] = "prescribed"
    elif damage == "change_format_title":
        requirements[2]["format_title"] = "CP2证书"
    elif damage == "replace_repeated_source_entry":
        requirements[1]["citation"]["block_ids"] = ["OTHER"]
    elif damage == "change_root_unresolved":
        second["unresolved_block_ids"] = ["OTHER"]
    elif damage == "change_root_format":
        second["format_status"] = "conflict"
    else:
        requirements[0]["citation"]["block_ids"] = ["TABLE", "CERT"]
    state, validated, relation, feedback = run_repair(tmp_path, monkeypatch, window, first, second)
    assert state["status"] == "failed" and state["error_code"] == "VALIDATION_REPAIR_COVERAGE_LOST"
    assert relation["status"] == "failed" and relation["repair_error_code"] == "VALIDATION_REPAIR_COVERAGE_LOST"
    assert validated == [first]
    assert feedback["code"] == "EXTRACTION_LABEL_NOT_GROUNDED"
    assert BAD_LABEL not in json.dumps(feedback, ensure_ascii=False)


@pytest.mark.parametrize("invalidity", ["missing_route", "invalid_route", "invalid_required_flag"])
def test_schema_repair_can_fix_only_missing_or_invalid_fields_while_preserving_legal_business_items(
        tmp_path, monkeypatch, invalidity):
    window, second = semantic_source(tmp_path)
    first = copy.deepcopy(second)
    if invalidity == "missing_route":
        first["requirements"][0].pop("response_mode")
    elif invalidity == "invalid_route":
        first["requirements"][0]["response_mode"] = "attachment_typo"
    else:
        first["requirements"][0]["mandatory"] = "invalid_boolean"
        second["requirements"][0]["mandatory"] = True
    state, validated, relation, feedback = run_repair(tmp_path, monkeypatch, window, first, second)
    assert state["status"] == "succeeded" and relation["status"] == "completed"
    assert validated == [first, second]
    assert feedback["code"] == "EXTRACTION_SCHEMA_INVALID"


@pytest.mark.parametrize("damage", ["merge_same_source_and_pad_count", "change_legal_route", "drop_legal_labels", "add_unrelated_source"])
def test_schema_repair_cannot_use_one_invalid_field_to_rewrite_other_legal_business_semantics(
        tmp_path, monkeypatch, damage):
    window, second = semantic_source(tmp_path)
    first = copy.deepcopy(second)
    first["requirements"][0].pop("response_mode")
    if damage == "merge_same_source_and_pad_count":
        second["requirements"][0]["response_mode"] = "factual_response"
        second["requirements"][1] = {"citation": {"block_ids": ["OTHER"]}, "category": "procedure",
                                    "response_mode": "procedure", "format_status": "not_prescribed"}
    elif damage == "change_legal_route":
        second["requirements"][2].update(category="commercial", response_mode="factual_response")
    elif damage == "drop_legal_labels":
        second["requirements"][2]["attachment_keywords"] = []
    else:
        second["requirements"][0]["citation"]["block_ids"] = ["TABLE", "CERT"]
    state, validated, relation, feedback = run_repair(tmp_path, monkeypatch, window, first, second)
    assert state["status"] == "failed" and state["error_code"] == "VALIDATION_REPAIR_COVERAGE_LOST"
    assert relation["status"] == "failed" and validated == [first]
    assert feedback["code"] == "EXTRACTION_SCHEMA_INVALID"


def test_an_earlier_label_error_cannot_admit_repair_for_another_legacy_citations_wrong_locator(
        tmp_path, monkeypatch):
    window, second = semantic_source(tmp_path)
    source = next(value for value in window["blocks"] if value["block_id"] == "TABLE")
    second["requirements"][1]["citation"].update({key: source[key] for key in ("source_id", "block_id", "locator")})
    first = copy.deepcopy(second)
    first["requirements"][0]["attachment_keywords"] = ["资质证书", BAD_LABEL, "原件扫描件"]
    first["requirements"][1]["citation"]["locator"] = "pdf/page[999]/table[wrong]"
    state, validated, relation, feedback = run_repair(
        tmp_path, monkeypatch, window, first, second, expected_calls=1)
    assert state["status"] == "failed" and state["error_code"] == "EXTRACTION_LABEL_NOT_GROUNDED"
    assert validated == [first] and relation is None and feedback is None


def test_a_label_repair_cannot_change_another_legacy_citations_valid_locator(tmp_path, monkeypatch):
    window, second = semantic_source(tmp_path)
    source = next(value for value in window["blocks"] if value["block_id"] == "TABLE")
    second["requirements"][1]["citation"].update({key: source[key] for key in ("source_id", "block_id", "locator")})
    first = copy.deepcopy(second)
    first["requirements"][0]["attachment_keywords"] = ["资质证书", BAD_LABEL, "原件扫描件"]
    second["requirements"][1]["citation"]["locator"] = "pdf/page[999]/table[wrong]"
    state, validated, relation, feedback = run_repair(tmp_path, monkeypatch, window, first, second)
    assert state["status"] == "failed"
    assert state["error_code"] in {"CITATION_LOCATION_INVALID", "VALIDATION_REPAIR_COVERAGE_LOST"}
    assert relation["status"] == "failed" and feedback["code"] == "EXTRACTION_LABEL_NOT_GROUNDED"
    assert validated == ([first] if state["error_code"] == "VALIDATION_REPAIR_COVERAGE_LOST" else [first, second])
