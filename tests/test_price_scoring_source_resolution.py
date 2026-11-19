"""A frozen price rule supplement stays separate from the reviewed model window."""
import copy

import pytest

from company_bid import price_scoring_sources as price_sources
from company_bid.extraction import (
    PRICE_SCORING_CONFIG_KEY, PlanningError, _hash, _index, _validate_normalized_window_result, _windows,
    extract_requirements, extraction_window_config, extraction_window_payload, validate_live_window,
)
from company_bid.planning import plan_bid
from test_source_selected_citations import Context, block, fixture


def source_fixture(tmp_path, monkeypatch):
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"
    top = ("1. Synthetic price basis threshold 7, fallback below 7. "
           "2. Synthetic rate scale 100. "
           "3. Equal 40, up per 1 by 2, down per 1 by 0.75, floor 20."
           "Above score 40 - abs(rate) * 100 * 2")
    tail = "Below score 40 - abs(rate) * 100 * 0.75. Synthetic rounding."
    for name, text in (("_START_TEMPLATE", top), ("_END_TEMPLATE", tail)):
        monkeypatch.setattr(price_sources, name, price_sources._text_hash(price_sources._NUMERIC_TOKEN.sub("#", text)))

    def add(identity, locator, text, page, bbox, **extra):
        value = block(parsed, identity, text, page=page, bbox=bbox, origin="native", **extra)
        value["locator"] = f"pdf/page[{page}]/{locator}"
        return value

    def footer(page):
        add(f"F{page}", "line[8]", str(page), page, [293, 793, 300, 804])

    add("A", "line[0]", "独立合成流程说明。", 1, [95, 80, 210, 90])
    footer(1)
    add("H", "line[0]", "价格评价条款", 2, [265, 539, 330, 550])
    left = add("L", "table[1]", "", 2, [85, 555, 510, 759], kind="table", column_widths=[64, 327, 34],
               rows=[["评价项目", "评价内容", "分值"], ["报价得分\n（40分）", top.replace("floor", "flo\nor"), "40"]],
               merges=[], header_rows=[])
    footer(2)
    right = add("T", "table[0]", "", 3, [85, 72, 510, 146], kind="table", column_widths=[63.5, 327.5, 34],
                rows=[["", tail, ""], ["合计", "", "40"]],
                merges=[{"row": 1, "column": 0, "row_span": 1, "col_span": 2}], header_rows=[])
    footer(3)
    historic_config = {"window_max_blocks": 5}
    old_blocks, _, docs = _index(parsed, manifest)
    old_windows, _ = _windows(docs, old_blocks, historic_config, manifest=manifest)
    raw = {"window_id": old_windows[0]["window_id"], "processing_declaration": "processed_not_semantically_accepted",
           "unresolved_block_ids": ["H", "L"], "requirements": [{"citation": {"block_ids": ["A"]},
               "category": "procedure", "response_mode": "procedure", "format_status": "not_prescribed"}],
           "format_chapters": [], "format_status": "none_verified"}
    config = {**historic_config, PRICE_SCORING_CONFIG_KEY: {"source_id": "T", "source_sha256": "a" * 64,
        "heading_block_id": "H", "table_block_id": "L", "continuation_block_id": "T", "raw_candidate_sha256": _hash(raw)}}
    return manifest, parsed, old_windows, config, raw, left, right


def test_price_scoring_source_resolution_preserves_wire_then_rebuilds_at_planning(tmp_path, monkeypatch):
    manifest, parsed, old_windows, config, raw, _, _ = source_fixture(tmp_path, monkeypatch)
    blocks, _, docs = _index(parsed, manifest)
    windows, _ = _windows(docs, blocks, config, manifest=manifest)
    frozen = extraction_window_config(config)
    for old, window in zip(old_windows, windows, strict=True):
        assert extraction_window_payload(old, {"window_max_chars": 14000, "window_max_blocks": 5}) == extraction_window_payload(window, frozen)
    assert [PRICE_SCORING_CONFIG_KEY in window for window in windows] == [True, False]
    with pytest.raises(PlanningError, match="LIVE_REQUIRED_BLOCK_UNRESOLVED"):
        validate_live_window(raw, old_windows[0])

    def offline_candidates(ctx, task, unit, payload, validate):
        if unit == raw["window_id"]:
            return validate(raw)
        return validate({**raw, "window_id": unit, "requirements": [], "unresolved_block_ids": []})

    extracted = extract_requirements(Context(), parsed, manifest, offline_candidates, config)
    model_result = extracted["window_results"][0]
    resolution = model_result[PRICE_SCORING_CONFIG_KEY]
    assert model_result["raw_model_candidate"] == raw
    assert model_result["processing"]["unresolved_block_ids"] == resolution["resolved_block_ids"] == ["H", "L"]
    assert len(model_result["validated"]["requirements"]) == 1
    assert len(extracted["requirements"]) == 2
    expected_quote = "\n".join(blocks[key]["text"] for key in ("H", "L", "T"))
    assert resolution["requirement"]["citation"]["quote"] == expected_quote
    assert resolution["proof"]["terms"]["below_benchmark_deduction_per_percent"] == "0.75"
    planned = plan_bid(Context(), extracted, parsed, manifest, None, {"map_table_fields": False})
    commercial_action, = [item for item in planned["review_actions"] if item["kind"] == "commercial_constraint"]
    assert commercial_action["source_requirement"] == expected_quote and commercial_action["status"] == "not_checked"

    forged = copy.deepcopy(model_result)
    forged[PRICE_SCORING_CONFIG_KEY]["proof"]["terms"]["minimum_points"] = "21"
    with pytest.raises(PlanningError, match="EXTRACTION_PRICE_SCORING_RESOLUTION_MISMATCH"):
        _validate_normalized_window_result(forged, windows[0])
    with pytest.raises(PlanningError, match="PRICE_SCORING_SOURCE_RAW_RESPONSE_MISMATCH"):
        validate_live_window({**raw, "requirements": []}, windows[0])


@pytest.mark.parametrize("damage", ["different_columns", "missing_merge", "altered_rounding", "altered_operation",
                                    "wrong_branch_factor", "wrong_floor", "wrong_total", "ocr_origin", "unproved_footer"])
def test_price_scoring_source_fails_before_model_on_unproved_source(tmp_path, monkeypatch, damage):
    manifest, parsed, _, config, _, left, right = source_fixture(tmp_path, monkeypatch)
    if damage == "different_columns":
        right["column_widths"] = [70, 321, 34]
    elif damage == "missing_merge":
        right["merges"] = []
    elif damage == "altered_rounding":
        right["rows"][0][1] += " Different rounding."
    elif damage == "altered_operation":
        right["rows"][0][1] = right["rows"][0][1].replace(" - ", " + ")
    elif damage == "wrong_branch_factor":
        right["rows"][0][1] = right["rows"][0][1].replace("0.75", "2")
    elif damage == "wrong_floor":
        left["rows"][1][1] = left["rows"][1][1].replace("or 20", "or 40")
    elif damage == "wrong_total":
        right["rows"][1][2] = "41"
    elif damage == "ocr_origin":
        right["origin"] = "ocr"
    elif damage == "unproved_footer":
        parsed["documents"][0]["units"][1]["blocks"][-1]["text"] = "其他正文。"
    with pytest.raises(PlanningError, match="PRICE_SCORING_SOURCE_CONTINUATION_UNPROVEN"):
        extract_requirements(Context(), parsed, manifest, lambda *args: pytest.fail("unexpected request"), config)
