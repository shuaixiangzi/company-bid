"""A proved source cover is the cover, with source slots still explicitly pending."""
import copy

import pytest

from company_bid.extraction import PlanningError, _index, extract_requirements
from company_bid.planning import _format_plan, plan_bid
from test_n3_source_roles import line
from test_source_selected_citations import Context, fixture, no_call


COVER = ["响应文件", "项目编号：", "响应人：（盖单位章）",
         "法定代表人或其委托代理人：（签字或盖章）", "年 月 日"]


def cover_case(tmp_path, change=None):
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"
    line(parsed, "REGION", "第五章 响应文件格式")
    for i, text in enumerate(COVER):
        line(parsed, f"COVER{i}", text, page=2)
    line(parsed, "FORM", "一、合成说明书", page=3)
    line(parsed, "BODY", "合成模板的固定声明原文。", page=3)
    line(parsed, "END", "第六章 合同条款", page=4)

    def candidate(ctx, task, unit, payload, validate):
        targets = {value["block_id"]: value for value in payload["window"]["blocks"]}
        chapters = []
        if "COVER0" in targets:
            chapters.append({"title": COVER[0], "level": 1, "citation": {"block_ids": ["COVER0"]},
                "content_block_ids": [f"COVER{i}" for i in range(len(COVER))], "requirement_indices": []})
        if "FORM" in targets:
            chapters.append({"title": targets["FORM"]["text"], "level": 1,
                "citation": {"block_ids": ["FORM"]}, "content_block_ids": ["BODY"], "requirement_indices": []})
        raw = {"window_id": payload["window"]["window_id"],
               "processing_declaration": "processed_not_semantically_accepted", "unresolved_block_ids": [],
               "requirements": [], "format_chapters": chapters,
               "format_status": "specified" if chapters else "none_verified"}
        if change:
            change(raw)
        return validate(raw)

    return manifest, parsed, extract_requirements(Context(), parsed, manifest, candidate, {})


def test_source_cover_promoted_with_exact_structural_requirement_and_all_empty_fields_pending(tmp_path):
    manifest, parsed, extracted = cover_case(tmp_path)
    frozen = copy.deepcopy((manifest, parsed, extracted))
    ctx = Context()
    result = plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    section = next(item for item in result["sections"] if item["section_id"] == result["prescribed_cover_section_id"])
    assert section["title"] == COVER[0]
    assert [value["text"] for value in section["fixed_content"]] == COVER[1:]
    assert not any(section[key] for key in ("fact_refs", "writing_tasks", "table_refs", "attachment_refs"))
    structural, = [item for item in result["requirements"] if item["requirement_id"] in section["requirement_refs"]]
    assert structural["text"] == "\n".join(COVER)
    assert structural["source_refs"] == section["source_refs"]
    assert len([issue for issue in result["issues"] if issue["section_id"] == section["section_id"]]) == 4
    report = ctx.captures["n3-planning-report"]["prescribed_cover"]
    assert report["section_id"] == section["section_id"] and len(report["source_proofs"]) == 1
    assert [item["block_id"] for item in report["source_proofs"][0]["members"]] == [f"COVER{i}" for i in range(5)]
    assert (manifest, parsed, extracted) == frozen


def test_source_cover_without_grounded_template_cannot_fall_back_to_generic_cover(tmp_path):
    def remove(raw):
        raw["format_chapters"] = [item for item in raw["format_chapters"] if item["title"] != COVER[0]]
        raw["format_status"] = "specified" if raw["format_chapters"] else "none_verified"
    manifest, parsed, extracted = cover_case(tmp_path, remove)
    ctx = Context()
    with pytest.raises(PlanningError, match="REQUIRED_FORMAT_UNRESOLVED"):
        plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    assert any(item["reason"] == "prescribed_cover_source_incomplete" for item in ctx.captures["n3-format-coverage"]["errors"])


def test_semantic_cover_requirement_and_structural_template_share_one_source_cover(tmp_path):
    def include_semantic_requirement(raw):
        for chapter in raw["format_chapters"]:
            if chapter["title"] == COVER[0]:
                raw["requirements"].append({"citation": {"block_ids": [f"COVER{i}" for i in range(5)]},
                    "category": "commercial", "response_mode": "fixed_text", "format_status": "prescribed",
                    "format_title": COVER[0], "required_fact_labels": ["项目编号", "响应人"]})
                chapter["requirement_indices"] = [0]
    manifest, parsed, extracted = cover_case(tmp_path, include_semantic_requirement)
    frozen = copy.deepcopy(extracted)
    planned = plan_bid(Context(), extracted, parsed, manifest, no_call, {})
    cover = next(item for item in planned["sections"] if item["section_id"] == planned["prescribed_cover_section_id"])
    full_templates = [item for item in planned["requirements"] if item["text"] == "\n".join(COVER)]
    assert len(full_templates) == 2
    assert {item["requirement_id"] for item in full_templates} == set(cover["requirement_refs"])
    assert len([issue for issue in planned["issues"] if issue["section_id"] == cover["section_id"]]) == 4
    assert {item["value"] for item in planned["facts"]} == {manifest["project"]["name"], manifest["company"]["name"]}
    assert not cover["fact_refs"] and extracted == frozen


def test_cover_cannot_absorb_foreign_template_content(tmp_path):
    manifest, parsed, extracted = cover_case(tmp_path)
    blocks, _, documents = _index(parsed, manifest)
    extracted["format_chapters"][0]["content_block_ids"].append("BODY")
    _, _, errors = _format_plan(extracted, blocks, documents)
    assert any(item["reason"] == "prescribed_cover_source_incomplete" for item in errors)

