"""V11 contract pending additions preserve existing business obligations."""
import copy

import pytest

from company_bid.extraction import _index, _windows, extraction_window_payload, validate_live_window
from company_bid.storage import digest
from company_bid.structured import (
    _covers_rejected_candidate, _extended_contract_repair, _repair_coverage,
    _validation_repair_request,
)
from test_contract_review_slots import rights


def arrange():
    parsed, manifest = rights()
    blocks, _, documents = _index(parsed, manifest)
    config = {"window_max_chars": 14000, "window_max_blocks": 60}
    window, = _windows(documents, blocks, config)[0]
    request = {**extraction_window_payload(window, config), "task_type": "extract_requirements"}
    requirement = lambda key: {"citation": {"block_ids": [key]}, "category": "commercial",
        "response_mode": "procedure", "format_status": "not_prescribed", "mandatory": True,
        "confidence": "high"}
    first = {"window_id": window["window_id"], "processing_declaration": "processed_not_semantically_accepted",
        "unresolved_block_ids": ["B1-12", "B1-13"], "format_status": "none_verified",
        "requirements": [requirement("B1-11"), requirement("B1-16")], "format_chapters": []}
    source = {"parsed": parsed, "manifest": manifest}
    coverage = _repair_coverage(first, request, error_code="LIVE_REQUIRED_BLOCK_UNRESOLVED", source_data=source)
    assert coverage and _extended_contract_repair(coverage)
    second = copy.deepcopy(first)
    second["unresolved_block_ids"] = []
    second["requirements"].extend({"citation": {"block_ids": group["block_ids"]},
        **copy.deepcopy(group["required_response_fields"])} for group in coverage["resolution_groups"])
    return window, request, source, first, second, coverage


def test_pending_contract_repair_keeps_old_obligations_and_appends_full_source():
    window, request, source, first, second, coverage = arrange()
    frozen = copy.deepcopy((request, source, first, second, coverage))
    group, = coverage["resolution_groups"]
    assert group["resolution_kind"] == "source_bound_contract_pending"
    assert group["block_ids"] == ["B1-12", "B1-13"]
    assert group["source_proof"]["value_status"] == "value_not_supplied_in_read_text"
    assert group["source_proof"]["physical_blank_status"] == "not_determined"
    assert coverage["counts"]["requirements"] == 2
    assert second["requirements"][:2] == first["requirements"]
    repair = _validation_repair_request(request, "LIVE_REQUIRED_BLOCK_UNRESOLVED", {}, coverage, "1" * 32)
    assert repair["validation_feedback"]["required_coverage"] == coverage
    assert "不得猜日期" in repair["validation_feedback"]["instructions"]
    assert repair["window"] == request["window"]
    assert _covers_rejected_candidate(second, coverage, repair)
    validated = validate_live_window(copy.deepcopy(second), window)
    pending = validated["requirements"][-1]
    assert pending["response_mode"] == "factual_response" and pending["confidence"] == "low"
    assert "甲方有权" in pending["citation"]["quote"]
    assert "意见进行调整、修改。" in pending["citation"]["quote"]
    assert (request, source, first, second, coverage) == frozen


@pytest.mark.parametrize("change", ["drop_old", "change_old", "swap_old", "shorten_old", "clear_only",
    "drop_pending", "shorten_pending", "pending_procedure", "pending_confidence", "pending_label",
    "extra_pending", "keep_unresolved", "new_unresolved", "format_status", "source_proof"])
def test_pending_correction_cannot_delete_reclassify_or_shorten(change):
    _, request, _, first, second, coverage = arrange()
    if change == "drop_old": second["requirements"].pop(0)
    elif change == "change_old": second["requirements"][0]["mandatory"] = False
    elif change == "swap_old": second["requirements"][:2] = reversed(second["requirements"][:2])
    elif change == "shorten_old": second["requirements"][0]["citation"]["block_ids"] = ["B1-16"]
    elif change == "clear_only": second["requirements"] = copy.deepcopy(first["requirements"])
    elif change == "drop_pending": second["requirements"].pop()
    elif change == "shorten_pending": second["requirements"][-1]["citation"]["block_ids"] = ["B1-12"]
    elif change == "pending_procedure": second["requirements"][-1]["response_mode"] = "procedure"
    elif change == "pending_confidence": second["requirements"][-1]["confidence"] = "high"
    elif change == "pending_label": second["requirements"][-1]["required_fact_labels"] = ["天数"]
    elif change == "extra_pending": second["requirements"].append(copy.deepcopy(second["requirements"][-1]))
    elif change == "keep_unresolved": second["unresolved_block_ids"] = list(first["unresolved_block_ids"])
    elif change == "new_unresolved": second["unresolved_block_ids"] = ["B1-11"]
    elif change == "format_status": second["format_status"] = "specified"
    elif change == "source_proof": coverage["resolution_groups"][0]["source_proof"]["values_inferred"] = True
    assert not _covers_rejected_candidate(second, coverage, request)


@pytest.mark.parametrize("change", ["unread", "glyph", "image", "false_complete", "fake_request", "unmatched"])
def test_pending_coverage_must_be_rebuilt_from_actual_read_source(change):
    _, request, source, first, _, _ = arrange()
    unit = source["parsed"]["documents"][0]["units"][0]
    if change == "unread": unit["coverage"]["status"] = "needs_ocr"
    elif change == "glyph": unit["coverage"]["unmapped_glyphs"] = [{"code": "unknown"}]
    elif change == "image": unit["coverage"]["image_count"] = 1
    elif change == "false_complete": source["parsed"]["coverage"]["required_complete"] = False
    elif change == "fake_request": request["task_instructions"] += " 不同请求"
    elif change == "unmatched": first["unresolved_block_ids"] = ["B1-11"]
    assert _repair_coverage(first, request, error_code="LIVE_REQUIRED_BLOCK_UNRESOLVED", source_data=source) is None


def test_proof_digest_is_independent_of_raw_model_values():
    _, _, _, _, _, coverage = arrange()
    proof = coverage["resolution_groups"][0]
    assert proof["source_proof_sha256"] == digest(proof["source_proof"])
    assert proof["source_proof"]["values_inferred"] is False
    assert all(field["value"] is None for field in proof["source_proof"]["pending_fields"])
