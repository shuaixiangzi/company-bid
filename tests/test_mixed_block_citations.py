"""Whole source paragraphs plus canonical table views, never rewritten prose."""
import copy
import hashlib

import pytest

from company_bid.extraction import PlanningError, _index, _windows, _wire_candidate, _validate_window, validate_live_window, extract_requirements
from company_bid.planning import plan_bid
from test_source_selected_citations import Context, fixture, block, selected, live_candidate, no_call
from test_cross_page_table_citations import sources as table_pair


def setup_source(tmp_path):
    manifest, parsed = fixture(tmp_path)
    a = block(parsed, "P", "仅在确认通知后支付服务费；收款信息按下表，不得另行指定。")
    t = block(parsed, "T", "", kind="table", rows=[["账户", "币种"], ["合成账户", "人民币"], ["例外不得省略", ""]],
              merges=[], column_widths=[1, 2], header_rows=[])
    end = block(parsed, "END", "未收到通知前不支付。")
    return manifest, parsed, a, t, end


def response(window, *parts):
    raw = live_candidate({"window": window}, selected(*parts))
    raw["requirements"][0].update(category="commercial", response_mode="procedure")
    return raw


def test_mixed_blocks_retain_canonical_views_and_round_trip(tmp_path):
    manifest, parsed, a, t, end = setup_source(tmp_path)
    bs, _, docs = _index(parsed, manifest)
    w = _windows(docs, bs, {})[0][0]
    result = validate_live_window(response(w, a, t, end), w)
    cite = result["requirements"][0]["citation"]
    assert cite["quote"] == "\n".join(bs[b["block_id"]]["text"] for b in (a, t, end))
    assert cite["basis"] == "consecutive_mixed_blocks_lf_v1"
    assert [p["kind"] for p in cite["block_parts"]] == ["paragraph", "table", "paragraph"]
    for p in cite["block_parts"]:
        assert hashlib.sha256(cite["quote"][p["quote_start"]:p["quote_end"]].encode()).hexdigest() == p["text_sha256"]
    assert all(x["block_id"] == "T" for x in cite["cell_refs"])
    assert _validate_window(_wire_candidate(result), w) == result


@pytest.mark.parametrize("damage", ["order", "gap", "partial_quote", "image", "rewritten_table", "other_source"])
def test_mixed_view_still_rejects_source_loss_or_substitution(tmp_path, damage):
    manifest, parsed, a, t, end = setup_source(tmp_path)
    bs, _, docs = _index(parsed, manifest)
    w = _windows(docs, bs, {})[0][0]
    raw = response(w, a, t, end)
    if damage == "order":
        raw["requirements"][0]["citation"] = selected(a, end, t)
    elif damage == "gap":
        raw["requirements"][0]["citation"] = selected(a, end)
    elif damage == "partial_quote":
        internal = validate_live_window(raw, w)
        internal["requirements"][0]["citation"]["quote"] = bs["T"]["text"]
        with pytest.raises(PlanningError):
            _validate_window(_wire_candidate(internal), w)
        return
    elif damage == "image":
        w["blocks"][1]["kind"] = "image"
    elif damage == "rewritten_table":
        w["blocks"][1]["text"] = "改写表格"
    else:
        w["blocks"][1]["source_id"] = "OTHER"
    with pytest.raises(PlanningError):
        validate_live_window(raw, w)


def test_n3_rebuilds_each_mixed_block_including_grid_hash(tmp_path):
    manifest, parsed, a, t, end = setup_source(tmp_path)
    def call(ctx, task_type, unit_id, payload, validate):
        return validate(response(payload["window"], a, t, end))
    extracted = extract_requirements(Context(), parsed, manifest, call, {})
    planned = plan_bid(Context(), extracted, parsed, manifest, no_call, {})
    assert "未收到通知前不支付" in planned["review_actions"][0]["source_requirement"]
    changed = copy.deepcopy(extracted)
    changed["window_results"][0]["validated"]["requirements"][0]["citation"]["block_parts"][1]["grid_sha256"] = "b" * 64
    with pytest.raises(PlanningError):
        plan_bid(Context(), changed, parsed, manifest, no_call, {})


def test_mixed_view_cannot_bypass_complete_proven_table_pair(tmp_path):
    manifest, parsed, a, b = table_pair(tmp_path)
    p = block(parsed, "P", "其后明确说明应完整保留。", page=2)
    bs, _, docs = _index(parsed, manifest)
    w = _windows(docs, bs, {})[0][0]
    with pytest.raises(PlanningError, match="TABLE_CONTINUATION_SELECTION_REQUIRED"):
        validate_live_window(response(w, b, p), w)
    cite = validate_live_window(response(w, a, b, p), w)["requirements"][0]["citation"]
    assert cite["basis"] == "consecutive_mixed_blocks_lf_v1"
    assert cite["table_continuation_proof_sha256"] == w["source_table_continuations"][0]["proof_sha256"]
    assert "☑不要求递交" in cite["quote"]
