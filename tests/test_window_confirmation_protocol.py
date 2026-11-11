"""Live confirmation accounts for frozen input without claiming semantics."""
import copy

import pytest

from company_bid.extraction import (
    PlanningError, _validate_window, _windows, _index, extract_requirements,
    source_selected_output_contract, validate_live_window, _validate_normalized_window_result,
    CALLER_CANDIDATE_LOCATION,
)
from company_bid.planning import plan_bid
from test_source_selected_citations import (
    Context, fixture, block, selected, candidate, live_candidate, window_for,
)


def test_live_schema_confirms_window_instead_of_echoing_target_ids():
    schema = source_selected_output_contract()
    assert "reviewed_block_ids" not in schema["properties"]
    assert {"window_id", "processing_declaration", "unresolved_block_ids"} <= set(schema["required"])
    assert schema["properties"]["processing_declaration"]["const"] == "processed_not_semantically_accepted"
    assert schema["additionalProperties"] is False
    assert "none_in_this_window" in schema["properties"]["format_status"]["description"]


@pytest.mark.parametrize("change,code", [
    ({"window_id": "OTHER-WINDOW"}, "LIVE_WINDOW_ID_MISMATCH"),
    ({"reviewed_block_ids": []}, "LIVE_REVIEWED_BLOCK_IDS_FORBIDDEN"),
    ({"unresolved_block_ids": ["UNKNOWN"]}, "LIVE_UNRESOLVED_BLOCK_IDS_INVALID"),
    ({"unresolved_block_ids": ["B1", "B1"]}, "LIVE_UNRESOLVED_BLOCK_IDS_INVALID"),
    ({"unresolved_block_ids": ["B1"]}, "LIVE_REQUIRED_BLOCK_UNRESOLVED"),
    ({"processing_declaration": "semantically_accepted"}, "EXTRACTION_SCHEMA_INVALID"),
])
def test_wrong_identity_old_protocol_and_unresolved_body_are_not_repaired(tmp_path, change, code):
    manifest, parsed = fixture(tmp_path)
    source = block(parsed, "B1", "必须说明巡检方法，不得省略许可条件。")
    window = window_for(parsed, manifest)
    raw = {**live_candidate({"window": window}, selected(source)), **change}
    saved = copy.deepcopy(raw)
    with pytest.raises(PlanningError, match=code):
        validate_live_window(raw, window)
    assert raw == saved


def test_missing_unresolved_list_and_extra_completion_claim_fail_closed(tmp_path):
    manifest, parsed = fixture(tmp_path)
    source = block(parsed, "B1", "应说明巡检方法。")
    window = window_for(parsed, manifest)
    for case in ("missing", "extra"):
        raw = live_candidate({"window": window}, selected(source))
        if case == "missing":
            raw.pop("unresolved_block_ids")
        else:
            raw["semantic_review"] = "accepted"
        with pytest.raises(PlanningError, match="EXTRACTION_SCHEMA_INVALID"):
            validate_live_window(raw, window)


def test_unresolved_adjacent_id_is_only_a_deferred_context_not_target_completion(tmp_path):
    manifest, parsed = fixture(tmp_path)
    first = block(parsed, "B1", "应说明巡检方法。")
    second = block(parsed, "B2", "应提交异常复测记录。")
    blocks, _, documents = _index(parsed, manifest)
    window = _windows(documents, blocks, {"window_max_blocks": 1})[0][0]
    assert second["block_id"] in {b["block_id"] for b in window["adjacent_context"]}
    raw = live_candidate({"window": window}, selected(first))
    raw["unresolved_block_ids"] = [second["block_id"]]
    result = validate_live_window(raw, window)
    assert result["reviewed_block_ids"] == [first["block_id"]]
    assert second["block_id"] not in result["reviewed_block_ids"]


def test_program_accounts_for_logo_footer_and_body_without_claiming_model_reviewed(tmp_path):
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"
    logo = block(parsed, "LOGO", "", kind="image")
    parsed["documents"][0]["units"][0]["coverage"]["ignored_objects"] = [
        {"locator": logo["locator"], "reason": "Synthetic exact cover logo: no requirement text."}]
    first = block(parsed, "B1", "必须先取得许可，方可开展巡检。", bbox=[60, 100, 300, 112])
    for page in (1, 2, 3):
        if page != 1:
            block(parsed, f"B{page}", "应提交记录。", page=page, bbox=[60, 100, 300, 112])
        block(parsed, f"F{page}", f"- {page} -", page=page, bbox=[290, 773, 305, 782])
    ctx = Context()
    def call(ctx, task, unit, payload, validate):
        raw = live_candidate(payload, selected(first))
        raw["unresolved_block_ids"] = ["LOGO", "F1"]
        return validate(raw)
    output = extract_requirements(ctx, parsed, manifest, call, {})
    result = output["window_results"][0]
    assert output["coverage"]["semantic_review"] == "requires_source_review"
    assert result["candidate_origin"] == "application_normalized"
    assert result["raw_model_candidate_location"] == CALLER_CANDIDATE_LOCATION
    assert len(result["candidate"]["reviewed_block_ids"]) == 7
    assert "window_id" not in result["candidate"]
    assert "raw_structured_candidate_sha256" in result["live_confirmation"]
    processing = result["processing"]
    assert processing["counts"] == {"supplied": 3, "ignored_by_frozen_reader": 1, "safe_footer": 3}
    assert processing["semantic_review"] == "requires_source_review"
    by_id = {row["block_id"]: row for row in processing["blocks"]}
    assert by_id["LOGO"]["status"] == "ignored_by_frozen_reader"
    assert by_id["F1"]["status"] == "safe_footer"
    assert by_id["B1"]["status"] == "supplied"
    assert all("model_reviewed" not in row for row in processing["blocks"])
    assert result["validated"]["reviewed_block_ids"] == result["candidate"]["reviewed_block_ids"]
    assert ctx.captures["n2-" + result["window_id"]]["candidate_origin"] == "application_normalized"


def test_unresolved_body_error_keeps_bounded_private_location_and_raw_candidate(tmp_path):
    manifest, parsed = fixture(tmp_path)
    source = block(parsed, "B1", "必须保留原文CANARY，不得宣称已处理。")
    ctx = Context()
    supplied = []
    def call(ctx, task, unit, payload, validate):
        raw = live_candidate(payload, selected(source)); raw["unresolved_block_ids"] = ["B1"]
        supplied.append(copy.deepcopy(raw))
        return validate(raw)
    with pytest.raises(PlanningError, match="LIVE_REQUIRED_BLOCK_UNRESOLVED") as error:
        extract_requirements(ctx, parsed, manifest, call, {})
    assert error.value.diagnostic_context == {"field": "unresolved_block_ids", "block_ids": ["B1"]}
    assert "CANARY" not in str(error.value)
    assert supplied[0]["unresolved_block_ids"] == ["B1"]
    assert not ctx.units


def test_catalog_later_format_is_not_this_windows_unread_or_global_no_format(tmp_path):
    manifest, parsed = fixture(tmp_path)
    first = block(parsed, "B1", "应说明巡检方法。")
    for index in range(2, 10):
        block(parsed, f"B{index}", "本段为其它招标正文。")
    later = block(parsed, "FORMAT", "第二章 投标文件格式")
    blocks, _, documents = _index(parsed, manifest)
    window = _windows(documents, blocks, {"window_max_blocks": 1})[0][0]
    assert later["block_id"] in {b["block_id"] for b in window["source_heading_catalog"]}
    assert later["block_id"] not in {b["block_id"] for b in window["blocks"] + window["adjacent_context"]}
    raw = live_candidate({"window": window}, selected(first))
    assert validate_live_window(raw, window)["format_status"] == "none_verified"
    raw["format_status"] = "unread"
    with pytest.raises(PlanningError, match="FORMAT_UNREAD_CANNOT_BECOME_FREE_WRITING"):
        validate_live_window(raw, window)


def test_recorded_internal_denominator_remains_exact_and_strict(tmp_path):
    manifest, parsed = fixture(tmp_path)
    source = block(parsed, "B1", "应说明完整巡检方法。")
    window = window_for(parsed, manifest)
    recorded = candidate({"window": window}, selected(source))
    assert _validate_window(recorded, window)["reviewed_block_ids"] == ["B1"]
    recorded["reviewed_block_ids"] = []
    with pytest.raises(PlanningError, match="WINDOW_BLOCK_DENOMINATOR_MISMATCH"):
        _validate_window(recorded, window)


def test_reference_keyword_from_another_requirement_is_not_borrowed(tmp_path):
    manifest, parsed = fixture(tmp_path)
    first = block(parsed, "B1", "应说明巡检方法。")
    block(parsed, "B2", "异常复测报告必须单独提交。")
    window = window_for(parsed, manifest)
    raw = live_candidate({"window": window}, selected(first))
    raw["requirements"][0]["reference_keywords"] = ["异常复测报告"]
    with pytest.raises(PlanningError, match="EXTRACTION_LABEL_NOT_GROUNDED"):
        validate_live_window(raw, window)
    raw["requirements"][0]["reference_keywords"] = []
    assert validate_live_window(raw, window)["requirements"][0]["reference_keywords"] == []


def test_empty_candidates_and_empty_units_are_accounted_without_semantic_acceptance(tmp_path):
    manifest, parsed = fixture(tmp_path)
    source = block(parsed, "B1", "应说明巡检方法。")
    block(parsed, "B2", "本段是背景说明。", page=2)
    parsed["documents"][0]["units"].append({"unit_id": "EMPTY", "source_id": "T", "locator": "pdf/page[3]",
        "page": 3, "blocks": [], "coverage": {"status": "empty"}})
    calls = []
    def call(ctx, task, unit, payload, validate):
        calls.append(unit)
        raw = live_candidate(payload, selected(source))
        if payload["window"]["blocks"][0]["block_id"] != "B1":
            raw["requirements"] = []
        return validate(raw)
    output = extract_requirements(Context(), parsed, manifest, call, {"window_max_blocks": 1})
    assert len(calls) == 2 and len(output["window_results"]) == 3
    assert output["coverage"]["semantic_review"] == "requires_source_review"
    no_requirements = [r for r in output["window_results"] if not r["validated"]["requirements"]]
    assert len(no_requirements) == 2
    empty = next(r for r in output["window_results"] if r["unit_ids"] == ["EMPTY"])
    assert empty["live_confirmation"] is None and empty["raw_model_candidate"] is None
    assert empty["processing"]["processing_declaration"] == "no_model_call_empty_window"
    assert empty["processing"]["source_sha256"] == manifest["sources"][0]["sha256"]
    assert empty["processing"]["semantic_review"] == "requires_source_review"
    assert "no model call" in empty["raw_model_candidate_location"]


@pytest.mark.parametrize("case", ["status", "locator", "source_hash", "counts", "semantic_claim", "declaration", "raw_quote", "normalized_quote", "raw_hash", "unit_ids", "candidate_origin", "format_scope", "coverage_semantic"])
def test_n3_rebuilds_processing_and_revalidates_raw_wire_and_normalized_citation(tmp_path, case):
    manifest, parsed = fixture(tmp_path)
    source = block(parsed, "B1", "未取得书面许可时不得操作；例外条件须完整保留。")
    ctx = Context()
    output = extract_requirements(ctx, parsed, manifest,
        lambda ctx, task, unit, payload, validate: validate(live_candidate(payload, selected(source))), {})
    result = output["window_results"][0]
    if case == "status": result["processing"]["blocks"][0]["status"] = "safe_footer"
    elif case == "locator": result["processing"]["blocks"][0]["locator"] = "pdf/page[99]"
    elif case == "source_hash": result["processing"]["source_sha256"] = "0" * 64
    elif case == "counts": result["processing"]["counts"]["supplied"] = 0
    elif case == "semantic_claim": result["processing"]["semantic_review"] = "accepted"
    elif case == "declaration": result["live_confirmation"]["processing_declaration"] = "semantically_accepted"
    elif case == "raw_quote": result["raw_model_candidate"]["requirements"][0]["citation"]["quote"] = "不得操作"
    elif case == "normalized_quote": result["candidate"]["requirements"][0]["citation"]["quote"] = "不得操作"
    elif case == "raw_hash": result["live_confirmation"]["raw_structured_candidate_sha256"] = "0" * 64
    elif case == "unit_ids": result["unit_ids"] = ["UNRELATED"]
    elif case == "candidate_origin": result["candidate_origin"] = "raw_model_candidate"
    elif case == "format_scope": result["format_status_scope"] = "entire_tender"
    elif case == "coverage_semantic": output["coverage"]["semantic_review"] = "accepted"
    def no_call(*args):
        pytest.fail("N3 must reject the mutated N2 input before mapping or writing")
    with pytest.raises(PlanningError):
        plan_bid(Context(), output, parsed, manifest, no_call, {})


def test_n3_same_frozen_window_revalidation_preserves_every_raw_and_normalized_character(tmp_path):
    manifest, parsed = fixture(tmp_path)
    source = block(parsed, "B1", "未取得许可不得开展操作，例外必须原样保留。")
    output = extract_requirements(Context(), parsed, manifest,
        lambda ctx, task, unit, payload, validate: validate(live_candidate(payload, selected(source))), {})
    result = output["window_results"][0]
    saved = copy.deepcopy(result)
    assert _validate_normalized_window_result(result, window_for(parsed, manifest)) == result["validated"]
    assert result == saved


def test_actual_recorded_gateway_is_exact_request_bound_and_never_claims_http_response(tmp_path):
    from company_bid.storage import digest
    from company_bid.structured import JsonTaskGateway
    manifest, parsed = fixture(tmp_path)
    source = block(parsed, "B1", "仅在取得许可后方可操作，不得省略条件。")
    ctx, http_calls, requests = Context(), [], []
    def no_http(*args):
        http_calls.append(args)
        pytest.fail("Recorded gateway must perform zero HTTP calls")
    gateway = JsonTaskGateway({"mode": "recorded"}, transport=no_http)
    def call(ctx, task_type, unit_id, payload, validate):
        request = {**payload, "task_type": task_type}
        requests.append(copy.deepcopy(request))
        gateway.recorded[f"{task_type}:{unit_id}"] = {
            "request_hash": digest(request), "response": live_candidate(payload, selected(source))}
        return gateway(ctx, task_type, unit_id, payload, validate)
    output = extract_requirements(ctx, parsed, manifest, call, {})
    result = output["window_results"][0]
    assert http_calls == [] and len(requests) == 1
    assert ctx.captures["recorded-candidate"]["external_call"] is False
    assert ctx.captures["recorded-candidate"]["request_hash"] == digest(requests[0])
    assert not any(name.startswith("response-") for name in ctx.captures)
    assert result["raw_model_candidate_location"] == CALLER_CANDIDATE_LOCATION
    assert "provider response" not in result["raw_model_candidate_location"]
    assert _validate_normalized_window_result(result, window_for(parsed, manifest)) == result["validated"]
