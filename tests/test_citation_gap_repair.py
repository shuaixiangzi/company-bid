"""V11 repairs restore one proved omitted line and preserve old requirements."""
import copy
import hashlib

import pytest

from company_bid.extraction import validate_live_window
from company_bid.structured import (
    _covers_rejected_candidate, _extended_contract_repair, _repair_coverage,
    _validation_repair_request,
)
from test_citation_gap_sources import candidate_data, request_data, source_data


def arrange():
    parsed, manifest = source_data()
    document = parsed["documents"][0]
    rows = document["units"][0]["blocks"]
    for key, text in [("A10", "第十条 争议处理"), ("END", "10.1双方应保存合成争议记录。")]:
        rows.append({"block_id": key, "source_id": "T", "kind": "paragraph", "origin": "native",
            "locator": f"pdf/page[1]/line[{len(rows)}]", "text": text})
    sha = hashlib.sha256("\n".join(row["text"] for row in rows).encode()).hexdigest()
    document["sha256"] = manifest["sources"][0]["sha256"] = sha
    window, request = request_data(parsed, manifest)
    first = candidate_data(window)
    source = {"parsed": parsed, "manifest": manifest}
    coverage = _repair_coverage(first, request, error_code="LIVE_REQUIRED_BLOCK_UNRESOLVED", source_data=source)
    assert coverage and coverage["citation_gap_restore"]
    second = copy.deepcopy(first)
    gap = coverage["citation_gap_restore"]
    second["requirements"][gap["candidate_index"]]["citation"]["block_ids"] = list(gap["restored_block_ids"])
    second["unresolved_block_ids"] = []
    second["requirements"].extend({"citation": {"block_ids": list(group["block_ids"])},
        **copy.deepcopy(group["required_response_fields"])} for group in coverage["resolution_groups"])
    return window, request, source, first, second, coverage


def test_one_gap_plus_independent_pending_runs_full_original_validation():
    window, request, source, first, second, coverage = arrange()
    frozen = copy.deepcopy((request, source, first, second, coverage))
    assert _extended_contract_repair(coverage)
    gap = coverage["citation_gap_restore"]
    assert gap["original_block_ids"] == ["G1", "G3"]
    assert gap["restored_block_ids"] == ["G1", "G2", "G3"]
    assert coverage["counts"]["requirements"] == len(first["requirements"]) == 7
    repair = _validation_repair_request(request, "LIVE_REQUIRED_BLOCK_UNRESOLVED", {}, coverage, "2" * 32)
    assert "一处引用漏行" in repair["validation_feedback"]["instructions"]
    assert repair["validation_feedback"]["required_coverage"] == coverage
    assert _covers_rejected_candidate(second, coverage, repair)
    validated = validate_live_window(copy.deepcopy(second), window)
    quote = validated["requirements"][6]["citation"]["quote"]
    assert "未经双方书面同意不得实施变更" in quote
    assert quote.endswith("否则乙方应承担由此产生的损失。")
    assert second["requirements"][:6] == first["requirements"][:6]
    assert validated["requirements"][-1]["response_mode"] == "factual_response"
    assert (request, source, first, second, coverage) == frozen


@pytest.mark.parametrize("change", ["drop_old", "change_old", "change_gap_field", "keep_gap", "shorten_gap",
    "reorder_gap", "extra_gap_id", "gap_quote", "drop_pending", "pending_mode", "clear_only",
    "keep_unresolved", "fake_gap_hash", "fake_pending_hash", "change_other_anchor"])
def test_gap_correction_cannot_hide_conditions_or_expand_authorized_changes(change):
    _, request, _, first, second, coverage = arrange()
    if change == "drop_old": second["requirements"].pop(0)
    elif change == "change_old": second["requirements"][0]["mandatory"] = False
    elif change == "change_gap_field": second["requirements"][6]["confidence"] = "high"
    elif change == "keep_gap": second["requirements"][6]["citation"]["block_ids"] = ["G1", "G3"]
    elif change == "shorten_gap": second["requirements"][6]["citation"]["block_ids"] = ["G1", "G2"]
    elif change == "reorder_gap": second["requirements"][6]["citation"]["block_ids"] = ["G1", "G3", "G2"]
    elif change == "extra_gap_id": second["requirements"][6]["citation"]["block_ids"].append("NEXT")
    elif change == "gap_quote": second["requirements"][6]["citation"]["quote"] = "甲方应提交申请。"
    elif change == "drop_pending": second["requirements"].pop()
    elif change == "pending_mode": second["requirements"][-1]["response_mode"] = "procedure"
    elif change == "clear_only": second["requirements"] = copy.deepcopy(first["requirements"])
    elif change == "keep_unresolved": second["unresolved_block_ids"] = list(first["unresolved_block_ids"])
    elif change == "fake_gap_hash": coverage["citation_gap_restore"]["source_proof"]["clause_number"] = "9.4"
    elif change == "fake_pending_hash": coverage["resolution_groups"][0]["source_proof"]["values_inferred"] = True
    elif change == "change_other_anchor": second["requirements"][0]["citation"]["block_ids"] = ["R2"]
    assert not _covers_rejected_candidate(second, coverage, request)


@pytest.mark.parametrize("change", ["second_gap", "two_missing", "non_native", "unread", "unmatched_pending", "fake_request"])
def test_gap_coverage_requires_one_source_proved_gap_and_a_separately_proved_pending(change):
    _, request, source, first, _, _ = arrange()
    rows = source["parsed"]["documents"][0]["units"][0]["blocks"]
    if change == "second_gap": first["requirements"][0]["citation"]["block_ids"] = ["R1", "R3"]
    elif change == "two_missing": first["requirements"][6]["citation"]["block_ids"] = ["P2", "G3"]
    elif change == "non_native": next(row for row in rows if row["block_id"] == "G2")["origin"] = "ocr"
    elif change == "unread": source["parsed"]["documents"][0]["units"][0]["coverage"]["status"] = "needs_ocr"
    elif change == "unmatched_pending": first["unresolved_block_ids"] = ["P1"]
    elif change == "fake_request": request["task_instructions"] += " 改写请求"
    assert _repair_coverage(first, request, error_code="LIVE_REQUIRED_BLOCK_UNRESOLVED", source_data=source) is None
