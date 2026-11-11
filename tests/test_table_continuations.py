"""A table-span proof preserves two original grids and fails on ambiguity."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from company_bid.table_continuations import build_table_continuations


def tsv(block):
    block["text"] = "\n".join("\t".join(row) for row in block["rows"])


def fixture():
    document = {"source_id": "source", "sha256": "a" * 64, "format": "pdf"}
    common = {"source_id": "source", "kind": "table", "origin": "native", "text_basis": "table_rows_tsv_v1",
              "column_widths": [55.5, 120.5, 280.25], "header_rows": [], "merges": [], "bbox": [1, 2, 3, 4]}
    left = {**copy.deepcopy(common), "block_id": "left", "locator": "pdf/page[4]/table[2]", "page": 4, "ordinal": 30,
            "rows": [["条款号", "条款名称", "编列内容"], ["4.6", "有效期", "原文期限"],
                     ["4.7", "保证金", "☑不要求递交\n□要求递交"]]}
    right = {**copy.deepcopy(common), "block_id": "right", "locator": "pdf/page[5]/table[1]", "page": 5, "ordinal": 32,
             "rows": [["条款号", "条款名称", "编列内容"], ["", "", "金额：5%\n形式及其他原文条件。"],
                      ["4.8", "付款方式", "以原文合同约定为准"], ["……", "其他原文说明", ""]],
             "merges": [{"row": 3, "column": 1, "row_span": 1, "col_span": 2}]}
    for block in (left, right):
        tsv(block)
    footer = {"block_id": "footer", "source_id": "source", "kind": "paragraph", "origin": "native",
              "page": 4, "ordinal": 31, "locator": "pdf/page[4]/line[20]", "text": "- 4 -", "bbox": [250, 775, 260, 782]}
    skip = {key: copy.deepcopy(footer[key]) for key in ("block_id", "source_id", "kind", "page", "locator", "bbox")}
    skip.update(status="skipped_page_footer", printed_page=4, rule="pdf-page-footer-v1",
                reason="bottommost_numeric_line_in_consistent_page_sequence", evidence_pages=[3, 4, 5])
    return document, [left, footer, right], {"footer": skip}


def test_complete_grids_option_states_source_order_and_later_merges_are_unchanged():
    document, blocks, skips = fixture()
    original = copy.deepcopy((document, blocks, skips))
    proofs = build_table_continuations(document, list(reversed(blocks)), skips)
    assert len(proofs) == 1
    proof = proofs[0]
    assert (document, blocks, skips) == original
    assert proof["block_ids"] == ["left", "right"]
    assert proof["quote"] == blocks[0]["text"] + "\n" + blocks[2]["text"]
    assert "☑不要求递交\n□要求递交" in proof["quote"] and "金额：5%" in proof["quote"]
    assert "4.8\t付款方式" in proof["quote"]
    assert proof["quote_sha256"] == hashlib.sha256(proof["quote"].encode()).hexdigest()
    assert proof["continuation"]["left_row"] == 2 and proof["continuation"]["right_row"] == 1
    assert proof["continuation"]["clause_number"] == "4.7"
    assert proof["tables"][1]["grid_sha256"]
    assert proof["skipped_blocks"][0]["block_id"] == "footer"
    hashed = {key: value for key, value in proof.items() if key != "proof_sha256"}
    assert proof["proof_sha256"] == hashlib.sha256(json.dumps(hashed, ensure_ascii=False, sort_keys=True,
                                                            separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def test_proof_rebuilds_without_coordinates_and_with_bounded_window_metadata():
    document, blocks, skips = fixture()
    proof = build_table_continuations(document, blocks, skips)
    stripped = [{key: value for key, value in block.items() if key != "bbox"} for block in blocks]
    stripped_skips = {key: {field: value for field, value in row.items() if field != "bbox"} for key, row in skips.items()}
    assert build_table_continuations(document, stripped, stripped_skips) == proof
    blocks.insert(0, {"block_id": "unrelated", "source_id": "source", "kind": "paragraph", "ordinal": 2})
    assert build_table_continuations(document, blocks, skips) == proof


def test_adjacent_pages_need_no_skip_if_ordinals_are_actually_contiguous():
    document, blocks, _ = fixture()
    blocks = [blocks[0], blocks[2]]
    blocks[1]["ordinal"] = blocks[0]["ordinal"] + 1
    proof = build_table_continuations(document, blocks, {})
    assert len(proof) == 1 and proof[0]["skipped_blocks"] == []


@pytest.mark.parametrize("mutation", [
    "docx", "ocr", "missing_native", "page_gap", "same_page", "width_mismatch", "width_missing", "four_columns",
    "different_header", "left_header_merge", "left_boundary_merge", "right_boundary_merge", "missing_clause_number",
    "unknown_clause_number", "missing_clause_name", "empty_left_body", "right_new_clause", "right_nonempty_label",
    "empty_continuation", "later_unknown_empty_keys", "missing_footer", "missing_ordinal", "body_paragraph", "image",
    "unproved_footer", "wrong_footer_text", "wrong_footer_source", "wrong_footer_rule", "wrong_footer_evidence",
    "stale_tsv", "bad_merge", "duplicate_ordinal", "duplicate_id", "different_source", "missing_source_hash",
])
def test_unsupported_boundaries_cannot_be_proved(mutation):
    document, blocks, skips = fixture()
    left, footer, right = blocks
    if mutation == "docx":
        document["format"] = "docx"
    elif mutation == "ocr":
        right["origin"] = "ocr"
    elif mutation == "missing_native":
        left.pop("origin")
    elif mutation == "page_gap":
        right.update(page=6, locator="pdf/page[6]/table[1]")
    elif mutation == "same_page":
        right.update(page=4, locator="pdf/page[4]/table[1]")
    elif mutation == "width_mismatch":
        right["column_widths"][0] += 0.01
    elif mutation == "width_missing":
        right.pop("column_widths")
    elif mutation == "four_columns":
        for row in right["rows"]:
            row.append("")
    elif mutation == "different_header":
        right["rows"][0][2] = "其他内容"
    elif mutation == "left_header_merge":
        left["merges"] = [{"row": 0, "column": 0, "col_span": 2}]
    elif mutation == "left_boundary_merge":
        left["merges"] = [{"row": 2, "column": 1, "col_span": 2}]
    elif mutation == "right_boundary_merge":
        right["merges"].append({"row": 1, "column": 0, "col_span": 2})
    elif mutation == "missing_clause_number":
        left["rows"][-1][0] = ""
    elif mutation == "unknown_clause_number":
        left["rows"][-1][0] = "……"
    elif mutation == "missing_clause_name":
        left["rows"][-1][1] = ""
    elif mutation == "empty_left_body":
        left["rows"][-1][2] = ""
    elif mutation == "right_new_clause":
        right["rows"][1][0] = "4.8"
    elif mutation == "right_nonempty_label":
        right["rows"][1][1] = "新的说明"
    elif mutation == "empty_continuation":
        right["rows"][1][2] = ""
    elif mutation == "later_unknown_empty_keys":
        right["rows"].append(["", "", "未知续行"])
    elif mutation == "missing_footer":
        blocks.remove(footer)
    elif mutation == "missing_ordinal":
        right["ordinal"] += 1
    elif mutation == "body_paragraph":
        footer["text"] = "不能跳过的正文"
    elif mutation == "image":
        footer["kind"] = "image"
    elif mutation == "unproved_footer":
        skips.clear()
    elif mutation == "wrong_footer_text":
        footer["text"] = "5"
    elif mutation == "wrong_footer_source":
        skips["footer"]["source_id"] = "other"
    elif mutation == "wrong_footer_rule":
        skips["footer"]["rule"] = "model_says_skip"
    elif mutation == "wrong_footer_evidence":
        skips["footer"]["evidence_pages"] = [4]
    elif mutation == "stale_tsv":
        right["text"] = "changed"
    elif mutation == "bad_merge":
        right["merges"].append({"row": 8, "column": 0, "col_span": 2})
    elif mutation == "duplicate_ordinal":
        footer["ordinal"] = left["ordinal"]
    elif mutation == "duplicate_id":
        footer["block_id"] = left["block_id"]
    elif mutation == "different_source":
        right["source_id"] = "other"
    elif mutation == "missing_source_hash":
        document.pop("sha256")
    if mutation != "stale_tsv":
        for block in (left, right):
            tsv(block)
    assert build_table_continuations(document, blocks, skips) == []


def test_source_identity_and_each_preserved_grid_change_the_proof():
    document, blocks, skips = fixture()
    original = build_table_continuations(document, blocks, skips)[0]
    document["sha256"] = "b" * 64
    changed_source = build_table_continuations(document, blocks, skips)[0]
    assert changed_source["proof_sha256"] != original["proof_sha256"]
    blocks[2]["rows"][2][2] = "另一项独立原文"
    tsv(blocks[2])
    changed_grid = build_table_continuations(document, blocks, skips)[0]
    assert changed_grid["proof_sha256"] != changed_source["proof_sha256"]
    assert changed_grid["quote_sha256"] != original["quote_sha256"]
