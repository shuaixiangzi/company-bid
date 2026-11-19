"""Diagnostic candidate revalidation uses real local evidence, never remote HTTP."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from urllib.error import URLError

import pytest

from company_bid import extraction
from company_bid.diagnostics import inspect_diagnostics
from company_bid.extraction import PlanningError, extract_requirements, run_extraction_window, _index, _windows
from company_bid.provider import ChatConfig, ChatProposalProvider
from company_bid.runtime import Executor, NodeSpec
from company_bid.storage import atomic_json, digest
from company_bid.structured import (JsonTaskGateway, PROMPT_VERSION, SYSTEM_PROMPT,
                                    _repair_context, _repair_coverage, _validation_repair_request)
from test_source_selected_citations import block, fixture


SOURCE_CANARY = "SYNTHETIC-DIAGNOSTIC-SOURCE-仅在取得书面许可后方可检查，不得省略条件。"


def response(candidate):
    return {"id": "synthetic-replay-http", "choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps(candidate, ensure_ascii=False)}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12}}


def candidate(request):
    source = request["window"]["blocks"][0]
    return {"window_id": request["window"]["window_id"],
            "processing_declaration": "processed_not_semantically_accepted", "unresolved_block_ids": [],
            "requirements": [{"citation": {"block_ids": [source["block_id"]]},
                              "category": "technical", "response_mode": "writing",
                              "format_status": "not_prescribed"}],
            "format_chapters": [], "format_status": "none_verified"}


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def snapshot(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


def make_source_run(tmp_path, monkeypatch, *, repaired=False, failed_first=False, two_windows=False,
                    failed_cell_label=False):
    input_dir = tmp_path / "source-input"
    input_dir.mkdir()
    manifest, parsed = fixture(input_dir)
    parsed["documents"][0]["format"] = "pdf"
    source = block(parsed, "B1", SOURCE_CANARY, page=1, origin="native")
    source["locator"] = "pdf/page[1]/line[0]"
    source["page"] = 1
    if failed_cell_label:
        source.update(kind="table", locator="pdf/page[1]/table[0]", text="事项\t要求\n材料\t须提供合成材料\n证明。",
                      rows=[["事项", "要求"], ["材料", "须提供合成材料\n证明。"]],
                      merges=[], column_widths=[1, 3], header_rows=[0])
    if two_windows:
        item = block(parsed, "B2", "合成第二页应保留检查记录及异常报告。", page=2, origin="native")
        item["locator"], item["page"] = "pdf/page[2]/line[0]", 2
    if failed_cell_label:
        item = block(parsed, "B3", "合成第三页应保留验收记录。", page=3, origin="native")
        item["locator"], item["page"] = "pdf/page[3]/line[0]", 3
    if repaired:
        for index, (identity, text) in enumerate([
            ("TITLE", "5.2 开启程序"), ("LITERAL", "/"),
            ("NEXT", "5.3 递交响应文件的响应人不足的情形"),
        ], 1):
            item = block(parsed, identity, text, page=1, origin="native")
            item["locator"], item["page"] = f"pdf/page[1]/line[{index}]", 1
    original = Path(manifest["sources"][0]["path"])
    original.write_text("\n".join(item["text"] for unit in parsed["documents"][0]["units"] for item in unit["blocks"]),
                        encoding="utf-8")
    source_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    manifest["sources"][0]["sha256"] = source_hash
    parsed["documents"][0]["sha256"] = source_hash
    options = {"mode": "chat_completions", "endpoint": "https://example.invalid/chat", "model": "synthetic",
               "auth_env": "TEST_DIAGNOSTIC_REPLAY_KEY", "max_attempts": 1, "max_output_tokens": 512,
               "max_input_chars": 100000, "max_run_calls": 5, "max_run_usd": 1,
               "input_usd_per_million": .30, "output_usd_per_million": 1.20}
    if repaired and not failed_first:
        options["validation_repair_attempts"] = 1
    monkeypatch.setenv(options["auth_env"], "synthetic-not-a-real-credential")
    calls = []

    def transport(endpoint, payload, secret, timeout):
        calls.append(copy.deepcopy(payload))
        raw = candidate(json.loads(payload["messages"][-1]["content"]))
        if failed_cell_label:
            raw["requirements"][0].update(category="qualification", response_mode="attachment",
                                          attachment_keywords=["合成材料证明"])
        elif repaired and len(calls) == 1:
            raw["unresolved_block_ids"] = ["LITERAL"]
        elif repaired:
            raw["requirements"].append({"citation": {"block_ids": ["TITLE", "LITERAL"]},
                "category": "procedure", "response_mode": "procedure", "mandatory": False, "confidence": "low",
                "format_status": "not_prescribed", "format_title": None,
                "required_fact_labels": [], "attachment_keywords": [], "reference_keywords": []})
        return response(raw)

    gateway = JsonTaskGateway(options, transport=transport)
    extraction_config = {"window_max_blocks": 1} if two_windows else {}

    def extract(ctx, value):
        return {**value, "extracted": extract_requirements(ctx, value["parsed"], value["manifest"], gateway, extraction_config)}

    def freeze(ctx, value):
        value = copy.deepcopy(value)
        source = value["manifest"]["sources"][0]
        reference = ctx.store_bytes("synthetic-source.txt", Path(source["path"]).read_bytes())
        source["path"] = str(ctx.executor.store.root / reference["path"])
        value["source_artifacts"] = [reference]
        return value

    executor = Executor(tmp_path / "source-runtime", [
        NodeSpec("N0", "synthetic-freeze-v1", freeze, ()),
        NodeSpec("N1", "synthetic-reader-v1", lambda ctx, value: value, ()),
        NodeSpec("N2", "synthetic-source-validator-v1", extract, ("structured_provider", "extraction")),
    ], {"structured_provider": options, "extraction": extraction_config, "debug_capture": True})
    value = {"manifest": manifest, "parsed": parsed, "source_artifacts": [], "input_kind": "automatic"}
    run = executor.create_run(value)
    with monkeypatch.context() as historical:
        if failed_cell_label:
            def reject_unjoined_label(requirement, available, window):
                raise PlanningError("EXTRACTION_LABEL_NOT_GROUNDED", {"field": "attachment_keywords", "label_index": 0})

            historical.setattr(extraction, "_label_grounding", reject_unjoined_label)
        state = executor.execute(run)
    assert state["status"] == ("failed" if failed_first else "succeeded"), state.get("error_code")
    if failed_first:
        assert state["error_code"] == ("EXTRACTION_LABEL_NOT_GROUNDED" if failed_cell_label else "LIVE_REQUIRED_BLOCK_UNRESOLVED")
    unit_ids = list(state["nodes"][2]["units"])
    unit_id = unit_ids[0]
    assert len(unit_ids) == (2 if two_windows and not failed_first else 1)
    expected_calls = 2 if (two_windows or repaired) and not failed_first else 1
    assert len(calls) == expected_calls
    assert inspect_diagnostics(executor.store.root, run)["actual_call_attempts"] == expected_calls
    return {"executor": executor, "run": run, "value": executor.output(run, "N1"), "options": options, "unit_id": unit_id,
            "calls": calls, "tmp_path": tmp_path, "unit_ids": unit_ids, "extraction": extraction_config}


@pytest.fixture
def source_run(tmp_path, monkeypatch):
    return make_source_run(tmp_path, monkeypatch)


@pytest.fixture
def source_repaired(tmp_path, monkeypatch):
    return make_source_run(tmp_path, monkeypatch, repaired=True)


@pytest.fixture
def source_standalone(tmp_path, monkeypatch):
    source = make_source_run(tmp_path, monkeypatch, repaired=True, failed_first=True)
    original_executor, original_run = source["executor"], source["run"]
    original_state = original_executor.inspect(original_run)
    n1 = original_state["nodes"][1]
    n2 = original_state["nodes"][2]
    unit = n2["units"][source["unit_id"]]
    payload = original_executor.store.read_json(unit["input_ref"])
    captures = {cap["name"]: cap for attempt in n2["attempts"] for cap in attempt["captures"]}
    call_id = next(name.removeprefix("request-").removesuffix("-1") for name in captures
                   if name.startswith("request-"))
    request_ref = captures[f"request-{call_id}-1"]["ref"]
    response_ref = captures[f"response-{call_id}-1"]["ref"]
    original_request = original_executor.store.read_json(request_ref)
    original_response = original_executor.store.read_json(response_ref)
    old_raw = json.loads(original_response["choices"][0]["message"]["content"])
    original_task = json.loads(original_request["messages"][-1]["content"])
    validation = original_executor.store.read_json(captures[f"validation-{call_id}-1"]["ref"])
    context = _repair_context(validation["diagnostic_context"])
    code = validation["error_code"]
    coverage = _repair_coverage(old_raw, original_task, error_code=code, diagnostic_context=context)
    assert coverage is not None
    correction = _validation_repair_request(original_task, code, context, coverage, call_id)
    binding = {
        "source_root": str(original_executor.store.root), "source_run_id": original_run,
        "source_state_sha256": hashlib.sha256((original_executor.store.run_dir(original_run) / "state.json").read_bytes()).hexdigest(),
        "source_N1_output_hash": n1["output_hash"], "source_checkpoint_relative_path": n1["output_ref"]["path"],
        "source_checkpoint_file_sha256": n1["output_ref"]["sha256"], "window_id": source["unit_id"],
        "historical_call_is_not_a_new_request": True, "task_payload_updated_for_new_prompt": False,
        "whole_N2_complete": False, "payload_sha256": digest(payload), "historical_failed_call_id": call_id,
        "historical_candidate_sha256": digest(old_raw), "historical_provider_input_sha256": digest(original_request),
        "historical_response_sha256": digest(original_response), "historical_task_payload_sha256": digest(original_task),
        "original_request_sha256": digest(original_task), "correction_request_sha256": digest(correction),
        "protected_inventory_sha256": digest(coverage), "required_coverage": coverage,
        "historical_request_ref": request_ref["path"], "historical_response_ref": response_ref["path"],
    }
    final_raw = copy.deepcopy(old_raw)
    final_raw["unresolved_block_ids"] = []
    final_raw["requirements"].append({"citation": {"block_ids": ["TITLE", "LITERAL"]},
        "category": "procedure", "response_mode": "procedure", "mandatory": False, "confidence": "low",
        "format_status": "not_prescribed", "format_title": None,
        "required_fact_labels": [], "attachment_keywords": [], "reference_keywords": []})
    blocks, _, documents = _index(source["value"]["parsed"], source["value"]["manifest"])
    window, = _windows(documents, blocks, payload["window_config"])[0]
    sent = []
    def transport(endpoint, wire, secret, timeout):
        sent.append(copy.deepcopy(wire))
        return response(final_raw)
    options = {key: value for key, value in source["options"].items() if key != "mode"}
    provider = ChatProposalProvider(ChatConfig(**options), transport=transport)
    def handler(ctx, value):
        def call(ctx, task_type, unit_id, original_payload, validate):
            ctx.capture("historical-failure-correction-binding", binding)
            return provider.generate_json(ctx, correction, unit_id=unit_id, task_type=task_type,
                                          prompt_version=PROMPT_VERSION, system_prompt=SYSTEM_PROMPT, validate=validate)
        return {"window": run_extraction_window(ctx, window, payload["window_config"], call)}
    executor = Executor(tmp_path / "window-runtime", [NodeSpec("N2_WINDOW_DIAGNOSTIC", "synthetic-window-probe-v1", handler)],
                        {"structured_provider": source["options"], "debug_capture": True})
    run = executor.create_run({"source_run_id": original_run})
    state = executor.execute(run)
    assert state["status"] == "succeeded", state.get("error_code")
    assert len(sent) == 1
    spec = {"schema_version": "diagnostic-replay-v1",
            "sources": {"history": str(original_executor.store.root), "probe": str(executor.store.root)},
            "bindings": {"extract_requirements:" + source["unit_id"]: {
                "alias": "probe", "run_id": run, "node_key": "N2_WINDOW_DIAGNOSTIC", "unit_id": source["unit_id"]}}}
    return source, spec, executor, run


def replay_config(source):
    return {"schema_version": "diagnostic-replay-v1", "sources": {"history": str(source["executor"].store.root)},
            "bindings": {"extract_requirements:" + source["unit_id"]: {
                "alias": "history", "run_id": source["run"], "node_key": "N2", "unit_id": source["unit_id"]}}}


def target_run(source, *, spec=None, options_change=None, value_change=None,
               purpose="diagnostic_replay", preflight=True, after_preflight=None, validator=None,
               transport=None, include_prefix=False, root=None, fail_after_extraction=False):
    spec = copy.deepcopy(spec if spec is not None else replay_config(source))
    options = {**source["options"], "mode": "diagnostic_mixed"}
    if options_change:
        options_change(options)
    value = copy.deepcopy(source["value"])
    if value_change:
        value_change(value)
    calls, gateways = [], []

    def send(endpoint, payload, secret, timeout):
        calls.append(copy.deepcopy(payload))
        if transport is None:
            pytest.fail("Declared diagnostic history must never fall back to HTTP")
        return transport(payload)

    config = {"run_purpose": purpose, "diagnostic_replay": spec, "structured_provider": options,
              "extraction": source.get("extraction", {}), "debug_capture": True}

    def execute(ctx, data):
        gateway = JsonTaskGateway(options, transport=send, diagnostic_replay=spec)
        gateways.append(gateway)
        if preflight:
            gateway.preflight(ctx, data)
        if after_preflight:
            after_preflight(data)
        if validator:
            unit = source["unit_id"]
            blocks, _, documents = extraction._index(data["parsed"], data["manifest"])
            window_config = {"window_max_chars": source.get("extraction", {}).get("window_max_chars", 14000),
                             "window_max_blocks": source.get("extraction", {}).get("window_max_blocks", 60)}
            windows, _ = extraction._windows(documents, blocks, window_config)
            window = next(item for item in windows if item["window_id"] == unit)
            original_payload = extraction.extraction_window_payload(window, window_config)
            result = ctx.run_unit(unit, original_payload,
                lambda: gateway(ctx, "extract_requirements", unit, original_payload, validator))
        else:
            result = extract_requirements(ctx, data["parsed"], data["manifest"], gateway, source.get("extraction", {}))
        if fail_after_extraction:
            from company_bid.provider import ProviderFailure
            raise ProviderFailure("SYNTHETIC_LATER_FAILURE")
        return {"extracted": result, "diagnostic_replay": gateway.summary()}

    # One production extraction body with the same durable Executor contracts.
    root = Path(root) if root is not None else source["tmp_path"] / (
        "target-runtime-" + str(len(list(source["tmp_path"].glob("target-runtime-*")))))
    # Source bytes stay in the explicitly declared original root. References in
    # another store are not relabelled as if copied into this synthetic prefix.
    prefix = [NodeSpec("N0", "synthetic-frozen-prefix", lambda ctx, value: {**value, "source_artifacts": []}, ()),
              NodeSpec("N1", "synthetic-read-prefix", lambda ctx, value: value, ())] if include_prefix else []
    executor = Executor(root, [*prefix, NodeSpec("N2", "synthetic-current-validator-v2", execute,
                                      ("structured_provider", "extraction", "run_purpose", "diagnostic_replay"))], config)
    run = executor.create_run(value)
    state = executor.execute(run)
    return executor, run, state, calls, gateways


def failed_without_http(result):
    _, _, state, calls, _ = result
    assert state["status"] == "failed", state
    assert calls == []
    assert state["error_code"].startswith("DIAGNOSTIC_REPLAY_"), state.get("error_code")


@pytest.fixture
def source_failed_cell_wrap(tmp_path, monkeypatch):
    source = make_source_run(tmp_path, monkeypatch, failed_first=True, two_windows=True, failed_cell_label=True)
    root = source["executor"].store.run_dir(source["run"])
    terminal = next(row for row in rows(root / "events.jsonl") if row["event"] == "call.failed")
    source["extraction"] = {**source["extraction"], "failed_cell_wrap_replay": {
        "unit_id": source["unit_id"], "call_id": terminal["call_id"],
        "source_state_sha256": hashlib.sha256((root / "state.json").read_bytes()).hexdigest()}}
    return source, root, terminal


def test_failed_cell_wrap_revalidates_unchanged_candidate_then_allows_later_windows(source_failed_cell_wrap):
    source, root, terminal = source_failed_cell_wrap
    before = snapshot(root)
    historical_state = source["executor"].inspect(source["run"])
    historical_attempt = historical_state["nodes"][2]["attempts"][0]
    reference = next(item["ref"] for item in historical_attempt["captures"]
                     if item["name"] == f"response-{terminal['call_id']}-1")
    old_candidate = json.loads(source["executor"].store.read_json(reference)["choices"][0]["message"]["content"])

    def second(wire):
        task = json.loads(wire["messages"][-1]["content"])
        assert task["window"]["window_id"] != source["unit_id"]
        return response(candidate(task))

    executor, run, state, calls, _ = target_run(source, transport=second)
    assert state["status"] == "succeeded", state.get("error_code")
    assert len(calls) == 2
    node = state["nodes"][0]
    checkpoint = node["units"][source["unit_id"]]["output_ref"]
    assert executor.store.read_json(checkpoint)["data"]["raw_model_candidate"] == old_candidate
    audit = executor.output(run, "N2")["diagnostic_replay"]
    origin = audit["origins"][0]
    assert audit["revalidated_count"] == 1 and audit["live_miss_count"] == 2
    assert audit["historical_calls_charged_again"] == 0
    assert origin["candidate_sha256"] == digest(old_candidate)
    assert origin["calls"][0]["response_sha256"] == terminal["output_hash"]
    assert origin["lineage_notes"][0]["historical_status"] == "failed"
    assert origin["lineage_notes"][0]["cell_proof"]["column"] == 1
    events = rows(executor.store.run_dir(run) / "events.jsonl")
    checkpoint_position = next(index for index, row in enumerate(events) if row["event"] == "unit.checkpointed")
    paid_position = next(index for index, row in enumerate(events) if row["event"] == "call.started")
    assert events[checkpoint_position]["unit_id"] == source["unit_id"]
    assert checkpoint_position < paid_position
    assert snapshot(root) == before


def test_failed_cell_wrap_same_run_resume_reuses_checkpoints_without_rebuying_failed_live_window(source_failed_cell_wrap):
    source, root, _ = source_failed_cell_wrap
    before = snapshot(root)

    def live(wire):
        task = json.loads(wire["messages"][-1]["content"])
        if task["window"]["blocks"][0]["block_id"] == "B3":
            raise URLError("synthetic live failure")
        return response(candidate(task))

    executor, run, failed, calls, _ = target_run(source, transport=live)
    assert failed["error_code"] == "PROVIDER_NETWORK_ERROR"
    assert len(calls) == 2
    before_units = failed["nodes"][0]["units"]
    assert [unit["status"] for unit in before_units.values()].count("succeeded") == 2
    reserved = copy.deepcopy(failed["call_budget"]["reservations"])
    assert len(reserved) == 2

    resumed = executor.resume(run)
    assert resumed["error_code"] == "DIAGNOSTIC_REPLAY_FAILED_CELL_WRAP_LIVE_WINDOW_ALREADY_RESERVED"
    assert len(calls) == 2 and resumed["call_budget"]["reservations"] == reserved
    after_units = resumed["nodes"][0]["units"]
    assert all(after_units[key] == unit for key, unit in before_units.items() if unit["status"] == "succeeded")
    assert snapshot(root) == before


@pytest.mark.parametrize("alteration", ["no_authorization", "no_binding", "wrong_call", "parallel", "response", "terminal_trace",
                                         "state", "new_proof_absent"])
def test_failed_cell_wrap_cannot_buy_a_request_when_binding_or_evidence_invalid(source_failed_cell_wrap, alteration, monkeypatch):
    source, root, terminal = source_failed_cell_wrap
    spec = replay_config(source)
    authorization = source["extraction"]["failed_cell_wrap_replay"]
    if alteration == "no_authorization":
        del source["extraction"]["failed_cell_wrap_replay"]
    elif alteration == "no_binding":
        spec["bindings"] = {}
    elif alteration == "wrong_call":
        authorization["call_id"] = "1" * 32
    elif alteration == "parallel":
        source["extraction"]["window_concurrency"] = 2
    elif alteration == "response":
        state = source["executor"].inspect(source["run"])
        reference = next(item["ref"] for item in state["nodes"][2]["attempts"][0]["captures"]
                         if item["name"] == f"response-{terminal['call_id']}-1")
        path = source["executor"].store.root / reference["path"]
        path.write_bytes(path.read_bytes() + b"\n")
    elif alteration == "terminal_trace":
        path = root / "spans.jsonl"
        spans = rows(path)
        span = next(row for row in spans if row["span_id"] == terminal["span_id"])
        span["attributes"]["output_hash"] = "f" * 64
        path.write_text("".join(json.dumps(row) + "\n" for row in spans), encoding="utf-8")
    elif alteration == "state":
        path = root / "state.json"
        path.write_bytes(path.read_bytes() + b"\n")
    else:
        current = extraction.validate_live_window

        def validate(raw, window):
            result = current(raw, window)
            result["requirements"][0]["citation"]["label_grounding"] = []
            return result

        monkeypatch.setattr(extraction, "validate_live_window", validate)
    failed_without_http(target_run(source, spec=spec))


def test_invalid_illustration_region_stops_mixed_preflight_before_an_unbound_window(source_run):
    source = {**source_run, "extraction": {**source_run.get("extraction", {}),
                                            "illustrative_ocr_regions": [{"source_id": "unbound"}]}}
    spec = {**replay_config(source), "bindings": {}}
    result = target_run(source, spec=spec)
    failed_without_http(result)
    assert result[2]["error_code"] == "DIAGNOSTIC_REPLAY_PREFLIGHT_FAILED"


def test_mixed_replay_resolves_origin_with_its_own_region_scope(source_run, monkeypatch):
    source = {**source_run, "extraction": {**source_run.get("extraction", {}), "illustrative_ocr_regions": []}}
    manifests = []
    original = extraction.illustration_source_uses

    def record_scope(documents, regions, manifest):
        manifests.append(manifest is not None)
        return original(documents, regions, manifest)

    monkeypatch.setattr(extraction, "illustration_source_uses", record_scope)
    _, _, state, calls, _ = target_run(source)
    assert state["status"] == "succeeded" and calls == []
    assert manifests[0] and False in manifests[1:] and manifests[-1]


def replace_capture(source, prefix, change):
    executor = source["executor"]
    state = executor.inspect(source["run"])
    node = next(node for node in state["nodes"] if node["key"] == source.get("node_key", "N2"))
    captures = [capture for attempt in node["attempts"]
                for capture in attempt.get("captures", []) if capture["name"].startswith(prefix)]
    capture, = captures
    value = executor.store.read_json(capture["ref"])
    change(value)
    reference = executor.store.put_json(source["run"], "synthetic-mutated-capture", value)
    capture.update(ref=reference, hash=reference["sha256"])
    atomic_json(executor.store.run_dir(source["run"]) / "state.json", state)


def test_history_is_revalidated_with_no_provider_call_new_charge_or_source_write(source_run, monkeypatch):
    source = source_run
    before = snapshot(source["executor"].store.root)
    # Revalidation has no reason to resolve any credential.
    monkeypatch.delenv(source["options"]["auth_env"])
    executor, run, state, calls, _ = target_run(source)
    assert state["status"] == "succeeded", state.get("error_code")
    assert calls == [] and "call_budget" not in state
    result = executor.output(run)
    summary = result["diagnostic_replay"]
    assert summary["diagnostic_only"] is True
    assert summary["binding_count"] == summary["verified_binding_count"] == summary["revalidated_count"] == 1
    assert summary["live_miss_count"] == 0 and len(summary["origins"]) == 1
    assert result["extracted"]["requirements"][0]["text"] == SOURCE_CANARY
    assert snapshot(source["executor"].store.root) == before
    report = inspect_diagnostics(executor.store.root, run)
    assert report["actual_call_attempts"] == 0 and report["gaps"] == []
    spans = rows(executor.store.run_dir(run) / "spans.jsonl")
    validations = [span for span in spans if span["name"] == "candidate.revalidation"]
    assert len(validations) == 1 and validations[0]["links"]
    assert not any(span["name"] == "provider.call" for span in spans)
    events = rows(executor.store.run_dir(run) / "events.jsonl")
    assert not any(row["event"] in {"call.started", "call.completed", "call.reserved", "call.usage_recorded"} for row in events)
    ordinary = "".join(path.read_text(encoding="utf-8") for path in executor.store.run_dir(run).glob("*.jsonl"))
    assert SOURCE_CANARY not in ordinary
    output_ref = state["nodes"][0]["units"][source["unit_id"]]["output_ref"]
    assert executor.resume(run)["status"] == "succeeded"
    assert executor.inspect(run)["nodes"][0]["units"][source["unit_id"]]["output_ref"] == output_ref
    assert len([span for span in rows(executor.store.run_dir(run) / "spans.jsonl")
                if span["name"] == "candidate.revalidation"]) == 1


@pytest.mark.parametrize("purpose", [None, "formal", "diagnostic", ""])
def test_diagnostic_mode_requires_explicit_frozen_purpose(source_run, purpose):
    failed_without_http(target_run(source_run, purpose=purpose))


def test_call_before_preflight_cannot_read_candidate_or_spend(source_run):
    failed_without_http(target_run(source_run, preflight=False))


@pytest.mark.parametrize("field,value", [
    ("model", "different-model"), ("endpoint", "https://other.invalid/chat"),
    ("max_output_tokens", 513), ("thinking_mode", "enabled"),
])
def test_effective_wire_or_provider_identity_drift_is_not_a_cache_miss(source_run, field, value):
    failed_without_http(target_run(source_run, options_change=lambda options: options.update({field: value})))


@pytest.mark.parametrize("scope", ["project", "company", "source_sha256", "source_text"])
def test_source_scope_and_private_window_drift_fail_before_http(source_run, scope):
    def change(value):
        if scope == "project":
            value["manifest"]["project"]["project_id"] = "OTHER-PROJECT"
        elif scope == "company":
            value["manifest"]["company"]["company_id"] = "OTHER-COMPANY"
        elif scope == "source_sha256":
            value["manifest"]["sources"][0]["sha256"] = "f" * 64
            value["parsed"]["documents"][0]["sha256"] = "f" * 64
        else:
            value["parsed"]["documents"][0]["units"][0]["blocks"][0]["text"] += "篡改原字"
    failed_without_http(target_run(source_run, value_change=change))


@pytest.mark.parametrize("mutation", ["system", "user", "model", "response", "response_finish"])
def test_rehashed_capture_cannot_escape_wire_and_response_lineage_checks(source_run, mutation):
    if mutation in {"system", "user", "model"}:
        def change(value):
            if mutation == "model":
                value["model"] = "different-model"
            else:
                value["messages"][0 if mutation == "system" else 1]["content"] += " tampered"
        replace_capture(source_run, "request-", change)
    else:
        def change(value):
            if mutation == "response_finish":
                value["choices"][0]["finish_reason"] = "length"
            else:
                raw = json.loads(value["choices"][0]["message"]["content"])
                raw["requirements"][0]["mandatory"] = False
                value["choices"][0]["message"]["content"] = json.dumps(raw)
        replace_capture(source_run, "response-", change)
    failed_without_http(target_run(source_run))


@pytest.mark.parametrize("mutation", ["missing_span", "wrong_span_identity", "missing_event", "wrong_event_hash",
                                      "wrong_span_parent", "wrong_unit_span_hash"])
def test_damaged_actual_call_evidence_never_becomes_live_fallback(source_run, mutation):
    directory = source_run["executor"].store.run_dir(source_run["run"])
    if mutation in {"missing_span", "wrong_span_identity", "wrong_span_parent", "wrong_unit_span_hash"}:
        path = directory / "spans.jsonl"
        values = rows(path)
        if mutation == "missing_span":
            values = [row for row in values if row["name"] != "provider.call"]
        elif mutation == "wrong_span_parent":
            next(row for row in values if row["name"] == "provider.call")["parent_span_id"] = "0" * 16
        elif mutation == "wrong_unit_span_hash":
            next(row for row in values if row["name"] == "unit.N2")["attributes"]["input_hash"] = "0" * 64
        else:
            next(row for row in values if row["name"] == "provider.call")["attributes"]["call_id"] = "0" * 32
    else:
        path = directory / "events.jsonl"
        values = rows(path)
        if mutation == "missing_event":
            values = [row for row in values if row["event"] != "call.completed"]
        else:
            next(row for row in values if row["event"] == "call.completed")["output_hash"] = "0" * 64
    path.write_text("".join(json.dumps(row) + "\n" for row in values), encoding="utf-8")
    failed_without_http(target_run(source_run))


def test_missing_declared_artifact_is_corruption_not_a_live_miss(source_run):
    state = source_run["executor"].inspect(source_run["run"])
    capture = next(cap for attempt in state["nodes"][2]["attempts"] for cap in attempt["captures"]
                   if cap["name"].startswith("response-"))
    source_run["executor"].store.resolve(capture["ref"]["path"]).unlink()
    failed_without_http(target_run(source_run))


def test_reference_cannot_escape_the_declared_source_root(source_run):
    executor = source_run["executor"]
    state = executor.inspect(source_run["run"])
    capture = next(cap for attempt in state["nodes"][2]["attempts"] for cap in attempt["captures"]
                   if cap["name"].startswith("response-"))
    capture["ref"]["path"] = "../outside-source-root.json"
    atomic_json(executor.store.run_dir(source_run["run"]) / "state.json", state)
    failed_without_http(target_run(source_run))


def test_missing_source_root_is_not_created_by_the_read_only_resolver(source_run):
    spec = replay_config(source_run)
    root = source_run["tmp_path"] / "nonexistent-history"
    spec["sources"]["history"] = str(root)
    failed_without_http(target_run(source_run, spec=spec))
    assert not root.exists()


def test_all_bindings_are_preflighted_before_an_undeclared_live_window(source_run):
    spec = replay_config(source_run)
    binding = next(iter(spec["bindings"].values()))
    binding["unit_id"] = "missing-unit"
    spec["bindings"] = {"extract_requirements:missing-unit": binding}
    failed_without_http(target_run(source_run, spec=spec,
        transport=lambda payload: response(candidate(json.loads(payload["messages"][-1]["content"])))))


def test_frozen_source_bytes_are_checked_in_addition_to_the_parsed_hash(source_run):
    Path(source_run["value"]["manifest"]["sources"][0]["path"]).write_text("changed-original", encoding="utf-8")
    failed_without_http(target_run(source_run))


def test_duplicate_historical_call_capture_is_not_selected_by_first_match(source_run):
    executor = source_run["executor"]
    state = executor.inspect(source_run["run"])
    captures = state["nodes"][2]["attempts"][-1]["captures"]
    request = next(cap for cap in captures if cap["name"].startswith("request-"))
    captures.append(copy.deepcopy(request))
    atomic_json(executor.store.run_dir(source_run["run"]) / "state.json", state)
    failed_without_http(target_run(source_run))


@pytest.mark.parametrize("mutation", ["schema", "extra_field", "binding_unit", "node", "alias"])
def test_invalid_manifest_is_rejected_before_transport(source_run, mutation):
    spec = replay_config(source_run)
    binding = next(iter(spec["bindings"].values()))
    if mutation == "schema":
        spec["schema_version"] = "unknown-version"
    elif mutation == "extra_field":
        spec["silent_live_on_error"] = True
    elif mutation == "binding_unit":
        binding["unit_id"] = "different-unit"
    elif mutation == "node":
        binding["node_key"] = "N4"
    else:
        binding["alias"] = "unknown-root"
    failed_without_http(target_run(source_run, spec=spec))


def test_only_an_undeclared_key_can_use_the_existing_bounded_live_provider(source_run):
    spec = replay_config(source_run)
    spec["bindings"] = {}
    executor, run, state, calls, _ = target_run(source_run, spec=spec,
        transport=lambda payload: response(candidate(json.loads(payload["messages"][-1]["content"]))))
    assert state["status"] == "succeeded", state.get("error_code")
    assert len(calls) == 1
    summary = executor.output(run)["diagnostic_replay"]
    assert summary["binding_count"] == summary["verified_binding_count"] == summary["revalidated_count"] == 0
    assert summary["live_miss_count"] == 1
    assert inspect_diagnostics(executor.store.root, run)["actual_call_attempts"] == 1


def test_live_miss_still_obeys_existing_budget_limit(source_run):
    spec = replay_config(source_run)
    spec["bindings"] = {}
    executor, run, state, calls, _ = target_run(source_run, spec=spec,
        options_change=lambda options: options.update(max_run_usd=.000000001),
        transport=lambda payload: response(candidate(json.loads(payload["messages"][-1]["content"]))))
    assert state["status"] == "failed" and state["error_code"] == "PROVIDER_RUN_BUDGET_EXCEEDED"
    assert calls == [] and inspect_diagnostics(executor.store.root, run)["actual_call_attempts"] == 0


def test_current_validator_failure_is_not_replaced_by_a_paid_request(source_run):
    seen = []
    def reject(value):
        seen.append(copy.deepcopy(value))
        raise PlanningError("SYNTHETIC_CURRENT_VALIDATOR_REJECTION")
    _, _, state, calls, _ = target_run(source_run, validator=reject)
    assert state["status"] == "failed"
    assert state["error_code"] in {"SYNTHETIC_CURRENT_VALIDATOR_REJECTION", "DIAGNOSTIC_REPLAY_VALIDATION_FAILED"}
    assert len(seen) == 1 and calls == []


def test_preflight_revalidates_against_the_current_source_validator(source_run, monkeypatch):
    def reject(*args, **kwargs):
        raise PlanningError("SYNTHETIC_CURRENT_SOURCE_REJECTION")
    monkeypatch.setattr(extraction, "validate_live_window", reject)
    _, _, state, calls, _ = target_run(source_run)
    assert state["status"] == "failed"
    assert state["error_code"] in {"SYNTHETIC_CURRENT_SOURCE_REJECTION", "DIAGNOSTIC_REPLAY_VALIDATION_FAILED"}
    assert calls == []


def test_payload_cannot_change_after_successful_preflight(source_run):
    def alter(value):
        value["parsed"]["documents"][0]["units"][0]["blocks"][0]["text"] += "changed-after-preflight"
    failed_without_http(target_run(source_run, after_preflight=alter))


def test_wire_message_order_cannot_change_after_preflight(source_run, monkeypatch):
    original = extraction.extraction_window_payload
    def alter(_):
        monkeypatch.setattr(extraction, "extraction_window_payload",
            lambda *args: dict(reversed(list(original(*args).items()))))
    failed_without_http(target_run(source_run, after_preflight=alter))


def test_damaged_sealed_evidence_blocks_an_earlier_undeclared_live_window(tmp_path, monkeypatch):
    source = make_source_run(tmp_path, monkeypatch, two_windows=True)
    before = snapshot(source["executor"].store.root)
    spec = replay_config(source)
    entry = next(iter(spec["bindings"].values()))
    second = source["unit_ids"][1]
    spec["bindings"] = {"extract_requirements:" + second: {**entry, "unit_id": second}}
    def corrupt(_):
        target = next(source["tmp_path"].glob("target-runtime-*"))
        next(target.rglob("*-historical-evidence.json")).write_bytes(b"damaged-sealed-evidence")
    _, _, state, calls, _ = target_run(source, spec=spec, after_preflight=corrupt,
                                      transport=lambda wire: response(candidate(json.loads(wire["messages"][-1]["content"]))))
    assert state["status"] == "failed" and state["error_code"] == "CHECKPOINT_INVALID"
    assert calls == []
    assert snapshot(source["executor"].store.root) == before


def test_wire_builder_is_the_actual_live_builder_and_never_reads_credentials(source_run, monkeypatch):
    from company_bid.provider import build_json_request
    options = {key: value for key, value in source_run["options"].items() if key != "mode"}
    old_wire = source_run["calls"][0]
    task = json.loads(old_wire["messages"][-1]["content"])
    monkeypatch.delenv(options["auth_env"])
    assert build_json_request(ChatConfig(**options), task, SYSTEM_PROMPT) == old_wire


def test_corrected_success_keeps_actual_feedback_wire_and_both_call_identities(source_repaired, monkeypatch):
    source = source_repaired
    before = snapshot(source["executor"].store.root)
    monkeypatch.delenv(source["options"]["auth_env"])
    first_task, repaired_task = [json.loads(wire["messages"][-1]["content"]) for wire in source["calls"]]
    assert "validation_feedback" not in first_task and "validation_feedback" in repaired_task
    assert first_task["window"] == repaired_task["window"]
    assert digest(source["calls"][0]) != digest(source["calls"][1])
    executor, run, state, calls, _ = target_run(source)
    assert state["status"] == "succeeded", state.get("error_code")
    assert calls == [] and "call_budget" not in state
    assert snapshot(source["executor"].store.root) == before
    result = executor.output(run)
    assert result["diagnostic_replay"]["revalidated_count"] == 1
    assert any(item["text"] == "5.2 开启程序\n/" for item in result["extracted"]["requirements"])
    source_spans = rows(source["executor"].store.run_dir(source["run"]) / "spans.jsonl")
    old_calls = {(span["trace_id"], span["span_id"]) for span in source_spans if span["name"] == "provider.call"}
    validation, = [span for span in rows(executor.store.run_dir(run) / "spans.jsonl")
                   if span["name"] == "candidate.revalidation"]
    assert {(link["trace_id"], link["span_id"]) for link in validation["links"]} == old_calls
    assert inspect_diagnostics(executor.store.root, run)["actual_call_attempts"] == 0


@pytest.mark.parametrize("damage", ["wrong_first_id", "wrong_first_candidate", "rebind_final_to_base", "missing_coverage"])
def test_corrected_response_requires_the_complete_unchanged_lineage(source_repaired, damage):
    def change(relation):
        if damage == "wrong_first_id":
            relation["first_call_id"] = "0" * 32
        elif damage == "wrong_first_candidate":
            relation["first_candidate_hash"] = "0" * 64
        elif damage == "rebind_final_to_base":
            relation["second_provider_input_hash"] = relation["first_provider_input_hash"]
        else:
            relation.pop("required_coverage")
    replace_capture(source_repaired, "validation-repair-result-", change)
    failed_without_http(target_run(source_repaired))


def test_standalone_correction_replays_only_with_its_original_failed_call_across_roots(source_standalone, monkeypatch):
    source, spec, probe, probe_run = source_standalone
    history_before, probe_before = snapshot(source["executor"].store.root), snapshot(probe.store.root)
    monkeypatch.delenv(source["options"]["auth_env"])
    executor, run, state, calls, _ = target_run(source, spec=spec)
    assert state["status"] == "succeeded", state.get("error_code")
    assert calls == [] and "call_budget" not in state
    assert snapshot(source["executor"].store.root) == history_before
    assert snapshot(probe.store.root) == probe_before
    summary = executor.output(run)["diagnostic_replay"]
    assert summary["verified_binding_count"] == summary["revalidated_count"] == 1
    origin, = summary["origins"]
    assert origin["node_key"] == "N2_WINDOW_DIAGNOSTIC"
    assert {call["source_alias"] for call in origin["calls"]} == {"history", "probe"}
    assert {call["run_id"] for call in origin["calls"]} == {source["run"], probe_run}
    assert origin["actual_final_wire_sha256"] != digest(source["calls"][0])
    assert origin["base_request_sha256"] == digest(json.loads(source["calls"][0]["messages"][-1]["content"]))
    assert inspect_diagnostics(executor.store.root, run)["actual_call_attempts"] == 0


@pytest.mark.parametrize("field", [
    "historical_failed_call_id", "source_state_sha256", "source_N1_output_hash",
    "historical_response_sha256", "historical_task_payload_sha256",
    "correction_request_sha256", "protected_inventory_sha256", "historical_request_ref",
])
def test_standalone_binding_fields_are_verified_instead_of_trusted(source_standalone, field):
    source, spec, executor, run = source_standalone
    def change(binding):
        binding[field] = "f" * (32 if field == "historical_failed_call_id" else 64)
    replace_capture({"executor": executor, "run": run, "node_key": "N2_WINDOW_DIAGNOSTIC"},
                    "historical-failure-correction-binding", change)
    failed_without_http(target_run(source, spec=spec))


def test_standalone_origin_root_must_be_explicitly_declared(source_standalone):
    source, spec, _, _ = source_standalone
    del spec["sources"]["history"]
    failed_without_http(target_run(source, spec=spec))


def test_failed_later_live_window_resumes_without_losing_revalidation_statistics(tmp_path, monkeypatch):
    source = make_source_run(tmp_path, monkeypatch, two_windows=True)
    before = snapshot(source["executor"].store.root)
    attempts = []
    def intermittent_transport(payload):
        attempts.append(copy.deepcopy(payload))
        if len(attempts) == 1:
            raise URLError("synthetic-offline-failure")
        return response(candidate(json.loads(payload["messages"][-1]["content"])))
    executor, run, failed, calls, gateways = target_run(source, transport=intermittent_transport)
    assert failed["status"] == "failed" and failed["error_code"] == "PROVIDER_NETWORK_ERROR"
    first, second = source["unit_ids"]
    assert failed["nodes"][0]["units"][first]["status"] == "succeeded"
    assert failed["nodes"][0]["units"][second]["status"] == "failed"
    frozen = failed["nodes"][0]["units"][first]["output_ref"]
    assert gateways[0].summary()["revalidated_count"] == 1
    completed = executor.resume(run)
    assert completed["status"] == "succeeded", completed.get("error_code")
    assert completed["nodes"][0]["units"][first]["output_ref"] == frozen
    summary = executor.output(run)["diagnostic_replay"]
    assert summary["binding_count"] == summary["verified_binding_count"] == summary["revalidated_count"] == 1
    assert summary["checkpoint_reuse_count"] == 1
    assert summary["revalidated_this_attempt_count"] == 0
    assert summary["live_miss_count"] == 1 and summary["live_checkpoint_reuse_count"] == 0
    assert len(calls) == len(attempts) == 2
    assert inspect_diagnostics(executor.store.root, run)["actual_call_attempts"] == 2
    spans = rows(executor.store.run_dir(run) / "spans.jsonl")
    assert len([span for span in spans if span["name"] == "candidate.revalidation"]) == 1
    assert snapshot(source["executor"].store.root) == before


def make_mixed_source(tmp_path, monkeypatch, *, later_failure=False):
    """One historical revalidation followed by one real offline Provider call."""
    original = make_source_run(tmp_path, monkeypatch, two_windows=True)
    executor, run, state, calls, _ = target_run(original, include_prefix=True,
        fail_after_extraction=later_failure,
        transport=lambda wire: response(candidate(json.loads(wire["messages"][-1]["content"]))))
    assert state["status"] == ("failed" if later_failure else "succeeded"), state.get("error_code")
    assert len(calls) == 1 and state["artifact_scope"] == "diagnostic"
    assert inspect_diagnostics(executor.store.root, run)["actual_call_attempts"] == 1
    return {**original, "executor": executor, "run": run, "value": executor.output(run, "N1"),
            "unit_id": original["unit_ids"][1], "calls": calls, "original_source": original}


def mixed_replay_config(source):
    # A revalidated unit remains bound to its actual original source.
    original = source["original_source"]
    spec = replay_config(original)
    spec["sources"]["mixed"] = str(source["executor"].store.root)
    live = source["unit_id"]
    spec["bindings"]["extract_requirements:" + live] = {
        "alias": "mixed", "run_id": source["run"], "node_key": "N2", "unit_id": live}
    return spec


@pytest.fixture
def mixed_source(tmp_path, monkeypatch):
    return make_mixed_source(tmp_path, monkeypatch)


@pytest.mark.parametrize("later_failure", [False, True])
@pytest.mark.parametrize("same_root", [False, True])
def test_sealed_mixed_actual_live_unit_is_revalidated_without_relabelling_history(
        tmp_path, monkeypatch, later_failure, same_root):
    source = make_mixed_source(tmp_path, monkeypatch, later_failure=later_failure)
    old_run_dir = source["executor"].store.run_dir(source["run"])
    before = snapshot(old_run_dir)
    original_before = snapshot(source["original_source"]["executor"].store.root)
    monkeypatch.delenv(source["options"]["auth_env"])
    executor, run, state, calls, _ = target_run(source, spec=mixed_replay_config(source),
        root=source["executor"].store.root if same_root else None)
    assert state["status"] == "succeeded", state.get("error_code")
    assert calls == [] and "call_budget" not in state
    summary = executor.output(run)["diagnostic_replay"]
    assert summary["verified_binding_count"] == summary["revalidated_count"] == 2
    assert summary["historical_calls_charged_again"] == summary["live_miss_count"] == 0
    live_origin, = [item for item in summary["origins"] if item["source_alias"] == "mixed"]
    assert live_origin["unit_id"] == source["unit_id"]
    assert {call["run_id"] for call in live_origin["calls"]} == {source["run"]}
    original_origin, = [item for item in summary["origins"] if item["source_alias"] == "history"]
    assert original_origin["run_id"] == source["original_source"]["run"]
    assert inspect_diagnostics(executor.store.root, run)["actual_call_attempts"] == 0
    assert snapshot(old_run_dir) == before
    assert snapshot(source["original_source"]["executor"].store.root) == original_before


def test_replayed_mixed_unit_is_not_an_actual_source_even_with_identical_raw_candidate(mixed_source):
    source = mixed_source
    first = source["unit_ids"][0]
    spec = mixed_replay_config(source)
    spec["bindings"]["extract_requirements:" + first].update(alias="mixed", run_id=source["run"])
    source_node = next(node for node in source["executor"].inspect(source["run"])["nodes"] if node["key"] == "N2")
    old = source["original_source"]
    old_node = next(node for node in old["executor"].inspect(old["run"])["nodes"] if node["key"] == "N2")
    assert source["executor"].store.read_json(source_node["units"][first]["output_ref"])["data"]["raw_model_candidate"] == (
        old["executor"].store.read_json(old_node["units"][first]["output_ref"])["data"]["raw_model_candidate"])
    result = target_run(source, spec=spec)
    failed_without_http(result)
    assert result[2]["error_code"] == "DIAGNOSTIC_REPLAY_SOURCE_ACTUAL_CALL_REQUIRED"


@pytest.mark.parametrize("damage", ["purpose", "scope", "capture_policy", "capture_disabled", "running", "paused",
    "missing_spec", "bad_spec_schema", "config_hash", "unfinished_execution", "missing_preflight", "missing_miss", "failed_unit"])
def test_mixed_source_requires_frozen_diagnostic_scope_and_terminal_complete_capture(mixed_source, damage):
    source = mixed_source
    executor = source["executor"]
    state = executor.inspect(source["run"])
    if damage == "purpose":
        state["execution_config"]["run_purpose"] = "formal"
    elif damage == "scope":
        state["artifact_scope"] = "standard"
    elif damage == "capture_policy":
        state["capture_policy"] = False
    elif damage == "capture_disabled":
        state["execution_config"]["debug_capture"] = False
    elif damage == "running":
        state["status"] = "running"
    elif damage == "paused":
        state["status"] = "paused_debug"
    elif damage == "missing_spec":
        del state["execution_config"]["diagnostic_replay"]
    elif damage == "bad_spec_schema":
        state["execution_config"]["diagnostic_replay"]["schema_version"] = "invented-replay"
    elif damage == "unfinished_execution":
        del state["execution_attempts"][-1]["ended_at"]
    elif damage == "failed_unit":
        node = next(node for node in state["nodes"] if node["key"] == "N2")
        node["units"][source["unit_id"]]["status"] = "failed"
    elif damage in {"missing_preflight", "missing_miss"}:
        node = next(node for node in state["nodes"] if node["key"] == "N2")
        node["attempts"][-1]["captures"] = [cap for cap in node["attempts"][-1]["captures"]
            if cap["name"] != ("diagnostic-replay-preflight" if damage == "missing_preflight" else "diagnostic-replay-live-miss")]
    state["config_hash"] = "f" * 64 if damage == "config_hash" else digest(state["execution_config"])
    atomic_json(executor.store.run_dir(source["run"]) / "state.json", state)
    failed_without_http(target_run(source, spec=mixed_replay_config(source)))


@pytest.mark.parametrize("damage", ["preflight_scope", "preflight_count", "preflight_origin", "imported_fees", "miss_reason", "miss_hash",
                                     "response", "ledger", "call_trace", "metadata_trace"])
def test_mixed_source_metadata_and_actual_wire_trace_budget_cannot_be_forged(mixed_source, damage):
    source = mixed_source
    if damage == "preflight_scope":
        replace_capture(source, "diagnostic-replay-preflight", lambda value: value.update(diagnostic_only=False))
    elif damage == "preflight_count":
        replace_capture(source, "diagnostic-replay-preflight", lambda value: value.update(verified_binding_count=0))
    elif damage == "preflight_origin":
        replace_capture(source, "diagnostic-replay-preflight", lambda value: value["origins"][0].update(run_id="f" * 32))
    elif damage == "imported_fees":
        replace_capture(source, "diagnostic-replay-preflight", lambda value: value.update(historical_calls_charged_again=1))
    elif damage == "miss_reason":
        replace_capture(source, "diagnostic-replay-live-miss", lambda value: value.update(reason="declared_but_live"))
    elif damage == "miss_hash":
        replace_capture(source, "diagnostic-replay-live-miss", lambda value: value.update(request_sha256="0" * 64))
    elif damage == "response":
        def corrupt(value):
            raw = json.loads(value["choices"][0]["message"]["content"])
            raw["requirements"][0]["mandatory"] = False
            value["choices"][0]["message"]["content"] = json.dumps(raw)
        replace_capture(source, "response-", corrupt)
    elif damage == "call_trace":
        path = source["executor"].store.run_dir(source["run"]) / "spans.jsonl"
        values = rows(path)
        next(item for item in values if item["name"] == "provider.call")["attributes"]["call_id"] = "0" * 32
        path.write_text("".join(json.dumps(item) + "\n" for item in values), encoding="utf-8")
    else:
        state = source["executor"].inspect(source["run"])
        if damage == "ledger":
            state["call_budget"]["reservations"] = []
        else:
            node = next(node for node in state["nodes"] if node["key"] == "N2")
            next(cap for cap in node["attempts"][-1]["captures"] if
                 cap["name"] == "diagnostic-replay-preflight")["trace_id"] = "0" * 32
        atomic_json(source["executor"].store.run_dir(source["run"]) / "state.json", state)
    failed_without_http(target_run(source, spec=mixed_replay_config(source)))


def test_ordinary_actual_call_cannot_be_disguised_as_mixed_by_editing_only_frozen_labels(source_run):
    source = source_run
    state = source["executor"].inspect(source["run"])
    state["artifact_scope"] = "diagnostic"
    state["execution_config"].update(run_purpose="diagnostic_replay", diagnostic_replay={
        "schema_version": "diagnostic-replay-v1", "sources": {"history": str(source["executor"].store.root)},
        "bindings": {}})
    state["execution_config"]["structured_provider"]["mode"] = "diagnostic_mixed"
    state["config_hash"] = digest(state["execution_config"])
    atomic_json(source["executor"].store.run_dir(source["run"]) / "state.json", state)
    failed_without_http(target_run(source))


def test_current_run_identity_is_never_loaded_as_its_own_source(mixed_source):
    from company_bid.diagnostic_replay import DiagnosticReplay
    from company_bid.provider import ProviderFailure
    source = mixed_source
    adapter = DiagnosticReplay({}, None)
    root = source["executor"].store.root.resolve()
    adapter.roots = {"current": root}
    adapter._target = (root, source["run"])
    before = snapshot(source["executor"].store.run_dir(source["run"]))
    with pytest.raises(ProviderFailure, match="DIAGNOSTIC_REPLAY_SOURCE_CURRENT_RUN"):
        adapter._run("current", source["run"])
    assert snapshot(source["executor"].store.run_dir(source["run"])) == before


def test_bad_mixed_metadata_blocks_an_earlier_undeclared_live_window(mixed_source):
    source = mixed_source
    replace_capture(source, "diagnostic-replay-preflight", lambda value: value.update(publication_status="published"))
    spec = mixed_replay_config(source)
    del spec["bindings"]["extract_requirements:" + source["unit_ids"][0]]
    failed_without_http(target_run(source, spec=spec,
        transport=lambda wire: response(candidate(json.loads(wire["messages"][-1]["content"])))))


@pytest.mark.parametrize("schema", ["validation-repair-v5", "validation-repair-v6", "validation-repair-v7"])
def test_correction_relation_versions_keep_the_same_full_lineage_contract(source_repaired, schema):
    replace_capture(source_repaired, "validation-repair-result-", lambda value: value.update(schema_version=schema))
    replace_capture(source_repaired, "validation-repair-scheduled-", lambda value: value.update(schema_version=schema))
    executor, run, state, calls, _ = target_run(source_repaired)
    assert state["status"] == "succeeded", state.get("error_code")
    assert calls == [] and "call_budget" not in state
    assert len(executor.output(run)["diagnostic_replay"]["origins"][0]["calls"]) == 2


@pytest.mark.parametrize("schema", ["validation-repair-v4", "validation-repair-v8"])
def test_unknown_correction_relation_schema_never_falls_back_to_http(source_repaired, schema):
    replace_capture(source_repaired, "validation-repair-result-", lambda value: value.update(schema_version=schema))
    replace_capture(source_repaired, "validation-repair-scheduled-", lambda value: value.update(schema_version=schema))
    failed_without_http(target_run(source_repaired))

