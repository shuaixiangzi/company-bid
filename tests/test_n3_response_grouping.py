"""Whole-source controls and outline grouping, without network requests."""
import copy

import pytest

from company_bid.extraction import _index, extract_requirements
from company_bid.planning import _attachment_candidates, _response_writing_control, plan_bid
from test_n3_source_roles import line
from test_source_selected_citations import Context, fixture, live_candidate, no_call, selected


SINGLE = "3.6.2 响应人只能提出唯一的响应方案。响应人在响应文件中提出多个响应方案的，其响应文件将被视为无效。"
SCOPE = "3.7.2 响应文件应当对采购文件有关工期、质量要求、技术标准和要求、采购范围等实质性内容作出响应。"
LICENSE = "（5）企业营业执照或事业单位法人证书等相关资质证明文件。"


def case(tmp_path, texts, *, technical_chapters=0, category="technical", mode="writing", keywords=None):
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"
    inputs = [line(parsed, f"INPUT-{i}", text) for i, text in enumerate(texts)]
    if technical_chapters:
        line(parsed, "REGION", "第五章 响应文件格式", page=2)
    for i in range(technical_chapters):
        line(parsed, f"FORMAT-{i}", f"{i+1} " + ["技术服务方案", "实施方案"][i], page=i+2)
        line(parsed, f"NOTE-{i}", "（格式自拟）", page=i+2)
    if technical_chapters:
        line(parsed, "END", "第六章 其他事项", page=4)

    def candidate(ctx, task, unit, payload, validate):
        ids = {b["block_id"] for b in payload["window"]["blocks"]}
        raw = live_candidate(payload, selected(inputs[0]))
        raw["requirements"] = [{"citation": selected(b), "category": category,
            "response_mode": mode, "format_status": "not_prescribed",
            "attachment_keywords": keywords or []} for b in inputs if b["block_id"] in ids]
        raw["format_chapters"] = [{"title": f"{i+1} " + ["技术服务方案", "实施方案"][i], "level": 1,
            "citation": {"block_ids": [f"FORMAT-{i}"]}, "content_block_ids": [f"NOTE-{i}"],
            "requirement_indices": []} for i in range(technical_chapters) if f"FORMAT-{i}" in ids]
        raw["format_status"] = "specified" if raw["format_chapters"] else "none_verified"
        return validate(raw)

    extracted = extract_requirements(Context(), parsed, manifest, candidate, {})
    return manifest, parsed, extracted


@pytest.mark.parametrize("text", [SINGLE, SCOPE])
def test_complete_writing_rule_is_pending_control_without_model_work(tmp_path, text):
    manifest, parsed, extracted = case(tmp_path, [text])
    frozen = copy.deepcopy(extracted)
    ctx = Context()
    result = plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    section, = result["sections"]
    assert section["kind"] == "facts" and not section["writing_tasks"]
    assert section["fixed_content"][0]["text"].endswith(text)
    assert result["issues"][0]["title"] == "响应编写规则待核对"
    row, = ctx.captures["n3-outline-grouping"]["writing_controls"]
    assert row["satisfaction_status"] == "not_determined"
    assert extracted == frozen


@pytest.mark.parametrize("change", ["suffix", "ocr", "short_quote"])
def test_control_does_not_strip_source_qualifications_or_accept_ocr(tmp_path, change):
    text = SINGLE + ("但是本项目允许备选方案。" if change == "suffix" else "")
    manifest, parsed, extracted = case(tmp_path, [text])
    blocks, _, _ = _index(parsed, manifest)
    detail = copy.deepcopy(extracted["requirements"][0])
    if change == "ocr":
        blocks["INPUT-0"]["origin"] = "ocr"
    elif change == "short_quote":
        blocks["INPUT-0"]["text"] += "但是本项目允许备选方案。"
    assert _response_writing_control(detail, blocks) is None


@pytest.mark.parametrize("chapter_count", [1, 2])
def test_technical_grouping_requires_unique_open_chapter_and_keeps_units(tmp_path, chapter_count):
    manifest, parsed, extracted = case(tmp_path,
        ["应编写检测流程与记录交付方法。", "应编写培训的拟实施方法。"], technical_chapters=chapter_count)
    frozen = copy.deepcopy(extracted)
    ctx = Context()
    result = plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    sections = [s for s in result["sections"] if s["writing_tasks"]]
    tasks = [t for s in sections for t in s["writing_tasks"]]
    assert len(tasks) == len({t["unit_id"] for t in tasks}) == 2
    assert {r for t in tasks for r in t["requirement_refs"]} == {r["requirement_id"] for r in extracted["requirements"]}
    assert len(sections) == (1 if chapter_count == 1 else 2)
    assert bool(ctx.captures["n3-outline-grouping"]["placements"]) == (chapter_count == 1)
    assert extracted == frozen


@pytest.mark.parametrize("text,expected_rule", [(LICENSE, "explicit_alternative_legal_credential"),
    (LICENSE.replace("或", "和"), "all_declared_keywords"),
    (LICENSE + "须同时提供二者。", "all_declared_keywords")])
def test_alternative_credentials_are_candidates_and_never_eligibility(tmp_path, text, expected_rule):
    manifest, parsed, extracted = case(tmp_path, [text], category="qualification", mode="attachment",
                                      keywords=["企业营业执照", "事业单位法人证书", "相关资质证明文件"])
    blocks, _, _ = _index(parsed, manifest)
    proofs = [{"attachment_id": "P1", "title": "营业执照复印件", "description": "已维护证明"},
              {"attachment_id": "P2", "title": "事业单位法人证书", "description": "已维护证明"},
              {"attachment_id": "P3", "title": "安全生产许可证", "description": "营业执照样本链接"}]
    matches, policy = _attachment_candidates(extracted["requirements"][0], proofs, blocks)
    assert policy["rule"] == expected_rule and policy["eligibility_status"] == "not_determined"
    assert [item["attachment_id"] for item in matches] == (["P1", "P2"] if expected_rule.startswith("explicit") else [])
