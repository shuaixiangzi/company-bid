"""Native scoring rows: source-preserving cross-page proofs, no network."""
from __future__ import annotations

import copy
import hashlib

import pytest

from company_bid.table_continuations import (
    build_table_continuations, build_table_row_catalog, scoring_table_continuation_issues,
)


def freeze(block):
    block["text"] = "\n".join("\t".join(row) for row in block["rows"])


def fixture():
    doc = {"source_id": "tender", "sha256": "a" * 64, "format": "pdf"}
    common = {"source_id": "tender", "kind": "table", "origin": "native", "text_basis": "table_rows_tsv_v1",
              "header_rows": [], "merges": [], "bbox": [60, 90, 490, 400]}
    left = {**copy.deepcopy(common), "block_id": "L", "locator": "pdf/page[4]/table[1]", "page": 4, "ordinal": 30,
            "column_widths": [38.344, 63.006, 280.694, 49.056],
            "rows": [["序号", "评审项目", "评审标准", "标准分"],
                     ["1", "独立评分事项", "证书须在有效期内；满足原文条件得分。", "3"],
                     ["2", "相关证明", "自2031年2月3日起计算。以下证明之一：\n（1）合同（含附件）；\n（2）验收文件（需包含", "7"]]}
    right = {**copy.deepcopy(common), "block_id": "R", "locator": "pdf/page[5]/table[0]", "page": 5, "ordinal": 32,
             "column_widths": [40.936, 62.9, 280.05, 52.186],
             "rows": [["", "", "各项签署情况等内容）；\n时间以合同签订或验收文件签署时间为准。", ""], ["合计", "", "", "10"]],
             "merges": [{"row": 1, "column": 0, "row_span": 1, "col_span": 3}]}
    for block in (left, right):
        freeze(block)
    footer = {"source_id": "tender", "block_id": "F", "kind": "paragraph", "origin": "native", "page": 4,
              "ordinal": 31, "locator": "pdf/page[4]/line[20]", "text": "- 4 -", "bbox": [290, 775, 305, 784]}
    skip = {key: footer[key] for key in ("source_id", "block_id", "kind", "page", "locator", "bbox")}
    skip.update(status="skipped_page_footer", printed_page=4, rule="pdf-page-footer-v1",
                reason="bottommost_numeric_line_in_consistent_page_sequence", evidence_pages=[4, 5, 6])
    return doc, [left, footer, right], {"F": skip}


def test_scoring_proof_preserves_every_cell_and_binds_alignment_brackets_and_source_total():
    doc, blocks, skips = fixture()
    original = copy.deepcopy((doc, blocks, skips))
    proofs = build_table_continuations(doc, blocks, skips)
    assert len(proofs) == 1
    proof = proofs[0]
    assert proof["rule"] == "native_pdf_scoring_table_continuation_v1"
    assert proof["quote"] == blocks[0]["text"] + "\n" + blocks[2]["text"]
    assert proof["quote_sha256"] == hashlib.sha256(proof["quote"].encode()).hexdigest()
    assert proof["continuation"]["left_row"] == 2 and proof["continuation"]["right_row"] == 0
    alignment = proof["column_alignment"]
    assert alignment["maximum_boundary_deviation"] < .01 and alignment["relative_total_width_difference"] < .02
    assert alignment["maximum_allowed_boundary_deviation"] == .01
    assert alignment["maximum_allowed_relative_total_difference"] == .02
    assert proof["score_total_check"]["sum_matches_source_total"] is True
    assert proof["tables"][1]["header"] is None and proof["tables"][1]["header_row"] is None
    assert proof["parenthesis_closure"]["left_open_offset"] == blocks[0]["rows"][2][2].rindex("（")
    assert proof["parenthesis_closure"]["right_close_offset"] == blocks[2]["rows"][0][2].index("）")
    assert scoring_table_continuation_issues(doc, blocks, proofs) == []
    assert (doc, blocks, skips) == original


def test_source_and_bbox_free_window_rebuild_identical_proof_and_exact_row_offsets():
    doc, blocks, skips = fixture()
    proofs = build_table_continuations(doc, blocks, skips)
    stripped = [{key: value for key, value in block.items() if key != "bbox"} for block in reversed(blocks)]
    stripped_skips = {key: {name: value for name, value in row.items() if name != "bbox"} for key, row in skips.items()}
    assert build_table_continuations(doc, stripped, stripped_skips) == proofs
    rows = build_table_row_catalog(doc, blocks, proofs)
    assert build_table_row_catalog(doc, stripped, proofs) == rows and len(rows) == 2
    assert rows[0]["quote"] == "\t".join(blocks[0]["rows"][1])
    assert rows[1]["quote"] == "\t".join(blocks[0]["rows"][2]) + "\n" + "\t".join(blocks[2]["rows"][0])
    assert "合计" not in rows[1]["quote"] and "独立评分事项" not in rows[1]["quote"]
    assert rows[1]["parts"][1]["row"] == 0 and len(rows[1]["cell_refs"]) == 8
    by_id = {block["block_id"]: block for block in blocks}
    for entry in rows:
        for part in entry["parts"]:
            parent = by_id[part["block_id"]]
            assert parent["text"][part["start"]:part["end"]] == part["quote"]
            for cell in part["cell_refs"]:
                assert parent["text"][cell["start"]:cell["end"]] == parent["rows"][cell["row"]][cell["column"]]


@pytest.mark.parametrize("damage", [
    "header", "headerless_left", "width_boundary", "width_total", "page_gap", "same_page", "ocr", "origin_missing",
    "source", "ordinal_gap", "body", "image", "unproved_footer", "missing_footer", "left_merge", "right_merge",
    "header_merge", "bad_merge", "closed_left", "ascii_open", "two_open", "wrong_close", "missing_close",
    "sentence_boundary", "right_key", "right_label", "right_score", "empty_right", "wrong_total", "wrong_total_label",
    "unmerged_total", "extra_tail", "nonnumeric_score", "unknown_key", "missing_label", "stale_text", "four_to_three",
])
def test_unsupported_scoring_boundary_has_no_proof_and_is_reported(damage):
    doc, blocks, skips = fixture()
    left, footer, right = blocks
    if damage == "header": left["rows"][0][1] = "未知项目"
    elif damage == "headerless_left": left["rows"][0] = ["", "", "", ""]
    elif damage == "width_boundary": right["column_widths"] = [65, 60, 260, 49]
    elif damage == "width_total": right["column_widths"] = [width * 1.03 for width in left["column_widths"]]
    elif damage == "page_gap": right.update(page=6, locator="pdf/page[6]/table[0]")
    elif damage == "same_page": right.update(page=4, locator="pdf/page[4]/table[0]")
    elif damage == "ocr": right["origin"] = "ocr"
    elif damage == "origin_missing": right.pop("origin")
    elif damage == "source": right["source_id"] = "other"
    elif damage == "ordinal_gap": right["ordinal"] += 1
    elif damage == "body": footer["text"] = "必须保留的中间正文"
    elif damage == "image": footer["kind"] = "image"
    elif damage == "unproved_footer": skips.clear()
    elif damage == "missing_footer": blocks.remove(footer)
    elif damage == "left_merge": left["merges"] = [{"row": 2, "column": 0, "col_span": 2}]
    elif damage == "right_merge": right["merges"].append({"row": 0, "column": 0, "col_span": 2})
    elif damage == "header_merge": left["merges"] = [{"row": 0, "column": 0, "col_span": 2}]
    elif damage == "bad_merge": right["merges"].append({"row": 99, "column": 0, "col_span": 2})
    elif damage == "closed_left": left["rows"][-1][2] += "原文内容）。"
    elif damage == "ascii_open": left["rows"][-1][2] = left["rows"][-1][2][:-4] + "(需包含"
    elif damage == "two_open": left["rows"][-1][2] += "（另外"
    elif damage == "wrong_close": right["rows"][0][2] = right["rows"][0][2].replace("）", ")", 1)
    elif damage == "missing_close": right["rows"][0][2] = right["rows"][0][2].replace("）", "", 1)
    elif damage == "sentence_boundary": right["rows"][0][2] = "独立新句。" + right["rows"][0][2]
    elif damage == "right_key": right["rows"][0][0] = "3"
    elif damage == "right_label": right["rows"][0][1] = "新事项"
    elif damage == "right_score": right["rows"][0][3] = "7"
    elif damage == "empty_right": right["rows"][0][2] = ""
    elif damage == "wrong_total": right["rows"][1][3] = "11"
    elif damage == "wrong_total_label": right["rows"][1][0] = "其他事项"
    elif damage == "unmerged_total": right["merges"] = []
    elif damage == "extra_tail": right["rows"].append(["", "", "更多未知条件。", ""])
    elif damage == "nonnumeric_score": left["rows"][-1][3] = "待确认"
    elif damage == "unknown_key": left["rows"][-1][0] = "另项"
    elif damage == "missing_label": left["rows"][-1][1] = ""
    elif damage == "stale_text": right["text"] += "改写"
    elif damage == "four_to_three": left["rows"][1].pop()
    if damage != "stale_text":
        freeze(left); freeze(right)
    proofs = build_table_continuations(doc, blocks, skips)
    assert proofs == []
    rows = build_table_row_catalog(doc, blocks, proofs)
    assert not any(entry["block_ids"] == ["L", "R"] for entry in rows)
    # At least one native partial side remains detectable, including a left
    # source whose alleged continuation was unreadable or outside this source.
    if damage not in {"closed_left", "nonnumeric_score", "unknown_key", "missing_label", "header", "headerless_left", "four_to_three"}:
        assert scoring_table_continuation_issues(doc, blocks, proofs)


def test_unknown_or_forged_proof_never_exposes_a_half_scoring_row():
    doc, blocks, skips = fixture()
    proof = build_table_continuations(doc, blocks, skips)[0]
    forged = copy.deepcopy(proof); forged["continuation"]["right_row"] = 1
    for supplied in ([], [forged]):
        rows = build_table_row_catalog(doc, blocks, supplied)
        assert len(rows) == 1 and rows[0]["parts"][0]["row"] == 1
        assert {issue["side"] for issue in scoring_table_continuation_issues(doc, blocks, supplied)} == {"left", "right"}
    assert scoring_table_continuation_issues(doc, [blocks[0]], [])
    assert scoring_table_continuation_issues(doc, [blocks[2]], [])


def test_headered_technical_rows_keep_source_header_lf_and_do_not_absorb_a_later_empty_total():
    doc, blocks, _ = fixture()
    table = copy.deepcopy(blocks[0]); table.update(block_id="TECH", ordinal=40)
    table["rows"] = [["序\n号", "评价项目", "评审标准", "分\n数"],
                     ["1", "机构设置", "合理得（7,10］分；欠缺得（0,4］分。\n无内容得0分。", "10"],
                     ["2", "技术方法", "完整得分；否则按原文扣分。", "5"]]
    freeze(table)
    total = copy.deepcopy(blocks[2]); total.update(block_id="TOTAL", ordinal=42)
    total["rows"] = [["", "", "", ""], ["技术部分总分", "", "", "15"]]
    freeze(total)
    rows = build_table_row_catalog(doc, [table, total], [])
    assert len(rows) == 2 and all(entry["block_ids"] == ["TECH"] for entry in rows)
    assert rows[0]["tables"][0]["header"] == table["rows"][0]
    assert rows[0]["tables"][0]["header_view"] == ["序号", "评价项目", "评审标准", "分数"]
    assert not scoring_table_continuation_issues(doc, [table, total], [])


def integration_fixture(tmp_path):
    from test_planning import fixture as manifest_fixture
    manifest, parsed = manifest_fixture(tmp_path)
    _, blocks, _ = fixture()
    left, footer, right = copy.deepcopy(blocks)
    footer5 = {**copy.deepcopy(footer), "block_id": "F5", "page": 5, "locator": "pdf/page[5]/line[20]", "text": "- 5 -"}
    footer6 = {**copy.deepcopy(footer), "block_id": "F6", "page": 6, "locator": "pdf/page[6]/line[20]", "text": "- 6 -"}
    body = {"block_id": "P6", "source_id": "tender", "kind": "paragraph", "origin": "native",
            "locator": "pdf/page[6]/line[0]", "text": "另一章独立说明。", "bbox": [60, 90, 490, 120]}
    parsed["documents"][0]["format"] = "pdf"
    parsed["documents"][0]["units"] = [
        {"unit_id": f"P{page}", "source_id": "tender", "locator": f"pdf/page[{page}]", "page": page,
         "coverage": {"status": "read"}, "blocks": values}
        for page, values in [(4, [left, footer]), (5, [right, footer5]), (6, [body, footer6])]]
    return manifest, parsed


@pytest.mark.parametrize("selector", ["row", "whole_pair"])
@pytest.mark.parametrize("block_limit", [1, 60])
def test_n2_n3_source_rebuild_supports_zero_index_right_row_and_split_windows(tmp_path, selector, block_limit):
    from company_bid.extraction import _index, _windows, extract_requirements, validate_live_window
    from company_bid.planning import plan_bid
    from test_planning import Context, no_call
    manifest, parsed = integration_fixture(tmp_path)
    blocks, _, documents = _index(parsed, manifest)
    windows, _ = _windows(documents, blocks, {"window_max_blocks": block_limit})
    selected_windows = []
    def caller(ctx, task_type, unit_id, payload, validate):
        window = payload["window"]
        requirements = []
        if window.get("source_table_continuations"):
            row = next(row for row in window["source_table_row_catalog"] if row["block_ids"] == ["L", "R"])
            citation = {key: row[key] for key in ("source_id", "block_id", "locator", "block_ids")}
            if selector == "row": citation["table_row_id"] = row["table_row_id"]
            requirements.append({"citation": citation, "category": "commercial", "response_mode": "procedure", "format_status": "not_prescribed"})
            selected_windows.append(window)
        raw = {"window_id": window["window_id"], "processing_declaration": "processed_not_semantically_accepted",
               "unresolved_block_ids": [], "requirements": requirements, "format_chapters": [], "format_status": "none_verified"}
        assert validate_live_window(raw, window) == validate(raw)
        return validate(raw)
    ctx = Context()
    extracted = extract_requirements(ctx, parsed, manifest, caller, {"window_max_blocks": block_limit})
    assert selected_windows and len(extracted["requirements"]) == 1
    citation = extracted["requirements"][0]["citations"][0]
    if selector == "row":
        assert citation["table_row_parts"][1]["row"] == 0
        for part in citation["table_row_parts"]:
            assert blocks[part["block_id"]]["text"][part["start"]:part["end"]] == part["quote"]
        assert "合计" not in citation["quote"]
    else:
        assert citation["quote"] == blocks["L"]["text"] + "\n" + blocks["R"]["text"]
    prepared = plan_bid(Context(), extracted, parsed, manifest, no_call, {})
    assert prepared["review_actions"][0]["source_requirement"] == citation["quote"]
    assert any("quote_sha256=" + citation["quote_sha256"] in source["locator"] for source in prepared["sources"])
    assert "证明之一" in citation["quote"] and "合同签订或验收文件签署时间为准" in citation["quote"]
    if block_limit == 1:
        assert all(len(window["blocks"]) == 1 for window in windows)


@pytest.mark.parametrize("damage", ["width", "closure"])
def test_unproved_scoring_input_is_rejected_before_any_candidate_call(tmp_path, damage):
    from company_bid.extraction import PlanningError, extract_requirements
    from test_planning import Context, no_call
    manifest, parsed = integration_fixture(tmp_path)
    right = parsed["documents"][0]["units"][1]["blocks"][0]
    if damage == "width": right["column_widths"][0] += 50
    else:
        right["rows"][0][2] = right["rows"][0][2].replace("）", ")", 1)
        freeze(right)
    ctx = Context()
    with pytest.raises(PlanningError, match="SCORING_TABLE_CONTINUATION_UNPROVEN"):
        extract_requirements(ctx, parsed, manifest, no_call, {"window_max_blocks": 1})
    captured = ctx.captures["n2-source-context-preflight"]
    assert captured["actual_call_started"] is False and captured["error_code"] == "SCORING_TABLE_CONTINUATION_UNPROVEN"


@pytest.mark.parametrize("merged_instructions", [True, False])
def test_independent_quotation_instruction_shape_is_not_scoring_evidence(tmp_path, merged_instructions):
    from company_bid.extraction import _index, _windows, extract_requirements
    from test_planning import Context
    manifest, parsed = integration_fixture(tmp_path)
    table = parsed["documents"][0]["units"][0]["blocks"][0]
    table["rows"] = [["", "", "报价填表说明（人民币）", ""], ["序号", "项目", "数量", "单价"], ["1", "合成项目", "", ""]]
    table["column_widths"] = [30, 90, 60, 60]
    table["merges"] = ([{"row": 0, "column": 0, "col_span": 2}, {"row": 0, "column": 2, "col_span": 2}]
                       if merged_instructions else [])
    table["header_rows"] = [1]
    freeze(table)
    caption = {"block_id": "QUOTE-CAPTION", "source_id": "tender", "kind": "paragraph", "origin": "native",
               "locator": "pdf/page[4]/line[0]", "text": "响应报价表", "bbox": [60, 60, 490, 78]}
    parsed["documents"][0]["units"] = [{**parsed["documents"][0]["units"][0], "blocks": [caption, table]}]
    blocks, _, documents = _index(parsed, manifest)
    windows, _ = _windows(documents, blocks, {})
    assert windows[0]["caption_table_bindings"]
    assert scoring_table_continuation_issues(documents[0], list(blocks.values()), []) == []
    calls = []
    def caller(ctx, task_type, unit_id, payload, validate):
        window = payload["window"]
        calls.append(window["window_id"])
        citation = {key: blocks["L"][key] for key in ("source_id", "block_id", "locator")}
        return validate({"window_id": window["window_id"], "processing_declaration": "processed_not_semantically_accepted",
                         "unresolved_block_ids": [], "requirements": [{"citation": citation, "category": "commercial",
                             "response_mode": "procedure", "format_status": "not_prescribed"}],
                         "format_chapters": [], "format_status": "none_verified"})
    extracted = extract_requirements(Context(), parsed, manifest, caller, {})
    assert calls
    assert extracted["requirements"][0]["citations"][0]["quote"] == table["text"]


def test_merged_tail_exemption_does_not_hide_a_partial_preceding_scoring_source():
    doc, blocks, skips = fixture()
    blocks[2]["merges"].append({"row": 0, "column": 0, "col_span": 2})
    assert build_table_continuations(doc, blocks, skips) == []
    issues = scoring_table_continuation_issues(doc, blocks, [])
    assert any(issue["side"] == "left" for issue in issues)
