"""Cross-page source selection through N2 and N3, without remote calls."""
import copy

import pytest

from company_bid.extraction import (PlanningError, _index, _windows, _validate_window,
    _wire_candidate, validate_live_window, extract_requirements)
from company_bid.planning import plan_bid
from test_source_selected_citations import Context, fixture, block, selected, live_candidate, no_call


def sources(tmp_path):
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"
    header = ["条款号", "条款名称", "编列内容"]
    a = block(parsed, "TABLE1", "", kind="table", page=1, origin="native",
              rows=[header, ["7.6", "履约担保", "☑不要求递交\n□要求递交"]],
              column_widths=[55., 125., 280.], merges=[], header_rows=[])
    b = block(parsed, "TABLE2", "", kind="table", page=2, origin="native",
              rows=[header, ["", "", "选中要求递交时：金额5%，期限按合同。"], ["7.7", "付款方式", "以合同约定为准"]],
              column_widths=[55., 125., 280.], merges=[], header_rows=[])
    a["locator"], b["locator"] = "pdf/page[1]/table[0]", "pdf/page[2]/table[0]"
    return manifest, parsed, a, b


def candidate(window, a, b):
    raw = live_candidate({"window": window}, selected(a, b))
    raw["requirements"][0].update(category="commercial", response_mode="procedure")
    return raw


def test_two_complete_tables_keep_checkbox_condition_and_all_cell_sources(tmp_path):
    manifest, parsed, a, b = sources(tmp_path)
    blocks, _, docs = _index(parsed, manifest)
    window = _windows(docs, blocks, {})[0][0]
    result = validate_live_window(candidate(window, a, b), window)
    citation = result["requirements"][0]["citation"]
    assert citation["quote"] == blocks["TABLE1"]["text"] + "\n" + blocks["TABLE2"]["text"]
    assert "☑不要求递交\n□要求递交" in citation["quote"]
    assert citation["basis"] == "consecutive_native_tables_tsv_v1"
    assert {cell["block_id"] for cell in citation["cell_refs"]} == {"TABLE1", "TABLE2"}
    assert citation["table_continuation_proof_sha256"] == window["source_table_continuations"][0]["proof_sha256"]
    assert _validate_window(_wire_candidate(result), window) == result


@pytest.mark.parametrize("selected_index", [0, 1])
def test_either_half_alone_cannot_lose_the_switch_or_continuation(tmp_path, selected_index):
    manifest, parsed, a, b = sources(tmp_path)
    blocks, _, docs = _index(parsed, manifest)
    window = _windows(docs, blocks, {})[0][0]
    raw = candidate(window, a, b)
    raw["requirements"][0]["citation"] = selected((a, b)[selected_index])
    with pytest.raises(PlanningError, match="TABLE_CONTINUATION_SELECTION_REQUIRED"):
        validate_live_window(raw, window)


@pytest.mark.parametrize("empty_answer", [False, True])
def test_unproved_tail_cannot_be_treated_as_an_ordinary_complete_table(tmp_path, empty_answer):
    manifest, parsed, a, b = sources(tmp_path)
    b["column_widths"][0] += .01
    blocks, _, docs = _index(parsed, manifest)
    window = _windows(docs, blocks, {})[0][0]
    assert window["source_table_continuations"] == []
    raw = candidate(window, a, b)
    raw["requirements"] = [] if empty_answer else [{**raw["requirements"][0], "citation": selected(b)}]
    with pytest.raises(PlanningError, match="TABLE_CONTINUATION_UNPROVEN"):
        validate_live_window(raw, window)


def test_unproved_tail_stops_before_actual_provider_call(tmp_path):
    manifest, parsed, a, b = sources(tmp_path)
    b["column_widths"][0] += .01
    with pytest.raises(PlanningError, match="TABLE_CONTINUATION_UNPROVEN"):
        extract_requirements(Context(), parsed, manifest, no_call, {})


@pytest.mark.parametrize("damage", ["proof", "text", "source_hash", "order"])
def test_context_proof_or_source_mutation_is_rejected(tmp_path, damage):
    manifest, parsed, a, b = sources(tmp_path)
    blocks, _, docs = _index(parsed, manifest)
    window = _windows(docs, blocks, {})[0][0]
    raw = candidate(window, a, b)
    if damage == "proof":
        window["source_table_continuations"][0]["proof_sha256"] = "b" * 64
    elif damage == "text":
        window["blocks"][1]["text"] += "篡改"
    elif damage == "source_hash":
        window["source_sha256"] = "b" * 64
    else:
        raw["requirements"][0]["citation"] = selected(b, a)
    with pytest.raises(PlanningError):
        validate_live_window(raw, window)


def test_split_windows_share_only_proved_table_context_and_n3_rebuilds(tmp_path):
    manifest, parsed, a, b = sources(tmp_path)
    requests = []
    def call(ctx, task_type, unit_id, payload, validate):
        window = payload["window"]
        requests.append(window)
        assert len(window["blocks"]) == 1
        assert {row["block_id"] for row in window["adjacent_context"]} == ({"TABLE1", "TABLE2"} - {window["blocks"][0]["block_id"]})
        return validate(candidate(window, a, b))
    extracted = extract_requirements(Context(), parsed, manifest, call, {"window_max_blocks": 1})
    assert len(requests) == 2 and extracted["coverage"]["tender_unit_count"] == 2
    assert len(extracted["requirements"]) == 1
    planned = plan_bid(Context(), extracted, parsed, manifest, no_call, {})
    assert len(planned["review_actions"]) == 1
    assert "☑不要求递交" in planned["review_actions"][0]["source_requirement"]
    changed = copy.deepcopy(extracted)
    changed["window_results"][0]["validated"]["requirements"][0]["citation"]["table_continuation_proof_sha256"] = "b" * 64
    with pytest.raises(PlanningError):
        plan_bid(Context(), changed, parsed, manifest, no_call, {})
