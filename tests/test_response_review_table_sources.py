"""Bounded preliminary-form proof, independent rows and audited citations."""
import copy

import pytest

from company_bid.extraction import PlanningError, _citation
from company_bid.response_review_table_sources import build_response_review_table_proof
from company_bid.source_response_audit import _scope


def _merge(row, column, row_span, col_span=1):
    return dict(row=row, column=column, row_span=row_span, col_span=col_span)


def _native_preliminary_form():
    native, footers = [], {}
    cfg = {"source_id": "synthetic", "physical_page": 9, "source_sha256": "a" * 64}

    def add(page, index, text, box, *, rows=None, merges=None):
        kind = "paragraph" if rows is None else "table"
        sub = "line" if rows is None else "table"
        block = {"source_id": "synthetic", "page": page, "ordinal": len(native), "origin": "native", "kind": kind,
                 "unit_ordinal": sum(item["page"] == page for item in native), "unit_id": f"page-{page}",
                 "block_id": f"p{page}-{sub}-{index}", "bbox": box, "text": text, "locator": f"pdf/page[{page}]/{sub}[{index}]"}
        if rows is not None:
            block.update(rows=rows, merges=merges, column_widths=[50, 75, 240, 40, 40],
                         header_rows=[], text_basis="table_rows_tsv_v1", text="\n".join("\t".join(row) for row in rows))
        native.append(block)
        return block

    def footer(p, i):
        item = add(p, i, str(p - 6), [294, 794, 304, 807])
        footers[item["block_id"]] = {key: item[key] for key in ("block_id", "source_id", "page", "locator", "kind", "bbox")} | {
            "rule": "pdf-page-footer-v1", "status": "skipped_page_footer", "printed_page": p - 6,
            "reason": "bottommost_numeric_line_in_consistent_page_sequence", "evidence_pages": list(range(8, 14))}

    stamp, date = "响应人： （盖单位章）", "年 月 日"
    for i, text in enumerate(("响应文件格式", "虚拟项目第一行", "虚拟项目第二行", "虚拟项目第三行", "响 应 文 件", stamp,
                              "法定代表人或其委托代理人： （签字或盖章）", date)):
        add(8, i, text, [90, 100 + i * 35, 260, 112 + i * 35])
    footer(8, 8)
    add(9, 0, "1.初步评审应答表", [90, 82, 220, 110])
    factors = ["响应人名称", "报价函签字盖章", "响应文件格式", "报价唯一", "资质要求", "项目经理（负责人）资格要求", "业绩要求", "财务要求"]
    first = [["条款号", "评审因素", "评审标准", "对应\n页码", "是否\n响应"]]
    first.extend([[{0: "形式评\n审标准", 4: "资格评\n审标准"}.get(i, ""), factor, "条件（见续页" if i == 7 else f"第{i}项。", "", ""]
                  for i, factor in enumerate(factors)])
    add(9, 0, "", [85, 104, 530, 760], rows=first, merges=[_merge(1, 0, 4), _merge(5, 0, 4)])
    footer(9, 1)
    middle = [["", "", "材料）", "", ""], ["", "信誉要求", "提供截图。", "", ""],
              ["", "其他要求（需提供承诺书）", "不另附。", "", ""], ["", "联合体", "本项另审。", "", ""],
              ["", "符合相关其他条件。", "", "", ""], ["响应性\n评审标\n准", "投标报价", "使用计算示例。", "", ""],
              ["", "计划实施周期", "遵循要求。", "", ""], ["", "项目地点", "以示例为准。", "", ""]]
    add(10, 0, "", [85, 72, 530, 760], rows=middle, merges=[_merge(0, 0, 5), _merge(5, 0, 3), _merge(4, 1, 1, 2)])
    footer(10, 0)
    add(11, 0, "", [85, 72, 530, 105], rows=[["", "投标有效期", "45个日历天。", "", ""]], merges=[])
    add(11, 0, stamp, [90, 142, 390, 154])
    add(11, 1, date, [385, 166, 505, 178])
    footer(11, 2)
    letter = ["2. 商务报价函", "合成对象：", *[f"示例行{i}" for i in range(2, 8)], "2.我方承诺商务报价函有效期为45 个日历天。",
              "示例行9", "示例行10", "示例行11", *[f"（{i}）我方承诺按合成规则{i}执行。" for i in range(2, 5)],
              stamp, "法定代表人（或其委托代理人）： （签字或盖章）", date]
    for i, text in enumerate(letter):
        add(12, i, text, [90, 100 + 23 * i, 400, 112 + 23 * i])
    footer(12, 18)
    add(13, 0, "报价明细表", [90, 80, 400, 95])
    return cfg, native, footers


def _rebuild_text(block):
    block["text"] = "\n".join("\t".join(row) for row in block["rows"])


def _source_citation(blocks, ids):
    return {key: blocks[ids[0]][key] for key in ("source_id", "block_id", "locator")} | {"block_ids": list(ids)}


def test_exact_form_source_proof_separates_the_split_financial_cell_and_independent_last_row():
    config, native, footers = _native_preliminary_form()
    proof = build_response_review_table_proof(config, native, footers)
    assert proof is not None
    members = proof["content_block_ids"]
    assert members == [native[i]["block_id"] for i in (9, 10, 12, 14, 15, 16)]
    assert proof["financial_requirement_continuation"]["left"]["row"] == 8
    assert proof["financial_requirement_continuation"]["right"]["row"] == 0
    assert proof["last_row_is_independent"]["preceding"]["row"] == 7
    assert proof["last_row_is_independent"]["last"]["row"] == 0
    independent = proof["independent_requirement_rows"]
    assert [len(row["parts"]) for row in independent] == [1, 1, 1, 2, 1, 1, 1, 1]
    assert independent[0]["block_ids"] == independent[1]["block_ids"] and independent[0]["quote"] != independent[1]["quote"]

    changed = copy.deepcopy(native)
    changed[12]["rows"][5][2] = "替换报价条件。"
    _rebuild_text(changed[12])
    changed_proof = build_response_review_table_proof(config, changed, footers)
    assert changed_proof is not None and changed_proof["proof_sha256"] != proof["proof_sha256"]


@pytest.mark.parametrize("fault", ["missing_footer", "wrong_column", "swapped_rows", "missing_financial_tail", "last_row_join", "inside_table_stamp"])
def test_source_proof_rejects_unproven_continuation_or_boundary(fault):
    config, native, footers = _native_preliminary_form()
    if fault == "missing_footer":
        footers.pop(native[11]["block_id"])
    elif fault == "wrong_column":
        native[12]["column_widths"] = [52, 73, 240, 40, 40]
    elif fault == "swapped_rows":
        native[12]["rows"][1], native[12]["rows"][2] = native[12]["rows"][2], native[12]["rows"][1]
        _rebuild_text(native[12])
    elif fault == "missing_financial_tail":
        native[12]["rows"][0][2] = ""
        _rebuild_text(native[12])
    elif fault == "last_row_join":
        native[14]["rows"][0][1] = ""
        _rebuild_text(native[14])
    else:
        native[15]["bbox"] = [90, 96, 390, 106]
    assert build_response_review_table_proof(config, native, footers) is None


def test_citation_requires_complete_form_and_exact_reviewed_independent_row():
    config, native, footers = _native_preliminary_form()
    proof = build_response_review_table_proof(config, native, footers)
    blocks = {block["block_id"]: block for block in native}
    ids = proof["content_block_ids"]
    continuation = [proof["table_continuation"]]
    complete = _citation(_source_citation(blocks, ids), blocks, footers, table_continuations=continuation)
    assert complete["skipped_block_ids"] == [native[i]["block_id"] for i in (11, 13)]
    assert complete["table_continuation_proof_sha256"] == proof["proof_sha256"]
    for invalid in (ids[:-1], ids[1:4], [ids[0], ids[2], ids[1], *ids[3:]], [*ids, native[18]["block_id"]]):
        with pytest.raises(PlanningError, match="RESPONSE_REVIEW_TABLE_EXACT_FORM_SELECTION_REQUIRED"):
            _citation(_source_citation(blocks, invalid), blocks, footers, table_continuations=continuation)
    rows = proof["independent_requirement_rows"]
    company, manager = rows[:2]
    selected = _source_citation(blocks, company["block_ids"]) | {"table_row_id": company["table_row_id"]}
    assert _citation(selected, blocks, footers, table_rows=rows)["quote"] == company["quote"]
    with pytest.raises(PlanningError, match="TABLE_ROW_QUOTE_MISMATCH"):
        _citation({**selected, "quote": manager["quote"]}, blocks, footers, table_rows=rows)


def test_source_audit_binds_proof_to_the_selected_row_quote_and_cells():
    config, native, footers = _native_preliminary_form()
    proof = build_response_review_table_proof(config, native, footers)
    blocks = {block["block_id"]: block for block in native}
    document = {"source_id": "synthetic", "sha256": config["source_sha256"]}
    rows = {row["proof_sha256"]: row for row in proof["independent_requirement_rows"]}
    qualification, manager = proof["independent_requirement_rows"][:2]
    key, quote = qualification["block_id"], qualification["quote"]
    cells = ",".join(f"{c['block_id']}:r{c['row']}c{c['column']}" for c in qualification["cell_refs"])
    locator = (f"{qualification['locator']}; basis=frozen_table_rows_tsv_v1; selected_row_view_chars[0:{len(quote)}]; "
               f"quote_sha256={qualification['quote_sha256']}; cells={cells}; through={qualification['locator']}; blocks={key}; "
               f"reviewed_row_proof_sha256={qualification['proof_sha256']}")
    source = {"sha256": document["sha256"], "locator": locator}
    assert all(row == 5 for _, row, _, _, _ in _scope(source, quote, blocks, [document], [], footers, rows))
    source["locator"] = locator.replace(qualification["proof_sha256"], manager["proof_sha256"])
    with pytest.raises(ValueError, match="SOURCE_AUDIT_ROW_SCOPE_INVALID"):
        _scope(source, quote, blocks, [document], [], footers, rows)
