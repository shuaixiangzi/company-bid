"""Canonical ID selectors derive coordinates from frozen sources, without repair."""
import copy
import json

import pytest

import company_bid.extraction as extraction
from company_bid.diagnostics import inspect_diagnostics
from company_bid.extraction import (
    PlanningError, _index, _validate_normalized_window_result, _windows,
    extract_requirements, source_selected_output_contract, validate_live_window,
)
from company_bid.planning import plan_bid
from company_bid.runtime import Executor, NodeSpec
from company_bid.structured import JsonTaskGateway
from test_source_selected_citations import Context, block, fixture, live_candidate, no_call, selected


def sources(tmp_path, *, block_limit=60):
    manifest, parsed = fixture(tmp_path)
    first = block(parsed, "B1", "只有取得书面许可后，")
    second = block(parsed, "B2", "方可开展巡检并提交记录。")
    third = block(parsed, "B3", "不得省略例外条件或复测记录。")
    blocks, _, documents = _index(parsed, manifest)
    windows, _ = _windows(documents, blocks, {"window_max_blocks": block_limit})
    return manifest, parsed, blocks, windows, (first, second, third)


def raw_for(window, ids):
    return live_candidate({"window": window}, {"block_ids": list(ids)})


def test_live_schema_only_exposes_canonical_block_ids_and_catalog_choices():
    schema = source_selected_output_contract()
    citation = schema["$defs"]["_Citation"]
    assert set(citation["properties"]) == {"block_ids", "clause_id", "table_row_id"}
    assert citation["required"] == ["block_ids"]
    assert citation["properties"]["block_ids"]["minItems"] == 1
    assert citation["properties"]["block_ids"]["maxItems"] == 24
    assert citation["additionalProperties"] is False
    for field in ("clause_id", "table_row_id"):
        description = citation["properties"][field]["description"]
        assert "完整block_ids" in description and "冻结来源派生" in description
        assert all(key not in description for key in ("source_id", "block_id/", "locator", "quote"))
    assert extraction._Citation.model_json_schema()["required"] == ["source_id", "block_id", "locator"]
    citation["properties"]["locator"] = {"type": "string"}
    assert "locator" not in source_selected_output_contract()["$defs"]["_Citation"]["properties"]


@pytest.mark.parametrize("ids", [["B1"], ["B1", "B2", "B3"]])
def test_single_and_multiple_block_ids_derive_exact_first_source_coordinates(tmp_path, ids):
    _, _, blocks, windows, _ = sources(tmp_path)
    raw = raw_for(windows[0], ids)
    saved = copy.deepcopy(raw)
    result = validate_live_window(raw, windows[0])
    citation = result["requirements"][0]["citation"]
    assert {key: citation[key] for key in ("source_id", "block_id", "locator")} == {
        key: blocks[ids[0]][key] for key in ("source_id", "block_id", "locator")}
    assert citation["quote"] == "\n".join(blocks[key]["text"] for key in ids)
    assert raw == saved


def test_first_selected_adjacent_block_supplies_coordinates_without_changing_target_denominator(tmp_path):
    _, _, blocks, windows, _ = sources(tmp_path, block_limit=1)
    window = windows[1]
    result = validate_live_window(raw_for(window, ["B1", "B2"]), window)
    assert result["reviewed_block_ids"] == ["B2"]
    citation = result["requirements"][0]["citation"]
    assert citation["block_id"] == "B1" and citation["locator"] == blocks["B1"]["locator"]


@pytest.mark.parametrize("ids", [[], ["UNKNOWN"], ["1"], ["B1", "UNKNOWN"], ["B1", "B1"],
                                 ["B2", "B1"], ["B1", "B3"], ["B1"] * 25, [1], [None]])
def test_invalid_ids_are_not_guessed_deduplicated_reordered_or_completed(tmp_path, ids):
    _, _, _, windows, _ = sources(tmp_path)
    raw = raw_for(windows[0], ids)
    saved = copy.deepcopy(raw)
    with pytest.raises(PlanningError):
        validate_live_window(raw, windows[0])
    assert raw == saved


@pytest.mark.parametrize("coordinates", [
    {"source_id": "T"}, {"block_id": "B1"}, {"locator": "pdf/page[1]/paragraph[B1]"},
    {"source_id": "T", "block_id": "B1"}, {"source_id": "T", "locator": "x"},
    {"block_id": "B1", "locator": "x"},
])
def test_partial_coordinates_are_rejected_instead_of_completed(tmp_path, coordinates):
    _, _, _, windows, _ = sources(tmp_path)
    raw = raw_for(windows[0], ["B1"])
    raw["requirements"][0]["citation"].update(coordinates)
    saved = copy.deepcopy(raw)
    with pytest.raises(PlanningError, match="EXTRACTION_SCHEMA_INVALID"):
        validate_live_window(raw, windows[0])
    assert raw == saved


def test_mixed_source_selection_still_uses_original_same_source_guard(tmp_path):
    _, _, _, windows, _ = sources(tmp_path)
    window = copy.deepcopy(windows[0])
    window["blocks"][1]["source_id"] = "OTHER"
    with pytest.raises(PlanningError, match="CITATION_SPAN_ORDER_INVALID"):
        validate_live_window(raw_for(window, ["B1", "B2"]), window)


def test_old_full_coordinates_keep_exact_location_check_without_fallback(tmp_path):
    _, _, _, windows, (first, _, _) = sources(tmp_path)
    raw = live_candidate({"window": windows[0]}, selected(first, block_ids=["B1"]))
    assert validate_live_window(raw, windows[0])["requirements"][0]["citation"]["locator"] == first["locator"]
    raw["requirements"][0]["citation"]["locator"] = "pdf/page[999]/paragraph[B1]"
    saved = copy.deepcopy(raw)
    with pytest.raises(PlanningError, match="CITATION_LOCATION_INVALID"):
        validate_live_window(raw, windows[0])
    assert raw == saved


def test_quote_in_selector_is_still_forbidden_and_labels_and_unresolved_are_not_waived(tmp_path):
    _, _, _, windows, _ = sources(tmp_path)
    for case, code in (("quote", "LIVE_CITATION_QUOTE_FORBIDDEN"),
                       ("label", "EXTRACTION_LABEL_NOT_GROUNDED"),
                       ("unresolved", "LIVE_REQUIRED_BLOCK_UNRESOLVED")):
        raw = raw_for(windows[0], ["B1"])
        if case == "quote":
            raw["requirements"][0]["citation"]["quote"] = "模型不得伪造原文"
        elif case == "label":
            raw["requirements"][0]["required_fact_labels"] = ["原文不存在的字段"]
        else:
            raw["unresolved_block_ids"] = ["B1"]
        with pytest.raises(PlanningError, match=code):
            validate_live_window(raw, windows[0])


def test_clause_and_row_ids_keep_original_catalog_source_and_business_guards(tmp_path):
    from test_clause_citations import source as clause_source, wire
    from test_cross_page_table_citations import sources as table_sources
    from test_table_row_citations import response

    _, _, _, window, clauses = clause_source(tmp_path)
    raw = wire(window, clauses)
    for item in raw["requirements"]:
        item["citation"] = {key: value for key, value in item["citation"].items()
                            if key in {"block_ids", "clause_id"}}
    validated = validate_live_window(raw, window)
    assert [item["citation"]["quote"] for item in validated["requirements"]] == [value["quote"] for value in clauses]
    wrong = copy.deepcopy(raw)
    wrong["requirements"][0]["response_mode"] = "procedure"
    with pytest.raises(PlanningError, match="CLAUSE_RESPONSE_MODE_INVALID"):
        validate_live_window(wrong, window)
    manifest, parsed, _, _ = table_sources(tmp_path)
    blocks, _, documents = _index(parsed, manifest)
    window = _windows(documents, blocks, {})[0][0]
    raw = response(window, "7.6")
    raw["requirements"][0]["citation"] = {key: value for key, value in raw["requirements"][0]["citation"].items()
                                             if key in {"block_ids", "table_row_id"}}
    citation = validate_live_window(raw, window)["requirements"][0]["citation"]
    assert citation["basis"] == "frozen_table_rows_tsv_v1" and "☑不要求递交" in citation["quote"]
    raw["requirements"][0]["citation"]["block_ids"] = ["B-NOT-A-CATALOG-MEMBER"]
    with pytest.raises(PlanningError):
        validate_live_window(raw, window)


def test_format_chapter_uses_selector_without_weakening_anchor_or_content_scope(tmp_path):
    manifest, parsed = fixture(tmp_path)
    block(parsed, "HEADING", "巡检方案")
    block(parsed, "BODY", "应说明巡检方法。")
    blocks, _, documents = _index(parsed, manifest)
    window = _windows(documents, blocks, {})[0][0]
    raw = raw_for(window, ["BODY"])
    raw.update(format_status="specified", format_chapters=[{"title": "巡检方案", "level": 1,
        "citation": {"block_ids": ["HEADING"]}, "content_block_ids": ["BODY"], "requirement_indices": [0]}])
    result = validate_live_window(raw, window)
    assert result["format_chapters"][0]["citation"]["locator"] == blocks["HEADING"]["locator"]
    raw["format_chapters"][0]["content_block_ids"] = ["UNKNOWN"]
    with pytest.raises(PlanningError, match="FORMAT_CONTENT_REFERENCE_INVALID"):
        validate_live_window(raw, window)


@pytest.mark.parametrize("damage", [None, "record", "raw", "coordinates", "record_missing"])
def test_raw_selector_stays_immutable_and_n3_rebuilds_and_checks_normalization(tmp_path, damage):
    manifest, parsed, blocks, windows, _ = sources(tmp_path)
    raw = raw_for(windows[0], ["B1", "B2", "B3"])
    saved = copy.deepcopy(raw)
    ctx = Context()
    extracted = extract_requirements(ctx, parsed, manifest,
        lambda ctx, task, unit, payload, validate: validate(raw), {})
    result = extracted["window_results"][0]
    record = result["source_selection_normalization"]
    selection, = record["selections"]
    assert raw == saved == result["raw_model_candidate"]
    assert selection["original_selector"] == {"block_ids": ["B1", "B2", "B3"]}
    assert selection["derived_coordinates"] == {key: blocks["B1"][key] for key in ("source_id", "block_id", "locator")}
    assert selection["coordinate_origin"] == "application_frozen_source"
    assert record["frozen_window_sha256"] == extraction._hash(windows[0])
    assert record == ctx.captures["n2-source-selection-normalization"]
    if damage is None:
        assert _validate_normalized_window_result(result, windows[0]) == result["validated"]
        assert plan_bid(Context(), extracted, parsed, manifest, no_call, {})["requirements"]
        return
    if damage == "record":
        selection["derived_coordinates"]["locator"] = "forged-private-audit"
    elif damage == "record_missing":
        result.pop("source_selection_normalization")
    elif damage == "raw":
        result["raw_model_candidate"]["requirements"][0]["citation"]["block_ids"] = ["B1"]
    else:
        result["candidate"]["requirements"][0]["citation"]["locator"] = "forged-normalized-coordinate"
    with pytest.raises(PlanningError):
        plan_bid(Context(), extracted, parsed, manifest, no_call, {})


@pytest.mark.parametrize("scenario", ["success", "label_failure", "label_repair", "first_capture_failure", "second_capture_failure"])
def test_real_executor_keeps_each_response_selector_and_call_correlated_normalization(tmp_path, monkeypatch, scenario):
    manifest, parsed, _, _, _ = sources(tmp_path)
    monkeypatch.setenv("TEST_SELECTOR_KEY", "synthetic-selector-credential")
    options = {"mode": "chat_completions", "endpoint": "https://example.invalid/chat", "model": "synthetic",
               "auth_env": "TEST_SELECTOR_KEY", "max_attempts": 1, "max_output_tokens": 512,
               "max_input_chars": 50000, "max_run_calls": 2, "max_run_usd": .1,
               "input_usd_per_million": 1, "output_usd_per_million": 1}
    options["validation_repair_attempts"] = 0 if scenario in {"success", "label_failure"} else 1
    sent, originals = [], []

    def transport(endpoint, payload, secret, timeout):
        sent.append(copy.deepcopy(payload))
        request = json.loads(payload["messages"][-1]["content"])
        raw = raw_for(request["window"], ["B1", "B2", "B3"])
        if scenario != "success" and len(sent) == 1:
            raw["requirements"][0]["required_fact_labels"] = ["原文不存在的字段CANARY"]
        originals.append(copy.deepcopy(raw))
        return {"id": "synthetic-selector-response", "choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(raw)}}], "usage": {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12}}

    gateway = JsonTaskGateway(options, transport=transport)
    executor = Executor(tmp_path / "runtime", [NodeSpec("N2", "selector-wire-test-v1",
        lambda ctx, value: extract_requirements(ctx, value["parsed"], value["manifest"], gateway, {}))],
        {"structured_provider": options, "debug_capture": True})
    rid = executor.create_run({"parsed": parsed, "manifest": manifest, "synthetic": True})
    fail_capture = {"first_capture_failure": 1, "second_capture_failure": 2}.get(scenario)
    if fail_capture:
        original_put = executor.store.put_json
        normalization_writes = []

        def put_json(run_id, kind, payload):
            if kind == "capture" and payload.get("rule") == "frozen_block_ids_to_canonical_coordinates_v1":
                normalization_writes.append(copy.deepcopy(payload))
                if len(normalization_writes) == fail_capture:
                    raise OSError("Synthetic normalization capture write failure")
            return original_put(run_id, kind, payload)

        monkeypatch.setattr(executor.store, "put_json", put_json)
    state = executor.execute(rid)
    expected_calls = 2 if scenario in {"label_repair", "second_capture_failure"} else 1
    success = scenario in {"success", "label_repair"}
    assert len(sent) == expected_calls
    assert state["status"] == ("succeeded" if success else "failed"), state.get("error_code")
    if not success:
        assert state["error_code"] == ("EXTRACTION_LABEL_NOT_GROUNDED" if scenario == "label_failure" else "DIAGNOSTICS_CAPTURE_FAILED")
    records = state["nodes"][0]["attempts"][-1]["captures"]
    captured = [(record["name"], json.loads(executor.store.read_bytes(record["ref"]))) for record in records if record.get("ref")]
    responses = [json.loads(value["choices"][0]["message"]["content"]) for name, value in captured if name.startswith("response-")]
    assert responses == originals
    audits = [record for record in records if record["name"] == "n2-source-selection-normalization"]
    assert len(audits) == expected_calls
    assert [record["status"] for record in audits] == (["captured"] * (expected_calls - 1) + ["write_failed"] if fail_capture else ["captured"] * expected_calls)
    normalizations = [value for name, value in captured if name == "n2-source-selection-normalization"]
    report = inspect_diagnostics(tmp_path / "runtime", rid)
    assert report["actual_call_attempts"] == expected_calls
    if not fail_capture:
        assert not report["gaps"]
    spans = {value["span_id"]: value for value in map(json.loads,
        (executor.store.run_dir(rid) / "spans.jsonl").read_text(encoding="utf-8").splitlines())}
    for index, normalization in enumerate(normalizations):
        call = report["calls"][index]
        assert normalization["provider_call"] == {key: call[key] for key in ("call_id", "call_attempt", "trace_id", "span_id")}
        assert spans[call["span_id"]]["name"] == "provider.call"
        selection, = normalization["selections"]
        assert selection["original_selector"] == originals[index]["requirements"][0]["citation"]
        assert selection["derived_coordinates"]["locator"] == parsed["documents"][0]["units"][0]["blocks"][0]["locator"]
    if success:
        result = executor.output(rid)["window_results"][0]
        assert result["raw_model_candidate"] == originals[-1]
        assert "locator" not in result["raw_model_candidate"]["requirements"][0]["citation"]
        assert {key: value for key, value in normalizations[-1].items() if key != "provider_call"} == result["source_selection_normalization"]
        assert executor.resume(rid)["status"] == "succeeded" and len(sent) == expected_calls
    else:
        assert "output_ref" not in state["nodes"][0]
