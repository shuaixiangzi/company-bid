"""Project identity is not a past-performance record or a missing company name."""
from copy import deepcopy

import pytest

import company_bid.planning as planning
from company_bid.planning import _guard_historical_table_fills, _table_mapping, _fact_index, plan_bid
from test_planning import Context, fixture, add_unit, cite, extract, response, requirement, no_call


def historical_source():
    rows = [["序号", "项目名称", "发包人", "服务日期", "合同额（万元）"], ["", "", "", "", ""]]
    caption = {"block_id": "CAP", "source_id": "T", "kind": "paragraph", "origin": "native", "unit_id": "U",
               "page": 1, "ordinal": 0, "locator": "pdf/page[1]/line[0]", "text": "响应人业绩统计表（后附业绩材料）"}
    table = {**caption, "block_id": "GRID", "kind": "table", "ordinal": 1, "locator": "pdf/page[1]/table[0]",
             "text": "\n".join("\t".join(row) for row in rows), "rows": rows, "merges": [], "column_widths": [1, 2, 2, 2, 2]}
    fact = {"fact_id": "CURRENT", "label": "项目名称", "value": "本次投标项目", "source_refs": ["T"]}
    manifest = {"project": {"name": fact["value"]}, "sources": [{"source_id": "T", "sha256": "a" * 64}]}
    return caption, table, fact, manifest


def test_historical_source_caption_and_record_grid_block_current_identity_after_original_mapping():
    caption, table, fact, manifest = historical_source()
    before = deepcopy((table, fact, manifest))
    raw_fills, _ = _table_mapping(Context(), table, [fact], {}, {}, no_call, {})
    assert raw_fills == {(1, 1): "CURRENT"}  # The original adjacent-label mapping is retained as evidence.
    fills, report = _guard_historical_table_fills(table, {"CAP": caption, "GRID": table}, raw_fills, {"CURRENT": fact}, manifest)
    assert fills == {} and raw_fills == {(1, 1): "CURRENT"}
    assert report["scope"] == "historical_performance"
    assert report["blocked"][0]["is_current_project_identity"] is True
    assert report["source_caption"]["locator"] == "pdf/page[1]/line[0]"
    assert report["source_file_sha256"] == "a" * 64
    assert (table, fact, manifest) == before


@pytest.mark.parametrize("mutation", ["ordinary_caption", "different_page", "different_unit", "different_file", "not_native", "not_record_grid"])
def test_unrelated_title_or_page_cannot_reassign_the_historical_table_scope(mutation):
    caption, table, fact, manifest = historical_source()
    if mutation == "ordinary_caption": caption["text"] = "本项目报价表"
    elif mutation == "different_page": caption["page"] = 2
    elif mutation == "different_unit": caption["unit_id"] = "OTHER"
    elif mutation == "different_file": caption["source_id"] = "OTHER"
    elif mutation == "not_native": caption["origin"] = "ocr"
    else: table["rows"][0] = ["序号", "项目名称", "内容", "数量", "备注"]
    fills, report = _guard_historical_table_fills(table, {"CAP": caption, "GRID": table}, {(1, 1): "CURRENT"}, {"CURRENT": fact}, manifest)
    assert fills == {(1, 1): "CURRENT"} and report["blocked"] == []
    assert report["scope"] == "not_proven_historical"


def test_unbound_atomic_historical_name_also_cannot_claim_a_complete_record_and_printed_source_is_untouched():
    caption, table, fact, manifest = historical_source()
    fact["value"] = "另一份合同中的项目名"
    fills, report = _guard_historical_table_fills(table, {"CAP": caption, "GRID": table}, {(1, 1): "CURRENT"}, {"CURRENT": fact}, manifest)
    assert fills == {} and report["blocked"][0]["is_current_project_identity"] is False
    table["rows"][1] = ["1", "原件已印刷的历史项目", "原件甲方", "2024年", "10"]
    before = deepcopy(table)
    fills, report = _guard_historical_table_fills(table, {"CAP": caption, "GRID": table}, {}, {"CURRENT": fact}, manifest)
    assert fills == {} and report["blocked"] == [] and table == before


def history_plan_fixture(tmp_path):
    manifest, parsed = fixture(tmp_path)
    heading = add_unit(parsed, "H", "一、响应人业绩统计表（后附业绩材料）", origin="native")
    rows = [["序号", "项目名称", "发包人", "服务日期", "合同额（万元）"], ["", "", "", "", ""]]
    table = {"block_id": "B-GRID", "source_id": "tender", "kind": "table", "origin": "native", "locator": "table:GRID",
             "text": "\n".join("\t".join(row) for row in rows), "rows": rows, "merges": [], "column_widths": [1, 2, 2, 2, 2], "header_rows": [0]}
    parsed["documents"][0]["units"][0]["blocks"].append(table)
    extracted = extract(Context(), parsed, manifest, lambda p: response(p, formats=[{
        "title": heading["text"], "level": 1, "citation": cite(heading), "content_block_ids": [table["block_id"]]}]))
    return manifest, parsed, extracted


def test_actual_plan_preserves_previous_marker_numbers_and_adds_one_source_linked_pending_cell(tmp_path, monkeypatch):
    manifest, parsed, extracted = history_plan_fixture(tmp_path)
    original_guard = planning._guard_historical_table_fills
    def former_mapping(block, blocks, fills, fact_map, manifest):
        _, report = original_guard(block, blocks, fills, fact_map, manifest)
        report.update(blocked=[], retained_filled_count=len(fills))
        return fills, report
    with monkeypatch.context() as patch:
        patch.setattr(planning, "_guard_historical_table_fills", former_mapping)
        old = plan_bid(Context(), extracted, parsed, manifest, no_call, {})
    ctx = Context()
    new = plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    assert old["tables"][0]["rows"][1][1]["text"] == manifest["project"]["name"]
    cell = new["tables"][0]["rows"][1][1]
    assert cell["text"] == "" and cell["fact_refs"] == [] and len(cell["issue_refs"]) == 1
    assert [(i["issue_id"], i["title"]) for i in new["issues"][:-1]] == [(i["issue_id"], i["title"]) for i in old["issues"]]
    assert new["issues"][-1]["issue_id"] == cell["issue_refs"][0]
    assert new["issues"][-1]["requirement_refs"] == new["tables"][0]["requirement_refs"]
    assert ctx.captures["n3-table-context-guard"]["tables"][0]["blocked"][0]["issue_id"] == cell["issue_refs"][0]


def test_current_project_quote_field_remains_eligible(tmp_path):
    manifest, parsed = fixture(tmp_path)
    rows = [["项目名称", ""], ["响应人名称", ""]]
    block = {"block_id": "GRID", "source_id": "tender", "kind": "table", "unit_id": "U", "page": 1, "ordinal": 0,
             "locator": "pdf/page[1]/table[0]", "rows": rows, "text": "项目名称\t\n响应人名称\t", "merges": [], "column_widths": [1, 2]}
    facts = _fact_index(manifest)
    fills, _ = _table_mapping(Context(), block, facts, {}, {}, no_call, {})
    kept, report = _guard_historical_table_fills(block, {"GRID": block}, fills, {f["fact_id"]: f for f in facts}, manifest)
    assert kept == fills and len(kept) == 2 and not report["blocked"]


@pytest.mark.parametrize("label", ["（乙方名称）", "（甲方名称）", "联系人"])
def test_known_company_contract_name_is_pending_entry_not_missing_fact_or_guessed_other_fields(tmp_path, label):
    manifest, parsed = fixture(tmp_path)
    block = add_unit(parsed, "C", "(2) " + label + "\n联系人：\n通讯地址：")
    extracted = extract(Context(), parsed, manifest, lambda p: response(p, [requirement(block,
        category="commercial", mode="factual_response", required_fact_labels=[label])]))
    ctx = Context()
    prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    item = next(i for i in prepared["issues"] if i["title"] == label + "待补")
    section = next(s for s in prepared["sections"] if s["section_id"] == item["section_id"])
    assert section["fact_refs"] == [] and prepared["requirements"][0]["text"] == block["text"]
    if label == "（乙方名称）":
        assert item["reason"] == "原合同字段尚未填写，已有公司名称可供填写。"
        assert manifest["company"]["name"] in item["action"]
        decisions = ctx.captures["n3-planning-report"]["factual_responses"][0]["decisions"]
        assert decisions[0]["status"] == "known_company_pending_manual_entry"
        assert decisions[0]["available_company"]["source_refs"] == manifest["company"]["source_refs"]
    else:
        assert "无唯一" in item["reason"] and manifest["company"]["name"] not in item["action"]
