"""Targeted correction preserves old business items and earns unresolved removal.

The real Executor/provider accounting path runs against synthetic HTTP replies.
No historical candidate is edited and no external request is made.
"""

import copy
import hashlib
import json

import pytest

from company_bid.diagnostics import inspect_diagnostics
from company_bid.extraction import (
    _clause_catalog_view, _index, _model_window, _window_source_catalogs, _windows,
    source_selected_output_contract, validate_live_window,
)
from company_bid.runtime import Executor
from company_bid.source_clauses import build_clause_catalog
from test_repair_semantic_inventory import run_repair
from test_source_clauses import window as clause_source_window
from test_source_selected_citations import block, fixture, live_candidate
from test_structured_validation_repair import artifacts, events, response, setup_run


BAD_TITLE = "未在冻结来源中的错误标题CANARY"
TRUE_TITLE = "7 合成资格证明材料"


def native_line(parsed, identity, text):
    value = block(parsed, identity, text, origin="native")
    value["locator"] = f"pdf/page[1]/line[{len(parsed['documents'][0]['units'][0]['blocks']) - 1}]"
    return value


def title_case(tmp_path):
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"
    native_line(parsed, "PROOF", "响应人应提供合成资格证明资料。")
    native_line(parsed, "FEE", "响应人自行承担服务费，费用上限为50万元。")
    native_line(parsed, "FORM", TRUE_TITLE)
    blocks, _, documents = _index(parsed, manifest)
    window = _windows(documents, blocks, {"window_max_blocks": 2})[0][0]
    assert any(item["title"] == TRUE_TITLE for item in window["source_heading_catalog"])
    first = live_candidate({"window": window}, {"block_ids": ["PROOF"]})
    first["requirements"] = [
        {"citation": {"block_ids": ["PROOF"]}, "category": "qualification", "response_mode": "attachment",
         "format_status": "prescribed", "format_title": BAD_TITLE, "attachment_keywords": ["资格证明资料"]},
        {"citation": {"block_ids": ["FEE"]}, "category": "commercial", "response_mode": "procedure",
         "format_status": "not_prescribed"},
    ]
    second = copy.deepcopy(first)
    second["requirements"][0]["format_title"] = TRUE_TITLE
    return window, first, second


def unresolved_case(tmp_path, *, chapter_status=None):
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"
    if chapter_status is not None:
        native_line(parsed, "FORM", "一、合成声明")
        native_line(parsed, "FIXED", "本合成声明的原始待填格式。")
    native_line(parsed, "FEE", "应按照合同约定支付服务费用。")
    native_line(parsed, "PROOF", "须提供有效检测资质证书。")
    native_line(parsed, "REQUIRED", "未取得书面许可不得作业；费用上限为50万元，")
    native_line(parsed, "TAIL", "费用含税，禁止自行增加工程范围；但原文另有约定的除外，")
    native_line(parsed, "EXCEPTION", "例外情况须经明确书面授权后执行。")
    for index in range(9):
        native_line(parsed, f"FILL{index}", f"其他原始背景说明第{index}段。")
    native_line(parsed, "REMOTE", "第三章 远处其他要求")
    blocks, _, documents = _index(parsed, manifest)
    window = _windows(documents, blocks, {"window_max_blocks": 5 if chapter_status is not None else 3})[0][0]
    expected_targets = {"FEE", "PROOF", "REQUIRED"} | ({"FORM", "FIXED"} if chapter_status is not None else set())
    assert {item["block_id"] for item in window["blocks"]} == expected_targets
    assert {"TAIL", "EXCEPTION"} <= {item["block_id"] for item in window["adjacent_context"]}
    assert "REMOTE" not in {item["block_id"] for item in window["blocks"] + window["adjacent_context"]}
    first = live_candidate({"window": window}, {"block_ids": ["FEE"]})
    first["requirements"] = [
        {"citation": {"block_ids": ["FEE"]}, "category": "commercial", "response_mode": "procedure",
         "format_status": "not_prescribed"},
        {"citation": {"block_ids": ["PROOF"]}, "category": "qualification", "response_mode": "attachment",
         "format_status": "not_prescribed", "attachment_keywords": ["检测资质证书"]},
    ]
    if chapter_status is not None:
        first["format_status"] = "specified"
        first["format_chapters"] = [{"title": "一、合成声明", "level": 1,
            "citation": {"block_ids": ["FORM"]}, "content_block_ids": ["FIXED"],
            "requirement_indices": [], "status": chapter_status}]
    first["unresolved_block_ids"] = ["REQUIRED", "TAIL", "EXCEPTION"]
    second = copy.deepcopy(first)
    second["unresolved_block_ids"] = []
    second["requirements"].append({"citation": {"block_ids": ["REQUIRED", "TAIL", "EXCEPTION"]},
        "category": "commercial", "response_mode": "procedure", "format_status": "not_prescribed"})
    return window, first, second


def assert_two_distinct_settled_calls(state, relation):
    budget = state["call_budget"]
    assert budget["reservation_count"] == 2
    records = budget["reservations"]
    assert len({record["call_id"] for record in records}) == 2
    assert all(record["settlement_status"] == "known_usage_cost_estimate" for record in records)
    assert {relation["first_call_id"], relation["second_call_id"]} == {record["call_id"] for record in records}


@pytest.mark.parametrize("title", [TRUE_TITLE, "合成资格证明资料"], ids=["exact-frozen-heading", "own-selected-source"])
def test_title_correction_changes_only_the_reported_field_and_keeps_the_source_inventory(tmp_path, monkeypatch, title):
    window, first, second = title_case(tmp_path)
    second["requirements"][0]["format_title"] = title
    state, validated, relation, feedback = run_repair(tmp_path, monkeypatch, window, first, second)
    assert state["status"] == "succeeded" and relation["status"] == "completed"
    assert validated == [first, second]
    assert feedback["code"] == "FORMAT_TITLE_NOT_GROUNDED"
    assert feedback["context"] == {"candidate_type": "requirement", "candidate_index": 0, "field": "format_title"}
    assert_two_distinct_settled_calls(state, relation)
    assert BAD_TITLE not in json.dumps(feedback, ensure_ascii=False)


@pytest.mark.parametrize("damage", [
    "delete_title", "null_title", "empty_title", "blank_title", "different_ungrounded_title",
    "other_candidate_title", "category", "mode", "default_mandatory", "default_confidence",
    "legal_labels", "old_source", "delete_item", "append_item", "swap_items", "root_unresolved", "root_format",
])
def test_title_correction_cannot_delete_the_relation_or_rewrite_any_other_legal_field(tmp_path, monkeypatch, damage):
    window, first, second = title_case(tmp_path)
    item = second["requirements"][0]
    if damage == "delete_title":
        item.pop("format_title")
    elif damage == "null_title":
        item["format_title"] = None
    elif damage == "empty_title":
        item["format_title"] = ""
    elif damage == "blank_title":
        item["format_title"] = "   "
    elif damage == "different_ungrounded_title":
        item["format_title"] = "7 合成资格证明资料"
    elif damage == "other_candidate_title":
        second["requirements"][1]["format_title"] = "服务费"
    elif damage == "category":
        item["category"] = "commercial"
    elif damage == "mode":
        item["response_mode"] = "factual_response"
    elif damage == "default_mandatory":
        item["mandatory"] = False
    elif damage == "default_confidence":
        item["confidence"] = "high"
    elif damage == "legal_labels":
        item["attachment_keywords"] = []
    elif damage == "old_source":
        item["citation"]["block_ids"] = ["PROOF", "FEE"]
    elif damage == "delete_item":
        second["requirements"].pop()
    elif damage == "append_item":
        second["requirements"].append(copy.deepcopy(second["requirements"][1]))
    elif damage == "swap_items":
        second["requirements"].reverse()
    elif damage == "root_unresolved":
        second["unresolved_block_ids"] = ["FORM"]
    else:
        second["format_status"] = "conflict"
    state, _, relation, feedback = run_repair(tmp_path, monkeypatch, window, first, second)
    assert state["status"] == "failed"
    assert state["error_code"] in {"VALIDATION_REPAIR_COVERAGE_LOST", "FORMAT_TITLE_NOT_GROUNDED"}
    assert relation["status"] == "failed" and feedback["code"] == "FORMAT_TITLE_NOT_GROUNDED"
    assert_two_distinct_settled_calls(state, relation)


def test_title_failure_does_not_mask_a_later_legacy_citations_wrong_location(tmp_path, monkeypatch):
    window, first, second = title_case(tmp_path)
    source = next(value for value in window["blocks"] if value["block_id"] == "FEE")
    first["requirements"][1]["citation"].update({key: source[key] for key in ("source_id", "block_id", "locator")})
    first["requirements"][1]["citation"]["locator"] = "pdf/page[999]/line[0]"
    state, validated, relation, feedback = run_repair(
        tmp_path, monkeypatch, window, first, second, expected_calls=1)
    assert state["status"] == "failed" and state["error_code"] == "FORMAT_TITLE_NOT_GROUNDED"
    assert validated == [first] and relation is feedback is None


def test_unresolved_target_is_resolved_only_by_an_appended_complete_source_candidate(tmp_path, monkeypatch):
    window, first, second = unresolved_case(tmp_path)
    state, validated, relation, feedback = run_repair(tmp_path, monkeypatch, window, first, second)
    assert state["status"] == "succeeded" and relation["status"] == "completed"
    assert validated == [first, second]
    assert feedback["code"] == "LIVE_REQUIRED_BLOCK_UNRESOLVED"
    assert second["requirements"][:-1] == first["requirements"]
    final = validate_live_window(second, window)
    source = {value["block_id"]: value for value in window["blocks"] + window["adjacent_context"]}
    assert final["requirements"][-1]["citation"]["quote"] == "\n".join(
        source[key]["text"] for key in ["REQUIRED", "TAIL", "EXCEPTION"])
    assert "不得作业" in final["requirements"][-1]["citation"]["quote"]
    assert "50万元" in final["requirements"][-1]["citation"]["quote"]
    assert "费用含税" in final["requirements"][-1]["citation"]["quote"]
    assert "但原文另有约定的除外" in final["requirements"][-1]["citation"]["quote"]
    assert_two_distinct_settled_calls(state, relation)


@pytest.mark.parametrize("damage", [
    "clear_without_candidate", "omit_target", "omit_continuation", "omit_exception", "context_only_id",
    "unknown_source", "change_old_business", "drop_old_labels", "drop_old_item", "insert_before_old",
    "swap_old_items", "change_old_source", "too_many_new_items", "keep_required_unresolved", "add_format",
])
def test_unresolved_correction_cannot_trade_old_business_or_clear_unearned_source_ids(tmp_path, monkeypatch, damage):
    window, first, second = unresolved_case(tmp_path)
    added = second["requirements"][-1]
    if damage == "clear_without_candidate":
        second["requirements"].pop()
    elif damage == "omit_target":
        added["citation"]["block_ids"] = ["TAIL", "EXCEPTION"]
    elif damage == "omit_continuation":
        added["citation"]["block_ids"] = ["REQUIRED", "EXCEPTION"]
    elif damage == "omit_exception":
        added["citation"]["block_ids"] = ["REQUIRED", "TAIL"]
    elif damage == "context_only_id":
        added["citation"]["block_ids"].append("REMOTE")
    elif damage == "unknown_source":
        added["citation"].update(source_id="FOREIGN", block_id="REQUIRED", locator="pdf/page[1]/line[2]")
    elif damage == "change_old_business":
        second["requirements"][0]["category"] = "procedure"
    elif damage == "drop_old_labels":
        second["requirements"][1]["attachment_keywords"] = []
    elif damage == "drop_old_item":
        second["requirements"].pop(0)
    elif damage == "insert_before_old":
        second["requirements"].insert(0, second["requirements"].pop())
    elif damage == "swap_old_items":
        second["requirements"][:2] = reversed(second["requirements"][:2])
    elif damage == "change_old_source":
        second["requirements"][0]["citation"]["block_ids"] = ["PROOF"]
    elif damage == "too_many_new_items":
        second["requirements"].append(copy.deepcopy(added))
    elif damage == "keep_required_unresolved":
        second["unresolved_block_ids"] = list(first["unresolved_block_ids"])
    else:
        second["format_status"] = "specified"
        second["format_chapters"] = [{"title": "合成格式", "level": 1, "citation": {"block_ids": ["REQUIRED"]},
            "content_block_ids": ["REQUIRED"], "requirement_indices": []}]
    state, _, relation, feedback = run_repair(tmp_path, monkeypatch, window, first, second)
    assert state["status"] == "failed"
    assert state["error_code"] in {"VALIDATION_REPAIR_COVERAGE_LOST", "LIVE_REQUIRED_BLOCK_UNRESOLVED",
                                    "CITATION_SOURCE_MISMATCH", "CITATION_SOURCE_INVALID", "CITATION_LOCATION_INVALID"}
    assert relation["status"] == "failed" and feedback["code"] == "LIVE_REQUIRED_BLOCK_UNRESOLVED"
    assert_two_distinct_settled_calls(state, relation)


@pytest.mark.parametrize("damage", [
    "ocr", "image", "empty_text", "read_disposition", "different_page", "ordinal_gap",
    "missing_middle_id", "foreign_source", "root_unread", "root_conflict", "old_unread", "invalid_old_schema",
    "wrong_old_locator", "noncontiguous_old_source", "ungrounded_old_title",
])
def test_unresolved_admission_requires_safe_native_source_and_valid_old_inventory(tmp_path, monkeypatch, damage):
    window, first, second = unresolved_case(tmp_path)
    source = {value["block_id"]: value for value in window["blocks"] + window["adjacent_context"]}
    tail = source["TAIL"]
    expected_code = "LIVE_REQUIRED_BLOCK_UNRESOLVED"
    if damage == "ocr":
        tail["origin"] = "ocr"
    elif damage == "image":
        tail["kind"] = "image"
    elif damage == "empty_text":
        tail["text"] = ""
    elif damage == "read_disposition":
        tail["read_disposition"] = {"status": "unread"}
    elif damage == "different_page":
        tail.update(page=2, locator="pdf/page[2]/line[0]")
    elif damage == "ordinal_gap":
        tail["ordinal"] += 10
    elif damage == "missing_middle_id":
        first["unresolved_block_ids"] = ["REQUIRED", "EXCEPTION"]
    elif damage == "foreign_source":
        tail["source_id"] = "FOREIGN"
    elif damage == "root_unread":
        first["format_status"] = "unread"
    elif damage == "root_conflict":
        first["format_status"] = "conflict"
    elif damage == "old_unread":
        first["requirements"][0]["format_status"] = "unread"
    elif damage == "invalid_old_schema":
        first["requirements"][0]["response_mode"] = "procedure_typo"
        expected_code = "EXTRACTION_SCHEMA_INVALID"
    elif damage == "wrong_old_locator":
        first["requirements"][0]["citation"].update(
            source_id="T", block_id="FEE", locator="pdf/page[1]/line[999]")
    elif damage == "noncontiguous_old_source":
        first["requirements"][0]["citation"]["block_ids"] = ["FEE", "REQUIRED"]
    else:
        first["requirements"][0]["format_title"] = BAD_TITLE
    state, validated, relation, feedback = run_repair(
        tmp_path, monkeypatch, window, first, second, expected_calls=1)
    assert state["status"] == "failed" and state["error_code"] == expected_code
    assert validated == [first] and relation is feedback is None


def test_unresolved_enumeration_order_does_not_change_the_frozen_source_order(tmp_path, monkeypatch):
    window, first, second = unresolved_case(tmp_path)
    first["unresolved_block_ids"] = ["TAIL", "REQUIRED", "EXCEPTION"]
    frozen = copy.deepcopy(first)
    state, validated, relation, feedback = run_repair(tmp_path, monkeypatch, window, first, second)
    assert state["status"] == "succeeded" and relation["status"] == "completed"
    assert first == frozen and validated == [first, second]
    assert second["requirements"][-1]["citation"]["block_ids"] == ["REQUIRED", "TAIL", "EXCEPTION"]
    assert feedback["code"] == "LIVE_REQUIRED_BLOCK_UNRESOLVED"


@pytest.mark.parametrize("mode", [None, "docx"], ids=["missing-source-mode", "non-pdf-source-mode"])
def test_native_line_looking_locators_do_not_replace_an_explicit_pdf_source_mode(tmp_path, monkeypatch, mode):
    window, first, second = unresolved_case(tmp_path)
    if mode is None:
        window.pop("source_format")
    else:
        window["source_format"] = mode
    state, validated, relation, feedback = run_repair(
        tmp_path, monkeypatch, window, first, second, expected_calls=1)
    assert state["status"] == "failed" and state["error_code"] == "LIVE_REQUIRED_BLOCK_UNRESOLVED"
    assert validated == [first] and relation is feedback is None


@pytest.mark.parametrize("status", ["unread", "conflict"])
def test_an_unresolved_or_conflicting_existing_format_cannot_buy_a_continuation_correction(tmp_path, monkeypatch, status):
    window, first, second = unresolved_case(tmp_path, chapter_status=status)
    _window_source_catalogs(window)  # The source and its real format blocks are valid.
    state, validated, relation, feedback = run_repair(
        tmp_path, monkeypatch, window, first, second, expected_calls=1)
    assert state["status"] == "failed" and state["error_code"] == "LIVE_REQUIRED_BLOCK_UNRESOLVED"
    assert validated == [first] and relation is feedback is None


def test_unresolved_target_cannot_mask_an_old_real_clause_with_an_illegal_business_route(tmp_path, monkeypatch):
    window = clause_source_window()
    window["blocks"].append({"block_id": "REQUIRED", "source_id": "S", "locator": "pdf/page[1]/line[4]",
        "page": 1, "unit_id": "U1", "ordinal": 4, "kind": "paragraph", "origin": "native", "text": "费用不得超过50万元，"})
    window["adjacent_context"] = [{"block_id": "TAIL", "source_id": "S", "locator": "pdf/page[1]/line[5]",
        "page": 1, "unit_id": "U1", "ordinal": 5, "kind": "paragraph", "origin": "native", "text": "但原文明确另有约定的除外。"}]
    clauses = build_clause_catalog(window)
    window["source_clause_catalog"] = _clause_catalog_view(clauses)
    _window_source_catalogs(window)  # Genuine reconstructed source proof, no invented catalog entry.
    clause = clauses[0]
    first = live_candidate({"window": window}, {"block_ids": clause["block_ids"], "clause_id": clause["clause_id"]})
    first["requirements"][0].update(category="commercial", response_mode="procedure")
    first["unresolved_block_ids"] = ["REQUIRED", "TAIL"]
    second = copy.deepcopy(first)
    second["unresolved_block_ids"] = []
    second["requirements"].append({"citation": {"block_ids": ["REQUIRED", "TAIL"]},
        "category": "commercial", "response_mode": "procedure", "format_status": "not_prescribed"})
    state, validated, relation, feedback = run_repair(
        tmp_path, monkeypatch, window, first, second, expected_calls=1)
    assert state["status"] == "failed" and state["error_code"] == "LIVE_REQUIRED_BLOCK_UNRESOLVED"
    assert validated == [first] and relation is feedback is None


def test_pure_adjacent_deferral_is_not_a_target_failure_or_a_reason_for_correction(tmp_path, monkeypatch):
    window, first, second = unresolved_case(tmp_path)
    first["unresolved_block_ids"] = ["TAIL", "EXCEPTION"]
    state, validated, relation, feedback = run_repair(
        tmp_path, monkeypatch, window, first, second, expected_calls=1)
    assert state["status"] == "succeeded" and validated == [first]
    assert relation is feedback is None


def test_two_call_accounting_trace_and_raw_responses_are_closed_without_a_third_request(tmp_path, monkeypatch):
    window, first, second = unresolved_case(tmp_path)
    originals = copy.deepcopy((first, second))
    executor, rid, _, payload, _, sent, validated, _ = setup_run(
        tmp_path, monkeypatch, max_input_chars=50000,
        validate=lambda value, _: validate_live_window(value, window),
        transport=lambda _, count: response(first if count == 1 else second))
    payload.update(window=_model_window(window), output_contract=source_selected_output_contract())
    state = executor.execute(rid)
    assert state["status"] == "succeeded" and len(sent) == 2 and validated == [first, second]
    assert (first, second) == originals
    captured = artifacts(executor, state)
    raw = [json.loads(value["choices"][0]["message"]["content"]) for record, value in captured
           if record["name"].startswith("response-")]
    assert raw == [first, second]
    relation = next(value for record, value in captured if record["name"].startswith("validation-repair-result-"))
    assert_two_distinct_settled_calls(state, relation)
    report = inspect_diagnostics(tmp_path, rid)
    assert report["actual_call_attempts"] == 2 and not report["gaps"]
    assert [call["outcome"] for call in report["calls"]] == ["failed", "completed"]
    assert sum(row["event"] == "call.retry_scheduled" for row in events(executor, rid)) == 1
    assert executor.resume(rid)["status"] == "succeeded" and len(sent) == 2


@pytest.mark.parametrize("restart", [False, True], ids=["same-executor", "reloaded-executor"])
def test_failed_targeted_correction_cannot_buy_a_third_response_on_resume(tmp_path, monkeypatch, restart):
    window, first, second = unresolved_case(tmp_path)
    second["requirements"].pop()  # Clearing the unresolved list has earned no resolution.
    originals = copy.deepcopy((first, second))
    executor, rid, _, payload, _, sent, _, _ = setup_run(
        tmp_path, monkeypatch, max_input_chars=50000,
        validate=lambda value, _: validate_live_window(value, window),
        transport=lambda _, count: response(first if count == 1 else second))
    payload.update(window=_model_window(window), output_contract=source_selected_output_contract())
    initial = executor.execute(rid)
    assert initial["status"] == "failed" and initial["error_code"] == "VALIDATION_REPAIR_COVERAGE_LOST"
    assert len(sent) == initial["call_budget"]["reservation_count"] == 2
    captured = artifacts(executor, initial)
    sealed = [(record["ref"], record["hash"]) for record, _ in captured]
    reservations = copy.deepcopy(initial["call_budget"]["reservations"])
    if restart:
        executor = Executor(executor.store.root, executor.nodes, executor.execution_config)
    resumed = executor.resume(rid)
    assert resumed["status"] == "failed" and resumed["error_code"] == "VALIDATION_REPAIR_ALREADY_ATTEMPTED"
    assert len(sent) == resumed["call_budget"]["reservation_count"] == 2
    assert resumed["call_budget"]["reservations"] == reservations
    assert (first, second) == originals
    for ref, digest in sealed:
        assert hashlib.sha256((executor.store.root / ref["path"]).read_bytes()).hexdigest() == digest
    after = [(record, json.loads(executor.store.read_bytes(record["ref"])))
             for node in resumed["nodes"] for attempt in node["attempts"]
             for record in attempt.get("captures", []) if record.get("ref")]
    raw = [json.loads(value["choices"][0]["message"]["content"]) for record, value in after
           if record["name"].startswith("response-")]
    assert raw == [first, second]
    report = inspect_diagnostics(tmp_path, rid)
    assert report["actual_call_attempts"] == 2 and not report["gaps"]
    assert sum(row["event"] == "call.retry_scheduled" for row in events(executor, rid)) == 1
