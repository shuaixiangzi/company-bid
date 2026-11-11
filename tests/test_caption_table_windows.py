"""Native table introductions stay with their grids without changing source text."""
import copy
import json

import pytest

from company_bid.extraction import (
    PlanningError, _caption_table_binding, _index, _window_source_catalogs,
    _windows, extract_requirements,
)
from company_bid.planning import plan_bid
from test_source_selected_citations import Context, block, fixture, live_candidate, no_call, selected


def sources(tmp_path):
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"
    before = block(parsed, "BEFORE", "前文一般说明。", origin="native")
    caption = block(parsed, "CAPTION", "技术部分详细评分表", origin="native")
    table = block(parsed, "TABLE", "", kind="table", origin="native",
                  rows=[["评价项", "评审标准"], ["服务方法", "应明确检查方法及记录要求。"]],
                  merges=[], column_widths=[1, 3], header_rows=[])
    after = block(parsed, "AFTER", "后文一般说明。", origin="native")
    blocks, _, docs = _index(parsed, manifest)
    return manifest, parsed, blocks, docs


def test_block_boundary_moves_only_caption_and_preserves_denominator(tmp_path):
    manifest, parsed, blocks, docs = sources(tmp_path)
    original = copy.deepcopy((manifest, parsed, blocks, docs))
    windows, ledger = _windows(docs, blocks, {"window_max_blocks": 2})
    assert [[b["block_id"] for b in w["blocks"]] for w in windows] == [
        ["BEFORE"], ["CAPTION", "TABLE"], ["AFTER"]]
    assert [b["block_id"] for w in windows for b in w["blocks"]] == list(blocks)
    assert ledger[0]["block_ids"] == list(blocks)
    assert windows[1]["caption_table_bindings"][0]["block_ids"] == ["CAPTION", "TABLE"]
    assert windows[0]["caption_table_bindings"] == windows[2]["caption_table_bindings"] == []
    assert (manifest, parsed, blocks, docs) == original
    for window in windows:
        _window_source_catalogs(window)


def test_character_boundary_keeps_pair_within_configured_limit(tmp_path):
    _, _, blocks, docs = sources(tmp_path)
    cost = lambda b: len(json.dumps(b, ensure_ascii=False))
    limit = cost(blocks["CAPTION"]) + cost(blocks["TABLE"])
    windows, _ = _windows(docs, blocks, {"window_max_chars": limit})
    paired = next(w for w in windows if any(b["block_id"] == "CAPTION" for b in w["blocks"]))
    assert [b["block_id"] for b in paired["blocks"]] == ["CAPTION", "TABLE"]
    assert all(sum(cost(b) for b in w["blocks"]) <= limit for w in windows)


@pytest.mark.parametrize("constraint", ["blocks", "characters"])
def test_pair_too_large_stops_before_provider_call(tmp_path, constraint):
    manifest, parsed, blocks, _ = sources(tmp_path)
    config = {"window_max_blocks": 1} if constraint == "blocks" else {
        "window_max_chars": sum(len(json.dumps(blocks[key], ensure_ascii=False)) for key in ("CAPTION", "TABLE")) - 1}
    with pytest.raises(PlanningError, match="CAPTION_TABLE_PAIR_EXCEEDS_WINDOW_LIMIT"):
        extract_requirements(Context(), parsed, manifest, no_call, config)


@pytest.mark.parametrize("mutation", ["docx", "caption_ocr", "table_ocr", "other_source", "different_page",
    "ordinal_gap", "not_a_title", "sentence", "multiline", "long", "unknown_grid_basis", "ignored_source"])
def test_unsupported_relationship_is_not_inferred(tmp_path, mutation):
    _, _, blocks, docs = sources(tmp_path)
    document, caption, table = docs[0], blocks["CAPTION"], blocks["TABLE"]
    if mutation == "docx":
        document["format"] = "docx"
    elif mutation == "caption_ocr":
        caption["origin"] = "ocr"
    elif mutation == "table_ocr":
        table["origin"] = "ocr"
    elif mutation == "other_source":
        table["source_id"] = "other"
    elif mutation == "different_page":
        table["page"] += 1
    elif mutation == "ordinal_gap":
        table["ordinal"] += 1
    elif mutation == "not_a_title":
        caption["text"] = "技术部分详细评分"
    elif mutation == "sentence":
        caption["text"] = "应当满足条件；详见下表"
    elif mutation == "multiline":
        caption["text"] = "技术部分\n详细评分表"
    elif mutation == "long":
        caption["text"] = "表" * 91
    elif mutation == "ignored_source":
        caption["read_disposition"] = {"status": "ignored_by_frozen_reader_rule"}
    else:
        table["text_basis"] = "unknown"
    assert _caption_table_binding(document, caption, table) is None


@pytest.mark.parametrize("mutation", ["hash", "source_hash", "caption_text", "table_text", "grid", "ordinal", "removed"])
def test_source_binding_is_rebuilt_and_tampering_is_rejected(tmp_path, mutation):
    _, _, blocks, docs = sources(tmp_path)
    window = _windows(docs, blocks, {"window_max_blocks": 2})[0][1]
    if mutation == "hash":
        window["caption_table_bindings"][0]["proof_sha256"] = "b" * 64
    elif mutation == "source_hash":
        window["source_sha256"] = "b" * 64
    elif mutation == "caption_text":
        window["blocks"][0]["text"] = "商务部分详细评分表"
    elif mutation == "table_text":
        window["blocks"][1]["text"] += "改变"
    elif mutation == "grid":
        window["blocks"][1]["column_widths"][0] += 1
    elif mutation == "ordinal":
        window["blocks"][1]["ordinal"] += 1
    else:
        window["caption_table_bindings"] = []
    with pytest.raises(PlanningError, match="WINDOW_CAPTION_TABLE_BINDINGS_MISMATCH"):
        _window_source_catalogs(window)


def test_capture_and_n3_rebuild_do_not_turn_scoring_caption_into_response_format(tmp_path):
    manifest, parsed, blocks, _ = sources(tmp_path)
    ctx = Context()
    requests = []
    def call(ctx, task_type, unit_id, payload, validate):
        window = payload["window"]
        requests.append(copy.deepcopy(window))
        raw = live_candidate(payload, selected(blocks["CAPTION"], blocks["TABLE"]))
        if not window["caption_table_bindings"]:
            raw["requirements"] = []
        else:
            raw["requirements"][0].update(category="commercial", response_mode="procedure")
        return validate(raw)
    result = extract_requirements(ctx, parsed, manifest, call, {"window_max_blocks": 2})
    assert len(requests) == 3
    assert result["format_chapters"] == []
    cite = result["requirements"][0]["citations"][0]
    assert cite["quote"] == blocks["CAPTION"]["text"] + "\n" + blocks["TABLE"]["text"]
    assert cite["basis"] == "consecutive_mixed_blocks_lf_v1"
    captured = ctx.captures["n2-caption-table-bindings"]
    assert captured["bindings"] == requests[1]["caption_table_bindings"]
    assert captured["basis"] == "application_generated_frozen_source"
    planned = plan_bid(Context(), result, parsed, manifest, no_call, {})
    assert planned["review_actions"][0]["source_requirement"] == cite["quote"]
    assert all(section["fixed_content"] == [] for section in planned["sections"])
    assert planned["tables"] == []
