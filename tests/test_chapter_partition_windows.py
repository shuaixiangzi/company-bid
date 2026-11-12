"""Synthetic native chapter boundaries preserve source and validation fences."""
import copy
import json

import pytest

from company_bid.extraction import (
    PlanningError, _index, _window_source_catalogs, _windows,
    extract_requirements, validate_live_window,
)
from company_bid.planning import plan_bid
from test_source_selected_citations import (
    Context, block, fixture, live_candidate, no_call, selected,
)


def native_line(parsed, identity, text, *, page=1, **extra):
    value = block(parsed, identity, text, page=page, origin="native", **extra)
    unit = next(unit for unit in parsed["documents"][0]["units"] if unit["page"] == page)
    value["locator"] = f"pdf/page[{page}]/line[{len(unit['blocks']) - 1}]"
    return value


def chapter_sources(tmp_path, *, source_format="pdf"):
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = source_format
    for page, number in enumerate(("三", "四", "五"), 1):
        native_line(parsed, f"H{page}", f"第{number}章 合成测试条款", page=page)
        native_line(parsed, f"BODY{page}", f"应保存第{page}组测试点的  原始记录。", page=page)
    return manifest, parsed


def create_windows(manifest, parsed, config=None):
    blocks, _, documents = _index(parsed, manifest)
    windows, ledger = _windows(documents, blocks, config or {})
    return blocks, documents, windows, ledger


def target_ids(windows):
    return [[value["block_id"] for value in window["blocks"]] for window in windows]


def source_cost(value):
    return len(json.dumps({key: item for key, item in value.items()
                           if key not in {"bbox", "image_ref"}}, ensure_ascii=False))


def conditional_sources(tmp_path):
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"
    native_line(parsed, "EARLIER", "第三章 合成评价说明")
    native_line(parsed, "EARLIER_BODY", "本章仅描述测试评价背景。")
    for identity, text in (
        ("FORMAT", "第五章 合成材料要求"),
        ("SECTION", "1.测试材料应包括以下组成："),
        ("OUTER", "B．技术册不得夹入商务报价："),
        ("CONDITION", "（1）仅适用于合成服务事项"),
        ("FIRST_A", "①应填写合成偏差记录，"),
        ("FIRST_B", "没有偏差时不得编造测试结果。"),
        ("SECOND_A", "②应说明测试实施步骤，"),
        ("SECOND_B", "并列明复测安排和记录方法。"),
        ("THIRD", "③应提供测试结束后的回访安排。"),
        ("FOURTH", "④应附合成示例中明确要求的其他材料。"),
        ("TAIL_A", "测试材料封面须由授权人员签署，"),
        ("TAIL_B", "并在约定时点递交至测试收件位置。"),
        ("NEXT", "2.合成材料的其他说明"),
        ("NEXT_BODY", "下节正文保留其原始要求。"),
    ):
        native_line(parsed, identity, text, page=2)
    return manifest, parsed


def caption_sources(tmp_path):
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"
    native_line(parsed, "H3", "第三章 合成评价")
    native_line(parsed, "BEFORE", "评价前的合成背景。")
    native_line(parsed, "CAPTION", "合成测试评价表")
    block(parsed, "TABLE", "", kind="table", origin="native",
          rows=[["测试项目", "测试规则"], ["记录", "应保留合成检查记录。"]],
          merges=[], column_widths=[1, 3], header_rows=[])
    native_line(parsed, "H4", "第四章 合成技术")
    native_line(parsed, "AFTER", "应说明合成检查方法。")
    return manifest, parsed


def extract_chapters(manifest, parsed):
    requests = []

    def call(ctx, task_type, unit_id, payload, validate):
        window = payload["window"]
        requests.append(copy.deepcopy(window))
        body, = [value for value in window["blocks"] if value["block_id"].startswith("BODY")]
        return validate(live_candidate(payload, selected(body)))

    result = extract_requirements(Context(), parsed, manifest, call, {})
    return result, requests


def test_native_chapters_partition_targets_without_changing_sources_or_ledger(tmp_path):
    manifest, parsed = chapter_sources(tmp_path)
    original = copy.deepcopy((manifest, parsed))
    blocks, _, documents = _index(parsed, manifest)
    indexed_original = copy.deepcopy((blocks, documents))
    windows, ledger = _windows(documents, blocks, {})
    assert target_ids(windows) == [["H1", "BODY1"], ["H2", "BODY2"], ["H3", "BODY3"]]
    assert [key for row in target_ids(windows) for key in row] == list(blocks)
    assert [key for unit in ledger for key in unit["block_ids"]] == list(blocks)
    assert [unit["unit_id"] for unit in ledger] == ["U1", "U2", "U3"]
    for window in windows:
        assert [value["text"] for value in window["blocks"]] == [
            blocks[value["block_id"]]["text"] for value in window["blocks"]]
        _window_source_catalogs(window)
    assert (manifest, parsed) == original
    assert (blocks, documents) == indexed_original

    docx = copy.deepcopy(parsed)
    docx["documents"][0]["format"] = "docx"
    assert target_ids(create_windows(manifest, docx)[2]) == [list(blocks)]


@pytest.mark.parametrize("case", ["docx", "ocr", "missing_origin", "ignored", "dotted_toc", "inline_reference"])
def test_untrusted_or_non_pdf_chapter_text_does_not_split(tmp_path, case):
    manifest, parsed = chapter_sources(tmp_path)
    document = parsed["documents"][0]
    heading = document["units"][1]["blocks"][0]
    if case == "docx":
        document["format"] = "docx"
    elif case == "ocr":
        heading["origin"] = "ocr"
    elif case == "missing_origin":
        heading.pop("origin")
    elif case == "ignored":
        document["units"][1]["coverage"]["ignored_objects"] = [
            {"locator": heading["locator"], "reason": "Synthetic decorative heading."}]
    elif case == "dotted_toc":
        heading["text"] += "……8"
    else:
        heading["text"] = "具体说明见第四章合成测试条款。"
    windows = create_windows(manifest, parsed)[2]
    expected = [["H1", "BODY1", "H2", "BODY2"], ["H3", "BODY3"]]
    assert target_ids(windows) == ([sum(expected, [])] if case == "docx" else expected)


def test_condition_children_and_unnumbered_tail_are_all_owned_by_one_chapter_target(tmp_path):
    manifest, parsed = conditional_sources(tmp_path)
    original = copy.deepcopy(parsed)
    blocks, _, windows, ledger = create_windows(manifest, parsed)
    expected = [value["block_id"] for value in parsed["documents"][0]["units"][1]["blocks"]]
    assert target_ids(windows) == [["EARLIER", "EARLIER_BODY"], expected]
    scope = windows[1]
    assert scope["source_clause_catalog"] == []
    assert {"OUTER", "CONDITION", "FIRST_A", "FIRST_B", "SECOND_A", "SECOND_B",
            "THIRD", "FOURTH", "TAIL_A", "TAIL_B", "NEXT", "NEXT_BODY"} <= set(expected)
    assert "不得" in next(value["text"] for value in scope["blocks"] if value["block_id"] == "OUTER")
    assert [key for unit in ledger for key in unit["block_ids"]] == list(blocks)
    assert parsed == original


def test_chapter_partition_does_not_authorize_noncontiguous_condition_and_second_child(tmp_path):
    manifest, parsed = conditional_sources(tmp_path)
    blocks, _, windows, _ = create_windows(manifest, parsed)
    window = next(value for value in windows if "CONDITION" in target_ids([value])[0])
    raw = live_candidate({"window": window}, selected(
        blocks["CONDITION"], blocks["SECOND_A"], blocks["SECOND_B"]))
    with pytest.raises(PlanningError, match="CITATION_SPAN_NOT_CONTIGUOUS"):
        validate_live_window(raw, window)


@pytest.mark.parametrize("constraint", ["characters", "blocks"])
def test_native_chapter_partition_still_obeys_each_capacity_limit(tmp_path, constraint):
    manifest, parsed = chapter_sources(tmp_path)
    for unit in parsed["documents"][0]["units"]:
        unit["blocks"][1]["text"] += "合成容量正文。" * 50
    blocks, _, _, _ = create_windows(manifest, parsed)
    limit = max(source_cost(value) for value in blocks.values())
    config = {"window_max_chars": limit} if constraint == "characters" else {"window_max_blocks": 1}
    _, _, windows, ledger = create_windows(manifest, parsed, config)
    assert target_ids(windows) == [[key] for key in blocks]
    assert [key for unit in ledger for key in unit["block_ids"]] == list(blocks)
    assert all(sum(source_cost(value) for value in window["blocks"]) <= limit for window in windows)


def test_chapter_boundary_does_not_bypass_an_oversized_source_block(tmp_path):
    manifest, parsed = chapter_sources(tmp_path)
    heading = parsed["documents"][0]["units"][1]["blocks"][0]
    heading["block_id"] = "SYNTHETIC-LONG-CHAPTER-" + "X" * 100
    blocks, _, _, _ = create_windows(manifest, parsed)
    limit = source_cost(blocks[heading["block_id"]]) - 1
    assert limit >= 256
    with pytest.raises(PlanningError, match="SOURCE_BLOCK_EXCEEDS_WINDOW_LIMIT"):
        extract_requirements(Context(), parsed, manifest, no_call, {"window_max_chars": limit})


@pytest.mark.parametrize("constraint", ["characters", "blocks"])
def test_caption_table_stays_atomic_at_chapter_and_capacity_boundaries(tmp_path, constraint):
    manifest, parsed = caption_sources(tmp_path)
    blocks, _, _, _ = create_windows(manifest, parsed)
    pair_cost = source_cost(blocks["CAPTION"]) + source_cost(blocks["TABLE"])
    config = {"window_max_chars": pair_cost} if constraint == "characters" else {"window_max_blocks": 2}
    _, _, windows, _ = create_windows(manifest, parsed, config)
    paired, = [window for window in windows if "CAPTION" in target_ids([window])[0]]
    assert target_ids([paired]) == [["CAPTION", "TABLE"]]
    assert paired["caption_table_bindings"][0]["block_ids"] == ["CAPTION", "TABLE"]
    assert target_ids(windows)[-1] == ["H4", "AFTER"]
    assert [key for row in target_ids(windows) for key in row] == list(blocks)
    for window in windows:
        _window_source_catalogs(window)


@pytest.mark.parametrize("constraint", ["characters", "blocks"])
def test_chapter_partition_cannot_admit_an_oversized_caption_table_pair(tmp_path, constraint):
    manifest, parsed = caption_sources(tmp_path)
    blocks, _, _, _ = create_windows(manifest, parsed)
    config = {"window_max_chars": source_cost(blocks["CAPTION"]) + source_cost(blocks["TABLE"]) - 1
              } if constraint == "characters" else {"window_max_blocks": 1}
    with pytest.raises(PlanningError, match="CAPTION_TABLE_PAIR_EXCEEDS_WINDOW_LIMIT"):
        extract_requirements(Context(), parsed, manifest, no_call, config)


def test_n3_rebuilds_chapter_targets_and_retains_source_review_requirement(tmp_path):
    manifest, parsed = chapter_sources(tmp_path)
    output, requests = extract_chapters(manifest, parsed)
    assert target_ids(requests) == [["H1", "BODY1"], ["H2", "BODY2"], ["H3", "BODY3"]]
    assert output["coverage"]["window_count"] == output["coverage"]["completed_window_count"] == 3
    assert output["coverage"]["semantic_review"] == "requires_source_review"
    assert output["deferred_context_closure"]["semantic_review"] == "requires_source_review"
    planning_ctx = Context()
    planned = plan_bid(planning_ctx, output, parsed, manifest, no_call, {})
    assert [value["text"] for value in planned["requirements"]] == [
        unit["blocks"][1]["text"] for unit in parsed["documents"][0]["units"]]
    assert planning_ctx.captures["n3-planning-report"]["coverage"]["semantic_review"] == "requires_source_review"


@pytest.mark.parametrize("claim", ["semantic_acceptance", "missing_chapter", "wrong_window_identity"])
def test_n3_does_not_trust_saved_chapter_coverage_claims(tmp_path, claim):
    manifest, parsed = chapter_sources(tmp_path)
    output, _ = extract_chapters(manifest, parsed)
    if claim == "semantic_acceptance":
        output["coverage"]["semantic_review"] = "accepted"
    elif claim == "missing_chapter":
        output["window_results"].pop()
        output["coverage"].update(window_count=2, completed_window_count=2)
    else:
        output["window_results"][0]["window_id"] = "invented-combined-chapter-window"
    with pytest.raises(PlanningError, match="EXTRACTION_DENOMINATOR_MISMATCH"):
        plan_bid(Context(), output, parsed, manifest, no_call, {})


def test_processed_chapter_heading_does_not_excuse_unresolved_body(tmp_path):
    manifest, parsed = chapter_sources(tmp_path)

    def call(ctx, task_type, unit_id, payload, validate):
        window = payload["window"]
        body, = [value for value in window["blocks"] if value["block_id"].startswith("BODY")]
        raw = live_candidate(payload, selected(body))
        raw["unresolved_block_ids"] = [body["block_id"]]
        return validate(raw)

    with pytest.raises(PlanningError, match="LIVE_REQUIRED_BLOCK_UNRESOLVED"):
        extract_requirements(Context(), parsed, manifest, call, {})
