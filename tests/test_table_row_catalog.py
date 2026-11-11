"""Exact source table rows, with application-proved cross-page continuations."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from company_bid.table_continuations import build_table_continuations, build_table_row_catalog


def freeze(block):
    block["text"] = "\n".join("\t".join(row) for row in block["rows"])


def fixture():
    document = {"source_id": "source", "sha256": "a" * 64, "format": "pdf"}
    common = {"source_id": "source", "kind": "table", "origin": "native", "text_basis": "table_rows_tsv_v1",
              "column_widths": [55.5, 120.5, 280.25], "header_rows": [], "merges": [], "bbox": [1, 2, 3, 4]}
    left = {**copy.deepcopy(common), "block_id": "left", "locator": "pdf/page[4]/table[2]", "page": 4, "ordinal": 30,
            "rows": [["条款号", "条款名称", "编列内容"],
                     ["4.5", "现场踏勘", "☑不组织\n□组织；条件  原文\n例外均保留。"],
                     ["4.6", "有效期", "期限及原文例外。"], ["4.7", "保证金", "☑不要求递交\n□要求递交"]]}
    right = {**copy.deepcopy(common), "block_id": "right", "locator": "pdf/page[5]/table[1]", "page": 5, "ordinal": 32,
             "rows": [["条款号", "条款名称", "编列内容"], ["", "", "金额：5%\n形式及原文条件  均保留。"],
                      ["4.8", "付款方式", "以原文合同约定为准"], ["……", "其他原文说明", ""]],
             "merges": [{"row": 3, "column": 1, "row_span": 1, "col_span": 2}]}
    for block in (left, right):
        freeze(block)
    footer = {"block_id": "footer", "source_id": "source", "kind": "paragraph", "origin": "native",
              "page": 4, "ordinal": 31, "locator": "pdf/page[4]/line[20]", "text": "- 4 -"}
    skip = {key: footer[key] for key in ("block_id", "source_id", "kind", "page", "locator")}
    skip.update(status="skipped_page_footer", printed_page=4, rule="pdf-page-footer-v1",
                reason="bottommost_numeric_line_in_consistent_page_sequence", evidence_pages=[3, 4, 5])
    blocks = [left, footer, right]
    return document, blocks, build_table_continuations(document, blocks, {"footer": skip})


def test_each_complete_row_is_separate_and_continuation_is_one_complete_catalog_entry():
    document, blocks, continuations = fixture()
    original = copy.deepcopy((document, blocks, continuations))
    catalog = build_table_row_catalog(document, blocks, continuations)
    assert len(catalog) == 4 and len({entry["table_row_id"] for entry in catalog}) == 4
    assert (document, blocks, continuations) == original
    ordinary, second, continued, following = catalog
    assert ordinary["quote"] == "\t".join(blocks[0]["rows"][1])
    assert ordinary["block_ids"] == ["left"] and ordinary["block_id"] == "left"
    assert second["quote"] == "\t".join(blocks[0]["rows"][2])
    assert continued["block_ids"] == ["left", "right"]
    assert continued["locator"] == blocks[0]["locator"]
    assert continued["quote"] == "\t".join(blocks[0]["rows"][3]) + "\n" + "\t".join(blocks[2]["rows"][1])
    assert "☑不要求递交\n□要求递交" in continued["quote"]
    assert "4.8" not in continued["quote"] and "以原文合同" not in continued["quote"]
    assert continued["continuation_proof_sha256"] == continuations[0]["proof_sha256"]
    assert following["quote"] == "\t".join(blocks[2]["rows"][2])
    assert following["block_ids"] == ["right"] and len(following["parts"]) == 1
    assert all("条款号" not in entry["quote"] for entry in catalog)


def test_offsets_cells_parent_hash_and_proof_are_rebuilt_from_the_frozen_source():
    document, blocks, continuations = fixture()
    by_id = {block["block_id"]: block for block in blocks}
    catalog = build_table_row_catalog(document, blocks, continuations)
    for entry in catalog:
        assert entry["quote_sha256"] == hashlib.sha256(entry["quote"].encode()).hexdigest()
        assert entry["parent_quote_sha256"] == hashlib.sha256("\n".join(by_id[key]["text"] for key in entry["block_ids"]).encode()).hexdigest()
        for part in entry["parts"]:
            parent = by_id[part["block_id"]]
            assert parent["text"][part["start"]:part["end"]] == part["quote"]
            assert part["quote"] == "\t".join(parent["rows"][part["row"]])
            for cell in part["cell_refs"]:
                assert parent["text"][cell["start"]:cell["end"]] == parent["rows"][cell["row"]][cell["column"]]
        proof = {key: value for key, value in entry.items() if key not in {"table_row_id", "proof_sha256"}}
        digest = hashlib.sha256(json.dumps(proof, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        assert digest == entry["proof_sha256"] and entry["table_row_id"] == "TROW-" + digest[:24]
    assert any(cell["start"] == cell["end"] for cell in catalog[2]["cell_refs"])


def test_bbox_stripping_source_order_and_bounded_window_do_not_change_ids_or_proofs():
    document, blocks, continuations = fixture()
    expected = build_table_row_catalog(document, blocks, continuations)
    stripped = [{key: value for key, value in block.items() if key != "bbox"} for block in reversed(blocks)]
    assert build_table_row_catalog(document, stripped, continuations) == expected


def test_unrelated_valid_merge_does_not_hide_other_rows_but_selected_merged_row_is_omitted():
    document, blocks, _ = fixture()
    left = blocks[0]
    left["rows"][1][2] = ""
    left["merges"].append({"row": 1, "column": 1, "col_span": 2})
    freeze(left)
    catalog = build_table_row_catalog(document, blocks, [])
    assert [entry["parts"][0]["row"] for entry in catalog if entry["block_id"] == "left"] == [2]
    assert any(entry["block_id"] == "right" and entry["parts"][0]["row"] == 2 for entry in catalog)


@pytest.mark.parametrize("mutation", ["no_proof", "hash", "quote", "row", "source_hash", "parent_content", "missing_footer"])
def test_unknown_or_tampered_continuation_never_yields_an_independent_half_row(mutation):
    document, blocks, continuations = fixture()
    if mutation == "no_proof":
        continuations = []
    elif mutation == "hash":
        continuations[0]["proof_sha256"] = "b" * 64
    elif mutation == "quote":
        continuations[0]["quote"] += "改变"
    elif mutation == "row":
        continuations[0]["continuation"]["left_row"] = 2
    elif mutation == "source_hash":
        document["sha256"] = "b" * 64
    elif mutation == "parent_content":
        blocks[0]["rows"][1][2] += "新增原文"
        freeze(blocks[0])
    else:
        blocks.pop(1)
    catalog = build_table_row_catalog(document, blocks, continuations)
    assert all(not (entry["block_id"] == "left" and entry["parts"][0]["row"] == 3) for entry in catalog)
    assert all(not (entry["block_id"] == "right" and entry["parts"][0]["row"] == 1) for entry in catalog)
    assert all(len(entry["block_ids"]) == 1 for entry in catalog)


@pytest.mark.parametrize("mutation", ["ocr", "docx", "header", "header_merge", "stale_text", "four_columns", "bad_merge", "unknown_key", "empty_label", "selected_merge"])
def test_ineligible_parent_or_selected_row_does_not_get_a_row_id(mutation):
    document, blocks, continuations = fixture()
    left = blocks[0]
    if mutation == "ocr":
        left["origin"] = "ocr"
    elif mutation == "docx":
        document["format"] = "docx"
    elif mutation == "header":
        left["rows"][0][2] = "其他内容"
    elif mutation == "header_merge":
        left["merges"].append({"row": 0, "column": 0, "col_span": 2})
    elif mutation == "stale_text":
        left["text"] += "改变"
    elif mutation == "four_columns":
        left["rows"][1].append("")
    elif mutation == "bad_merge":
        left["merges"].append({"row": 99, "column": 0, "col_span": 2})
    elif mutation == "unknown_key":
        left["rows"][1][0] = "另行规定"
    elif mutation == "empty_label":
        left["rows"][1][1] = "  "
    else:
        left["rows"][1][2] = ""
        left["merges"].append({"row": 1, "column": 1, "col_span": 2})
    if mutation != "stale_text":
        freeze(left)
    catalog = build_table_row_catalog(document, blocks, continuations)
    assert all(not (entry["block_id"] == "left" and entry["parts"][0]["row"] == 1) for entry in catalog)


def test_same_text_in_different_rows_and_changed_parent_material_have_distinct_stable_ids():
    document, blocks, _ = fixture()
    left = blocks[0]
    left["rows"][2] = list(left["rows"][1])
    freeze(left)
    catalog = build_table_row_catalog(document, blocks, [])
    same = [entry for entry in catalog if entry["block_id"] == "left"]
    assert same[0]["quote"] == same[1]["quote"] and same[0]["table_row_id"] != same[1]["table_row_id"]
    document["sha256"] = "b" * 64
    changed = build_table_row_catalog(document, blocks, [])
    assert changed[0]["table_row_id"] != catalog[0]["table_row_id"]


@pytest.mark.parametrize("tail_count", [1, 2, 4])
def test_same_table_empty_key_conditions_never_leave_a_selectable_half_clause(tail_count):
    document, blocks, _ = fixture()
    block = blocks[0]
    block["rows"] = [block["rows"][0], ["4.4", "独立事项", "完整原文。"],
                     ["4.5", "履约担保", "☑不要求递交\n□要求递交"],
                     *[["", "", f"仅在书面批准后；否则不得执行。原文续行{index}"] for index in range(tail_count)],
                     ["4.6", "另一事项", "不属于前条的完整原文。"]]
    freeze(block)
    original = copy.deepcopy(block)
    catalog = build_table_row_catalog(document, [block], [])
    assert [entry["quote"].split("\t", 1)[0] for entry in catalog] == ["4.4", "4.6"]
    assert all(len(entry["parts"]) == 1 for entry in catalog)
    assert block == original and "仅在书面批准后；否则不得执行。" in block["text"]


@pytest.mark.parametrize("separator", ["blank", "merged_gap", "merged_tail", "merged_explicit"])
def test_table_gaps_and_merges_cannot_justify_clipping_conditions_or_swallowing_other_clauses(separator):
    document, blocks, _ = fixture()
    block = blocks[0]
    block["rows"] = [block["rows"][0], ["4.4", "独立事项", "完整原文。"],
                     ["4.5", "担保事项", "☑不要求递交"],
                     ["", "", ""], ["", "", "仅在书面批准后；否则不得执行。"],
                     ["4.6", "另一事项", "完整原文。"]]
    if separator == "merged_gap":
        block["merges"] = [{"row": 3, "column": 0, "col_span": 2}]
    elif separator == "merged_tail":
        block["merges"] = [{"row": 4, "column": 0, "col_span": 2}]
    elif separator == "merged_explicit":
        block["rows"][2][2] = ""
        block["merges"] = [{"row": 2, "column": 1, "col_span": 2}]
    freeze(block)
    catalog = build_table_row_catalog(document, [block], [])
    assert [entry["quote"].split("\t", 1)[0] for entry in catalog] == ["4.4", "4.6"]


@pytest.mark.parametrize("gap", [False, True])
def test_cross_page_first_continuation_with_additional_empty_key_tail_is_not_truncated(gap):
    document, blocks, old_proofs = fixture()
    left, _, right = blocks
    right["merges"] = []
    additions = ([["", "", ""]] if gap else []) + [["", "", "仅在另行书面批准后；否则不得执行。"]]
    right["rows"][2:2] = additions
    freeze(right)
    skips = {item["footer_proof"]["block_id"]: item["footer_proof"] for item in old_proofs[0]["skipped_blocks"]}
    proofs = build_table_continuations(document, blocks, skips)
    # Additional unbound empty-key rows invalidate the current whole-table
    # proof too; no stale proof is needed to suppress the left half-clause.
    assert proofs == []
    catalog = build_table_row_catalog(document, blocks, proofs)
    assert not any(entry["block_id"] == "left" and entry["parts"][0]["row"] == len(left["rows"]) - 1 for entry in catalog)
    assert not any(entry["block_id"] == "right" and entry["parts"][0]["row"] == 1 for entry in catalog)
    assert any(entry["quote"].startswith("4.8\t") for entry in catalog)
    assert all(len(entry["parts"]) == 1 for entry in catalog)
