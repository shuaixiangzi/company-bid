"""One already-successful owner can prove one exact adjacent duplicate only."""
from __future__ import annotations

import copy

import pytest

from company_bid.adjacent_owner_sources import owned_adjacent_requirement_proof
from company_bid.extraction import _index, _normalize_source_selectors, _windows, extraction_window_payload
from company_bid.storage import digest
from test_source_selected_citations import block, fixture


def source_case(tmp_path):
    manifest, parsed = fixture(tmp_path)
    document = parsed["documents"][0]
    document["format"] = "pdf"
    lines = ["4.3 本条原义务保留。", "4.4 本条另有独立义务。", "4.5 其他：",
        "4.6 乙方应在每次付款之前开具合法发票；", "未开具时甲方有权延缓付款，但约定例外保留。",
        "4.7 甲方开票信息：", "纳税人名称：", "4.8 其他："]
    for index, text in enumerate(lines):
        value = block(parsed, f"B{index}", text, origin="native", bbox=[90, 80 + index * 20, 500, 93 + index * 20])
        value["locator"] = f"pdf/page[1]/line[{index}]"
    document["units"][0]["coverage"].update(image_count=0, unread_objects=[], unmapped_glyphs=[], error_code=None)
    document["expected_units"] = 1
    config = {"window_max_chars": 14000, "window_max_blocks": 3}
    blocks, _, documents = _index(parsed, manifest)
    windows, _ = _windows(documents, blocks, config)
    current, owner = windows[:2]
    request = {**extraction_window_payload(current, config), "task_type": "extract_requirements"}
    requirement = {"citation": {"block_ids": ["B3", "B4"]}, "category": "commercial", "response_mode": "procedure",
        "format_status": "not_prescribed", "mandatory": True, "confidence": "high"}
    candidate = {"window_id": current["window_id"], "processing_declaration": "processed_not_semantically_accepted",
        "unresolved_block_ids": ["B2"], "requirements": [{**copy.deepcopy(requirement), "citation": {"block_ids": ["B0"]}},
            copy.deepcopy(requirement)], "format_chapters": [], "format_status": "none_verified"}
    owner_candidate = {"window_id": owner["window_id"], "processing_declaration": "processed_not_semantically_accepted",
        "unresolved_block_ids": [], "requirements": [{**copy.deepcopy(requirement), "format_title": None,
            "required_fact_labels": [], "attachment_keywords": [], "reference_keywords": []}],
        "format_chapters": [], "format_status": "none_verified"}
    owner_task = {**extraction_window_payload(owner, config), "task_type": "extract_requirements"}
    binding = {"alias": "owners", "run_id": "a" * 32, "node_key": "N2", "unit_id": owner["window_id"]}
    origin = {"source_alias": binding["alias"], "run_id": binding["run_id"], "node_key": "N2", "unit_id": owner["window_id"],
        "historical_producer_version": "synthetic-n2", "candidate_sha256": digest(owner_candidate),
        "base_request_sha256": digest(owner_task), "actual_final_wire_sha256": "b" * 64, "external_call": False,
        "calls": [{"source_alias": "owners", "run_id": "a" * 32, "call_id": "c" * 32, "call_attempt": 1,
            "trace_id": "d" * 32, "span_id": "e" * 16, "request_sha256": "b" * 64, "response_sha256": "f" * 64}]}
    witness = {"binding": binding, "entry": {"candidate": owner_candidate, "request_sha256": digest(owner_task), "origin": origin},
        "full_window": owner}
    return candidate, request, parsed, manifest, witness


def proof(case):
    return owned_adjacent_requirement_proof(*case)


def refresh(case):
    """Rebuild projections after source edits, never hide source proof faults."""
    candidate, request, parsed, manifest, witness = case
    blocks, _, documents = _index(parsed, manifest)
    windows, _ = _windows(documents, blocks, request["window_config"])
    current = next(window for window in windows if window["window_id"] == candidate["window_id"])
    owner = next(window for window in windows if window["window_id"] == witness["binding"]["unit_id"])
    case = candidate, {**extraction_window_payload(current, request["window_config"]), "task_type": "extract_requirements"}, parsed, manifest, witness
    witness["full_window"] = owner
    owner_task = {**extraction_window_payload(owner, request["window_config"]), "task_type": "extract_requirements"}
    witness["entry"]["request_sha256"] = witness["entry"]["origin"]["base_request_sha256"] = digest(owner_task)
    witness["entry"]["origin"]["candidate_sha256"] = digest(witness["entry"]["candidate"])
    return case


def test_one_complete_owner_requirement_proves_duplicate_without_mutation(tmp_path):
    case = source_case(tmp_path)
    before = copy.deepcopy(case)
    result = proof(case)
    assert result and case == before
    assert result["resolution_kind"] == "source_bound_successful_owner_duplicate_requirement"
    assert result["candidate_index"] == 1 and result["owner_candidate_index"] == 0
    assert result["block_ids"] == ["B3", "B4"] and result["owner_window_id"] != result["window_id"]
    assert result["owner_response_sha256"] == "f" * 64
    assert result["original_typed_item"]["required_fact_labels"] == []
    assert result["original_typed_item"]["reference_keywords"] == []
    normalized, _ = _normalize_source_selectors(case[0], case[1]["window"])
    assert result["received_requirement_sha256"] == digest(case[0]["requirements"][1])
    assert result["original_typed_item"]["citation"]["block_ids"] == normalized["requirements"][1]["citation"]["block_ids"]
    assert result["source_proof"]["clause_number"] == "4.6"
    assert result["source_proof"]["next_boundary"]["block_id"] == "B5"
    assert [member["block_id"] for member in result["source_proof"]["members"]] == ["B3", "B4"]
    assert result["source_proof_sha256"] == digest(result["source_proof"])
    assert result["proof_sha256"] == digest({key: value for key, value in result.items() if key != "proof_sha256"})
    result["original_typed_item"]["confidence"] = "low"
    result["origin"]["calls"].clear()
    assert case == before


@pytest.mark.parametrize("change", ["partial_owner", "partial_current", "both_drop_condition", "both_drop_start", "mixed",
    "different_mode", "different_category", "different_mandatory", "different_confidence", "different_format",
    "different_title", "different_fact", "different_attachment", "ignored_reference", "two_adjacent", "no_adjacent",
    "duplicate_owner", "unresolved_owner", "owner_only_reviewed", "quoted_current", "reviewed_current", "quoted_owner",
    "reversed", "duplicate_id", "unknown_id", "format_link", "different_owner_window", "same_owner_window"])
def test_partial_or_nonidentical_requirements_never_authorize_deletion(tmp_path, change):
    case = source_case(tmp_path)
    candidate, request, parsed, manifest, witness = case
    original = candidate["requirements"][1]
    owner = witness["entry"]["candidate"]
    received = owner["requirements"][0]
    if change == "partial_owner": received["citation"]["block_ids"] = ["B3"]
    elif change == "partial_current": original["citation"]["block_ids"] = ["B3"]
    elif change == "both_drop_condition":
        original["citation"]["block_ids"] = received["citation"]["block_ids"] = ["B3"]
    elif change == "both_drop_start":
        original["citation"]["block_ids"] = received["citation"]["block_ids"] = ["B4"]
    elif change == "mixed": original["citation"]["block_ids"] = ["B2", "B3", "B4"]
    elif change == "different_mode": received["response_mode"] = "factual_response"
    elif change == "different_category": received["category"] = "procedure"
    elif change == "different_mandatory": received["mandatory"] = False
    elif change == "different_confidence": received["confidence"] = "medium"
    elif change == "different_format": received["format_status"] = "unread"
    elif change == "different_title": received["format_title"] = "发票"
    elif change == "different_fact": received["required_fact_labels"] = ["发票"]
    elif change == "different_attachment": received["attachment_keywords"] = ["发票"]
    elif change == "ignored_reference": received["reference_keywords"] = ["发票"]
    elif change == "two_adjacent": candidate["requirements"].append(copy.deepcopy(original))
    elif change == "no_adjacent": candidate["requirements"].pop()
    elif change == "duplicate_owner": owner["requirements"].append(copy.deepcopy(received))
    elif change == "unresolved_owner": owner["unresolved_block_ids"] = ["B3"]
    elif change == "owner_only_reviewed":
        owner["requirements"] = []
        owner["reviewed_block_ids"] = ["B3", "B4", "B5"]
    elif change == "quoted_current": original["citation"]["quote"] = "4.6 乙方应在每次付款之前开具合法发票；"
    elif change == "reviewed_current": candidate["reviewed_block_ids"] = ["B0", "B1", "B2"]
    elif change == "quoted_owner": received["citation"]["quote"] = "4.6 乙方应在每次付款之前开具合法发票；"
    elif change == "reversed": original["citation"]["block_ids"] = ["B4", "B3"]
    elif change == "duplicate_id": original["citation"]["block_ids"] = ["B3", "B4", "B4"]
    elif change == "unknown_id": original["citation"]["block_ids"] = ["UNKNOWN"]
    elif change == "format_link": candidate["format_chapters"] = [{"title": "合成函", "level": 2,
        "citation": {"block_ids": ["B0"]}, "content_block_ids": ["B0"], "requirement_indices": [1], "status": "prescribed"}]
    elif change == "different_owner_window": witness["binding"]["unit_id"] = "wrong-window"
    elif change == "same_owner_window": witness["full_window"] = copy.deepcopy(request["window"])
    witness["entry"]["origin"]["candidate_sha256"] = digest(owner)
    before = copy.deepcopy(case)
    assert proof(case) is None and case == before


@pytest.mark.parametrize("change", ["not_pdf", "incomplete", "unread", "image", "unknown_images", "image_block", "glyph",
    "error", "ignored", "ocr", "line_gap", "unit_locator", "bbox", "missing_bbox", "wrong_boundary", "missing_boundary",
    "source_hash", "required_incomplete", "source_id"])
def test_incomplete_or_non_native_source_cannot_prove_complete_clause(tmp_path, change):
    case = source_case(tmp_path)
    _, _, parsed, manifest, _ = case
    doc = parsed["documents"][0]
    unit = doc["units"][0]
    selected = unit["blocks"][3]
    if change == "not_pdf": doc["format"] = "docx"
    elif change == "incomplete": doc["expected_units"] = 2
    elif change == "unread": unit["coverage"]["status"] = "unread"
    elif change == "image": unit["coverage"]["image_count"] = 1
    elif change == "unknown_images": unit["coverage"].pop("image_count")
    elif change == "image_block": unit["blocks"][4]["kind"] = "image"
    elif change == "glyph": unit["coverage"]["unmapped_glyphs"] = [{"codepoint": "U+E001"}]
    elif change == "error": unit["coverage"]["error_code"] = "READ_ERROR"
    elif change == "ignored": unit["coverage"]["ignored_objects"] = [{"locator": selected["locator"], "reason": "synthetic ignored"}]
    elif change == "ocr": selected["origin"] = "ocr"
    elif change == "line_gap": selected["locator"] = "pdf/page[1]/line[20]"
    elif change == "unit_locator": unit["locator"] = "pdf/page[2]"
    elif change == "bbox": selected["bbox"] = [500, 80, 90, 93]
    elif change == "missing_bbox": selected.pop("bbox")
    elif change == "wrong_boundary": unit["blocks"][5]["text"] = "4.9 甲方开票信息："
    elif change == "missing_boundary": unit["blocks"][5]["text"] = "未能证明边界的普通正文。"
    elif change == "source_hash": doc["sha256"] = manifest["sources"][0]["sha256"] = "0" * 64
    elif change == "required_incomplete": parsed["coverage"]["required_complete"] = False
    elif change == "source_id": selected["source_id"] = "OTHER"
    try:
        case = refresh(case)
    except ValueError:
        assert proof(case) is None
        return
    before = copy.deepcopy(case)
    assert proof(case) is None and case == before


@pytest.mark.parametrize("change", ["wire", "task", "wrong_binding", "origin_identity", "candidate_hash", "request_hash",
    "no_calls", "call_id", "trace", "response_hash", "wire_hash", "owner_projection", "raw_unknown", "final_call_run",
    "final_call_alias", "producer_version"])
def test_witness_consistency_and_frozen_request_are_required(tmp_path, change):
    case = source_case(tmp_path)
    candidate, request, parsed, manifest, witness = case
    entry = witness["entry"]
    if change == "wire": request["task_instructions"] += " altered"
    elif change == "task": request["task_type"] = "other"
    elif change == "wrong_binding": witness["binding"]["run_id"] = "b" * 32
    elif change == "origin_identity": entry["origin"]["unit_id"] = candidate["window_id"]
    elif change == "candidate_hash": entry["origin"]["candidate_sha256"] = "b" * 64
    elif change == "request_hash": entry["request_sha256"] = "b" * 64
    elif change == "no_calls": entry["origin"]["calls"] = []
    elif change == "call_id": entry["origin"]["calls"][0]["call_id"] = "0" * 32
    elif change == "trace": entry["origin"]["calls"][0]["span_id"] = "bad"
    elif change == "response_hash": entry["origin"]["calls"][0]["response_sha256"] = "bad"
    elif change == "wire_hash": entry["origin"]["actual_final_wire_sha256"] = "a" * 64
    elif change == "owner_projection": witness["full_window"]["blocks"][0]["text"] += " altered"
    elif change == "raw_unknown": entry["candidate"]["unexpected"] = True
    elif change == "final_call_run": entry["origin"]["calls"][0]["run_id"] = "b" * 32
    elif change == "final_call_alias": entry["origin"]["calls"][0]["source_alias"] = "another"
    elif change == "producer_version": entry["origin"].pop("historical_producer_version")
    before = copy.deepcopy(case)
    assert proof(case) is None and case == before


def test_next_article_is_a_finite_complete_clause_boundary(tmp_path):
    case = source_case(tmp_path)
    case[2]["documents"][0]["units"][0]["blocks"][5]["text"] = "第五条 下一个完整合同条款"
    result = proof(refresh(case))
    assert result and result["source_proof"]["next_boundary"]["block_id"] == "B5"


def test_same_citation_cannot_hide_multiple_target_owners(tmp_path):
    case = source_case(tmp_path)
    candidate, request, parsed, manifest, witness = case
    request["window_config"]["window_max_blocks"] = 4
    blocks, _, documents = _index(parsed, manifest)
    windows, _ = _windows(documents, blocks, request["window_config"])
    candidate["window_id"] = windows[2]["window_id"] if len(windows) > 2 else windows[1]["window_id"]
    # B3 is now owned by the first window while B4 is owned by the second.
    candidate["requirements"] = [candidate["requirements"][1]]
    candidate["unresolved_block_ids"] = []
    request = {**extraction_window_payload(windows[1], request["window_config"]), "task_type": "extract_requirements"}
    candidate["window_id"] = windows[1]["window_id"]
    assert owned_adjacent_requirement_proof(candidate, request, parsed, manifest, witness) is None


@pytest.mark.parametrize("character", ["\ufffd", "\ue001", "\x01", "\x7f", "\u200b", "\ud800", "\u0378"])
def test_rebuilt_native_owner_source_rejects_unknown_characters(tmp_path, character):
    case = source_case(tmp_path)
    case[2]["documents"][0]["units"][0]["blocks"][4]["text"] += character
    try:
        case = refresh(case)
    except UnicodeEncodeError:
        # Invalid surrogate source text may fail source identity construction
        # before a request exists; it must still never earn a proof.
        assert proof(case) is None
        return
    before = copy.deepcopy(case)
    assert proof(case) is None and case == before


@pytest.mark.parametrize("space", ["\u00a0", "\u3000"])
def test_known_unicode_space_is_preserved_in_rebuilt_owner_proof(tmp_path, space):
    case = source_case(tmp_path)
    case[2]["documents"][0]["units"][0]["blocks"][4]["text"] += space
    result = proof(refresh(case))
    assert result and result["source_proof"]["members"][-1]["text"].endswith(space)
