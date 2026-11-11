"""N2 snippets cannot grant N3 permission contrary to full source context."""
from __future__ import annotations

import copy
import hashlib

import pytest

from company_bid.extraction import PlanningError, extract_requirements
from company_bid.planning import plan_bid


class Context:
    def __init__(self):
        self.captures = {}

    def run_unit(self, unit_id, input_data, fn):
        return fn()

    def capture(self, name, value, *, partial=False):
        self.captures[name] = copy.deepcopy(value)


def prepare(tmp_path, permission, *, previous=None, clipped_quote=None, window_blocks=60, title="8 技术服务方案"):
    tender = {"source_id": "tender", "title": "合成招标文件", "path": str(tmp_path / "synthetic-tender.txt"),
              "sha256": "a" * 64, "locator": "document", "kind": "tender", "provenance": "synthetic", "synthetic": True}
    company = {**tender, "source_id": "company", "kind": "manual_input", "sha256": "b" * 64}
    manifest = {"project": {"project_id": "P", "name": "合成测试项目", "source_refs": ["tender"]},
                "company": {"company_id": "C", "name": "合成测试公司", "source_refs": ["company"]},
                "sources": [tender, company], "source_roles": {"tender": "tender", "company": "company_material"},
                "facts": [], "attachments": [], "synthetic": True}
    texts = [title, *([previous] if previous is not None else []), permission, "应说明异常复测方法。"]
    blocks = [{"block_id": f"B{i}", "source_id": "tender", "locator": f"docx/body/p[{i}]", "kind": "paragraph", "text": text}
              for i, text in enumerate(texts)]
    units = [{"unit_id": f"U{i}", "source_id": "tender", "locator": block["locator"], "page": None,
              "coverage": {"status": "read"}, "blocks": [block]} for i, block in enumerate(blocks)]
    parsed = {"documents": [{"source_id": "tender", "role": "tender", "sha256": tender["sha256"], "units": units}],
              "coverage": {"status": "complete", "required_complete": True}}
    permission_block = blocks[-2]
    requirement_block = permission_block if clipped_quote is not None else blocks[-1]
    citation = {key: requirement_block[key] for key in ("source_id", "block_id", "locator")}
    # Live candidates now select the complete source block. Source-context
    # negation/actor assertions below still exercise the N3 permission guard.
    heading_citation = {key: blocks[0][key] for key in ("source_id", "block_id", "locator")}
    def candidate(ctx, task_type, unit_id, payload, validate):
        targets = {block["block_id"] for block in payload["window"]["blocks"]}
        requirements = [{"citation": citation, "category": "technical", "response_mode": "writing",
                         "format_status": "prescribed", "format_title": title}] if requirement_block["block_id"] in targets else []
        formats = [{"title": title, "level": 1, "citation": heading_citation,
                    "content_block_ids": [block["block_id"] for block in blocks[1:] if block["block_id"] in targets]}] if "B0" in targets else []
        return validate({"window_id": payload["window"]["window_id"], "processing_declaration": "processed_not_semantically_accepted", "unresolved_block_ids": [],
                         "requirements": requirements, "format_chapters": formats,
                         "format_status": "specified" if formats else "none_verified"})
    ctx = Context()
    extracted = extract_requirements(ctx, parsed, manifest, candidate, {"window_max_blocks": window_blocks})
    def no_model(*args):
        pytest.fail("Permission validation must not invoke a model")
    return ctx, extracted, parsed, manifest, no_model, blocks


@pytest.mark.parametrize("permission,previous,quote", [
    ("本项目技术方案必须逐项沿用指定格式，不得自行编写。", None, "自行编写"),
    ("自行编写的响应格式不予接受。", None, "自行编写"),
    ("投标人不得以改变规定章节、原表结构或附件顺序的方式自行编写。", None, "自行编写"),
    ("自行编写。", "投标人不得", None),
    ("自行编写。", "采购人", None),
    ("自行编写。", "由主管部门", None),
    ("负责人随后应当自行编写。", None, "自行编写"),
    ("格式自拟。", "若未提供统一模板", None),
    ("自行编写。", "经书面同意后方可", None),
    ("格式自拟，但须经书面批准。", None, "格式自拟"),
])
def test_source_negation_actor_and_conditions_cannot_be_clipped_or_split(tmp_path, permission, previous, quote):
    ctx, extracted, parsed, manifest, no_model, _ = prepare(
        tmp_path, permission, previous=previous, clipped_quote=quote, window_blocks=1)
    with pytest.raises(PlanningError, match="REQUIRED_FORMAT_UNRESOLVED"):
        plan_bid(ctx, extracted, parsed, manifest, no_model, {})
    assert any(error["reason"] == "technical_writing_space_not_grounded"
               for error in ctx.captures["n3-format-coverage"]["errors"])
    assert "n3-planning-report" not in ctx.captures


@pytest.mark.parametrize("permission,previous", [
    ("（响应人自行编制）", "本部分须保留以下声明文字。"),
    ("投标人可以自行编写技术方案。", None),
    ("格式自拟。", None),
    ("本技术方案格式自拟。", None),
    ("自行编写。", "由响应人"),
])
def test_clear_bidder_permission_remains_usable_and_quotes_full_source(tmp_path, permission, previous):
    ctx, extracted, parsed, manifest, no_model, blocks = prepare(
        tmp_path, permission, previous=previous, clipped_quote="自行编写" if "自行编写" in permission else None,
        window_blocks=1)
    prepared = plan_bid(ctx, extracted, parsed, manifest, no_model, {})
    assert len(prepared["sections"][0]["writing_tasks"]) == 1
    decision = ctx.captures["n3-planning-report"]["writing_space_decisions"][0]
    assert decision["decision"] == "grounded_open_body_in_prescribed_section"
    source = {block["block_id"]: block for block in blocks}
    for citation in decision["citations"]:
        expected = "\n".join(source[key]["text"] for key in citation["block_ids"])
        assert citation["quote"] == expected
        assert citation["quote_sha256"] == hashlib.sha256(expected.encode()).hexdigest()
        assert permission in citation["quote"]


def test_technical_title_alone_is_not_permission(tmp_path):
    ctx, extracted, parsed, manifest, no_model, _ = prepare(tmp_path, "本表应逐项填写。")
    with pytest.raises(PlanningError, match="REQUIRED_FORMAT_UNRESOLVED"):
        plan_bid(ctx, extracted, parsed, manifest, no_model, {})
