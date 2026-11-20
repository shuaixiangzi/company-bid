"""Deterministic native contract range and unsupported-layout regressions."""
from copy import deepcopy
import hashlib

import pytest

from company_bid.contract_clause_index import build_contract_clause_index
from company_bid.storage import digest


def source(*pages):
    document = {"source_id": "contract-test", "sha256": "1" * 64, "format": "pdf", "units": []}
    blocks = {}
    for page, contents in enumerate(pages, 1):
        unit = {"unit_id": f"U{page}", "page": page, "blocks": []}
        document["units"].append(unit)
        for ordinal, content in enumerate(contents):
            row = {"text": content} if isinstance(content, str) else content
            block_id = f"B{page}.{ordinal}"
            block = {"block_id": block_id, "source_id": document["source_id"], "origin": "native",
                     "kind": "paragraph", "locator": f"pdf/page[{page}]/line[{ordinal}]",
                     "bbox": [50, 70 + ordinal * 25, 450, 80 + ordinal * 25], **row}
            unit["blocks"].append(deepcopy(block))
            blocks[block_id] = {**block, "page": page, "unit_id": unit["unit_id"]}
    return document, blocks


def footer(number, bottom=790):
    return {"text": str(number), "bbox": [290, bottom, 305, bottom + 10]}


def check_spans(result, blocks):
    for span in result["spans"]:
        assert span["text"] == blocks[span["block_id"]]["text"][span["start_char"]:span["end_char"]]
    assert result["quote"] == "\n".join(span["text"] for span in result["spans"])
    assert result["quote_sha256"] == hashlib.sha256(result["quote"].encode("utf-8")).hexdigest()
    assert result["proof_sha256"] == digest({k: v for k, v in result.items() if k != "proof_sha256"})


def test_locates_native_body_excludes_toc_citations_quantity_and_annex():
    document, blocks = source(
        ["目录", "第一部分 采购要求", "第二部分 合同文本", "第三部分 附件清单", footer(31)],
        ["第一部分 采购要求", "第一条 报价", "1.1 非合同。", footer(32)],
        ["第二部分 合同文本", "目 录", "第一条合", "同范围................4", "第二条 履行.............5", footer(33)],
        ["第一条 实际范围", "1.1 共同范围：", "重量为", "1.2kg。", "1.2 条规定见说明。", "其余条件共同适用，", footer(34)],
        ["应提供 Q_GGW", "03001.2—2024 的资料。", "1.2 实际第二条。", "第二条 履行", "2.1 首款。", footer(35)],
        ["2.2 明确尾款。", "（以下无正文）", footer(36)],
        ["附件1：原件", "第一条 另一套", "1.1 附件正文。", footer(37)],
    )
    original = deepcopy((document, blocks))
    index = build_contract_clause_index(document, blocks)
    assert not index["unresolved"]
    assert [entry["clause_number"] for entry in index["entries"]] == ["1.1", "1.2", "2.1", "2.2"]
    first, _, third, last = index["entries"]
    assert first["quote"] == ("1.1 共同范围：\n重量为\n1.2kg。\n1.2 条规定见说明。\n"
                              "其余条件共同适用，\n应提供 Q_GGW\n03001.2—2024 的资料。")
    assert first["start"]["page"] == 4 and first["end"]["page"] == 5
    assert first["next_boundary"]["clause_number"] == "1.2"
    assert first["skipped"][0]["printed_page"] == 34
    assert {3, 4, 5, 6} <= set(first["skipped"][0]["evidence_pages"])
    assert third["next_boundary"]["page"] == 6
    assert last["next_boundary"]["kind"] == "contract_body_end"
    for entry in index["entries"]:
        assert entry["complete"]
        check_spans(entry, blocks)
    assert (document, blocks) == original
    assert index == build_contract_clause_index(document, blocks)


def test_exact_offsets_when_contract_has_multiline_blocks_and_inline_numbered_items():
    document, blocks = source([
        "合同条款及格式", "第一条 履行",
        "１．１ 约定如下：（1）提交一份；\r\n（2）其他安排写明。\r\n"
        "1.2 另列：（1）提交材料；（2）完成核验；\r\n(3)结果反馈。",
        "第一条约定适用于乙方。", "（合同正文结束）",
    ])
    first, second = build_contract_clause_index(document, blocks)["entries"]
    assert first["clause_number"] == "1.1"
    assert second["clause_number"] == "1.2"
    assert first["quote"] == "１．１ 约定如下：（1）提交一份；\r\n（2）其他安排写明。"
    assert second["start"]["start_char"] == blocks["B1.2"]["text"].index("1.2")
    assert second["intro_spans"][0]["text"] == "1.2 另列："
    assert [s["quote"] for s in second["items"]] == [
        "（1）提交材料；", "（2）完成核验；", "(3)结果反馈。\n第一条约定适用于乙方。",
    ]
    for result in [first, second, *second["items"]]:
        check_spans(result, blocks)


def test_chapter_level_scalar_lists_and_nested_letter_items_have_separate_ranges():
    document, blocks = source([
        "合同正文", "第一条 术语", "解释如下：", "1．模板；", "2.其他定义。", "第二条 通知",
        "2.1 按以下规则：", "（1）选项如下：", "(a) 电子送达；", "(b) 书面送达。",
        "（2）双方确保接收。", "2.2 另行约定。", "（本页无正文，为签章页）",
    ])
    index = build_contract_clause_index(document, blocks)
    first, second = index["sections"][0]["chapters"]
    main, following = index["entries"]
    assert [c["item_number"] for c in first["items"]] == ["1", "2"]
    assert first["intro_spans"][0]["text"] == "解释如下："
    assert first["items"][1]["next_boundary"]["chapter_number"] == 2
    assert second["items"] == []
    assert [x["item_number"] for x in main["items"]] == ["1", "2"]
    assert [x["item_number"] for x in main["items"][0]["items"]] == ["a", "b"]
    assert main["items"][0]["items"][-1]["next_boundary"]["item_number"] == "2"
    assert following["next_boundary"]["kind"] == "contract_body_end"


def test_quoted_reference_does_not_compete_with_real_decimal_and_nonnative_is_explicit():
    document, blocks = source([
        "合同条款", "第一条 范围", "1.1 原件含引述：“",
        "1.2 引述条款，", "第二条 引述标题", "这是引述。”",
        "1.2 本合同真实条款。", {"origin": "ocr", "text": "尚有 OCR 转录。"}, "（以下无正文）",
    ])
    index = build_contract_clause_index(document, blocks)
    assert len(index["sections"][0]["chapters"]) == 1
    first, second = index["entries"]
    assert "1.2 引述条款" in first["quote"] and first["next_boundary"]["text"] == "1.2 本合同真实条款。"
    assert first["complete"] and not second["complete"]
    assert {u["reason"] for u in index["unresolved"]} == {"non_native_or_unread_contract_content"}


@pytest.mark.parametrize("ambiguous", [
    ["1.1 第一项。", "1.3 序号跳过。"],
    ["1.1 第一项。", "1.1 此号重复。", "1.2 第二项。"],
    ["1.1 第一项。", "1.2 第二项。", "1.1.1 返回已闭合父项。"],
])
def test_ambiguous_decimal_sequence_exposed_without_certifying_assumed_range(ambiguous):
    index = build_contract_clause_index(*source(["合同文本", "第一条 范围", *ambiguous, "（以下无正文）"]))
    assert index["entries"] == []
    assert {x["reason"] for x in index["unresolved"]} == {"decimal_numbering_not_confirmed"}


def test_plain_number_without_independent_footer_family_remains_in_span_and_unresolved():
    index = build_contract_clause_index(*source(
        ["合同文本", "第一条 证据", "1.1 第一行，", footer(5)],
        ["可能是条款的一部分。", "1.2 结尾。", "（以下无正文）", footer(6)],
    ))
    first, second = index["entries"]
    assert first["skipped"] == []
    assert "\n5\n" in first["quote"] and not first["complete"]
    assert second["complete"]
    assert [u["reason"] for u in index["unresolved"]] == ["unproven_page_footer"]


def test_blank_native_overlay_keeps_layout_proof_under_actual_following_paragraph():
    document, blocks = source([
        "合同条款", "第一条 说明", {"text": "1.1 第一款。", "bbox": [50, 140, 450, 152]},
        {"text": "\t", "kind": "table", "rows": [["", ""]], "bbox": [180, 160, 240, 174]},
        {"text": "1.2 拟定  责任人。", "bbox": [50, 162, 450, 176]}, "（以下无正文）",
    ])
    first, second = build_contract_clause_index(document, blocks)["entries"]
    assert first["quote"] == "1.1 第一款。" and first["layout_evidence"] == []
    assert second["quote"] == "1.2 拟定  责任人。"
    assert second["layout_evidence"][0]["block_id"] == "B1.3"
    assert second["layout_evidence"][0]["owner"]["block_id"] == "B1.4"
    assert not first["skipped"] and not second["skipped"]


def test_short_leaf_at_end_kept_but_incomplete_when_physical_end_is_unproven():
    index = build_contract_clause_index(*source(["合同文本", "第一条 范围", "1.1 条款主体仍然"]))
    assert index["entries"][0]["next_boundary"]["kind"] == "unconfirmed_section_end"
    assert not index["entries"][0]["complete"]
    assert [row["reason"] for row in index["unresolved"]] == ["unconfirmed_section_end"]


def test_required_consistency_does_not_treat_legacy_missing_origin_as_native():
    document, blocks = source(["合同文本", "第一条 范围", "1.1 安全原则。"])
    for block in [*document["units"][0]["blocks"], *blocks.values()]:
        block.pop("origin")
    assert not build_contract_clause_index(document, blocks)["entries"]
    blocks["B1.2"]["text"] = "1.1 变更的原则。"
    with pytest.raises(ValueError, match="CONTRACT_CLAUSE_INDEX_SOURCE_MISMATCH"):
        build_contract_clause_index(document, blocks)
