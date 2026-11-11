"""Frozen row selectors preserve conditions and survive N2/N3 source rebuilds."""
import copy

import pytest

from company_bid.extraction import (
    PlanningError, _index, _windows, _wire_candidate, _validate_window,
    validate_live_window, extract_requirements,
)
from company_bid.planning import plan_bid
from test_source_selected_citations import Context, live_candidate, no_call
from test_cross_page_table_citations import sources


def selector(window, number):
    row = next(row for row in window["source_table_row_catalog"] if row["quote"].startswith(number + "\t"))
    return {key: row[key] for key in ("source_id", "block_id", "locator", "block_ids", "table_row_id")}


def response(window, number):
    raw = live_candidate({"window": window}, selector(window, number))
    raw["requirements"][0].update(category="commercial", response_mode="procedure")
    return raw


@pytest.mark.parametrize("number", ["7.6", "7.7"])
def test_exact_row_quote_and_parent_offsets_round_trip(tmp_path, number):
    manifest, parsed, a, b = sources(tmp_path)
    blocks, _, docs = _index(parsed, manifest)
    window = _windows(docs, blocks, {})[0][0]
    result = validate_live_window(response(window, number), window)
    cite = result["requirements"][0]["citation"]
    assert cite["basis"] == "frozen_table_rows_tsv_v1"
    if number == "7.6":
        assert "☑不要求递交\n□要求递交" in cite["quote"]
        assert "金额5%" in cite["quote"] and "7.7" not in cite["quote"]
        assert len(cite["table_row_parts"]) == 2
    else:
        assert cite["quote"] == "7.7\t付款方式\t以合同约定为准"
        assert "5%" not in cite["quote"] and len(cite["table_row_parts"]) == 1
    for part in cite["table_row_parts"]:
        assert blocks[part["block_id"]]["text"][part["start"]:part["end"]] == part["quote"]
    assert _validate_window(_wire_candidate(result), window) == result


@pytest.mark.parametrize("damage", ["unknown_id", "missing_parent", "clause_id", "quote", "catalog", "grid"])
def test_row_selector_rejects_forgery_or_loss(tmp_path, damage):
    manifest, parsed, a, b = sources(tmp_path)
    blocks, _, docs = _index(parsed, manifest)
    window = _windows(docs, blocks, {})[0][0]
    raw = response(window, "7.6")
    citation = raw["requirements"][0]["citation"]
    if damage == "unknown_id":
        citation["table_row_id"] = "TROW-unknown"
    elif damage == "missing_parent":
        citation["block_ids"] = ["TABLE1"]
    elif damage == "clause_id":
        citation["clause_id"] = "CLAUSE-fabricated"
    elif damage == "quote":
        internal = _wire_candidate(validate_live_window(raw, window))
        internal["requirements"][0]["citation"]["quote"] = "要求递交"
        with pytest.raises(PlanningError, match="TABLE_ROW_QUOTE_MISMATCH"):
            _validate_window(internal, window)
        return
    elif damage == "catalog":
        window["source_table_row_catalog"][0]["quote"] = "要求递交"
    else:
        window["blocks"][0]["column_widths"][0] += 1
    with pytest.raises(PlanningError):
        validate_live_window(raw, window)


def test_row_is_not_a_format_heading(tmp_path):
    manifest, parsed, a, b = sources(tmp_path)
    blocks, _, docs = _index(parsed, manifest)
    window = _windows(docs, blocks, {})[0][0]
    raw = response(window, "7.6")
    raw["format_status"] = "specified"
    raw["format_chapters"] = [{"title": "履约担保", "level": 1, "citation": selector(window, "7.6"),
                               "content_block_ids": ["TABLE1"], "requirement_indices": [0]}]
    with pytest.raises(PlanningError, match="FORMAT_TABLE_ROW_SELECTION_FORBIDDEN"):
        validate_live_window(raw, window)


def test_unproved_cross_page_context_stops_before_paying_for_first_split_window(tmp_path):
    manifest, parsed, a, b = sources(tmp_path)
    b["column_widths"][0] += .01
    context = Context()
    with pytest.raises(PlanningError, match="WINDOW_TABLE_ROW_CATALOG_MISMATCH"):
        extract_requirements(context, parsed, manifest, no_call, {"window_max_blocks": 1})
    report = context.captures["n2-source-context-preflight"]
    assert report["actual_call_started"] is False
    assert report["error_code"] == "WINDOW_TABLE_ROW_CATALOG_MISMATCH"


@pytest.mark.parametrize("damage", [None, "parts", "parents", "proof"])
def test_n3_rebuilds_row_evidence_and_preserves_split_denominator(tmp_path, damage):
    manifest, parsed, a, b = sources(tmp_path)
    requests = []
    def call(ctx, task_type, unit_id, payload, validate):
        window = payload["window"]
        requests.append(window)
        assert len(window["blocks"]) == 1
        return validate(response(window, "7.6"))
    context = Context()
    extracted = extract_requirements(context, parsed, manifest, call, {"window_max_blocks": 1})
    assert len(requests) == 2 and extracted["coverage"]["tender_unit_count"] == 2
    assert len(extracted["requirements"]) == 1
    assert any("table-row-proofs" in name for name in context.captures)
    if damage:
        changed = copy.deepcopy(extracted)
        cite = changed["window_results"][0]["validated"]["requirements"][0]["citation"]
        if damage == "parts":
            cite["table_row_parts"][1]["start"] += 1
        elif damage == "parents":
            cite["table_row_parents"][0]["grid_sha256"] = "b" * 64
        else:
            cite["table_row_proof_sha256"] = "b" * 64
        with pytest.raises(PlanningError):
            plan_bid(Context(), changed, parsed, manifest, no_call, {})
    else:
        planned = plan_bid(Context(), extracted, parsed, manifest, no_call, {})
        assert len(planned["review_actions"]) == 1
        assert "☑不要求递交" in planned["review_actions"][0]["source_requirement"]
        assert "金额5%" in planned["review_actions"][0]["source_requirement"]
        assert "7.7" not in planned["review_actions"][0]["source_requirement"]
        assert any("selected_row_view_chars" in s["locator"] for s in planned["sources"])
