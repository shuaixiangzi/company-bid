"""Source-shaped fixed letters and drafting notes; synthetic data, no network."""
from __future__ import annotations

import copy
import hashlib

import pytest

from company_bid.extraction import _index, _heading_level, extract_requirements
from company_bid.planning import PlanningError, _format_plan, plan_bid


class Context:
    def __init__(self):
        self.captures = {}

    def run_unit(self, unit_id, input_data, fn):
        return fn()

    def capture(self, name, payload, *, partial=False):
        self.captures[name] = copy.deepcopy(payload)


def fixture(tmp_path):
    tender = {"source_id": "tender", "title": "合成招标文件", "path": str(tmp_path / "tender.pdf"),
              "sha256": "a" * 64, "locator": "document", "kind": "tender", "provenance": "synthetic", "synthetic": True}
    company = {**tender, "source_id": "company", "kind": "manual_input", "sha256": "b" * 64}
    manifest = {"project": {"project_id": "P", "name": "合成项目", "source_refs": ["tender"]},
                "company": {"company_id": "C", "name": "合成公司", "source_refs": ["company"]},
                "sources": [tender, company], "source_roles": {"tender": "tender", "company": "company_material"},
                "facts": [], "attachments": [], "synthetic": True}
    parsed = {"documents": [{"source_id": "tender", "role": "tender", "sha256": tender["sha256"],
                              "format": "pdf", "expected_units": 0, "units": []}],
              "coverage": {"status": "complete", "required_complete": True}}
    return manifest, parsed


def line(parsed, page, text, *, footer=False):
    units = parsed["documents"][0]["units"]
    unit = next((unit for unit in units if unit["page"] == page), None)
    if unit is None:
        unit = {"unit_id": f"P{page}", "source_id": "tender", "locator": f"pdf/page[{page}]", "page": page,
                "coverage": {"status": "read", "origin": "native", "image_count": 0,
                             "unread_objects": [], "unmapped_glyphs": []}, "blocks": []}
        units.append(unit)
        parsed["documents"][0]["expected_units"] = len(units)
    index = len(unit["blocks"])
    block = {"block_id": f"P{page}-L{index}", "source_id": "tender", "kind": "paragraph", "origin": "native", "text": text,
             "locator": f"pdf/page[{page}]/line[{index}]",
             "bbox": [60, 773, 80, 782] if footer else [60, 60 + index * 14, 500, 72 + index * 14]}
    unit["blocks"].append(block)
    return block


def cite(block):
    return {key: block[key] for key in ("source_id", "block_id", "locator")}


def no_call(*args):
    pytest.fail("N3 must not call a provider")


def extract(ctx, parsed, manifest, chapters, *, requirement=None, declared=False):
    def candidate(ctx, task_type, unit_id, payload, validate):
        targets = {block["block_id"] for block in payload["window"]["blocks"]}
        formats = [{"title": heading["text"], "level": 1, "citation": cite(heading),
                    "content_block_ids": [block["block_id"] for block in content if block["block_id"] in targets] if declared else []}
                   for heading, content in chapters if heading["block_id"] in targets]
        requirements = []
        if requirement and requirement["block_id"] in targets:
            requirements = [{"citation": cite(requirement), "category": "technical", "response_mode": "writing",
                             "format_status": "prescribed", "format_title": chapters[0][0]["text"]}]
        return validate({"window_id": payload["window"]["window_id"],
                         "processing_declaration": "processed_not_semantically_accepted", "unresolved_block_ids": [],
                         "requirements": requirements, "format_chapters": formats,
                         "format_status": "specified" if formats else "none_verified"})
    return extract_requirements(ctx, parsed, manifest, candidate, {"window_max_blocks": 60 if declared else 3})


def letter(tmp_path, *, next_title="16. 授权委托书", title="6 声明函"):
    manifest, parsed = fixture(tmp_path)
    line(parsed, 1, "第三章 响应文件格式")
    heading = line(parsed, 1, title)
    content = [line(parsed, 1, "我方郑重声明："), line(parsed, 1, "以下原文条件及选项按规定保留。")]
    for number, text in enumerate([
        "具有明确的原文条件；", "具备原文规定的条件；", "具有原文所列记录；",
        "参加活动时遵守原文规则；", "未参与原文禁止的活动；", "按照原文提供明确资料；",
    ], 1):
        content.append(line(parsed, 1, f"{number}. {text}"))
    line(parsed, 1, "1", footer=True)
    for text in [
        "7. 不存在与其他响应人具有原文禁止的管理关系并共同参加本次活动",
        "的情形。", "8. 我方保留以下原文选项；",
        "9. 我公司在原文规定期间没有发生原文列明的重大事项",
        "及其相关情形。", "10. 未被主管机关在原文规定的系统中列入禁止名单",
        "或其他限制范围。", "11. 我方遵守原文列明的规则；",
        "12. 我公司承诺保留所有原文条款并承担相应责任",
        "和原文规定的后果。", "13. 我公司与原文相关单位之间的信息据实申报",
        "（注意保留原文有或无选项）。", "14. 本项目经理在服务期间遵守原文有关任职的规则",
        "如需更换按原文程序办理，相关条件",
    ]:
        content.append(line(parsed, 2, text))
    line(parsed, 2, "2", footer=True)
    for text in ["不得低于采购文件要求。", "15. （原文件如有其他要求可自行补充）",
                 "如违反以上声明，我方承担原文规定的责任。", "响应人：（盖单位章）",
                 "法定代表人或委托代理人：（签字）", "年 月 日"]:
        content.append(line(parsed, 3, text))
    line(parsed, 3, "3", footer=True)
    next_heading = line(parsed, 4, next_title)
    next_body = line(parsed, 4, "以下为另一份格式的原文。")
    line(parsed, 4, "4", footer=True)
    contract = line(parsed, 5, "第四章 合同条款")
    contract_body = line(parsed, 5, "合同正文应留在格式区域之外。")
    line(parsed, 5, "5", footer=True)
    return manifest, parsed, heading, content, next_heading, next_body, contract, contract_body


@pytest.mark.parametrize("declared", [False, True])
def test_numbered_fixed_letter_keeps_complete_cross_page_original_until_next_form(tmp_path, declared):
    manifest, parsed, heading, content, following, following_body, contract, contract_body = letter(tmp_path)
    ctx = Context()
    extracted = extract(ctx, parsed, manifest, [(heading, content), (following, [following_body])], declared=declared)
    prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    fixed = prepared["sections"][0]["fixed_content"]
    assert [block["text"] for block in fixed] == [block["text"] for block in content]
    assert all(block["source_refs"] for block in fixed)
    assert not prepared["sections"][0]["writing_tasks"]
    all_text = [block.get("text") for section in prepared["sections"] for block in section["fixed_content"]]
    assert contract["text"] not in all_text and contract_body["text"] not in all_text
    assert prepared["sections"][1]["fixed_content"][0]["text"] == following_body["text"]
    ledger = {row["block_id"]: row for row in ctx.captures["n3-format-coverage"]["blocks"]}
    assert ctx.captures["n3-format-coverage"]["status"] == "complete"
    assert sum(row["status"] == "skipped_page_footer" for row in ledger.values()) == 5
    assert ledger[heading["block_id"]]["boundary_basis"]["block_id"] == following["block_id"]
    for number in [7, 9, 10, 12, 13, 14, 15]:
        block = next(block for block in content if block["text"].startswith(f"{number}."))
        assert _heading_level(block["text"]) == 1
        basis = ledger[block["block_id"]]["continuation_basis"]
        assert basis["item_number"] == number and basis["previous_item_block_id"]
        assert basis["anchor_block_id"] == heading["block_id"]
        assert basis["text_sha256"] == hashlib.sha256(block["text"].encode()).hexdigest()
        assert basis["proof_sha256"]


def test_missing_next_form_is_not_swallowed_even_with_consecutive_number(tmp_path):
    manifest, parsed, heading, content, following, following_body, *_ = letter(tmp_path)
    ctx = Context()
    extracted = extract(ctx, parsed, manifest, [(heading, content)])
    blocks, _, documents = _index(parsed, manifest)
    formats, ledger, errors = _format_plan(extracted, blocks, documents)
    assert following["block_id"] not in formats[0]["content_block_ids"]
    assert following_body["block_id"] not in formats[0]["content_block_ids"]
    assert any(error["reason"] == "unextracted_format_heading" and error["block_id"] == following["block_id"] for error in errors)
    assert next(row for row in ledger if row["block_id"] == following_body["block_id"])["status"] == "unresolved"
    with pytest.raises(PlanningError, match="REQUIRED_FORMAT_UNRESOLVED"):
        plan_bid(ctx, extracted, parsed, manifest, no_call, {})


@pytest.mark.parametrize("change", ["no_introducer", "no_list_start", "gap", "different_marker", "non_letter", "unknown_heading"])
def test_ambiguous_numbered_blocks_do_not_gain_fixed_letter_ownership(tmp_path, change):
    manifest, parsed, heading, content, following, following_body, *_ = letter(tmp_path)
    risky = next(block for block in content if block["text"].startswith("7."))
    if change == "no_introducer":
        content[0]["text"] = "本部分列出相关资料。"
    elif change == "no_list_start":
        content[2]["text"] = "原文说明，不是编号列表。"
    elif change == "gap":
        risky["text"] = risky["text"].replace("7.", "17.", 1)
    elif change == "different_marker":
        risky["text"] = risky["text"].replace("7.", "7、", 1)
    elif change == "non_letter":
        heading["text"] = "6 技术服务方案"
    else:
        risky["text"] = "7. 其他响应内容"
    ctx = Context()
    extracted = extract(ctx, parsed, manifest, [(heading, content), (following, [following_body])])
    blocks, _, documents = _index(parsed, manifest)
    formats, ledger, errors = _format_plan(extracted, blocks, documents)
    assert risky["block_id"] not in formats[0]["content_block_ids"]
    assert content[9]["block_id"] not in formats[0]["content_block_ids"]
    assert not next(row for row in ledger if row["block_id"] == risky["block_id"]).get("continuation_basis")
    assert errors


@pytest.mark.parametrize("instruction", [
    "格式自拟，根据技术方案及详细评审中的技术部分要求编制",
    "（响应人自行编制）", "投标人可以自行编写技术方案。", "本技术方案格式自拟。",
])
def test_whole_grounded_drafting_instruction_is_evidence_not_final_body(tmp_path, instruction):
    manifest, parsed = fixture(tmp_path)
    heading = line(parsed, 1, "4 技术服务方案")
    fixed = line(parsed, 1, "本部分保留以下原文说明。")
    permission = line(parsed, 1, instruction)
    requirement = line(parsed, 1, "应说明测点编号和异常复测方法。")
    ctx = Context()
    extracted = extract(ctx, parsed, manifest, [(heading, [fixed, permission, requirement])], requirement=requirement, declared=True)
    prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    texts = [block["text"] for block in prepared["sections"][0]["fixed_content"]]
    assert fixed["text"] in texts and instruction not in texts
    assert len(prepared["sections"][0]["writing_tasks"]) == 1
    report = ctx.captures["n3-planning-report"]
    assert any(instruction in citation["quote"] for decision in report["writing_space_decisions"] for citation in decision["citations"])
    omission = report["drafting_instruction_omissions"][0]
    assert omission["block_id"] == permission["block_id"] and omission["source_ref"]
    assert omission["text_sha256"] == hashlib.sha256(instruction.encode()).hexdigest()


@pytest.mark.parametrize("text", ["不得格式自拟。", "格式自拟。我方承诺保留本句原文。", "格式自拟，根据技术要求编制，并保留以下签字要求。"])
def test_negative_or_mixed_text_is_preserved_when_other_permission_exists(tmp_path, text):
    manifest, parsed = fixture(tmp_path)
    heading = line(parsed, 1, "4 技术服务方案")
    permission = line(parsed, 1, "格式自拟。")
    fixed = line(parsed, 1, text)
    requirement = line(parsed, 1, "应说明异常复测方法。")
    ctx = Context()
    extracted = extract(ctx, parsed, manifest, [(heading, [permission, fixed, requirement])], requirement=requirement, declared=True)
    prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    assert fixed["text"] in [block["text"] for block in prepared["sections"][0]["fixed_content"]]
    assert fixed["block_id"] not in {row["block_id"] for row in ctx.captures["n3-planning-report"]["drafting_instruction_omissions"]}


@pytest.mark.parametrize("text", ["不得格式自拟。", "格式自拟的响应文件不予接受。"])
def test_negative_instruction_cannot_authorize_free_writing_or_removal(tmp_path, text):
    manifest, parsed = fixture(tmp_path)
    heading = line(parsed, 1, "4 技术服务方案")
    instruction = line(parsed, 1, text)
    requirement = line(parsed, 1, "应说明异常复测方法。")
    ctx = Context()
    extracted = extract(ctx, parsed, manifest, [(heading, [instruction, requirement])], requirement=requirement, declared=True)
    with pytest.raises(PlanningError, match="REQUIRED_FORMAT_UNRESOLVED"):
        plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    assert "n3-planning-report" not in ctx.captures
