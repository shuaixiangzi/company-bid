"""N3 may prove a remote format, without rewriting N2's unread observation."""
import copy
import hashlib
import json

import pytest

from company_bid.extraction import PlanningError, extract_requirements
from company_bid.planning import plan_bid
from test_requirement_format_source_separation import CONTENT_IDS, FORMAT_TITLE, sources, wire
from test_source_selected_citations import Context, no_call


def extracted_case(tmp_path, case=None):
    block_limit = 60 if case == "same_owner_nonoverlap" else 3
    manifest, parsed, _, _ = sources(tmp_path, block_limit=block_limit)
    if case == "duplicate_template":
        unit = parsed["documents"][0]["units"][-1]
        heading = copy.deepcopy(unit["blocks"][0])
        heading.update(block_id="DUPLICATE_HEADING", text=FORMAT_TITLE,
                       locator="pdf/page[3]/paragraph[DUPLICATE_HEADING]")
        body = copy.deepcopy(heading)
        body.update(block_id="DUPLICATE_BODY", text="备用签署日期：________",
                    locator="pdf/page[3]/paragraph[DUPLICATE_BODY]")
        unit["blocks"][1:1] = [heading, body]
    elif case == "different_source":
        original = parsed["documents"][0]
        other_source = {**manifest["sources"][0], "source_id": "OTHER", "sha256": "b" * 64,
                        "path": str(tmp_path / "other-synthetic-source.txt")}
        manifest["sources"].append(other_source)
        manifest["source_roles"]["OTHER"] = "tender"
        remote_units = original["units"][1:]
        original["units"] = original["units"][:1]
        for unit in remote_units:
            unit.update(source_id="OTHER", unit_id="OTHER-" + unit["unit_id"])
            for value in unit["blocks"]:
                value["source_id"] = "OTHER"
        parsed["documents"].append({"source_id": "OTHER", "sha256": other_source["sha256"],
                                    "role": "tender", "units": remote_units})
    elif case == "missing_body":
        for unit in parsed["documents"][0]["units"]:
            unit["blocks"] = [value for value in unit["blocks"] if value["block_id"] not in CONTENT_IDS]
    elif case == "unread_format_image":
        value = next(value for unit in parsed["documents"][0]["units"] for value in unit["blocks"]
                     if value["block_id"] == "COVER_DATE")
        value.update(kind="image", text="合成的未读格式图片")
    elif case == "unknown_structural_boundary":
        value = next(value for unit in parsed["documents"][0]["units"] for value in unit["blocks"]
                     if value["block_id"] == "COVER_DATE")
        value["text"] = "二、尚未提取的独立声明书"

    def candidate(ctx, task, unit, payload, validate):
        raw = wire(payload["window"])
        targets = {value["block_id"] for value in payload["window"]["blocks"]}
        if case in {"missing_template", "different_source"}:
            raw["requirements"] = [value for value in raw["requirements"]
                                   if value["citation"]["block_ids"] == ["OBLIGATION"]]
            for chapter in raw["format_chapters"]:
                chapter["requirement_indices"] = []
        for value in raw["requirements"]:
            if value["citation"]["block_ids"] == ["OBLIGATION"]:
                value["format_status"] = "unread"
                if case == "requirement_conflict":
                    value["format_status"] = "conflict"
                elif case == "fuzzy_title":
                    value["format_title"] = "响应文件封"
                elif case == "missing_title":
                    value["format_title"] = None
                elif case == "local_template_unread":
                    value["format_status"] = "prescribed"
            elif case == "local_template_unread":
                value["format_status"] = "unread"
        if case in {"root_unread", "root_conflict"} and "OBLIGATION" in targets:
            raw["format_status"] = case.removeprefix("root_")
        if case in {"chapter_unread", "chapter_conflict"}:
            for chapter in raw["format_chapters"]:
                chapter["status"] = case.removeprefix("chapter_")
        if case == "missing_template":
            raw["format_chapters"] = []
            raw["format_status"] = "none_verified"
        if "DUPLICATE_HEADING" in targets:
            raw["format_chapters"].append({"title": FORMAT_TITLE, "level": 1,
                "citation": {"block_ids": ["DUPLICATE_HEADING"]},
                "content_block_ids": ["DUPLICATE_BODY"] if "DUPLICATE_BODY" in targets else [],
                "requirement_indices": []})
            raw["format_status"] = "specified"
        return validate(raw)

    extracted = extract_requirements(Context(), parsed, manifest, candidate, {"window_max_blocks": block_limit})
    return manifest, parsed, extracted


def test_complete_remote_format_resolves_only_derived_n3_state(tmp_path):
    manifest, parsed, extracted = extracted_case(tmp_path)
    requirement = next(value for value in extracted["requirements"] if value["format_status"] == "unread")
    original = copy.deepcopy((manifest, parsed, extracted))
    ctx = Context()
    prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    contract = ctx.captures["n3-contract-pending-plan"]
    assert contract["catalog_summary"] == {"exhaustive": False, "coverage_status": "supported_contract_scope_not_detected"}
    assert not contract["bindings"] and not contract["derived_requirements"]

    resolution, = ctx.captures["n3-planning-report"]["remote_format_resolutions"]
    assert resolution["requirement_id"] == requirement["requirement_id"]
    assert resolution["format_id"] == extracted["format_chapters"][0]["format_id"]
    assert resolution["original_format_status"] == "unread"
    assert resolution["resolved_format_status"] == "prescribed"
    assert resolution["basis"] == "complete_source_verified_remote_format_v1"
    source = parsed["documents"][0]
    chapter, = extracted["format_chapters"]
    assert resolution["source_id"] == source["source_id"] == "T"
    assert resolution["source_sha256"] == source["sha256"] == manifest["sources"][0]["sha256"]
    assert resolution["requirement_citations"] == requirement["citations"]
    assert resolution["format_citation"] == chapter["citation"]
    assert resolution["content_block_ids"] == CONTENT_IDS
    assert resolution["origin_window_ids"] == requirement["window_ids"]
    owner = next(result for result in extracted["window_results"]
                 if chapter["citation"]["block_id"] in result["validated"]["reviewed_block_ids"])
    assert resolution["format_owner_window_id"] == owner["window_id"]
    assert owner["window_id"] not in resolution["origin_window_ids"]
    expected_coverage_hash = hashlib.sha256(json.dumps(extracted["coverage"], ensure_ascii=False,
        sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    assert resolution["full_N2_coverage_sha256"] == expected_coverage_hash
    source_coverage = resolution["format_source_coverage"]
    assert source_coverage == [row for row in ctx.captures["n3-format-coverage"]["blocks"]
                               if row["format_id"] == chapter["format_id"]]
    rows = {row["block_id"]: row for row in source_coverage}
    assert set(rows) == {"COVER_HEADING", *CONTENT_IDS}
    assert rows["COVER_HEADING"]["status"] == "chapter_boundary"
    assert rows["COVER_PROJECT"]["status"] == rows["COVER_NAME"]["status"] == "declared_content"
    assert rows["COVER_DATE"]["status"] == "structural_continuation"
    for block in [block for unit in source["units"] for block in unit["blocks"] if block["block_id"] in rows]:
        assert rows[block["block_id"]]["source_id"] == block["source_id"]
        assert rows[block["block_id"]]["locator"] == block["locator"]
    assert {key for citation in resolution["requirement_citations"]
            for key in citation.get("block_ids") or [citation["block_id"]]}.isdisjoint(rows)
    assert ctx.captures["n3-format-coverage"]["remote_format_resolutions"] == [resolution]
    assert ctx.captures["n3-format-coverage"]["status"] == "complete"
    section, = prepared["sections"]
    assert section["title"] == FORMAT_TITLE
    assert requirement["requirement_id"] in section["requirement_refs"]
    assert [value["text"] for value in section["fixed_content"]] == [
        "项目名称：________", "响应人名称：________", "签署日期：____年____月____日"]
    assert not section["writing_tasks"] and not section["fact_refs"]
    assert prepared["issues"]  # Locating a form never supplies the missing facts/signatures.
    assert (manifest, parsed, extracted) == original
    origin = next(result for result in extracted["window_results"]
                  if any(value["format_status"] == "unread" for value in result["validated"]["requirements"]))
    assert origin["raw_model_candidate"]["requirements"][0]["format_status"] == "unread"
    assert origin["validated"]["requirements"][0]["format_status"] == "unread"


def test_disjoint_local_unread_does_not_become_remote_when_anchor_has_same_owner_window(tmp_path):
    manifest, parsed, extracted = extracted_case(tmp_path, "same_owner_nonoverlap")
    result, = extracted["window_results"]
    requirement = next(value for value in extracted["requirements"] if value["format_status"] == "unread")
    chapter, = extracted["format_chapters"]
    assert requirement["window_ids"] == [result["window_id"]]
    assert chapter["citation"]["block_id"] in result["validated"]["reviewed_block_ids"]
    template_ids = {chapter["citation"]["block_id"], *chapter["content_block_ids"]}
    assert {key for citation in requirement["citations"]
            for key in citation.get("block_ids") or [citation["block_id"]]}.isdisjoint(template_ids)
    original = copy.deepcopy((manifest, parsed, extracted))
    ctx = Context()
    with pytest.raises(PlanningError, match="REQUIRED_FORMAT_UNRESOLVED"):
        plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    assert ctx.captures["n3-format-coverage"]["remote_format_resolutions"] == []
    assert (manifest, parsed, extracted) == original


@pytest.mark.parametrize("case", [
    "root_unread", "root_conflict", "chapter_unread", "chapter_conflict", "requirement_conflict",
    "fuzzy_title", "missing_title", "missing_template", "duplicate_template", "local_template_unread",
    "different_source", "missing_body", "unread_format_image", "unknown_structural_boundary",
])
def test_remote_resolution_cannot_override_unread_conflict_ambiguity_or_source_fault(tmp_path, case):
    manifest, parsed, extracted = extracted_case(tmp_path, case)
    original = copy.deepcopy((manifest, parsed, extracted))
    ctx = Context()
    with pytest.raises(PlanningError, match="REQUIRED_FORMAT_UNRESOLVED"):
        plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    assert (manifest, parsed, extracted) == original
    assert "n3-planning-report" not in ctx.captures


@pytest.mark.parametrize("damage", ["coverage_count", "missing_window", "source_hash", "source_quote", "n1_read_status"])
def test_remote_resolution_requires_whole_frozen_input_and_n2_revalidation(tmp_path, damage):
    manifest, parsed, extracted = extracted_case(tmp_path)
    if damage == "coverage_count":
        extracted["coverage"]["completed_window_count"] -= 1
    elif damage == "missing_window":
        extracted["window_results"].pop()
    elif damage == "source_hash":
        extracted["source_hashes"]["T"] = "f" * 64
    elif damage == "source_quote":
        result = next(value for value in extracted["window_results"] if value["validated"]["format_chapters"])
        result["validated"]["format_chapters"][0]["citation"]["quote"] = "伪造格式来源"
    else:
        parsed["documents"][0]["units"][1]["coverage"]["status"] = "needs_ocr"
    original = copy.deepcopy((manifest, parsed, extracted))
    ctx = Context()
    with pytest.raises(PlanningError):
        plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    assert (manifest, parsed, extracted) == original
    assert "n3-planning-report" not in ctx.captures
