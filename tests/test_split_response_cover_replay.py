"""Only an authenticated original cover can resolve a split-window format."""
import copy

import pytest

from company_bid.extraction import (PlanningError, _index, _pdf_footer_skips,
    _validate_normalized_window_result, _windows, extraction_window_config, extraction_window_payload,
    extract_requirements, validate_live_window)
from company_bid.planning import _format_plan, plan_bid
from company_bid.response_cover_sources import RESPONSE_COVER_CONFIG_KEY, build_response_cover_proof
from company_bid.storage import digest
from test_n3_source_roles import line
from test_source_selected_citations import Context, fixture, no_call


def cover_fixture(tmp_path, *, alternate_name=False):
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"
    tender = ("虚构单位 年度测试设备覆盖", "试运行与应急校核综合", "服务项目", "询比采购文件")
    for index, text in enumerate(tender):
        line(parsed, f"T{index}", text, bbox=[90, 140 + 35 * index, 505, 170 + 35 * index])
    for index, text in enumerate(("验收应记录完整检测结果。", "本节为服务验收要求。", "各项测试按源数据复核。")):
        line(parsed, f"S{index}", text, page=2)
    cover = ("响应文件格式", tender[0], tender[1], "调试项目" if alternate_name else tender[2], "响 应 文 件",
             "响应人： （盖单位章）", "法定代表人或其委托代理人： （签字或盖章）", "年 月 日", "3")
    boxes = ([90, 72, 220, 94], [93, 155, 502, 195], [93, 186, 502, 226], [204, 217, 391, 257],
             [198, 354, 397, 391], [96, 610, 498, 626], [90, 641, 507, 658], [240, 673, 418, 689], [292, 794, 303, 805])
    for index, (text, bbox) in enumerate(zip(cover, boxes, strict=True)):
        line(parsed, f"C{index}", text, page=3, bbox=bbox)
    line(parsed, "FORM", "一、独立说明书", page=4, bbox=[90, 130, 181, 153])
    line(parsed, "BODY", "此处保留独立说明书的原文。", page=4, bbox=[90, 160, 410, 177])
    line(parsed, "P4", "4", page=4, bbox=boxes[-1])
    line(parsed, "P5", "5", page=5, bbox=boxes[-1])
    document = parsed["documents"][0]
    document["expected_units"] = len(document["units"])
    for unit in document["units"]:
        unit["coverage"]["origin"] = "native"
    blocks, _, documents = _index(parsed, manifest)
    config = extraction_window_config({"window_max_blocks": 9})
    windows, _ = _windows(documents, blocks, config, manifest=manifest)
    assert [block["block_id"] for block in windows[0]["blocks"][-2:]] == ["C0", "C1"]
    raw = template(windows[0]["window_id"], unresolved=["C1"], format_status="unread", requirements=[{
        "category": "technical", "response_mode": "writing", "format_status": "not_prescribed",
        "citation": {"block_ids": ["S0"]}, "reference_keywords": ["检测结果"]}])
    options = {"source_id": "T", "source_sha256": windows[0]["source_sha256"], "physical_page": 3,
               "window_id": windows[0]["window_id"], "continuation_owner_window_id": windows[1]["window_id"],
               "raw_candidate_sha256": digest(raw), "tender_name_block_ids": ["T0", "T1", "T2"]}
    proof = build_response_cover_proof(options, [blocks[f"C{i}"] for i in range(9)], [blocks[f"T{i}"] for i in range(4)],
                                       _pdf_footer_skips(blocks)["C8"])
    assert proof is not None
    return manifest, parsed, blocks, windows, raw, {**config, RESPONSE_COVER_CONFIG_KEY: {
        **options, "source_proof_sha256": proof["proof_sha256"]}}


def template(window_id, *, unresolved=(), format_status="none_verified", chapters=(), requirements=()):
    return {"window_id": window_id, "processing_declaration": "processed_not_semantically_accepted",
            "unresolved_block_ids": list(unresolved), "format_status": format_status, "requirements": list(requirements),
            "format_chapters": list(chapters)}


def run_extraction(case, *, neighbor_cover=True, neighbor_content=("C5", "C6", "C7"), deferred=("C1",)):
    manifest, parsed, blocks, windows, raw, config = case

    def call(ctx, task, window_id, payload, validate):
        original = next(window for window in windows if window["window_id"] == window_id)
        assert payload == extraction_window_payload(original, extraction_window_config({"window_max_blocks": 9}))
        if window_id == raw["window_id"]:
            return validate(raw)
        if window_id != windows[1]["window_id"]:
            return validate(template(window_id))
        chapters = [{"title": blocks["FORM"]["text"], "level": 1, "citation": {"block_ids": ["FORM"]},
                     "content_block_ids": ["BODY"]}]
        requirements = []
        if neighbor_cover:
            requirements.append({"category": "procedure", "response_mode": "fixed_text", "format_status": "prescribed",
                                 "citation": {"block_ids": ["C4", "C5", "C6", "C7"]}})
            chapters.insert(0, {"title": blocks["C4"]["text"], "level": 1, "citation": {"block_ids": ["C4"]},
                                "content_block_ids": list(neighbor_content), "requirement_indices": [0]})
        return validate(template(window_id, unresolved=deferred, format_status="specified", chapters=chapters, requirements=requirements))

    return extract_requirements(Context(), parsed, manifest, call, config)


def test_source_cover_is_separately_merged_in_source_order_and_consumed_by_n3(tmp_path):
    case = cover_fixture(tmp_path)
    manifest, parsed, blocks, windows, raw, _ = case
    with pytest.raises(PlanningError, match="LIVE_REQUIRED_BLOCK_UNRESOLVED"):
        validate_live_window(raw, windows[0])
    extracted = run_extraction(case)
    replayed, neighbor = extracted["window_results"][:2]
    assert replayed["raw_model_candidate"] == raw and replayed["live_confirmation"]["unresolved_block_ids"] == ["C1"]
    assert replayed["processing"]["unresolved_block_ids"] == ["C1"]
    assert replayed["validated"]["format_status"] == "specified" and not replayed["validated"]["format_chapters"]
    resolution = replayed[RESPONSE_COVER_CONFIG_KEY]
    assert resolution["original_format_status"] == "unread" and resolution["resolved_block_ids"] == ["C1"]
    assert resolution["proof"]["name_comparison"]["status"] == "same_printed_name"
    assert [chapter["title"] for chapter in extracted["format_chapters"]] == [blocks["C0"]["text"], blocks["FORM"]["text"]]
    assert neighbor["validated"]["format_chapters"][0]["citation"]["block_id"] == "C4"
    assert len(extracted["format_chapters"][0]["requirement_refs"]) == 1
    assert extracted["deferred_context_closure"]["items"][0]["resolution"] == "owner_target_source_proven_cover"
    assert not extracted["requirements"][0]["format_title"]
    result = plan_bid(Context(), extracted, parsed, manifest, no_call, {})
    cover = next(section for section in result["sections"] if section["section_id"] == result["prescribed_cover_section_id"])
    assert cover["title"] == blocks["C0"]["text"]
    assert [item["text"] for item in cover["fixed_content"]] == [blocks[f"C{i}"]["text"] for i in range(1, 8)]
    assert not cover["writing_tasks"] and not cover["fact_refs"]


def test_different_printed_names_remain_independent(tmp_path):
    case = cover_fixture(tmp_path, alternate_name=True)
    extracted = run_extraction(case, neighbor_cover=False)
    name = extracted["window_results"][0][RESPONSE_COVER_CONFIG_KEY]["proof"]["name_comparison"]
    assert name["status"] == "different_printed_names_preserved"
    assert name["response_cover_printed_name"].endswith("调试项目")
    assert name["tender_cover_printed_name"].endswith("服务项目")


def test_foreign_format_content_is_not_merged_and_owner_still_has_to_validate(tmp_path):
    case = cover_fixture(tmp_path)
    extracted = run_extraction(case, neighbor_content=("C5", "C6", "BODY"))
    assert [chapter["citation"]["block_id"] for chapter in extracted["format_chapters"]] == ["C0", "C4", "FORM"]
    manifest, parsed, blocks, _, _, _ = case
    _, _, documents = _index(parsed, manifest)
    _, _, errors = _format_plan(extracted, blocks, documents)
    assert any(error["reason"] == "ambiguous_prescribed_cover_source" for error in errors)
    with pytest.raises(PlanningError, match="LIVE_REQUIRED_BLOCK_UNRESOLVED"):
        run_extraction(case, deferred=("C1", "C5"))


@pytest.mark.parametrize("change", ["line_order", "title_geometry", "signature_in_name", "unread_page", "wrong_owner"])
def test_bound_cover_needs_complete_native_page_roles_and_ownership(tmp_path, change):
    manifest, parsed, _, windows, _, config = cover_fixture(tmp_path)
    cover_unit = parsed["documents"][0]["units"][2]
    if change == "line_order":
        cover_unit["blocks"][2], cover_unit["blocks"][3] = cover_unit["blocks"][3], cover_unit["blocks"][2]
    elif change == "title_geometry":
        cover_unit["blocks"][4]["bbox"][1] = 219
    elif change == "signature_in_name":
        cover_unit["blocks"][3]["text"] = "盖章项目"
    elif change == "unread_page":
        cover_unit["coverage"]["unread_objects"] = [{"kind": "image"}]
    else:
        config[RESPONSE_COVER_CONFIG_KEY]["continuation_owner_window_id"] = windows[2]["window_id"]
    blocks, _, documents = _index(parsed, manifest)
    with pytest.raises(PlanningError, match="RESPONSE_COVER_SOURCE_"):
        _windows(documents, blocks, config, manifest=manifest)


@pytest.mark.parametrize("change", ["unrelated_unresolved", "bad_label", "model_format_chapter", "forged_result"])
def test_scope_and_remaining_model_guards_fail_closed(tmp_path, change):
    manifest, parsed, blocks, _, raw, config = case = cover_fixture(tmp_path)
    _, _, documents = _index(parsed, manifest)
    resolved, _ = _windows(documents, blocks, config, manifest=manifest)
    if change == "forged_result":
        output = run_extraction(case)
        first = output["window_results"][0]
        first[RESPONSE_COVER_CONFIG_KEY]["resolved_block_ids"].append("S0")
        with pytest.raises(PlanningError, match="EXTRACTION_RESPONSE_COVER_RESOLUTION_MISMATCH"):
            _validate_normalized_window_result(first, resolved[0])
        return
    raw = copy.deepcopy(raw)
    if change == "unrelated_unresolved":
        raw["unresolved_block_ids"].append("S0")
    elif change == "bad_label":
        raw["requirements"][0]["reference_keywords"] = ["未出现的字词"]
    else:
        raw["format_chapters"].append({"title": "响应文件格式", "level": 1, "citation": {"block_ids": ["C0"]},
                                       "content_block_ids": ["C1"]})
    with pytest.raises(PlanningError, match="RESPONSE_COVER_SOURCE_RAW_RESPONSE_MISMATCH"):
        validate_live_window(raw, resolved[0])
    resolved[0][RESPONSE_COVER_CONFIG_KEY]["config"]["raw_candidate_sha256"] = digest(raw)
    expected_error = "EXTRACTION_LABEL_NOT_GROUNDED" if change == "bad_label" else "RESPONSE_COVER_SOURCE_RAW_RESPONSE_MISMATCH"
    with pytest.raises(PlanningError, match=expected_error):
        validate_live_window(raw, resolved[0])
