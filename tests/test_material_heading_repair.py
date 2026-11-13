"""Pending material categories require a complete native format hierarchy.

All provider replies and credentials are synthetic; no historical run or
external model is used by these tests.
"""
import copy
import hashlib
import json

import pytest

from company_bid.extraction import _index, _windows, _normalize_source_selectors, extraction_window_payload, validate_live_window
from company_bid.runtime import Executor
from company_bid.storage import digest
from company_bid.structured import (
    _MATERIAL_RESPONSE_FIELDS, _material_heading_resolution_groups, _repair_coverage,
    _validation_repair_request,
)
from test_repair_semantic_inventory import run_repair
from test_source_selected_citations import block, fixture, live_candidate
from test_structured_validation_repair import artifacts, response, setup_run
from test_targeted_validation_repair import assert_two_distinct_settled_calls


def material_case(tmp_path):
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"
    def line(identity, text):
        value = block(parsed, identity, text, origin="native")
        paragraphs = [b for b in parsed["documents"][0]["units"][0]["blocks"] if b["kind"] == "paragraph"]
        value["locator"] = f"pdf/page[1]/line[{len(paragraphs) - 1}]"
        return value
    def table(identity, rows):
        value = block(parsed, identity, "", kind="table", origin="native", rows=rows,
                      merges=[], column_widths=[1] * len(rows[0]), header_rows=[])
        tables = [b for b in parsed["documents"][0]["units"][0]["blocks"] if b["kind"] == "table"]
        value["locator"] = f"pdf/page[1]/table[{len(tables) - 1}]"
        return value
    line("REGION", "第五章 响应文件要求及格式")
    names = ["4 合成报价明细表", "5 合成商务偏离表", "6 合成技术偏离表"]
    for index, title in enumerate(names):
        line(f"F{index}", title)
        table(f"T{index}", [["项目", "报价"], ["", ""]])
    line("PARENT", "7 资格审查材料")
    line("NOTE", "注：所有材料按照“第三部分 询比要求”中要求提供")
    line("H1", "7.1、资质材料：")
    line("H2", "7.2、业绩材料：")
    line("CAPTION", "响应人业绩统计表（后附业绩材料）")
    table("PERFORMANCE", [["序号", "项目名称", "服务日期"], ["", "", ""]])
    line("H3", "7.3、财务要求材料：")
    line("H4", "7.4、信誉要求材料：")
    line("H5", "7.5、人员要求材料：")
    line("H6", "7.6、其他材料：")
    line("RELATED", "其中单位负责人为同一人或者存在控股、管理关系的不同单位，不得参加同一标")
    line("TAIL", "段采购项目或者未划分标段的同一采购项目，格式如下：")
    line("NEXT", "8 技术服务方案")
    blocks, _, documents = _index(parsed, manifest)
    window = _windows(documents, blocks, {"window_max_blocks": 14})[0][0]
    assert {b["block_id"] for b in window["blocks"]} == {
        "REGION", "F0", "T0", "F1", "T1", "F2", "T2", "PARENT", "NOTE", "H1", "H2", "CAPTION", "PERFORMANCE", "H3"}
    first = live_candidate({"window": window}, {"block_ids": ["F0", "T0"]})
    first["requirements"], first["format_chapters"] = [], []
    for index, title in enumerate(names):
        first["requirements"].append({"citation": {"block_ids": [f"F{index}", f"T{index}"]},
            "category": "fixed_table", "response_mode": "table", "format_status": "prescribed", "format_title": title})
        first["format_chapters"].append({"title": title, "level": 1,
            "citation": {"block_ids": [f"F{index}", f"T{index}"]}, "content_block_ids": [f"F{index}", f"T{index}"],
            "requirement_indices": [index], "status": "prescribed"})
    first["requirements"].extend([
        {"citation": {"block_ids": ["PARENT", "NOTE"]}, "category": "qualification", "response_mode": "attachment", "format_status": "not_prescribed"},
        {"citation": {"block_ids": ["H2", "CAPTION", "PERFORMANCE"]}, "category": "qualification", "response_mode": "table",
         "format_status": "prescribed", "format_title": "响应人业绩统计表（后附业绩材料）", "attachment_keywords": ["业绩材料"]},
    ])
    first["format_chapters"].append({"title": "响应人业绩统计表（后附业绩材料）", "level": 2,
        "citation": {"block_ids": ["CAPTION", "PERFORMANCE"]}, "content_block_ids": ["CAPTION", "PERFORMANCE"],
        "requirement_indices": [4], "status": "prescribed"})
    first["format_status"] = "specified"
    first["unresolved_block_ids"] = ["H1", "H3", "H4", "H5", "H6", "RELATED", "TAIL"]
    second = copy.deepcopy(first)
    second["unresolved_block_ids"] = ["H4", "H5", "H6", "RELATED", "TAIL"]
    for identity in ("H1", "H3"):
        second["requirements"].append({"citation": {"block_ids": [identity], "clause_id": None, "table_row_id": None},
                                       **copy.deepcopy(_MATERIAL_RESPONSE_FIELDS)})
    return window, first, second


def coverage(window, first):
    request = extraction_window_payload(window, {}) if "source_sha256" in window else {"window": window}
    return _repair_coverage(first, request, error_code="LIVE_REQUIRED_BLOCK_UNRESOLVED",
                            diagnostic_context={"field": "unresolved_block_ids"})


def test_complete_format_hierarchy_earns_two_pending_material_responses(tmp_path, monkeypatch):
    window, first, second = material_case(tmp_path)
    frozen = copy.deepcopy((window, first, second))
    state, validated, relation, feedback = run_repair(tmp_path, monkeypatch, window, first, second)
    assert state["status"] == "succeeded", state.get("error_code")
    assert relation["schema_version"] == "validation-repair-v7" and relation["status"] == "completed"
    assert validated == [first, second] and len(second["requirements"]) == 7 and len(second["format_chapters"]) == 4
    assert_two_distinct_settled_calls(state, relation)
    groups = feedback["required_coverage"]["resolution_groups"]
    assert _material_heading_resolution_groups(_normalize_source_selectors(first, window)[0], window) == groups
    assert [group["block_ids"] for group in groups] == [["H1"], ["H3"]]
    assert all(group["resolution_kind"] == "qualification_material_category_heading" for group in groups)
    for group in groups:
        proof = group["source_proof"]
        assert group["source_proof_sha256"] == digest(proof)
        assert proof["source_sha256"] == window["source_sha256"]
        assert proof["reference_title"] == "第三部分 询比要求" and proof["reference_resolution_status"] == "not_resolved"
        assert proof["semantic_review"] == "requires_source_review"
        assert proof["parent_requirement_index"] == 3 and proof["performance_requirement_index"] == 4
        assert proof["performance_format_index"] == 3
        assert [b["block_id"] for b in proof["blocks"]] == ["PARENT", "NOTE", "H1", "H2", "CAPTION", "PERFORMANCE", "H3", "H4", "H5", "H6"]
    final = validate_live_window(second, window)
    assert [item["citation"]["quote"] for item in final["requirements"][-2:]] == ["7.1、资质材料：", "7.3、财务要求材料："]
    assert all(item["category"] == "qualification" and item["response_mode"] == "factual_response" for item in final["requirements"][-2:])
    assert second["requirements"][:5] == first["requirements"] and second["format_chapters"] == first["format_chapters"]
    assert second["unresolved_block_ids"] == ["H4", "H5", "H6", "RELATED", "TAIL"]
    assert (window, first, second) == frozen
    assert "不能猜证书名称" in feedback["instructions"] and "相邻窗口已处理" in feedback["instructions"]
    assert extraction_window_payload(window, {})["producer_version"] == "requirements-window-v21"


@pytest.mark.parametrize("damage", [
    "docx", "missing_hash", "zero_hash", "ocr", "image", "foreign_source", "unread", "safe_skip", "cross_page",
    "wrong_unit", "unit_gap", "ordinal_gap", "line_gap", "duplicate_id", "no_parent", "duplicate_parent", "wrong_parent",
    "no_note", "note_condition", "no_sibling", "wrong_sibling_number", "unknown_sibling", "substantive_heading",
    "substantive_body", "missing_ancestor", "wrong_ancestor", "intervening_chapter", "bad_ancestor_hash", "bad_parent_hash",
    "foreign_parent_catalog", "wrong_caption", "filled_table", "nonrectangular_table", "table_ocr", "wrong_table_text",
    "missing_parent_item", "parent_is_procedure", "missing_performance_format", "wrong_format_mapping",
    "old_label_error", "old_unread", "root_conflict", "extra_target", "unknown_unresolved", "adjacent_unread",
])
def test_material_admission_requires_the_entire_positive_source_structure(tmp_path, damage):
    window, first, _ = material_case(tmp_path)
    source = {b["block_id"]: b for b in window["blocks"] + window["adjacent_context"]}
    if damage == "docx": window["source_format"] = "docx"
    elif damage == "missing_hash": window.pop("source_sha256")
    elif damage == "zero_hash": window["source_sha256"] = "0" * 64
    elif damage in {"ocr", "image", "foreign_source", "unread", "cross_page", "wrong_unit", "unit_gap", "ordinal_gap", "line_gap"}:
        target = source["H1"]
        if damage == "ocr": target["origin"] = "ocr"
        elif damage == "image": target["kind"] = "image"
        elif damage == "foreign_source": target["source_id"] = "FOREIGN"
        elif damage == "unread": target["read_disposition"] = {"status": "unread"}
        elif damage == "cross_page": target.update(page=2, locator="pdf/page[2]/line[9]")
        elif damage == "wrong_unit": target["unit_id"] = "OTHER"
        elif damage == "unit_gap": target["unit_ordinal"] += 50
        elif damage == "ordinal_gap": target["ordinal"] += 50
        else: target["locator"] = "pdf/page[1]/line[99]"
    elif damage == "safe_skip": window["safe_skip_blocks"].append({"block_id": "H1"})
    elif damage == "duplicate_id": window["adjacent_context"].append(copy.deepcopy(source["H1"]))
    elif damage in {"no_parent", "no_note", "no_sibling"}:
        identity = {"no_parent": "PARENT", "no_note": "NOTE", "no_sibling": "H5"}[damage]
        window["blocks"] = [b for b in window["blocks"] if b["block_id"] != identity]
        window["adjacent_context"] = [b for b in window["adjacent_context"] if b["block_id"] != identity]
    elif damage == "duplicate_parent":
        value = copy.deepcopy(source["PARENT"]); value["block_id"] = "DUPLICATE"; window["adjacent_context"].append(value)
    elif damage == "wrong_parent": source["PARENT"]["text"] = "7 普通材料"
    elif damage == "note_condition": source["NOTE"]["text"] += "，但财务材料无需提供。"
    elif damage == "wrong_sibling_number": source["H4"]["text"] = "8.4、信誉要求材料："
    elif damage == "unknown_sibling": source["H4"]["text"] = "7.4、未知材料："
    elif damage == "substantive_heading": source["H1"]["text"] = "7.1、资质材料：须提供二级证书。"
    elif damage == "substantive_body":
        value = copy.deepcopy(source["H1"]); value.update(block_id="BODY", text="必须提供有效二级证书。")
        window["adjacent_context"].append(value)
    elif damage in {"missing_ancestor", "wrong_ancestor", "intervening_chapter", "bad_ancestor_hash", "bad_parent_hash", "foreign_parent_catalog"}:
        if damage == "missing_ancestor": window["source_heading_catalog"] = [x for x in window["source_heading_catalog"] if x["block_id"] != "REGION"]
        elif damage == "intervening_chapter":
            entry = copy.deepcopy(next(x for x in window["source_heading_catalog"] if x["block_id"] == "REGION"))
            entry.update(block_id="OTHER_CHAPTER", title="第六章 合同条款", locator=source["F2"]["locator"], quote_sha256=hashlib.sha256("第六章 合同条款".encode()).hexdigest())
            window["source_heading_catalog"].append(entry)
        else:
            identity = "PARENT" if damage in {"bad_parent_hash", "foreign_parent_catalog"} else "REGION"
            entry = next(x for x in window["source_heading_catalog"] if x["block_id"] == identity)
            if damage == "wrong_ancestor": entry["title"] = "第五章 普通资料"
            elif damage == "foreign_parent_catalog": entry["source_id"] = "FOREIGN"
            else: entry["quote_sha256"] = "0" * 64
    elif damage == "wrong_caption": source["CAPTION"]["text"] = "通用统计表（资料待补）"
    elif damage == "filled_table":
        source["PERFORMANCE"]["rows"][1][1] = "已完成真实项目"
        source["PERFORMANCE"]["text"] = "\n".join("\t".join(row) for row in source["PERFORMANCE"]["rows"])
    elif damage == "nonrectangular_table": source["PERFORMANCE"]["rows"][1].pop()
    elif damage == "table_ocr": source["PERFORMANCE"]["origin"] = "ocr"
    elif damage == "wrong_table_text": source["PERFORMANCE"]["text"] += "changed"
    elif damage == "missing_parent_item": first["requirements"].pop(3)
    elif damage == "parent_is_procedure": first["requirements"][3].update(category="procedure", response_mode="procedure")
    elif damage == "missing_performance_format": first["format_chapters"].pop()
    elif damage == "wrong_format_mapping": first["format_chapters"][-1]["requirement_indices"] = [0]
    elif damage == "old_label_error": first["requirements"][0]["required_fact_labels"] = ["source-not-present"]
    elif damage == "old_unread": first["requirements"][0]["format_status"] = "unread"
    elif damage == "root_conflict": first["format_status"] = "conflict"
    elif damage == "extra_target": first["unresolved_block_ids"].append("F0")
    elif damage == "unknown_unresolved": first["unresolved_block_ids"].append("UNKNOWN")
    else: source["RELATED"]["read_disposition"] = {"status": "unread"}
    assert coverage(window, first) is None


@pytest.mark.parametrize("damage", [
    "just_clear", "drop_old", "swap_old", "change_old_default", "change_old_source", "change_old_mode", "drop_old_format",
    "change_format_content", "change_format_mapping", "only_one_append", "extra_append", "swap_new", "combine_titles",
    "include_parent", "include_adjacent", "clear_adjacent", "reorder_adjacent", "keep_critical", "procedure", "attachment",
    "optional", "high_confidence", "prescribed", "invented_title", "fact_labels", "proof_keywords", "reference_keywords",
    "missing_explicit_field",
])
def test_material_correction_preserves_every_old_item_and_owner_window_responsibility(tmp_path, monkeypatch, damage):
    window, first, second = material_case(tmp_path)
    item = second["requirements"][-2]
    if damage == "just_clear": second["requirements"] = second["requirements"][:5]
    elif damage == "drop_old": second["requirements"].pop(0)
    elif damage == "swap_old": second["requirements"][:2] = reversed(second["requirements"][:2])
    elif damage == "change_old_default": second["requirements"][0]["mandatory"] = False
    elif damage == "change_old_source": second["requirements"][0]["citation"]["block_ids"] = ["F1", "T1"]
    elif damage == "change_old_mode": second["requirements"][3].update(category="procedure", response_mode="procedure")
    elif damage == "drop_old_format": second["format_chapters"].pop(0)
    elif damage == "change_format_content": second["format_chapters"][-1]["content_block_ids"].pop()
    elif damage == "change_format_mapping": second["format_chapters"][-1]["requirement_indices"] = [0]
    elif damage == "only_one_append": second["requirements"].pop()
    elif damage == "extra_append": second["requirements"].append(copy.deepcopy(item))
    elif damage == "swap_new": second["requirements"][-2:] = reversed(second["requirements"][-2:])
    elif damage == "combine_titles": item["citation"]["block_ids"] = ["H1", "H3"]
    elif damage == "include_parent": item["citation"]["block_ids"] = ["PARENT", "NOTE", "H1"]
    elif damage == "include_adjacent": second["requirements"][-1]["citation"]["block_ids"] = ["H3", "H4"]
    elif damage == "clear_adjacent": second["unresolved_block_ids"] = []
    elif damage == "reorder_adjacent": second["unresolved_block_ids"].reverse()
    elif damage == "keep_critical": second["unresolved_block_ids"].insert(0, "H1")
    elif damage == "procedure": item.update(category="procedure", response_mode="procedure")
    elif damage == "attachment": item["response_mode"] = "attachment"
    elif damage == "optional": item["mandatory"] = False
    elif damage == "high_confidence": item["confidence"] = "high"
    elif damage == "prescribed": item["format_status"] = "prescribed"
    elif damage == "invented_title": item["format_title"] = "资质证明格式"
    elif damage == "fact_labels": item["required_fact_labels"] = ["资质材料"]
    elif damage == "proof_keywords": item["attachment_keywords"] = ["资质证书"]
    elif damage == "reference_keywords": item["reference_keywords"] = ["资质材料"]
    else: item.pop("mandatory")
    state, _, relation, _ = run_repair(tmp_path, monkeypatch, window, first, second)
    assert state["status"] == "failed" and state["error_code"] == "VALIDATION_REPAIR_COVERAGE_LOST"
    assert relation["status"] == "failed"
    assert_two_distinct_settled_calls(state, relation)


def test_unproved_material_heading_family_does_not_buy_a_second_call(tmp_path, monkeypatch):
    window, first, second = material_case(tmp_path)
    window["source_heading_catalog"] = []
    state, validated, relation, feedback = run_repair(tmp_path, monkeypatch, window, first, second, expected_calls=1)
    assert state["status"] == "failed" and state["error_code"] == "LIVE_REQUIRED_BLOCK_UNRESOLVED"
    assert validated == [first] and relation is feedback is None


@pytest.mark.parametrize("code, groups, extra, expected_sha256", [
    ("EXTRACTION_SCHEMA_INVALID", [], {}, "aac6244a750428e16a28dec2ed16d9b749e7b4b8312bcd8747ad0772831e24cb"),
    ("FORMAT_TITLE_NOT_GROUNDED", [], {}, "c4225e32847f8b1052c45364340afdc6313a4a5d9ff741e01d508ff97d10bbae"),
    ("LIVE_REQUIRED_BLOCK_UNRESOLVED", [{"block_ids": ["TARGET", "ADJACENT"]}], {},
     "31b9af319c10dc32ef4bdcbf2c25fa775c9827821931fd14acffb5360a676cad"),
    ("LIVE_REQUIRED_BLOCK_UNRESOLVED", [{"resolution_kind": "read_literal_source_note"}], {},
     "df03c6471a3c040f9d10969f868e354f1196cec367053d8284408742ffbc8b93"),
    ("LIVE_REQUIRED_BLOCK_UNRESOLVED", [{"block_ids": ["TARGET", "ADJACENT"]}], {"exact_cell_label_restore": {"bounded": True}},
     "4fc5e51240e32bc3e9646aa222adeb4c6025710d260c6dc727d365056915e550"),
])
def test_existing_v5_v6_feedback_instruction_bytes_remain_frozen(code, groups, extra, expected_sha256):
    original = {"task_instructions": "original frozen base", "window": {"window_id": "W"}}
    frozen = copy.deepcopy(original)
    inventory = {"resolution_groups": groups, **extra}
    repaired = _validation_repair_request(original, code, {"field": "unresolved_block_ids"}, inventory, "a" * 32)
    instructions = repaired["validation_feedback"]["instructions"]
    assert hashlib.sha256(instructions.encode()).hexdigest() == expected_sha256
    assert repaired["task_instructions"] == original["task_instructions"] + "\n\n" + instructions
    assert original == frozen


def test_failed_material_correction_cannot_buy_another_candidate_after_restart(tmp_path, monkeypatch):
    window, first, second = material_case(tmp_path)
    second["requirements"].pop()
    executor, rid, _, payload, _, sent, _, _ = setup_run(tmp_path, monkeypatch, max_input_chars=85000,
        validate=lambda value, _: validate_live_window(value, window),
        transport=lambda _, count: response(first if count == 1 else second))
    payload.clear()
    payload.update(extraction_window_payload(window, {}))
    state = executor.execute(rid)
    assert state["status"] == "failed" and state["error_code"] == "VALIDATION_REPAIR_COVERAGE_LOST" and len(sent) == 2
    restarted = Executor(executor.store.root, executor.nodes, executor.execution_config)
    state = restarted.resume(rid)
    assert state["status"] == "failed" and state["error_code"] == "VALIDATION_REPAIR_ALREADY_ATTEMPTED" and len(sent) == 2

