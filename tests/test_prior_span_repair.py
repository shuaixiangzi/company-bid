"""A frozen prior source span can bound one new review of a target-only tail.

The prior unit is produced by the real Executor and extraction validator using
an explicitly offline fixture. Only the current unit uses synthetic HTTP. A
prior success supplies source membership, never a business answer or semantic
completion certificate.
"""
import copy
import json

import pytest

from company_bid.diagnostics import inspect_diagnostics
from company_bid.extraction import (
    _index, _windows, run_extraction_window, validate_live_window,
)
from company_bid.runtime import Executor, NodeSpec, call_budget_summary
from company_bid.storage import digest
from company_bid.structured import JsonTaskGateway
from test_source_selected_citations import fixture, live_candidate
from test_structured_validation_repair import artifacts, response
from test_targeted_validation_repair import native_line


SPAN = ["TITLE", "CONDITION", "PROOF", "DATE_BASIS", "TAIL"]
CONFIG = {"window_max_blocks": 4, "window_max_chars": 14000}


def source_case(tmp_path):
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"
    for key, text in [
        ("TITLE", "3 业绩要求"),
        ("CONDITION", "2023年1月1日至响应截止日，应至少有1项合成检测业绩。"),
        ("PROOF", "须提供有效业绩证明：合同首页、服务范围页和签署页，"),
        ("DATE_BASIS", "或通知书扫描件；时间以合同签订时间或通知书"),
        ("TAIL", "签发时间为准。"),
        ("FEE", "服务费用上限为50万元，不得另行加价，但书面许可的除外。"),
        ("CERT", "须提供有效检测资质证书。"),
        ("BACKGROUND", "本段是其它原始背景说明。"),
        ("LATER", "后续原始事项待另行说明。"),
        ("AFTER", "更后的原始事项尚未核对。"),
    ]:
        native_line(parsed, key, text)
    blocks, _, documents = _index(parsed, manifest)
    windows, _ = _windows(documents, blocks, CONFIG)
    prior, current = windows[:2]
    assert [block["block_id"] for block in prior["blocks"]] == SPAN[:-1]
    assert current["blocks"][0]["block_id"] == "TAIL"
    assert set(SPAN) <= {block["block_id"] for block in current["blocks"] + current["adjacent_context"]}
    prior_raw = live_candidate({"window": prior}, {"block_ids": SPAN})
    prior_raw["requirements"][0].update(
        category="qualification", response_mode="attachment", attachment_keywords=["业绩证明"])
    first = live_candidate({"window": current}, {"block_ids": ["FEE"]})
    # Twelve independent, same-source observations must not be merged or padded.
    first["requirements"] = [
        {"citation": {"block_ids": ["FEE"]}, "category": "commercial", "response_mode": "procedure",
         "format_status": "not_prescribed", "mandatory": bool(index % 2),
         "confidence": "high" if index % 2 else "medium"}
        for index in range(12)
    ]
    first["unresolved_block_ids"] = ["TAIL"]
    second = copy.deepcopy(first)
    second["unresolved_block_ids"] = []
    second["requirements"].append({"citation": {"block_ids": SPAN}, "category": "qualification",
        "response_mode": "attachment", "format_status": "not_prescribed"})
    return prior, current, prior_raw, first, second


def replace_checkpoint(ctx, record, mutate):
    """Make a new corrupt synthetic artifact; never overwrite an old artifact."""
    envelope = ctx.executor.store.read_json(record["output_ref"])
    mutate(envelope)
    envelope["output_hash"] = digest(envelope["data"])
    record["output_hash"] = envelope["output_hash"]
    record["output_ref"] = ctx.executor.store.put_json(ctx.run_id, "checkpoint", envelope)


def replace_input(ctx, record, mutate):
    source_input = ctx.executor.store.read_json(record["input_ref"])
    mutate(source_input)
    record["input_ref"] = ctx.executor.store.put_json(ctx.run_id, "unit-input", source_input)
    record["input_hash"] = digest(source_input)
    replace_checkpoint(ctx, record, lambda value: value.update(input_hash=record["input_hash"]))


def damage_prior(ctx, record, damage):
    if damage == "checkpoint_hash":
        record["output_ref"]["sha256"] = "f" * 64
    elif damage == "input_hash":
        record["input_ref"]["sha256"] = "f" * 64
    elif damage == "version":
        record["version"] = "another-frozen-node-version"
    elif damage == "not_succeeded":
        record["status"] = "failed"
    elif damage in {"checkpoint_run", "checkpoint_node", "checkpoint_unit"}:
        field = {"checkpoint_run": "run_id", "checkpoint_node": "node_key", "checkpoint_unit": "unit_id"}[damage]
        replace_checkpoint(ctx, record, lambda value: value.update({field: "UNRELATED"}))
    elif damage == "source_hash":
        def mutate(value):
            value["source_sha256"] = value["window"]["source_sha256"] = "b" * 64
        replace_input(ctx, record, mutate)
    elif damage in {"noncontiguous", "cross_page", "ocr", "disposition", "blank_text", "source_id", "source_format"}:
        def mutate(value):
            source = next(block for block in value["window"]["blocks"] if block["block_id"] == "CONDITION")
            if damage == "noncontiguous":
                source["ordinal"] += 100
            elif damage == "cross_page":
                source["page"] = 2
                source["locator"] = "pdf/page[2]/line[1]"
            elif damage == "ocr":
                source["origin"] = "ocr"
            elif damage == "disposition":
                source["read_disposition"] = {"status": "ignored_by_frozen_reader_rule", "reason": "test"}
            elif damage == "blank_text":
                source["text"] = " "
            elif damage == "source_id":
                source["source_id"] = "UNRELATED"
            else:
                value["window"]["source_format"] = "docx"
        replace_input(ctx, record, mutate)
    elif damage == "wrong_quote":
        def mutate(value):
            value["data"]["candidate"]["requirements"][0]["citation"]["quote"] = "FALSE SOURCE QUOTE"
        replace_checkpoint(ctx, record, mutate)
    elif damage == "raw_selection_mismatch":
        def mutate(value):
            value["data"]["raw_model_candidate"]["requirements"][0]["citation"]["block_ids"] = SPAN[1:]
        replace_checkpoint(ctx, record, mutate)
    elif damage == "validated_mismatch":
        def mutate(value):
            value["data"]["validated"]["requirements"][0]["citation"]["quote"] = "FALSE VALIDATED QUOTE"
        replace_checkpoint(ctx, record, mutate)
    elif damage == "missing_trace":
        record["attempts"][-1].pop("trace_id", None)
    else:
        assert damage is None


def run_case(tmp_path, monkeypatch, *, correction=None, damage=None, no_prior=False,
             prior_mode="attachment", remaining_adjacent=False, unrelated_adjacent=False,
             fail_repair_capture=False):
    prior, current, prior_raw, first, second = source_case(tmp_path)
    if prior_mode == "factual_response":
        prior_raw["requirements"][0].update(response_mode="factual_response", attachment_keywords=[],
                                           required_fact_labels=["业绩证明"])
    if remaining_adjacent:
        first["unresolved_block_ids"] = ["CONDITION", "TAIL", "TITLE"]
        # Only the original-unresolved intersection with this full group earns removal.
        second["unresolved_block_ids"] = []
    if unrelated_adjacent:
        assert {"LATER", "AFTER"} <= {block["block_id"] for block in current["adjacent_context"]}
        first["unresolved_block_ids"] = ["AFTER", "TAIL", "LATER"]
        second["unresolved_block_ids"] = ["AFTER", "LATER"]
    if correction:
        correction(second, first, current)
    originals = copy.deepcopy((prior_raw, first, second))
    monkeypatch.setenv("TEST_PRIOR_SPAN_KEY", "synthetic-prior-span-key")
    options = {"mode": "chat_completions", "endpoint": "https://example.invalid/chat", "model": "synthetic",
        "auth_env": "TEST_PRIOR_SPAN_KEY", "max_attempts": 1, "validation_repair_attempts": 1,
        "max_output_tokens": 1000, "max_input_chars": 65000, "max_run_calls": 2, "max_run_usd": .1,
        "input_usd_per_million": 1, "output_usd_per_million": 1}
    sent, snapshots = [], []

    def transport(endpoint, payload, secret, timeout):
        sent.append(copy.deepcopy(payload))
        return response(first if len(sent) == 1 else second)

    gateway = JsonTaskGateway(options, transport=transport)

    def offline_prior(ctx, task_type, unit_id, payload, validate):
        ctx.capture("synthetic-offline-prior-fixture", {"scope": "test_source_fixture", "actual_http_calls": 0})
        return validate(copy.deepcopy(prior_raw))

    def handler(ctx, value):
        if not no_prior:
            run_extraction_window(ctx, prior, CONFIG, offline_prior)
            record = ctx._record["units"][prior["window_id"]]
            assert record["status"] == "succeeded" and record["attempts"][-1]["trace_id"]
            snapshots.append(copy.deepcopy(record))
            damage_prior(ctx, record, damage)
        result = run_extraction_window(ctx, current, CONFIG, gateway)
        return {"scope": "two_synthetic_windows_not_complete_N2", "window_result": result}

    specs = [NodeSpec("N2", "synthetic-prior-span-v1", handler)]
    config = {"structured_provider": options, "debug_capture": True}
    executor = Executor(tmp_path / "runtime", specs, config)
    rid = executor.create_run({"synthetic": True})
    if fail_repair_capture:
        put_json = executor.store.put_json

        def fail_capture(run_id, kind, value):
            if kind == "capture" and value.get("status") == "scheduled" and "first_call_id" in value:
                raise OSError("Synthetic repair evidence capture failed")
            return put_json(run_id, kind, value)

        monkeypatch.setattr(executor.store, "put_json", fail_capture)
    state = executor.execute(rid)
    assert originals == (prior_raw, first, second)
    captured = artifacts(executor, state)
    responses = [json.loads(value["choices"][0]["message"]["content"])
        for record, value in captured if record["name"].startswith("response-")]
    assert responses == [first, second][:len(sent)]
    feedback = json.loads(sent[1]["messages"][-1]["content"])["validation_feedback"] if len(sent) == 2 else None
    return {"executor": executor, "rid": rid, "specs": specs, "config": config, "state": state,
        "sent": sent, "captured": captured, "feedback": feedback, "prior": prior, "current": current,
        "first": first, "second": second, "snapshots": snapshots, "runtime": tmp_path / "runtime"}


def test_target_only_tail_earns_one_correction_from_a_frozen_prior_complete_source_span(tmp_path, monkeypatch):
    case = run_case(tmp_path, monkeypatch)
    state = case["state"]
    assert state["status"] == "succeeded" and len(case["sent"]) == 2
    final = case["executor"].output(case["rid"])["window_result"]
    assert final["raw_model_candidate"]["requirements"][:12] == case["first"]["requirements"]
    source = {block["block_id"]: block for block in case["current"]["blocks"] + case["current"]["adjacent_context"]}
    quote = final["validated"]["requirements"][-1]["citation"]["quote"]
    assert quote == "\n".join(source[key]["text"] for key in SPAN)
    assert "2023年1月1日" in quote and "至少有1项" in quote and "或通知书" in quote
    assert final["raw_model_candidate"]["unresolved_block_ids"] == []
    assert case["second"]["requirements"][:-1] == case["first"]["requirements"]
    assert state["nodes"][0]["units"][case["prior"]["window_id"]] == case["snapshots"][0]
    feedback = case["feedback"]
    assert feedback["code"] == "LIVE_REQUIRED_BLOCK_UNRESOLVED"
    group = feedback["required_coverage"]["resolution_groups"][0]
    assert group["block_ids"] == SPAN and group["required_target_ids"] == ["TAIL"]
    assert case["first"]["unresolved_block_ids"] == ["TAIL"]
    budget = call_budget_summary(state["call_budget"])
    assert budget["reservation_count"] == budget["known_usage_estimate_count"] == 2
    report = inspect_diagnostics(case["runtime"], case["rid"])
    assert report["actual_call_attempts"] == 2 and not report["gaps"]
    relation = next(value for record, value in case["captured"] if record["name"].startswith("validation-repair-result-"))
    assert relation["status"] == "completed" and relation["first_call_id"] != relation["second_call_id"]
    restarted = Executor(case["runtime"], case["specs"], case["config"])
    assert restarted.resume(case["rid"])["status"] == "succeeded" and len(case["sent"]) == 2


@pytest.mark.parametrize("damage", ["checkpoint_hash", "input_hash", "version", "not_succeeded",
    "checkpoint_run", "checkpoint_node", "checkpoint_unit", "source_hash", "wrong_quote",
    "raw_selection_mismatch", "validated_mismatch", "missing_trace", "noncontiguous", "cross_page",
    "ocr", "disposition", "blank_text", "source_id", "source_format"])
def test_an_invalid_prior_witness_cannot_buy_a_target_only_correction(tmp_path, monkeypatch, damage):
    case = run_case(tmp_path, monkeypatch, damage=damage)
    assert case["state"]["status"] == "failed"
    assert len(case["sent"]) == 1 and case["feedback"] is None
    assert call_budget_summary(case["state"]["call_budget"])["reservation_count"] == 1


def test_target_only_unresolved_without_a_prior_witness_stays_failed_without_extra_http(tmp_path, monkeypatch):
    case = run_case(tmp_path, monkeypatch, no_prior=True)
    assert case["state"]["error_code"] == "LIVE_REQUIRED_BLOCK_UNRESOLVED"
    assert len(case["sent"]) == 1


@pytest.mark.parametrize("selection", [["TAIL"], ["DATE_BASIS", "TAIL"], SPAN[1:], SPAN[:-1],
    ["TITLE", "CONDITION", "DATE_BASIS", "TAIL"], list(reversed(SPAN))],
    ids=["tail-only", "wrap-only", "missing-heading", "missing-target", "missing-proof", "wrong-order"])
def test_a_correction_must_keep_every_member_of_the_prior_source_span_in_source_order(tmp_path, monkeypatch, selection):
    def correction(second, first, current):
        second["requirements"][-1]["citation"]["block_ids"] = selection
    case = run_case(tmp_path, monkeypatch, correction=correction)
    assert case["state"]["status"] == "failed" and len(case["sent"]) == 2
    assert case["state"]["error_code"] in {
        "VALIDATION_REPAIR_COVERAGE_LOST", "CITATION_SPAN_NOT_CONTIGUOUS", "CITATION_SPAN_ORDER_INVALID"}


@pytest.mark.parametrize("change", ["clear-only", "old-mode", "old-default", "old-source", "old-delete", "old-reorder", "insert"])
def test_a_prior_source_witness_does_not_allow_old_inventory_or_business_rewrites(tmp_path, monkeypatch, change):
    def correction(second, first, current):
        if change == "clear-only":
            second["requirements"].pop()
        elif change == "old-mode":
            second["requirements"][0]["response_mode"] = "factual_response"
        elif change == "old-default":
            second["requirements"][0]["mandatory"] = not first["requirements"][0]["mandatory"]
        elif change == "old-source":
            second["requirements"][0]["citation"]["block_ids"] = ["CERT"]
        elif change == "old-delete":
            second["requirements"].pop(0)
        elif change == "old-reorder":
            second["requirements"][:2] = reversed(second["requirements"][:2])
        else:
            second["requirements"].insert(0, second["requirements"].pop())
    case = run_case(tmp_path, monkeypatch, correction=correction)
    assert case["state"]["error_code"] == "VALIDATION_REPAIR_COVERAGE_LOST"
    assert len(case["sent"]) == 2


def test_prior_source_membership_does_not_require_copying_the_prior_business_answer(tmp_path, monkeypatch):
    case = run_case(tmp_path, monkeypatch, prior_mode="factual_response")
    assert case["state"]["status"] == "succeeded" and len(case["sent"]) == 2
    # Same exact source interval, independently classified by the current model.
    assert case["second"]["requirements"][-1]["response_mode"] == "attachment"
    final = validate_live_window(case["second"], case["current"])
    assert final["requirements"][-1]["citation"]["block_ids"] == SPAN
    group = case["feedback"]["required_coverage"]["resolution_groups"][0]
    assert not {"category", "response_mode", "required_fact_labels", "attachment_keywords"} & set(group)


def test_original_unresolved_members_in_the_guard_group_need_no_new_unresolved_ids(tmp_path, monkeypatch):
    case = run_case(tmp_path, monkeypatch, remaining_adjacent=True)
    assert case["state"]["status"] == "succeeded" and len(case["sent"]) == 2
    assert case["first"]["unresolved_block_ids"] == ["CONDITION", "TAIL", "TITLE"]
    assert case["second"]["unresolved_block_ids"] == []


def test_unrelated_adjacent_unresolved_members_keep_their_original_order(tmp_path, monkeypatch):
    case = run_case(tmp_path, monkeypatch, unrelated_adjacent=True)
    assert case["state"]["status"] == "succeeded" and len(case["sent"]) == 2
    assert case["first"]["unresolved_block_ids"] == ["AFTER", "TAIL", "LATER"]
    assert case["second"]["unresolved_block_ids"] == ["AFTER", "LATER"]


def test_failure_to_capture_the_prior_guarded_repair_stops_before_the_second_http(tmp_path, monkeypatch):
    case = run_case(tmp_path, monkeypatch, fail_repair_capture=True)
    assert case["state"]["error_code"] == "DIAGNOSTICS_CAPTURE_FAILED"
    assert len(case["sent"]) == 1
    assert call_budget_summary(case["state"]["call_budget"])["known_usage_estimate_count"] == 1


def test_failed_correction_cannot_buy_a_third_response_after_an_executor_restart(tmp_path, monkeypatch):
    def correction(second, first, current):
        second["requirements"][-1]["citation"]["block_ids"] = ["TAIL"]
    case = run_case(tmp_path, monkeypatch, correction=correction)
    assert case["state"]["status"] == "failed" and len(case["sent"]) == 2
    before = copy.deepcopy(case["state"]["call_budget"])
    restarted = Executor(case["runtime"], case["specs"], case["config"])
    resumed = restarted.resume(case["rid"])
    assert resumed["error_code"] == "VALIDATION_REPAIR_ALREADY_ATTEMPTED"
    assert len(case["sent"]) == 2 and resumed["call_budget"] == before
