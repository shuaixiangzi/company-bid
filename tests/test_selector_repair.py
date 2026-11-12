"""Selector-only candidates retain bounded repair anchors without real HTTP."""
import copy
import json

import pytest

from company_bid.diagnostics import inspect_diagnostics
from company_bid.extraction import (
    PlanningError, _index, _model_window, _windows, source_selected_output_contract, validate_live_window,
)
from company_bid.structured import _covers_rejected_candidate, _repair_coverage
from test_source_selected_citations import block, fixture, live_candidate
from test_structured_validation_repair import artifacts, events, response, setup_run


def selector_source(tmp_path):
    manifest, parsed = fixture(tmp_path)
    block(parsed, "B", "必须保留完整的原始许可条件，", origin="native")
    block(parsed, "TAIL", "未取得许可不得操作，所有例外必须逐项列明。", origin="native")
    blocks, _, documents = _index(parsed, manifest)
    window = _windows(documents, blocks, {})[0][0]
    raw = live_candidate({"window": window}, {"block_ids": ["B", "TAIL"]})
    return window, raw


def configure_payload(payload, window):
    payload["window"] = _model_window(window)
    payload["output_contract"] = source_selected_output_contract()


def test_selector_only_label_repair_keeps_raw_selectors_and_derives_the_same_source_anchors(tmp_path, monkeypatch):
    window, second = selector_source(tmp_path)
    first = copy.deepcopy(second)
    first["requirements"][0]["required_fact_labels"] = ["不在原文的字段CANARY"]
    saved = copy.deepcopy((first, second))
    executor, rid, _, payload, _, sent, validated, _ = setup_run(
        tmp_path, monkeypatch, max_input_chars=50000,
        validate=lambda value, _: validate_live_window(value, window),
        transport=lambda _, count: response(first if count == 1 else second))
    configure_payload(payload, window)
    state = executor.execute(rid)
    assert state["status"] == "succeeded" and len(sent) == len(validated) == 2
    assert (first, second) == saved and validated == [first, second]
    feedback = json.loads(sent[1]["messages"][1]["content"])["validation_feedback"]
    assert feedback["required_coverage"] == {
        "counts": {"requirements": 1, "format_chapters": 0},
        "anchors": {"requirements": [{"source_id": "T", "block_id": "B", "block_ids": ["B", "TAIL"]}],
                    "format_chapters": []}}
    captured = artifacts(executor, state)
    raw_responses = [json.loads(value["choices"][0]["message"]["content"])
                     for record, value in captured if record["name"].startswith("response-")]
    assert raw_responses == [first, second]
    final = next(value["validated_output"] for record, value in captured
                 if record["name"].startswith("validation-") and value.get("status") == "passed")
    citation = final["requirements"][0]["citation"]
    assert {key: citation[key] for key in ("source_id", "block_id", "locator")} == {
        key: window["blocks"][0][key] for key in ("source_id", "block_id", "locator")}
    assert citation["block_ids"] == ["B", "TAIL"]
    report = inspect_diagnostics(tmp_path, rid)
    assert report["actual_call_attempts"] == 2 and report["gaps"] == []
    assert [call["outcome"] for call in report["calls"]] == ["failed", "completed"]
    assert executor.resume(rid)["status"] == "succeeded" and len(sent) == 2


@pytest.mark.parametrize("damage", ["drop_count", "drop_anchor", "drop_format", "drop_condition",
                                    "drop_clause_id", "drop_table_row_id"])
def test_selector_repair_cannot_delete_original_candidates_or_selected_source_proof(tmp_path, monkeypatch, damage):
    window, _ = selector_source(tmp_path)
    citation = {"block_ids": ["B", "TAIL"]}
    selector_key = "clause_id" if damage == "drop_clause_id" else "table_row_id" if damage == "drop_table_row_id" else None
    if selector_key:
        citation[selector_key] = "CLAUSE-1" if selector_key == "clause_id" else "TROW-1"
    first = {"requirements": [{"citation": citation}], "format_chapters": [{"citation": {"block_ids": ["B"]}}]}
    second = copy.deepcopy(first)
    if damage == "drop_count":
        second["requirements"] = []
    elif damage == "drop_anchor":
        second["requirements"][0]["citation"]["block_ids"] = ["TAIL"]
    elif damage == "drop_format":
        second["format_chapters"] = []
    elif damage == "drop_condition":
        second["requirements"][0]["citation"]["block_ids"] = ["B"]
    else:
        second["requirements"][0]["citation"].pop(selector_key)

    def check(value, count):
        if count == 1:
            raise PlanningError("EXTRACTION_SCHEMA_INVALID")
        return value

    executor, rid, _, payload, _, sent, validated, _ = setup_run(
        tmp_path, monkeypatch, validate=check, max_input_chars=50000,
        transport=lambda _, count: response(first if count == 1 else second))
    configure_payload(payload, window)
    if selector_key:
        canonical = {key: window["blocks"][0][key] for key in ("source_id", "block_id", "locator")}
        payload["window"]["source_clause_catalog" if selector_key == "clause_id" else "source_table_row_catalog"] = [
            {**canonical, **citation}]
    state = executor.execute(rid)
    assert state["error_code"] == "VALIDATION_REPAIR_COVERAGE_LOST"
    assert len(sent) == 2 and len(validated) == 1
    feedback = json.loads(sent[1]["messages"][1]["content"])["validation_feedback"]
    assert feedback["required_coverage"]["counts"] == {"requirements": 1, "format_chapters": 1}
    assert feedback["required_coverage"]["anchors"]["requirements"][0]["block_ids"] == ["B", "TAIL"]


@pytest.mark.parametrize("damage", ["unknown", "empty", "partial_source", "partial_locator", "mixed_source",
                                    "duplicate", "too_many_ids", "unknown_clause", "unknown_row"])
def test_unsafe_first_selector_inventory_never_schedules_a_repair(tmp_path, monkeypatch, damage):
    window, first = selector_source(tmp_path)
    cite = first["requirements"][0]["citation"]
    if damage == "unknown":
        cite["block_ids"] = ["B", "UNKNOWN"]
    elif damage == "empty":
        cite["block_ids"] = []
    elif damage == "partial_source":
        cite["source_id"] = "T"
    elif damage == "partial_locator":
        cite["locator"] = window["blocks"][0]["locator"]
    elif damage == "mixed_source":
        window["blocks"][1]["source_id"] = "OTHER"
    elif damage == "duplicate":
        cite["block_ids"] = ["B", "B"]
    elif damage == "too_many_ids":
        cite["block_ids"] = ["B"] * 25
    elif damage == "unknown_clause":
        cite["clause_id"] = "CLAUSE-UNKNOWN"
    else:
        cite["table_row_id"] = "TROW-UNKNOWN"
    saved = copy.deepcopy(first)

    def fail_validation(*_):
        raise PlanningError("EXTRACTION_SCHEMA_INVALID")

    executor, rid, _, payload, _, sent, _, _ = setup_run(
        tmp_path, monkeypatch, max_input_chars=50000, validate=fail_validation,
        transport=lambda *_: response(first))
    configure_payload(payload, window)
    assert _repair_coverage(first, payload) is None
    state = executor.execute(rid)
    assert state["error_code"] == "EXTRACTION_SCHEMA_INVALID" and len(sent) == 1
    assert first == saved
    assert not any(row["event"] == "call.retry_scheduled" for row in events(executor, rid))


@pytest.mark.parametrize("damage", ["unknown", "partial", "empty"])
def test_invalid_replacement_selector_is_rejected_before_the_second_validator(tmp_path, monkeypatch, damage):
    window, first = selector_source(tmp_path)
    second = copy.deepcopy(first)
    cite = second["requirements"][0]["citation"]
    if damage == "unknown":
        cite["block_ids"] = ["B", "UNKNOWN"]
    elif damage == "partial":
        cite["source_id"] = "T"
    else:
        cite["block_ids"] = []

    def check(value, count):
        if count == 1:
            raise PlanningError("EXTRACTION_SCHEMA_INVALID")
        return value

    executor, rid, _, payload, _, sent, validated, _ = setup_run(
        tmp_path, monkeypatch, max_input_chars=50000, validate=check,
        transport=lambda _, count: response(first if count == 1 else second))
    configure_payload(payload, window)
    state = executor.execute(rid)
    assert state["error_code"] == "VALIDATION_REPAIR_COVERAGE_LOST"
    assert len(sent) == 2 and len(validated) == 1


def test_old_complete_but_wrong_locator_is_never_derived_again_or_automatically_repaired(tmp_path, monkeypatch):
    window, first = selector_source(tmp_path)
    cite = first["requirements"][0]["citation"]
    cite.update({key: window["blocks"][0][key] for key in ("source_id", "block_id", "locator")})
    cite["locator"] = "pdf/page[1]/paragraph[WRONG]"
    saved = copy.deepcopy(first)
    executor, rid, _, payload, _, sent, _, _ = setup_run(
        tmp_path, monkeypatch, max_input_chars=50000,
        validate=lambda value, _: validate_live_window(value, window), transport=lambda *_: response(first))
    configure_payload(payload, window)
    state = executor.execute(rid)
    assert state["error_code"] == "CITATION_LOCATION_INVALID" and len(sent) == 1
    assert first == saved
    assert not any(row["event"] == "call.retry_scheduled" for row in events(executor, rid))


def test_two_argument_legacy_anchor_match_stays_compatible_but_request_path_rejects_partial_coordinates(tmp_path):
    window, _ = selector_source(tmp_path)
    legacy = {"requirements": [{"citation": {"source_id": "T", "block_id": "B", "block_ids": ["B", "TAIL"]}}]}
    coverage = {"counts": {"requirements": 1}, "anchors": {"requirements": [copy.deepcopy(legacy["requirements"][0]["citation"])]}}
    assert _covers_rejected_candidate(legacy, coverage)
    assert not _covers_rejected_candidate(legacy, coverage, {"window": _model_window(window)})
