"""Obligation citations and remote format structure use separate source paths."""
import copy

import pytest

from company_bid.extraction import (
    PlanningError, _index, _windows, extract_requirements, validate_live_window,
)
from company_bid.planning import plan_bid
from test_source_selected_citations import Context, block, fixture, no_call


FORMAT_TITLE = "响应文件封面"
CONTENT_IDS = ["COVER_PROJECT", "COVER_NAME", "COVER_DATE"]


def sources(tmp_path, *, block_limit=60):
    manifest, parsed = fixture(tmp_path)
    block(parsed, "OBLIGATION", "响应人须提供供货联系人姓名，并填写《响应文件封面》。", page=1)
    block(parsed, "SEPARATOR", "采购文件其他条款在此单独列示。", page=1)
    block(parsed, "FORMAT_REGION", "第三章 响应文件格式", page=2)
    block(parsed, "COVER_HEADING", FORMAT_TITLE, page=2)
    block(parsed, "COVER_PROJECT", "项目名称：________", page=2)
    block(parsed, "COVER_NAME", "响应人名称：________", page=2)
    block(parsed, "COVER_DATE", "签署日期：____年____月____日", page=3)
    block(parsed, "CONTRACT", "第四章 合同条款", page=3)
    block(parsed, "CONTRACT_BODY", "合同条件不得并入封面格式正文。", page=3)
    blocks, _, documents = _index(parsed, manifest)
    windows, _ = _windows(documents, blocks, {"window_max_blocks": block_limit})
    return manifest, parsed, blocks, windows


def requirement(identity, label):
    return {"citation": {"block_ids": [identity]}, "category": "commercial",
            "response_mode": "fixed_text", "format_status": "prescribed",
            "format_title": FORMAT_TITLE, "required_fact_labels": [label]}


def wire(window):
    targets = {value["block_id"] for value in window["blocks"]}
    requirements = []
    if "OBLIGATION" in targets:
        requirements.append(requirement("OBLIGATION", "供货联系人姓名"))
    if "COVER_NAME" in targets:
        requirements.append(requirement("COVER_NAME", "响应人名称"))
    formats = []
    if "COVER_HEADING" in targets:
        formats.append({"title": FORMAT_TITLE, "level": 1, "citation": {"block_ids": ["COVER_HEADING"]},
                        "content_block_ids": [identity for identity in CONTENT_IDS if identity in targets],
                        "requirement_indices": list(range(len(requirements)))})
    return {"window_id": window["window_id"], "processing_declaration": "processed_not_semantically_accepted",
            "unresolved_block_ids": [], "requirements": requirements, "format_chapters": formats,
            "format_status": "specified" if formats else "none_verified"}


def test_remote_cover_cannot_be_joined_to_obligation_to_supply_its_fact_labels(tmp_path):
    _, _, _, windows = sources(tmp_path)
    raw = wire(windows[0])
    raw["requirements"][0]["citation"]["block_ids"] = ["OBLIGATION", "COVER_NAME"]
    raw["requirements"][0]["required_fact_labels"] = ["供货联系人姓名", "响应人名称"]
    saved = copy.deepcopy(raw)
    with pytest.raises(PlanningError, match="CITATION_SPAN_NOT_CONTIGUOUS"):
        validate_live_window(raw, windows[0])
    assert raw == saved


def test_separate_obligation_and_cover_requirements_keep_their_own_quotes_and_labels(tmp_path):
    _, _, blocks, windows = sources(tmp_path)
    raw = wire(windows[0])
    saved = copy.deepcopy(raw)
    result = validate_live_window(raw, windows[0])
    obligation, cover = result["requirements"]
    assert obligation["citation"]["quote"] == blocks["OBLIGATION"]["text"]
    assert obligation["required_fact_labels"] == ["供货联系人姓名"]
    assert cover["citation"]["quote"] == blocks["COVER_NAME"]["text"]
    assert cover["required_fact_labels"] == ["响应人名称"]
    assert obligation["format_title"] == cover["format_title"] == FORMAT_TITLE
    chapter, = result["format_chapters"]
    assert chapter["requirement_indices"] == [0, 1]
    assert chapter["content_block_ids"] == CONTENT_IDS
    assert raw == saved


def test_format_association_does_not_make_cover_words_grounded_in_obligation(tmp_path):
    _, _, _, windows = sources(tmp_path)
    raw = wire(windows[0])
    raw["requirements"][0]["required_fact_labels"] = ["响应人名称"]
    with pytest.raises(PlanningError, match="EXTRACTION_LABEL_NOT_GROUNDED") as failure:
        validate_live_window(raw, windows[0])
    assert failure.value.diagnostic_context["candidate_index"] == 0
    assert failure.value.diagnostic_context["field"] == "required_fact_labels"


def test_adjacent_format_continuation_cannot_be_claimed_as_current_target_content(tmp_path):
    _, _, _, windows = sources(tmp_path, block_limit=3)
    window = next(value for value in windows if "COVER_HEADING" in {b["block_id"] for b in value["blocks"]})
    assert "COVER_DATE" in {value["block_id"] for value in window["adjacent_context"]}
    assert "COVER_DATE" not in {value["block_id"] for value in window["blocks"]}
    raw = wire(window)
    raw["format_chapters"][0]["content_block_ids"].append("COVER_DATE")
    with pytest.raises(PlanningError, match="FORMAT_CONTENT_REFERENCE_INVALID"):
        validate_live_window(raw, window)


def test_target_only_format_leaves_neighbor_to_owner_and_n3_assembles_original_continuation(tmp_path):
    manifest, parsed, blocks, _ = sources(tmp_path, block_limit=3)
    ctx = Context()

    def candidate(ctx, task, unit, payload, validate):
        raw = wire(payload["window"])
        if raw["format_chapters"]:
            raw["unresolved_block_ids"] = ["COVER_DATE"]
        return validate(raw)

    extracted = extract_requirements(ctx, parsed, manifest, candidate, {"window_max_blocks": 3})
    origin = next(result for result in extracted["window_results"] if result["validated"]["format_chapters"])
    assert origin["format_status_scope"] == "window_target_blocks"
    chapter, = origin["validated"]["format_chapters"]
    assert chapter["content_block_ids"] == ["COVER_PROJECT", "COVER_NAME"]
    assert "COVER_DATE" not in origin["validated"]["reviewed_block_ids"]
    assert "COVER_DATE" not in {row["block_id"] for row in origin["processing"]["blocks"]}
    deferred, = origin["processing"]["deferred_context"]
    assert deferred["block_id"] == "COVER_DATE" and deferred["status"] == "deferred_context"
    owner = next(result for result in extracted["window_results"]
                 if "COVER_DATE" in result["validated"]["reviewed_block_ids"])
    assert owner["window_id"] != origin["window_id"]
    closure, = extracted["deferred_context_closure"]["items"]
    assert closure["owner_window_id"] == owner["window_id"]
    assert closure["resolution"] == "owner_target_structurally_processed"
    assert closure["semantic_review"] == "requires_source_review"
    saved = copy.deepcopy(extracted)

    prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    section, = prepared["sections"]
    assert section["title"] == FORMAT_TITLE
    assert [value["text"] for value in section["fixed_content"]] == [blocks[key]["text"] for key in CONTENT_IDS]
    assert blocks["CONTRACT_BODY"]["text"] not in [value["text"] for value in section["fixed_content"]]
    assert not section["writing_tasks"] and not section["fact_refs"]
    assert {value["requirement_id"] for value in extracted["requirements"]} <= set(section["requirement_refs"])
    assert {value["text"] for value in prepared["requirements"]} >= {
        blocks["OBLIGATION"]["text"], blocks["COVER_NAME"]["text"]}
    coverage = {row["block_id"]: row for row in ctx.captures["n3-format-coverage"]["blocks"]}
    assert coverage["COVER_DATE"]["status"] == "structural_continuation"
    assert coverage["COVER_HEADING"]["boundary_basis"]["block_id"] == "CONTRACT"
    assert ctx.captures["n3-format-coverage"]["status"] == "complete"
    assert extracted == saved
