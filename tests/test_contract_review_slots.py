"""Synthetic-only review-slot evidence; no runtime, source files or providers."""
import copy
import hashlib

import pytest

from company_bid.contract_review_slots import contract_review_slot_catalog
from company_bid.extraction import PlanningError
from company_bid.storage import digest


def source(pages):
    document = {"source_id": "S", "sha256": "a" * 64, "format": "pdf", "role": "tender",
                "expected_units": len(pages), "units": []}
    for page, lines in enumerate(pages, 1):
        unit = {"unit_id": f"U{page}", "source_id": "S", "page": page, "locator": f"pdf/page[{page}]",
                "coverage": {"status": "read", "origin": "native", "image_count": 0,
                             "unread_objects": [], "unmapped_glyphs": []}, "blocks": []}
        for line, text in enumerate(lines):
            unit["blocks"].append({"block_id": f"B{page}-{line}", "source_id": "S", "kind": "paragraph",
                                   "origin": "native", "locator": f"pdf/page[{page}]/line[{line}]", "text": text,
                                   "bbox": [40, 30 + line * 20, 500, 45 + line * 20]})
        document["units"].append(unit)
    parsed = {"documents": [document], "coverage": {"required_complete": True}}
    manifest = {"sources": [{"source_id": "S", "sha256": "a" * 64}], "source_roles": {"S": "tender"}}
    return parsed, manifest


def rights():
    return source([["第六章 合同条款", "第三条 各方权利义务", "3.1甲方权利与义务",
        "(1) 甲方应向乙方提供技术资料、数据、材料或样品,甲方对其提供资料的准确",
        "性和完整性负责：", "(a) ；", "(b) ；", "(c) ；", "(d) ；",
        "(2) 甲方应向乙方提供工作条件：", "(3) 甲方应向乙方提供的其它协作：",
        "(4) 甲方有权检查合成服务的进度，乙方应提供书面说明。",
        "(5) 甲方有权对乙方的技术服务工作提出改进意见，乙方应当在 日内对甲方的",
        "意见进行调整、修改。", "(6) 甲方按约定支付费用。", "3.2乙方权利与义务",
        "(1) 乙方应提交合成测试报告。", "(2) 本合同项下技术服务项目结束后，乙方应当为甲方就相关报告内容的理解和",
        "实施过程中遇到的相关问题提供免费服务，具体包括： 。",
        "(3) 乙方应遵守合成保密约定。", "3.3其他：", "第四条 费用", "4.1费用按另行约定执行。"]])


def fees():
    return source([["第六章 合同条款", "第四条 费用",
        "4.1本合同项下技术服务费用含税总额为： 元人民币，(大写：人民币 )；不",
        "含税总额为： 元人民币，(大写：人民币 )；税率 %。",
        "本费用为测试用固定总价，含全部人工费用，不得另行增加费用。",
        "4.2技术服务费用由甲方按照以下第 条约定，付款方式为： □银行转账；□",
        "银行汇票；□其他 ，支付到本合同第4.4条规定的乙方银行账户。",
        "(1) 一次性支付：在服务结束并提交所有成果且通过验收后 日内，付清费用。",
        "(2) 分期支付：", "(a) 本合同生效后 日内，支付报酬总价 %；",
        "(b) 每一期付款均须书面约定。", "(c) 项目验收合格后 日内，支付至报酬总价的 %。",
        "4.3 技术服务费用由甲方按照以下第 条约定", "①不适用质量保证金。",
        "②适用质量保证金。质量保证金为合同总价款的 百分之五（5%）。",
        "在质量保证期结束后（且无服务质量问题后）十（10）个工作日一次支付。",
        "4.4乙方开户银行名称、地址、帐号和税号（如适用）为：", "开户银行：", "地址：",
        "银行联行行号：", "帐号：", "税号：", "4.5其他：", "第五条 履约担保", "5.1测试正文。"]])


def get(parsed, manifest):
    return contract_review_slot_catalog(parsed, manifest)


@pytest.mark.parametrize("character", ["\ufffd", "\ue001", "\x01", "\x7f", "\u200b", "\ud800", "\u0378"])
def test_rebuilt_catalogue_does_not_adopt_unknown_text_as_contract_pending(character):
    parsed, manifest = fees()
    # This substantive continuation is retained in the full amount clause.
    # Its unknown character must not disappear through lexical field matching.
    parsed["documents"][0]["units"][0]["blocks"][4]["text"] += character
    before = copy.deepcopy((parsed, manifest))
    try:
        result = get(parsed, manifest)
    except UnicodeEncodeError:
        # An unencodable surrogate also invalidates the complete N1 digest.
        assert character == "\ud800"
    else:
        assert not any("fee_amounts_and_rate" in item["slot_forms"] for item in result["items"])
    assert (parsed, manifest) == before


@pytest.mark.parametrize("space", ["\u00a0", "\u3000"])
def test_known_unicode_space_remains_in_catalogue_source_proof(space):
    parsed, manifest = fees()
    parsed["documents"][0]["units"][0]["blocks"][4]["text"] += space
    result = get(parsed, manifest)
    amount, = [item for item in result["items"] if "fee_amounts_and_rate" in item["slot_forms"]]
    assert amount["source_proof"]["members"][-1]["text"].endswith(space)


def test_rights_keep_full_obligations_and_parent_and_party_a_is_not_company_material_gap():
    parsed, manifest = rights()
    before = copy.deepcopy((parsed, manifest))
    result = get(parsed, manifest)
    assert len(result["items"]) == 6
    material, conditions, cooperation, deadline, free_service, other = result["items"]
    assert len(material["pending_fields"]) == 4
    assert all(item["owner_party"] == "甲方" for item in (material, conditions, cooperation))
    assert deadline["owner_party"] == free_service["owner_party"] == "乙方"
    assert "甲方有权" in deadline["text"] and "意见进行调整、修改。" in deadline["text"]
    assert "项目结束后" in free_service["text"] and "提供免费服务" in free_service["text"]
    assert material["text"].endswith("(d) ；")
    assert deadline["source_proof"]["parent_context"][0]["text"] == "3.1甲方权利与义务"
    assert "category" not in deadline and "response_mode" not in deadline
    assert other["owner_party"] is None
    assert (parsed, manifest) == before


def test_fees_preserve_all_conditions_and_fixed_numbers_are_not_pending():
    parsed, manifest = fees()
    result = get(parsed, manifest)
    amount, payment, deposit, bank, other = result["items"]
    assert len(amount["pending_fields"]) == 3
    assert "不得另行增加费用" in amount["text"]
    assert len(payment["pending_fields"]) == 7
    assert "通过验收后" in payment["text"] and "每一期付款均须书面约定" in payment["text"]
    assert len(deposit["pending_fields"]) == 1
    assert "5%" in deposit["text"] and "十（10）" in deposit["text"]
    assert deposit["owner_party"] is None
    assert len(bank["pending_fields"]) == 5 and bank["owner_party"] == "乙方"
    assert other["slot_forms"] == ["other_terms"]


def test_proof_identity_hashes_and_no_value_or_satisfaction_inference():
    parsed, manifest = rights()
    result = get(parsed, manifest)
    assert result["exhaustive"] is False and result["coverage_status"] == "limited_lexical_rules"
    assert result["uncovered_clauses"] and result["limitations"]
    for item in result["items"]:
        assert item["reading_status"] == "read"
        assert item["value_status"] == "value_not_supplied_in_read_text"
        assert item["physical_blank_status"] == item["satisfaction"] == "not_determined"
        assert item["values_inferred"] is False
        assert all(field["value"] is None and field["values_inferred"] is False and
                   field["applicability"] == "not_determined" for field in item["pending_fields"])
        proof = item["source_proof"]
        assert proof["parsed_sha256"] == digest(parsed)
        assert item["source_proof_sha256"] == digest(proof)
        assert proof["source_sha256"] == "a" * 64
        assert item["block_ids"] == [b["block_id"] for b in proof["members"]]
        for member in proof["members"]:
            assert member["text_sha256"] == hashlib.sha256(member["text"].encode()).hexdigest()
            assert member["page"] == 1 and member["locator"].startswith("pdf/page[1]/line[")
    assert get(parsed, manifest) == result


@pytest.mark.parametrize("change", ["failed", "needs_ocr", "glyph", "error", "image", "unknown_image_count",
    "ocr", "line_gap", "unit_locator", "block_locator", "source_hash", "expected_pages", "missing_page",
    "ignored", "replacement", "duplicate_id", "article", "chapter", "next_number"])
def test_failed_or_ambiguous_reading_and_boundaries_never_become_blank_values(change):
    parsed, manifest = rights()
    d, unit = parsed["documents"][0], parsed["documents"][0]["units"][0]
    if change in {"failed", "needs_ocr"}: unit["coverage"]["status"] = change
    elif change == "glyph": unit["coverage"]["unmapped_glyphs"] = [{"code": "unknown"}]
    elif change == "error": unit["coverage"]["error_code"] = "READ_FAILED"
    elif change == "image": unit["coverage"]["image_count"] = 1
    elif change == "unknown_image_count": unit["coverage"].pop("image_count")
    elif change == "ocr": unit["blocks"][12]["origin"] = "ocr"
    elif change == "line_gap": unit["blocks"][12]["locator"] = "pdf/page[1]/line[99]"
    elif change == "unit_locator": unit["locator"] = "pdf/page[7]"
    elif change == "block_locator": unit["blocks"][12]["locator"] = "pdf/page[2]/line[12]"
    elif change == "source_hash": d["sha256"] = "b" * 64
    elif change == "expected_pages": d["expected_units"] = 2
    elif change == "missing_page": unit["page"] = 2
    elif change == "ignored": unit["coverage"]["ignored_objects"] = [{"locator": unit["blocks"][12]["locator"], "reason": "test"}]
    elif change == "replacement": unit["blocks"][12]["text"] += "\ufffd"
    elif change == "duplicate_id": unit["blocks"][12]["block_id"] = unit["blocks"][0]["block_id"]
    elif change == "article": unit["blocks"][1]["text"] = "第五条 各方权利义务"
    elif change == "chapter": unit["blocks"][0]["text"] = "第六章 响应格式"
    elif change == "next_number": unit["blocks"][15]["text"] = "3.4乙方权利与义务"
    try:
        result = get(parsed, manifest)
    except PlanningError:
        assert change in {"source_hash", "duplicate_id"}
        return
    assert not any("improvement_deadline" in item["slot_forms"] for item in result["items"])
    assert all(item["reading_status"] == "read" for item in result["items"])


@pytest.mark.parametrize("text", [
    "甲方应向乙方提供工作条件：合成测试场地。", "甲方应向乙方提供的其它协作：书面通知。",
    "甲方有权对乙方的技术服务工作提出改进意见，乙方应当在3日内对甲方的意见进行调整、修改。",
    "本合同项下技术服务项目结束后，乙方应当为甲方就相关报告内容的理解和实施过程中遇到的相关问题提供免费服务，具体包括：合成培训。",
    "甲方可另行通知乙方在 日内提交合成报告。", "合成内容留有 一个空格。", "。", "其他：另有实质条款。",
    "本合同项下技术服务费用含税总额为：100元人民币，(大写：人民币壹佰)；税率6%。",
])
def test_substantive_values_fixed_numbers_and_unknown_single_spaces_are_not_matched(text):
    parsed, manifest = source([["第六章 合同条款", "第一条 合成测试", "1.1 " + text, "1.2合成正常条款。"]])
    result = get(parsed, manifest)
    assert result["items"] == []
    assert result["uncovered_clauses"][0]["reason"] == "not_matched_by_finite_rules"


@pytest.mark.parametrize("mark", ["☑", "☒", "■", "[x]", "√"])
def test_selected_mutually_exclusive_payment_options_are_not_reported_as_pending(mark):
    parsed, manifest = fees()
    parsed["documents"][0]["units"][0]["blocks"][5]["text"] = (
        parsed["documents"][0]["units"][0]["blocks"][5]["text"].replace("□银行转账", mark + "银行转账"))
    assert not any("payment_choices_and_schedule" in item["slot_forms"] for item in get(parsed, manifest)["items"])


def test_nonempty_material_list_and_missing_sibling_are_not_wholly_empty_list():
    for text in ("(b) 合成数据；", "(e) ；"):
        parsed, manifest = rights()
        parsed["documents"][0]["units"][0]["blocks"][6]["text"] = text
        assert not any("party_a_material_list" in item["slot_forms"] for item in get(parsed, manifest)["items"])


def test_attachment_scope_is_not_main_contract_and_list_reference_does_not_reassign_scope():
    parsed, manifest = source([
        ["第六章 合同条款", "第一条 主合同合成事项", "1.1其他：", "第二条 合成附件目录",
         "2.1 合成正文。", "附件四：合成廉洁条款", "附件五：合成保密条款", "2.2其他：", "第三条 合成尾项", "3.1合成正文。"],
        ["附件四：合成廉洁条款", "第一条 合成事项", "1.1其他：", "第二条 合成责任", "2.1合成正文。"],
        ["附件五：合成保密条款", "第一条 合成事项", "1.1其他：", "第二条 合成责任", "2.1合成正文。"],
    ])
    items = get(parsed, manifest)["items"]
    assert [item["scope"]["kind"] for item in items] == ["main_contract", "main_contract", "contract_attachment", "contract_attachment"]
    assert [item["scope"].get("attachment_number") for item in items] == [None, None, "四", "五"]
    assert len({item["pending_id"] for item in items}) == 4
    assert items[2]["clause_number"] == items[3]["clause_number"] == "1.1"


def test_cross_page_clause_retains_full_conditions_and_proven_footer_separation():
    parsed, manifest = source([
        ["第六章 合同条款", "第一条 合成前言", "1.1合成正文。", "1"],
        ["第二条 合成费用", "2.1本合同项下技术服务费用含税总额为： 元人民币，(大写：人民币 )；", "2"],
        ["本费用包含全部合成测试成本。", "2.2其他：", "第三条 合成末项", "3.1合成正文。", "3"],
    ])
    for unit in parsed["documents"][0]["units"]:
        unit["blocks"][-1]["bbox"] = [290, 780, 310, 790]
    items = get(parsed, manifest)["items"]
    amount = next(item for item in items if "fee_amounts_and_rate" in item["slot_forms"])
    assert amount["text"].endswith("本费用包含全部合成测试成本。")
    assert [member["page"] for member in amount["source_proof"]["members"]] == [2, 3]
    assert amount["source_proof"]["separated_page_footers"][0]["text"] == "2"
    assert amount["source_proof"]["separated_page_footers"][0]["footer_proof"]["rule"] == "pdf-page-footer-v1"
    parsed["documents"][0]["units"][2]["blocks"][0]["locator"] = "pdf/page[3]/line[5]"
    assert not any("fee_amounts_and_rate" in item["slot_forms"] for item in get(parsed, manifest)["items"])


def test_nested_decimal_clause_and_last_unbounded_clause_stay_explicitly_uncovered():
    parsed, manifest = source([["第六章 合同条款", "第一条 合成事项", "1.1合成条件如下：",
                                "1.1.1其他：", "1.2其他："]])
    result = get(parsed, manifest)
    assert not result["items"]
    assert result["uncovered_clauses"][-1]["reason"] == "source_or_boundary_not_proven"
    assert any("nested" in limitation for limitation in result["limitations"])


def test_article_preamble_is_preserved_with_its_own_source_proof():
    parsed, manifest = source([["第六章 合同条款", "第一条 合成条件",
                                "下列各项仅在甲方书面要求且双方另行确认后适用。", "1.1其他：", "1.2合成尾项。"]])
    item, = get(parsed, manifest)["items"]
    assert item["source_proof"]["article_preamble"][0]["text"].startswith("下列各项仅在甲方书面要求")
    parsed["documents"][0]["units"][0]["blocks"][2]["locator"] = "pdf/page[1]/line[20]"
    assert not get(parsed, manifest)["items"]


def test_inline_attachment_with_own_article_is_scoped_and_ambiguous_attachment_stops_inheritance():
    parsed, manifest = source([["第六章 合同条款", "第一条 合成前言", "1.1合成正文。",
                                "附件五：合成保密条款", "第一条 合成定义", "1.1其他：", "1.2合成尾项。"]])
    item, = get(parsed, manifest)["items"]
    assert item["scope"]["kind"] == "contract_attachment" and item["scope"]["attachment_number"] == "五"
    parsed["documents"][0]["units"][0]["blocks"].insert(4, {
        "block_id": "AMBIGUOUS", "source_id": "S", "kind": "paragraph", "origin": "native",
        "locator": "pdf/page[1]/line[4]", "text": "此处作用域尚不能证成。"})
    for i, block in enumerate(parsed["documents"][0]["units"][0]["blocks"]):
        block["locator"] = f"pdf/page[1]/line[{i}]"
    assert not get(parsed, manifest)["items"]
    assert get(parsed, manifest)["unresolved_scopes"]


def acceptance():
    return source([["第六章 合同条款", "第八条 合成验收",
        "8.1双方确定，按以下标准和方式对乙方提交的技术服务工作成果进行验收：",
        "(1) 技术服务成果形式： ,份数为 份，介质为 。",
        "(2) 技术服务工作成果的验收程序：各项技术服务工作成果自完成之日起 日内，",
        "乙方应书面提请验收。验收不合格时应免费修改，并自验收不合格之日起 日内重新提请验收。",
        "任一项技术服务工作成果连续 次验收不合格的，则视为未完成该项合成服务。",
        "甲方有权停止支付剩余合成服务费用，并按测试约定扣减费用。",
        "(3) 技术服务工作成果的验收标准：按合成测试约定执行。",
        "(4) 技术服务工作成果的验收方法： 。", "(5) 验收时间和地点： 。",
        "8.2质量保证期为固定12个月。"]])


def test_acceptance_complete_numbered_subclauses_keep_conditions_and_consequences():
    parsed, manifest = acceptance()
    items = get(parsed, manifest)["items"]
    assert [item["subclause_number"] for item in items] == ["1", "2", "4", "5"]
    assert [len(item["pending_fields"]) for item in items] == [3, 3, 1, 1]
    procedure = items[1]
    assert len(procedure["block_ids"]) == 4
    assert "免费修改" in procedure["text"] and "停止支付" in procedure["text"] and "扣减费用" in procedure["text"]
    assert procedure["source_span_kind"] == "complete_numbered_subclause"
    assert procedure["source_proof"]["subclause_next_boundary"]["text"].startswith("(3)")
    assert "双方确定" in procedure["source_proof"]["parent_context"][0]["text"]
    assert all(item["satisfaction"] == "not_determined" and item["owner_party"] is None for item in items)


@pytest.mark.parametrize("change", ["subitem_gap", "read_gap", "fixed", "unmapped"])
def test_acceptance_unknown_structure_or_real_values_cannot_be_empty_values(change):
    parsed, manifest = acceptance()
    rows = parsed["documents"][0]["units"][0]["blocks"]
    if change == "subitem_gap": rows[8]["text"] = rows[8]["text"].replace("(3)", "(6)")
    elif change == "read_gap": rows[6]["locator"] = "pdf/page[1]/line[90]"
    elif change == "unmapped": parsed["documents"][0]["units"][0]["coverage"]["unmapped_glyphs"] = ["?"]
    elif change == "fixed":
        rows[4]["text"] = rows[4]["text"].replace("日起 日内", "日起3日内")
        rows[5]["text"] = rows[5]["text"].replace("日起 日内", "日起5日内")
        rows[6]["text"] = rows[6]["text"].replace("连续 次", "连续2次")
    assert not any("acceptance_deadlines" in item["slot_forms"] for item in get(parsed, manifest)["items"])


def ip_source():
    return source([["第六章 合同条款", "第十一条 合成知识产权",
        "11.3 受限于第0条和第0条规定的效力，如果乙方利用甲方提供的技术资料实现任何第0条所述技术服务工作成果以外的新技术成果，",
        "这些新技术成果及其知识产权的归属采用以下第 种方式：",
        "(1) 归甲方所有，未经甲方同意乙方不得向第三方披露合成成果。",
        "(2) 归甲方所有，乙方 □有权无偿使用；□可以在双方协商确定向甲方支付的使用价格后有偿使用，但不得擅自许可。",
        "(3) 归乙方所有，甲方 有权无偿 使用。未经乙方同意，甲方不得向第三方披露合成成果。",
        "(4) 双方共有，任何一方都有权自己使用；但任何一方转让均须对方同意，收益按甲方占50%，乙方占50%的比例进行分配。",
        "11.4其他：", "第十二条 合成通知"]])


def test_ip_choice_and_literal_zero_references_are_separate_unresolved_statuses():
    parsed, manifest = ip_source()
    item = get(parsed, manifest)["items"][0]
    assert item["slot_forms"] == ["ip_choice_with_literal_zero_references"]
    assert len(item["block_ids"]) == 6 and len(item["pending_fields"]) == 2
    assert "不得向第三方披露" in item["text"] and "50%" in item["text"]
    assert item["owner_party"] is None and item["satisfaction"] == "not_determined"
    issue, = item["blocking_issues"]
    assert issue["value_status"] == "reference_requires_clarification" and issue["resolved_reference"] is None
    assert item["reference_status"] == "requires_clarification"
    assert item["value_status"] == "value_not_supplied_in_read_text"
    assert issue["literal_reference"] == "第0条" and issue["occurrence_count"] == 3


@pytest.mark.parametrize("change", ["chosen", "checked", "missing_alternative", "other_bad_reference", "no_zero", "read_gap"])
def test_ip_rule_cannot_resolve_choices_or_generalize_unknown_references(change):
    parsed, manifest = ip_source()
    rows = parsed["documents"][0]["units"][0]["blocks"]
    if change == "chosen": rows[3]["text"] = rows[3]["text"].replace("第 种", "第2种")
    elif change == "checked": rows[5]["text"] = rows[5]["text"].replace("□有权", "☑有权")
    elif change == "missing_alternative": rows[6]["text"] = rows[6]["text"].replace("(3)", "(5)")
    elif change == "other_bad_reference": rows[2]["text"] = rows[2]["text"].replace("第0条", "第99条", 1)
    elif change == "no_zero": rows[2]["text"] = rows[2]["text"].replace("第0条", "第1条")
    elif change == "read_gap": rows[6]["locator"] = "pdf/page[1]/line[99]"
    assert not any("ip_choice_with_literal_zero_references" in item["slot_forms"] for item in get(parsed, manifest)["items"])


def contract_tail():
    parsed, manifest = source([
        ["第六章 合同条款", "第二十六条 合成前言", "26.1合成正文。", "1"],
        ["第二十七条 合同生效及其他事项", "27.8合成固定条款。", "27.9其他：", "（以下无正文）", "2"],
        ["（本页无正文，为《 》签章页）", "甲方：", "（盖章）", "法定代表人（负责人）或委托代理人：", "日期：",
         "乙方：", "（盖章）", "法定代表人（负责人）或委托代理人：", "日期：", "3"],
    ])
    for unit in parsed["documents"][0]["units"]:
        unit["blocks"][-1]["bbox"] = [290, 780, 310, 790]
    return parsed, manifest


def test_contract_tail_uses_explicit_end_and_complete_signature_page_without_fixed_clause_number():
    parsed, manifest = contract_tail()
    item, = get(parsed, manifest)["items"]
    assert item["clause_number"] == "27.9" and item["scope"]["kind"] == "main_contract"
    proof = item["source_proof"]["contract_end_boundary"]
    assert proof["kind"] == "explicit_contract_end_and_native_signature_page"
    assert len(proof["members"]) == 10
    assert proof["separated_page_footers"][0]["footer_proof"]["rule"] == "pdf-page-footer-v1"


@pytest.mark.parametrize("change", ["arbitrary_boundary", "missing_signature", "unknown_footer", "partial_signature", "gap", "ocr"])
def test_contract_tail_does_not_accept_arbitrary_text_or_unproven_signature_reading(change):
    parsed, manifest = contract_tail()
    units = parsed["documents"][0]["units"]
    if change == "arbitrary_boundary": units[1]["blocks"][3]["text"] = "一般合成说明。"
    elif change == "missing_signature": units[2]["blocks"][0]["text"] = "合成说明页。"
    elif change == "unknown_footer": units[1]["blocks"][-1]["bbox"] = [20, 100, 30, 110]
    elif change == "partial_signature": units[2]["blocks"][7]["text"] = "未知签署项目："
    elif change == "gap": units[2]["blocks"][7]["locator"] = "pdf/page[3]/line[99]"
    elif change == "ocr": units[2]["blocks"][7]["origin"] = "ocr"
    assert not get(parsed, manifest)["items"]


def attachment_tail():
    parsed, manifest = source([
        ["第六章 合同条款", "第一条 合成主合同", "1.1合成正文。", "1"],
        ["附件五：合成保密条款", "第一条 合成事项", "1.1合成正文。", "2"],
        ["第七条 合成责任", "7.1乙方按合成条件承担责任。", "7.2其他：", "3"],
    ])
    for unit in parsed["documents"][0]["units"]:
        unit["blocks"][-1]["bbox"] = [290, 780, 310, 790]
    return parsed, manifest


def test_final_attachment_other_has_expected_reading_and_geometric_footer_boundary():
    parsed, manifest = attachment_tail()
    item, = get(parsed, manifest)["items"]
    assert item["scope"]["kind"] == "contract_attachment" and item["scope"]["attachment_number"] == "五"
    assert item["clause_number"] == "7.2" and item["text"] == "7.2其他："
    proof = item["source_proof"]["document_end_boundary"]
    assert proof["kind"] == "expected_last_native_page_with_proven_footer" and proof["expected_units"] == 3
    assert proof["footer_proof"]["rule"] == "pdf-page-footer-v1" and len(proof["last_page_members"]) == 4


@pytest.mark.parametrize("change", ["no_expected_units", "wrong_expected_units", "not_complete", "footer_geometry",
    "extra_text", "not_footer", "ocr", "line_gap", "substantive", "main_scope"])
def test_document_end_is_not_an_arbitrary_file_end_or_a_reading_exemption(change):
    parsed, manifest = attachment_tail()
    d = parsed["documents"][0]
    unit = d["units"][-1]
    if change == "no_expected_units": d.pop("expected_units")
    elif change == "wrong_expected_units": d["expected_units"] = 4
    elif change == "not_complete": parsed["coverage"]["required_complete"] = False
    elif change == "footer_geometry": unit["blocks"][-1]["bbox"] = [20, 100, 30, 110]
    elif change == "extra_text": unit["blocks"][-1]["text"] = "3 后续文字尚未定界。"
    elif change == "not_footer": unit["blocks"][-1]["text"] = "未知尾项。"
    elif change == "ocr": unit["blocks"][0]["origin"] = "ocr"
    elif change == "line_gap": unit["blocks"][1]["locator"] = "pdf/page[3]/line[99]"
    elif change == "substantive": unit["blocks"][2]["text"] = "7.2其他：合成明确约定。"
    elif change == "main_scope": d["units"][1]["blocks"][0]["text"] = "合成普通章节说明。"
    assert not get(parsed, manifest)["items"]


@pytest.mark.parametrize("text, kind, field_count, owner", [
    ("乙方应当按照下列进度要求向甲方提供技术服务：\n本技术服务项目的履行期限为： 至 。具体进度要求为：\n。", "schedule", 2, None),
    ("乙方将按照下列质量要求向甲方提供技术服务：\n。", "quality", 1, None),
    ("在履行本合同期间，每一方应承担自身税费。本合同执行 行业增值税税率，固定其他税费条件。", "industry_tax_basis", 1, None),
    ("开具发票所需甲方信息：\n纳税人名称：\n税务登记号：\n开户行名称：\n开户行账号：\n税务登记地址：\n财务电话：", "invoice_details", 6, "甲方"),
    ("乙方应在甲方向乙方支付每期费用之前针对 □全部；□当期服务费用 向甲方开具合法发票。", "invoice_scope_choice", 1, None),
    ("乙方保证其在本合同签署之日 □属于；□不属于 依据合成企业划型标准确定的中小企业。", "sme_choice", 1, "乙方"),
    ("技术服务工作成果验收完成后 个工作日内，乙方应归还甲方交予的全部资料、数据等，不得擅自留存复制品，或者乙方应按照甲方允许的方式销毁全部资料、数据。", "return_materials_deadline", 1, "乙方"),
])
def test_other_finite_forms(text, kind, field_count, owner):
    lines = text.splitlines()
    lines[0] = "1.1" + lines[0]
    parsed, manifest = source([["第六章 合同条款", "第一条 合成事项", *lines, "1.2合成尾项。"]])
    item, = get(parsed, manifest)["items"]
    assert item["slot_forms"] == [kind]
    assert len(item["pending_fields"]) == field_count and item["owner_party"] == owner
