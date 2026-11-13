"""Frozen N1 proofs for duplicate letter bodies; synthetic inputs, no calls."""
from __future__ import annotations

import copy
import hashlib

import pytest

from company_bid.extraction import (
    PlanningError, _FormatChapter, _index, _normalize_source_selectors, _windows,
    extraction_window_payload, validate_live_window,
)
from company_bid.format_continuation_sources import (
    fixed_letter_items, format_continuation_source_proof,
)
from company_bid.storage import digest
from test_source_selected_citations import block, fixture


def source_case(tmp_path, *, title="27 声明函", count=6, boundary="第八章 合同条款", max_blocks=7):
    manifest, parsed = fixture(tmp_path)
    document = parsed["documents"][0]
    document["format"] = "pdf"

    def line(identity, text, page):
        value = block(parsed, identity, text, page=page, origin="native")
        unit = next(unit for unit in document["units"] if unit["page"] == page)
        value["locator"] = f"pdf/page[{page}]/line[{len(unit['blocks']) - 1}]"
        return value

    line("REGION", "第七章 响应文件要求及格式", 1)
    line("TITLE", title, 1)
    line("INTRO", "我方郑重声明：", 1)
    for number in range(1, count + 1):
        line(f"I{number}", f"{number}. 我方保留原文规定的条件；" if number != 6 else
             f"{number}. 未被主管机关列入原文禁止名单", 1 if number <= 4 else 2)
    line("CLOSE", "以上声明保留全部否定及例外。", 2)
    line("SIGN", "响应人： （盖章）", 2)
    line("DATE", "日期：待填", 2)
    line("BOUNDARY", boundary, 3)
    line("NEXT-BODY", "其他原文保持独立。", 3)
    for unit in document["units"]:
        unit["coverage"].update(image_count=0, unread_objects=[], unmapped_glyphs=[], error_code=None)
    document["expected_units"] = len(document["units"])
    document["sha256"] = manifest["sources"][0]["sha256"] = hashlib.sha256(b"Synthetic letter proof fixture.").hexdigest()
    first_selected = max(5, max_blocks - 2)
    selected = [f"I{number}" for number in range(first_selected, count + 1)] + ["CLOSE", "SIGN", "DATE"]
    candidate, request = packet(parsed, manifest, selected, max_blocks=max_blocks, title=title)
    return candidate, request, parsed, manifest


def packet(parsed, manifest, selected, *, max_blocks=7, title="27 声明函"):
    blocks, _, documents = _index(parsed, manifest)
    config = {"window_max_chars": 50000, "window_max_blocks": max_blocks}
    windows, _ = _windows(documents, blocks, config)
    window = next(value for value in windows if set(selected) <= {block["block_id"] for block in value["blocks"]})
    request = {**extraction_window_payload(window, config), "task_type": "extract_requirements"}
    candidate = {"window_id": window["window_id"], "processing_declaration": "processed_not_semantically_accepted",
        "unresolved_block_ids": [], "requirements": [{"citation": {"block_ids": selected},
            "category": "fixed_table", "response_mode": "fixed_text", "format_status": "prescribed",
            "format_title": title, "required_fact_labels": ["响应人"], "attachment_keywords": [],
            "reference_keywords": [], "mandatory": True, "confidence": "high"}],
        "format_chapters": [{"title": title, "level": 2, "citation": {"block_ids": selected},
            "content_block_ids": list(selected), "requirement_indices": [0], "status": "prescribed"}],
        "format_status": "specified"}
    return candidate, request


def values(parsed):
    return {block["block_id"]: block for doc in parsed["documents"] for unit in doc["units"] for block in unit["blocks"]}


def reindex_lines(parsed):
    for unit in parsed["documents"][0]["units"]:
        for index, block in enumerate(unit["blocks"]):
            if block["kind"] == "paragraph":
                block["locator"] = f"pdf/page[{unit['page']}]/line[{index}]"


def rebuild(case):
    candidate, request, parsed, manifest = case
    selected = candidate["requirements"][0]["citation"]["block_ids"]
    return (*packet(parsed, manifest, selected,
                    max_blocks=request["window_config"]["window_max_blocks"],
                    title=candidate["format_chapters"][0]["title"]), parsed, manifest)


def proof(case, index=0):
    return format_continuation_source_proof(*case, index)


def test_source_proves_duplicate_continuation_without_changing_any_candidate_or_reading(tmp_path):
    case = source_case(tmp_path)
    before = copy.deepcopy(case)
    candidate, request, parsed, manifest = case
    result = proof(case)
    assert result and case == before
    assert result["resolution_kind"] == "source_bound_format_continuation"
    assert result["expected_format_status"] == "none_verified"
    assert result["format_index"] == 0
    assert result["retained_requirement_indices"] == result["original_requirement_indices"] == [0]
    assert result["canonical_anchor"]["block_id"] == "TITLE"
    assert result["owner_window_id"] != request["window"]["window_id"]
    assert result["owner_processing_status"] == "not_asserted_by_source_proof"
    assert result["parsed_sha256"] == digest(parsed)
    assert result["source_reading_sha256"] == digest(parsed["documents"][0])
    assert result["received_format_sha256"] == digest(candidate["format_chapters"][0])
    normalized, _ = _normalize_source_selectors(candidate, request["window"])
    assert result["format_sha256"] == digest(_FormatChapter.model_validate(normalized["format_chapters"][0], strict=True).model_dump(mode="json"))
    assert result["proof_sha256"] == digest({key: value for key, value in result.items() if key != "proof_sha256"})
    members = result["full_format_members"]
    assert [member["block_id"] for member in members] == ["TITLE", "INTRO", *[f"I{number}" for number in range(1, 7)], "CLOSE", "SIGN", "DATE"]
    assert result["full_format_members_sha256"] == digest(members)
    for member in members:
        original = values(parsed)[member["block_id"]]
        assert member["text_sha256"] == hashlib.sha256(original["text"].encode()).hexdigest()
        assert member["locator"] == original["locator"]
    assert result["boundary"]["block_id"] == "BOUNDARY"
    assert result["target_block_ids"] == candidate["format_chapters"][0]["content_block_ids"]
    assert [item["item_number"] for item in result["numbered_items"]] == list(range(1, 7))
    assert result["numbered_items"][-1]["continuation_basis_sha256"]
    indexed, _, documents = _index(parsed, manifest)
    windows, _ = _windows(documents, indexed, request["window_config"])
    window = next(item for item in windows if item["window_id"] == candidate["window_id"])
    with pytest.raises(PlanningError, match="FORMAT_HEADING_NOT_GROUNDED"):
        validate_live_window(candidate, window)
    hypothesis = copy.deepcopy(candidate)
    hypothesis["format_chapters"] = []
    hypothesis["format_status"] = "none_verified"
    validated = validate_live_window(hypothesis, window)
    assert len(validated["requirements"]) == 1 and validated["format_chapters"] == []
    assert hypothesis["requirements"] == before[0]["requirements"]


@pytest.mark.parametrize("title,count", [("3 保证书", 5), ("42 承诺书", 7), ("8 投标函", 8), ("91 声明书", 6)])
def test_title_number_and_list_length_are_not_sample_constants(tmp_path, title, count):
    case = source_case(tmp_path, title=title, count=count, max_blocks=max(7, count + 1))
    # A larger capacity shifts the cut, so use exactly the target-owned suffix.
    candidate, request, parsed, manifest = case
    targets = [block["block_id"] for block in request["window"]["blocks"]]
    selected = [key for key in targets if key.startswith("I") or key in {"CLOSE", "SIGN", "DATE"}]
    candidate["requirements"][0]["citation"]["block_ids"] = selected
    candidate["format_chapters"][0]["citation"]["block_ids"] = list(selected)
    candidate["format_chapters"][0]["content_block_ids"] = list(selected)
    result = proof(case)
    assert result and result["title"] == title
    assert len(result["numbered_items"]) == count


def test_already_normalized_received_selectors_are_supported_without_misnaming_them_raw(tmp_path):
    candidate, request, parsed, manifest = source_case(tmp_path)
    candidate, _ = _normalize_source_selectors(candidate, request["window"])
    result = proof((candidate, request, parsed, manifest))
    assert result and result["received_format_sha256"] == digest(candidate["format_chapters"][0])


def test_other_true_format_in_the_same_window_is_preserved_and_keeps_root_specified(tmp_path):
    case = source_case(tmp_path, count=5, boundary="9 独立授权函")
    candidate = case[0]
    candidate["format_chapters"].append({"title": "9 独立授权函", "level": 1,
        "citation": {"block_ids": ["BOUNDARY"]}, "content_block_ids": ["BOUNDARY", "NEXT-BODY"],
        "requirement_indices": [], "status": "prescribed"})
    before = copy.deepcopy(case)
    result = proof(case)
    assert result and result["expected_format_status"] == "specified"
    assert result["boundary"]["block_id"] == "BOUNDARY"
    assert case == before


def test_the_selected_duplicate_may_be_at_a_nonzero_format_index(tmp_path):
    case = source_case(tmp_path, count=5, boundary="9 独立授权函")
    candidate = case[0]
    candidate["format_chapters"].insert(0, {"title": "9 独立授权函", "level": 1,
        "citation": {"block_ids": ["BOUNDARY"]}, "content_block_ids": ["BOUNDARY", "NEXT-BODY"],
        "requirement_indices": [], "status": "prescribed"})
    result = proof(case, 1)
    assert result and result["format_index"] == 1 and result["expected_format_status"] == "specified"


def test_all_original_same_title_indices_and_full_text_links_survive_in_proof(tmp_path):
    case = source_case(tmp_path)
    candidate = case[0]
    candidate["requirements"].append(copy.deepcopy(candidate["requirements"][0]))
    candidate["requirements"][1]["confidence"] = "low"
    candidate["format_chapters"][0]["requirement_indices"] = [1, 0]
    result = proof(case)
    assert result and result["original_requirement_indices"] == [1, 0]
    assert result["retained_requirement_indices"] == [0, 1]
    assert [item["requirement_index"] for item in result["retained_links"]] == [0, 1]


def test_remote_original_condition_changes_proof_even_when_the_current_window_is_identical(tmp_path):
    case = source_case(tmp_path, count=8, max_blocks=10)
    before = proof(case)
    assert before
    # I1 is beyond this window's six backward context lines.
    values(case[2])["I1"]["text"] = "1. 我方保留变更后的原文条件；"
    after_case = rebuild(case)
    assert after_case[1] == case[1]
    after = proof(after_case)
    assert after and before["full_format_members_sha256"] != after["full_format_members_sha256"]
    assert before["proof_sha256"] != after["proof_sha256"]


def test_adjacent_unresolved_handoff_remains_present_and_is_not_resolved_by_this_proof(tmp_path):
    case = source_case(tmp_path)
    candidate, request, _, _ = case
    candidate["unresolved_block_ids"] = ["I4"]
    assert "I4" in {block["block_id"] for block in request["window"]["adjacent_context"]}
    assert proof(case)
    assert candidate["unresolved_block_ids"] == ["I4"]


@pytest.mark.parametrize("damage", ["no_title", "same_window_title", "title_synonym", "title_space", "non_letter",
    "no_introducer", "no_one", "number_gap", "number_duplicate", "marker_change", "unknown_heading", "new_letter",
    "table", "image", "ocr_anchor", "ocr_body", "unread", "unmapped", "read_error", "unit_missing",
    "foreign_hash", "foreign_role", "false_image_zero", "image_unrepresented", "line_gap", "page_gap", "no_region",
    "unit_locator", "expected_units_bool"])
def test_unproved_or_interrupted_source_never_permits_removing_a_format(tmp_path, damage):
    case = source_case(tmp_path)
    candidate, request, parsed, manifest = case
    by_id = values(parsed)
    if damage == "no_title": by_id["TITLE"]["text"] = "27 其他说明"
    elif damage == "same_window_title":
        return_case = source_case(tmp_path, max_blocks=60)
        assert proof(return_case) is None
        return
    elif damage == "title_synonym": by_id["TITLE"]["text"] = "27 承诺函"
    elif damage == "title_space": by_id["TITLE"]["text"] = "27  声明函"
    elif damage == "non_letter": by_id["TITLE"]["text"] = "27 技术服务方案"
    elif damage == "no_introducer": by_id["INTRO"]["text"] = "以下是相关内容。"
    elif damage == "no_one": by_id["I1"]["text"] = "我方保留原文规定的条件；"
    elif damage == "number_gap": by_id["I3"]["text"] = "9. 我方保留原文规定的条件；"
    elif damage == "number_duplicate": by_id["I3"]["text"] = "2. 我方保留原文规定的条件；"
    elif damage == "marker_change": by_id["I3"]["text"] = "3、我方保留原文规定的条件；"
    elif damage in {"unknown_heading", "new_letter"}: by_id["I4"]["text"] = "4. 未知边界" if damage == "unknown_heading" else "4. 另一声明函"
    elif damage in {"table", "image"}:
        by_id["I4"]["kind"] = damage
        by_id["I4"]["locator"] = f"pdf/page[1]/{damage}[0]"
        if damage == "table": by_id["I4"].update(rows=[["原表", "值"], ["", ""]], merges=[], header_rows=[], column_widths=[1, 1])
    elif damage in {"ocr_anchor", "ocr_body"}: by_id["TITLE" if damage == "ocr_anchor" else "I3"]["origin"] = "ocr"
    elif damage in {"unread", "unmapped", "read_error", "false_image_zero", "image_unrepresented"}:
        coverage = parsed["documents"][0]["units"][1]["coverage"]
        if damage == "unread": coverage["status"] = "needs_ocr"
        elif damage == "unmapped": coverage["unmapped_glyphs"] = ["unknown"]
        elif damage == "read_error": coverage["error_code"] = "READ_FAILED"
        elif damage == "false_image_zero": coverage["image_count"] = False
        else: coverage["image_count"] = 1
    elif damage == "unit_missing": parsed["documents"][0]["expected_units"] += 1
    elif damage == "foreign_hash": manifest["sources"][0]["sha256"] = "b" * 64
    elif damage == "foreign_role": parsed["documents"][0]["role"] = "reference_bid"
    elif damage == "unit_locator": parsed["documents"][0]["units"][1]["locator"] = "pdf/page[99]"
    elif damage == "expected_units_bool": parsed["documents"][0]["expected_units"] = True
    elif damage == "line_gap": by_id["I3"]["locator"] = "pdf/page[1]/line[99]"
    elif damage == "page_gap":
        parsed["documents"][0]["units"][1]["page"] = 5
        for value in parsed["documents"][0]["units"][1]["blocks"]:
            value["locator"] = value["locator"].replace("page[2]", "page[5]")
    else: by_id["REGION"]["text"] = "第七章 其他合同规则"
    try:
        case = rebuild(case)
    except (PlanningError, StopIteration, ValueError):
        pass
    assert proof(case) is None


def test_a_second_exact_source_title_is_ambiguous_even_with_different_body(tmp_path):
    case = source_case(tmp_path)
    values(case[2])["NEXT-BODY"]["text"] = "27 声明函"
    assert proof(rebuild(case)) is None


@pytest.mark.parametrize("damage", ["format_own_content", "format_own_citation", "req_title", "req_route",
    "req_status", "req_label", "format_status", "format_clause", "format_row", "indices_empty", "indices_unknown",
    "indices_duplicate", "content_duplicate", "content_reverse", "unresolved", "wrong_window", "schema",
    "second_false_chapter", "current_true_anchor", "title_fake_clause"])
def test_every_deleted_content_and_link_must_survive_in_unchanged_legal_requirements(tmp_path, damage):
    case = source_case(tmp_path)
    candidate, request, _, _ = case
    requirement = candidate["requirements"][0]
    chapter = candidate["format_chapters"][0]
    if damage in {"format_own_content", "format_own_citation"}:
        requirement["citation"]["block_ids"] = ["CLOSE", "SIGN", "DATE"]
        if damage == "format_own_content": chapter["citation"]["block_ids"] = ["CLOSE", "SIGN", "DATE"]
    elif damage == "req_title": requirement["format_title"] = None
    elif damage == "req_route": requirement.update(category="procedure", response_mode="procedure")
    elif damage == "req_status": requirement["format_status"] = "not_prescribed"
    elif damage == "req_label": requirement["required_fact_labels"] = ["不存在的原字"]
    elif damage == "format_status": chapter["status"] = "unread"
    elif damage == "format_clause": chapter["citation"]["clause_id"] = "forged"
    elif damage == "format_row": chapter["citation"]["table_row_id"] = "forged"
    elif damage == "indices_empty": chapter["requirement_indices"] = []
    elif damage == "indices_unknown": chapter["requirement_indices"] = [1]
    elif damage == "indices_duplicate": chapter["requirement_indices"] = [0, 0]
    elif damage == "content_duplicate": chapter["content_block_ids"].append("SIGN")
    elif damage == "content_reverse": chapter["content_block_ids"].reverse()
    elif damage == "unresolved": candidate["unresolved_block_ids"] = ["I5"]
    elif damage == "wrong_window": candidate["window_id"] = "foreign-window"
    elif damage == "schema": requirement["unexpected"] = "unknown"
    elif damage == "second_false_chapter": candidate["format_chapters"].append(copy.deepcopy(chapter))
    elif damage == "current_true_anchor": chapter["citation"]["block_ids"] = ["TITLE"]
    else: chapter["title"] = "6. 未被主管机关列入原文禁止名单"
    assert proof(case) is None


@pytest.mark.parametrize("field", ["task_type", "producer_version", "window_config", "window", "source_sha256", "extra"])
def test_source_request_must_be_the_actual_frozen_production_request(tmp_path, field):
    case = source_case(tmp_path)
    request = case[1]
    if field == "task_type": request[field] = "write_section"
    elif field == "producer_version": request[field] = "foreign"
    elif field == "window_config": request[field]["window_max_blocks"] += 1
    elif field == "window": request[field]["blocks"][0]["text"] += "改写"
    elif field == "source_sha256": request[field] = "b" * 64
    else: request[field] = True
    assert proof(case) is None


@pytest.mark.parametrize("index", [-1, 1, True, None, "0"])
def test_only_one_actual_integer_format_index_can_be_requested(tmp_path, index):
    assert proof(source_case(tmp_path), index) is None


def test_optional_task_type_and_default_fields_are_rebuilt_without_mutation(tmp_path):
    case = source_case(tmp_path)
    case[1].pop("task_type")
    case[0]["format_chapters"][0].pop("status")
    assert proof(case)


def test_shared_fixed_letter_rule_preserves_existing_proof_shape(tmp_path):
    candidate, _, parsed, manifest = source_case(tmp_path)
    blocks, _, _ = _index(parsed, manifest)
    result = fixed_letter_items(list(blocks.values()), {"TITLE": {"title": candidate["format_chapters"][0]["title"], "status": "prescribed"}})
    basis = result["I6"]
    assert basis["rule"] == "fixed_letter_consecutive_declarations_v1"
    assert basis["item_number"] == 6 and basis["previous_item_block_id"] == "I5"
    assert basis["proof_sha256"] == digest({key: value for key, value in basis.items() if key != "proof_sha256"})
