"""Independent v11 inventory, authenticated-owner and historical-wire checks."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from company_bid.diagnostic_replay import DiagnosticReplay
from company_bid.extraction import _index, _windows, extraction_window_payload, validate_live_window
from company_bid.provider import ChatConfig, ProviderFailure, build_json_request
from company_bid.storage import atomic_json, digest
from company_bid.structured import (
    PROMPT_VERSION, SYSTEM_PROMPT, _covers_rejected_candidate, _repair_coverage, _validation_repair_request,
)
from test_adjacent_owner_sources import source_case
from test_diagnostic_replay import make_source_run, replace_capture, snapshot, target_run


def wire_case():
    provider = ChatConfig(endpoint="https://example.invalid/chat", model="synthetic", auth_env="UNUSED_SYNTHETIC_KEY")
    replay = DiagnosticReplay({}, provider)
    historical = {"window_config": {"window_max_blocks": 3, "window_max_chars": 14000},
                  "window": {"items": [1, 2]}, "task_type": "extract_requirements"}
    current = {"task_type": "extract_requirements", "window": {"items": [1, 2]},
               "window_config": {"window_max_chars": 14000, "window_max_blocks": 3}}
    call = {"task": historical, "request": build_json_request(provider, historical, SYSTEM_PROMPT),
            "terminal": {"prompt_version": PROMPT_VERSION, "model": provider.model}}
    return replay, SimpleNamespace(config=provider), call, current


def test_historical_wire_keeps_authenticated_json_order_without_changing_current_task():
    replay, run, call, task = wire_case()
    before = copy.deepcopy((call, task))
    assert call["task"] == task
    assert call["request"] != build_json_request(replay.provider_config, task, SYSTEM_PROMPT)
    replay._wire(run, call, task)
    assert (call, task) == before


@pytest.mark.parametrize("change", ["different_value", "extra_field", "missing_field", "list_order", "bool_for_int",
    "float_for_int", "unpaired_task", "system", "model", "tokens", "temperature", "response_format", "prompt_version",
    "terminal_model", "endpoint", "service"])
def test_historical_wire_still_rejects_every_value_or_call_parameter_change(change):
    replay, run, call, task = wire_case()
    if change == "different_value": task["window_config"]["window_max_blocks"] = 4
    elif change == "extra_field": task["extra"] = True
    elif change == "missing_field": task.pop("task_type")
    elif change == "list_order": task["window"]["items"].reverse()
    elif change == "bool_for_int": task["window"]["items"][0] = True
    elif change == "float_for_int": task["window"]["items"][0] = 1.0
    elif change == "unpaired_task":
        call["task"]["window_config"]["window_max_blocks"] = 4
        task["window_config"]["window_max_blocks"] = 4
    elif change == "system": call["request"]["messages"][0]["content"] += " changed"
    elif change == "model": call["request"]["model"] = "other-model"
    elif change == "tokens": call["request"]["max_tokens"] += 1
    elif change == "temperature": call["request"]["temperature"] += .1
    elif change == "response_format": call["request"]["response_format"] = {"type": "text"}
    elif change == "prompt_version": call["terminal"]["prompt_version"] = "other"
    elif change == "terminal_model": call["terminal"]["model"] = "other"
    elif change == "endpoint": run.config = ChatConfig(endpoint="https://another.invalid/chat", model="synthetic")
    elif change == "service": run.config = SimpleNamespace(endpoint=run.config.endpoint, service="other")
    with pytest.raises(ProviderFailure, match="DIAGNOSTIC_REPLAY_(REQUEST|PROVIDER)_MISMATCH"):
        replay._wire(run, call, task)


def repair_case(tmp_path, *, with_source=False):
    candidate, request, parsed, manifest, witness = source_case(tmp_path)
    unit = parsed["documents"][0]["units"][0]
    rows = unit["blocks"]
    prefix = [{**copy.deepcopy(rows[0]), "block_id": "CH", "text": "第六章 合同条款"},
              {**copy.deepcopy(rows[0]), "block_id": "ARTICLE", "text": "第三条 各方权利义务"}]
    rows[:0] = prefix
    rows[2]["text"] = "3.1 本条原义务保留。"
    rows[3]["text"] = "3.2 本条另有独立义务。"
    rows[4]["text"] = "3.3 其他："
    rows.insert(5, {**copy.deepcopy(rows[0]), "block_id": "A4", "text": "第四条 费用"})
    for index, row in enumerate(rows):
        row["locator"] = f"pdf/page[1]/line[{index}]"
        row["bbox"] = [90, 80 + index * 20, 500, 93 + index * 20]
    config = {"window_max_chars": 14000, "window_max_blocks": 5}
    blocks, _, documents = _index(parsed, manifest)
    windows, _ = _windows(documents, blocks, config)
    current, owner = windows[:2]
    request = {**extraction_window_payload(current, config), "task_type": "extract_requirements"}
    candidate["window_id"] = current["window_id"]
    witness["full_window"] = owner
    witness["binding"]["unit_id"] = witness["entry"]["origin"]["unit_id"] = owner["window_id"]
    witness["entry"]["candidate"]["window_id"] = owner["window_id"]
    owner_task = {**extraction_window_payload(owner, config), "task_type": "extract_requirements"}
    witness["entry"]["request_sha256"] = witness["entry"]["origin"]["base_request_sha256"] = digest(owner_task)
    witness["entry"]["origin"]["candidate_sha256"] = digest(witness["entry"]["candidate"])
    coverage = _repair_coverage(candidate, request, error_code="LIVE_REQUIRED_BLOCK_UNRESOLVED",
        source_data={"parsed": parsed, "manifest": manifest}, owner_witness=witness)
    assert coverage and coverage["counts"]["requirements"] == 2
    corrected = copy.deepcopy(candidate)
    corrected["requirements"].pop(coverage["owned_adjacent_requirement"]["candidate_index"])
    for group in coverage["resolution_groups"]:
        corrected["requirements"].append({"citation": {"block_ids": group["block_ids"]}, **group["required_response_fields"]})
    resolved = {key for group in coverage["resolution_groups"] for key in group["block_ids"]}
    corrected["unresolved_block_ids"] = [key for key in corrected["unresolved_block_ids"] if key not in resolved]
    repair = _validation_repair_request(request, "LIVE_REQUIRED_BLOCK_UNRESOLVED", {}, coverage, "a" * 32)
    result = candidate, corrected, coverage, repair, current
    if with_source:
        return (*result, {"parsed": parsed, "manifest": manifest}, witness, request)
    return result


def test_v11_removes_only_owner_duplicate_and_appends_pending_with_full_original_inventory(tmp_path):
    original, corrected, coverage, request, window = repair_case(tmp_path)
    before = copy.deepcopy((original, corrected, coverage, request, window))
    assert len(original["requirements"]) == len(corrected["requirements"]) == 2
    assert coverage["counts"]["requirements"] == 2
    assert len(coverage["entries"]["requirements"]) == 2
    assert coverage["owned_adjacent_requirement"]["candidate_index"] == 1
    assert len(coverage["resolution_groups"]) == 1
    assert _covers_rejected_candidate(corrected, coverage, request)
    result = validate_live_window(copy.deepcopy(corrected), window)
    assert result["requirements"][0]["citation"]["block_ids"] == ["B0"]
    assert result["requirements"][1]["response_mode"] == "factual_response"
    assert (original, corrected, coverage, request, window) == before


@pytest.mark.parametrize("change", ["drop_retained", "change_business", "change_anchor", "swap", "retain_duplicate",
    "omit_pending", "pending_as_procedure", "pending_label", "original_index", "original_item", "proof_hash", "counts",
    "uncleared_unresolved", "clear_unearned", "extra_item"])
def test_v11_comparison_view_does_not_allow_other_deletions_or_source_loss(tmp_path, change):
    original, corrected, coverage, request, window = repair_case(tmp_path)
    if change == "drop_retained": corrected["requirements"].pop(0)
    elif change == "change_business": corrected["requirements"][0]["mandatory"] = False
    elif change == "change_anchor": corrected["requirements"][0]["citation"]["block_ids"] = ["B1"]
    elif change == "swap": corrected["requirements"].reverse()
    elif change == "retain_duplicate": corrected["requirements"][0] = copy.deepcopy(original["requirements"][1])
    elif change == "omit_pending": corrected["requirements"].pop()
    elif change == "pending_as_procedure": corrected["requirements"][-1]["response_mode"] = "procedure"
    elif change == "pending_label": corrected["requirements"][-1]["required_fact_labels"] = ["发票"]
    elif change == "original_index": coverage["owned_adjacent_requirement"]["candidate_index"] = 0
    elif change == "original_item": coverage["owned_adjacent_requirement"]["original_typed_item"]["mandatory"] = False
    elif change == "proof_hash": coverage["owned_adjacent_requirement"]["proof_sha256"] = "0" * 64
    elif change == "counts": coverage["counts"]["requirements"] = 1
    elif change == "uncleared_unresolved": corrected["unresolved_block_ids"] = original["unresolved_block_ids"]
    elif change == "clear_unearned":
        coverage["original_unresolved_block_ids"].append("B0")
        corrected["unresolved_block_ids"] = []
    elif change == "extra_item": corrected["requirements"].append(copy.deepcopy(corrected["requirements"][-1]))
    assert not _covers_rejected_candidate(corrected, coverage, request)


def authentic_replay(source, *, current=None):
    root = source["executor"].store.root.resolve()
    provider = ChatConfig(**{key: value for key, value in source["options"].items() if key not in {"mode", "validation_repair_attempts"}})
    replay = DiagnosticReplay({}, provider)
    replay.roots = {"history": root}
    replay._target = (root, "0" * 32)
    replay.current = copy.deepcopy(current or source["value"])
    config = {"window_max_chars": 14000, "window_max_blocks": 60}
    blocks, _, documents = _index(replay.current["parsed"], replay.current["manifest"])
    windows, _ = _windows(documents, blocks, config)
    replay.windows = {window["window_id"]: window for window in windows}
    replay.window_config = config
    binding = {"alias": "history", "run_id": source["run"], "node_key": "N2", "unit_id": source["unit_id"]}
    return replay, binding


@pytest.fixture
def authentic_owner(tmp_path, monkeypatch):
    source = make_source_run(tmp_path, monkeypatch)
    replay, binding = authentic_replay(source)
    window = replay.windows[binding["unit_id"]]
    task = {**extraction_window_payload(window, replay.window_config), "task_type": "extract_requirements"}
    entry = replay._load_entry(binding, task, window)
    return source, {"owned_adjacent_requirement": {"binding": binding, "origin": entry["origin"]}}


def test_completed_owner_witness_reloads_actual_http_trace_n1_and_keeps_sources_unchanged(authentic_owner):
    source, coverage = authentic_owner
    before = snapshot(source["executor"].store.root)
    replay, _ = authentic_replay(source)
    witness = replay._completed_owner_witness(coverage)
    assert witness["entry"]["origin"] == coverage["owned_adjacent_requirement"]["origin"]
    assert witness["entry"]["origin"]["calls"]
    for run in replay._runs.values():
        run.unchanged()
    assert snapshot(source["executor"].store.root) == before


@pytest.mark.parametrize("change", ["running", "failed_unit", "response", "request", "trace", "n1_source", "current_n1",
    "fake_origin", "current_run"])
def test_completed_owner_cannot_be_forged_from_status_hash_or_review_metadata(authentic_owner, change):
    source, coverage = authentic_owner
    executor, run_id = source["executor"], source["run"]
    state = executor.inspect(run_id)
    current = None
    if change == "running": state["status"] = "running"
    elif change == "failed_unit": state["nodes"][2]["units"][source["unit_id"]]["status"] = "failed"
    elif change == "response": replace_capture(source, "response-", lambda value: value["choices"][0]["message"].update(content="{}"))
    elif change == "request": replace_capture(source, "request-", lambda value: value.update(temperature=.9))
    elif change == "trace":
        path = executor.store.run_dir(run_id) / "spans.jsonl"
        rows = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
        span = next(row for row in rows if row["name"] == "provider.call")
        span["attributes"]["input_hash"] = "0" * 64
        path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    elif change == "n1_source":
        from pathlib import Path
        Path(source["value"]["manifest"]["sources"][0]["path"]).write_text("tampered source", encoding="utf-8")
    elif change == "current_n1":
        current = copy.deepcopy(source["value"])
        current["parsed"]["documents"][0]["units"][0]["blocks"][0]["text"] += " changed"
    elif change == "fake_origin": coverage["owned_adjacent_requirement"]["origin"]["calls"][0]["response_sha256"] = "0" * 64
    if change in {"running", "failed_unit"}: atomic_json(executor.store.run_dir(run_id) / "state.json", state)
    replay, _ = authentic_replay(source, current=current)
    if change == "current_run": replay._target = (executor.store.root.resolve(), run_id)
    with pytest.raises(ProviderFailure, match="DIAGNOSTIC_REPLAY_"):
        replay._completed_owner_witness(coverage)


def test_replay_only_checkpoint_cannot_become_an_actual_owner(authentic_owner):
    source, coverage = authentic_owner
    executor, run_id, state, calls, _ = target_run(source, include_prefix=True)
    assert state["status"] == "succeeded" and calls == []
    candidate_source = {**source, "executor": executor, "run": run_id, "value": executor.output(run_id, "N1")}
    replay, binding = authentic_replay(candidate_source)
    replay.roots["original"] = source["executor"].store.root.resolve()
    coverage["owned_adjacent_requirement"]["binding"] = binding
    with pytest.raises(ProviderFailure, match="DIAGNOSTIC_REPLAY_SOURCE_ACTUAL_CALL_REQUIRED"):
        replay._completed_owner_witness(coverage)


@pytest.mark.parametrize("actual_attempt,accepted", [(1, True), (True, False), (1.0, False)])
def test_correction_wire_compares_algorithm_expected_not_just_itself(tmp_path, monkeypatch, actual_attempt, accepted):
    import company_bid.structured as structured
    original, corrected, coverage, repair, _ = repair_case(tmp_path)
    base = {key: copy.deepcopy(value) for key, value in repair.items() if key != "validation_feedback"}
    base["task_instructions"] = base["task_instructions"].rsplit("\n\n", 1)[0]
    expected = _validation_repair_request(base, "LIVE_REQUIRED_BLOCK_UNRESOLVED", {}, coverage, "a" * 32)
    assert expected == repair
    actual = copy.deepcopy(expected)
    actual["validation_feedback"]["repair_attempt"] = actual_attempt
    replay, run, _, _ = wire_case()
    replay.current = {}
    monkeypatch.setattr(replay, "_n1", lambda run: ({}, None))
    monkeypatch.setattr(replay, "_witnesses", lambda *args: [])
    monkeypatch.setattr(replay, "_completed_owner_witness", lambda *args: None)
    monkeypatch.setattr(structured, "_repair_coverage", lambda *args, **kwargs: copy.deepcopy(coverage))
    first = {"candidate": original, "task": base, "validation": {"diagnostic_context": {}},
             "terminal": {"error_code": "LIVE_REQUIRED_BLOCK_UNRESOLVED"}, "call_id": "a" * 32, "call_attempt": 1}
    final = {"task": actual, "request": build_json_request(replay.provider_config, actual, SYSTEM_PROMPT),
             "terminal": {"prompt_version": PROMPT_VERSION, "model": replay.provider_config.model}, "candidate": corrected}
    if accepted:
        replay._correction(object(), first, run, final, coverage, {}, {}, "LIVE_REQUIRED_BLOCK_UNRESOLVED")
    else:
        assert actual == expected and digest(actual) != digest(expected)
        with pytest.raises(ProviderFailure, match="DIAGNOSTIC_REPLAY_REQUEST_MISMATCH"):
            replay._correction(object(), first, run, final, coverage, {}, {}, "LIVE_REQUIRED_BLOCK_UNRESOLVED")


def standalone_context_case(tmp_path, monkeypatch, *, actual=None, sent=None):
    original, corrected, coverage, _, window, source, witness, base = repair_case(tmp_path, with_source=True)
    actual = {"field": "unresolved_block_ids"} if actual is None else actual
    sent = {} if sent is None else sent
    repair = _validation_repair_request(base, "LIVE_REQUIRED_BLOCK_UNRESOLVED", sent, coverage, "a" * 32)
    replay, run, _, _ = wire_case()
    replay.provider_config = run.config = ChatConfig(endpoint="https://example.invalid/chat", model="synthetic",
        auth_env="UNUSED_SYNTHETIC_KEY", max_input_chars=100000)
    run.run_id = "2" * 32
    replay.current = source
    monkeypatch.setattr(replay, "_n1", lambda run: (source, None))
    monkeypatch.setattr(replay, "_witnesses", lambda *args: [])
    monkeypatch.setattr(replay, "_completed_owner_witness", lambda *args: witness)
    first = {"candidate": original, "task": base, "validation": {"diagnostic_context": actual},
             "terminal": {"error_code": "LIVE_REQUIRED_BLOCK_UNRESOLVED"}, "call_id": "a" * 32, "call_attempt": 1}
    final = {"task": repair, "request": build_json_request(replay.provider_config, repair, SYSTEM_PROMPT),
             "terminal": {"prompt_version": PROMPT_VERSION, "model": replay.provider_config.model}, "candidate": corrected}
    return replay, SimpleNamespace(run_id="1" * 32), first, run, final, coverage, window, sent


def test_standalone_v11_omitted_redundant_field_is_explicit_and_preserves_actual_wire(tmp_path, monkeypatch):
    args = standalone_context_case(tmp_path, monkeypatch)
    replay, source, first, final_run, final, coverage, window, sent = args
    before = copy.deepcopy((first, final, coverage, window))
    notes = replay._correction(source, first, final_run, final, coverage, window, sent,
        "LIVE_REQUIRED_BLOCK_UNRESOLVED", standalone_node_key="N2_WINDOW_DIAGNOSTIC")
    note, = notes
    assert note["kind"] == "historical_standalone_v11_redundant_error_field_omission"
    assert note["source_error_context"] == {"field": "unresolved_block_ids"}
    assert note["sent_feedback_context"] == final["task"]["validation_feedback"]["context"] == {}
    assert note["source_error_context_sha256"] == digest({"field": "unresolved_block_ids"})
    assert note["sent_feedback_context_sha256"] == digest({})
    assert note["actual_context_coverage_sha256"] == note["sent_context_coverage_sha256"] == digest(coverage)
    assert note["actual_wire_sha256"] == digest(final["request"])
    assert note["omitted_field_was_sent"] is note["historical_request_modified"] is note["historical_candidate_modified"] is False
    assert (first, final, coverage, window) == before
    validate_live_window(copy.deepcopy(final["candidate"]), window)


@pytest.mark.parametrize("change", ["automatic", "node", "same_run", "different_error", "different_field", "more_actual_fields",
    "more_sent_fields", "sent_list", "sent_wrong_type", "coverage_actual_diff", "coverage_actual_type", "coverage_current_diff",
    "ordinary_v10", "actual_wire_changed", "missing_requirement"])
def test_standalone_context_compatibility_has_no_general_error_or_coverage_waiver(tmp_path, monkeypatch, change):
    import company_bid.structured as structured
    args = standalone_context_case(tmp_path, monkeypatch)
    replay, source, first, final_run, final, coverage, window, context = args
    node_key, code = "N2_WINDOW_DIAGNOSTIC", "LIVE_REQUIRED_BLOCK_UNRESOLVED"
    if change == "automatic": node_key = None
    elif change == "node": node_key = "N2"
    elif change == "same_run": final_run.run_id = source.run_id
    elif change == "different_error": first["terminal"]["error_code"] = code = "FORMAT_TITLE_NOT_GROUNDED"
    elif change == "different_field": first["validation"]["diagnostic_context"] = {"field": "format_title"}
    elif change == "more_actual_fields": first["validation"]["diagnostic_context"]["candidate_index"] = 1
    elif change == "more_sent_fields": context = {"other": "value"}
    elif change == "sent_list": context = []
    elif change == "sent_wrong_type": context = {"field": True}
    elif change == "ordinary_v10": coverage.pop("owned_adjacent_requirement")
    elif change == "actual_wire_changed": final["request"]["temperature"] += .1
    elif change == "missing_requirement": final["candidate"]["requirements"].pop(0)
    elif change.startswith("coverage_"):
        original = structured._repair_coverage
        if change == "coverage_current_diff": replay.current = copy.deepcopy(replay.current)
        def unequal(*args, **kwargs):
            value = original(*args, **kwargs)
            if kwargs.get("diagnostic_context") == {"field": "unresolved_block_ids"}:
                if change != "coverage_current_diff" or kwargs["source_data"] is replay.current:
                    value["counts"]["requirements"] = 2.0 if change == "coverage_actual_type" else 3
            return value
        monkeypatch.setattr(structured, "_repair_coverage", unequal)
    with pytest.raises(ProviderFailure, match="DIAGNOSTIC_REPLAY_"):
        replay._correction(source, first, final_run, final, coverage, window, context, code, standalone_node_key=node_key)


def test_correct_future_standalone_context_needs_no_compatibility_note(tmp_path, monkeypatch):
    args = standalone_context_case(tmp_path, monkeypatch, sent={"field": "unresolved_block_ids"})
    replay, source, first, final_run, final, coverage, window, context = args
    assert replay._correction(source, first, final_run, final, coverage, window, context,
        "LIVE_REQUIRED_BLOCK_UNRESOLVED", standalone_node_key="N2_WINDOW_DIAGNOSTIC") == []
