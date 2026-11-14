"""N4 diagnostic reuse authenticates real offline executor evidence, not cached prose."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from company_bid import pipeline
from company_bid import writing_replay as replay
from company_bid.diagnostics import inspect_diagnostics
from company_bid.models import PreparedBid, file_sha256
from company_bid.provider import ChatProposalProvider, ProviderFailure
from company_bid.runtime import Executor, NodeSpec
from company_bid.storage import digest


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def snapshot(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


def synthetic_prepared(tmp_path):
    source = tmp_path / "synthetic-tender.txt"
    texts = [f"合成要求{i}：仅在取得书面许可后逐项检查、记录并复核合成对象。" for i in range(9)]
    context = "服务范围：仅合成管道巡检，不包含储罐、独立直接评价或未经授权的维修。"
    source.write_text("\n".join(texts), encoding="utf-8")
    extra_source = tmp_path / "synthetic-extra-tender.txt"
    extra_source.write_text(context, encoding="utf-8")
    requirements = [{"requirement_id": f"R-{i}", "text": text, "category": "technical",
                     "source_refs": ["T"], "response_mode": "writing"} for i, text in enumerate(texts)]
    requirements.append({"requirement_id": "R-CONTEXT", "text": context, "category": "commercial",
                         "source_refs": ["ET"], "response_mode": "procedure"})
    tasks = [{"unit_id": f"WRITE-{i}", "title": f"合成巡检方法{i}", "objective": "编写拟实施巡检方法。",
              "must_cover": [text], "requirement_refs": [f"R-{i}"], "reference_refs": [],
              "forbidden_fields": ["不得虚构事实或扩大服务范围。"],
              "completion_criteria": ["记录检查对象、原始结果、异常复核和交付。"],
              "operation": "bounded_new_draft", "no_reference_reason": "无可用参考。"}
             for i, text in enumerate(texts)]
    return PreparedBid.model_validate({
        "synthetic": True, "input_kind": "manual_prepared", "preparation_note": "Synthetic adapter test only.",
        "project": {"project_id": "P", "name": "合成项目", "source_refs": ["T"]},
        "company": {"company_id": "C", "name": "合成公司", "source_refs": ["T"]},
        "sources": [{"source_id": "T", "title": "合成招标原文", "path": str(source), "sha256": file_sha256(source),
                     "locator": "document", "kind": "tender", "provenance": "synthetic", "synthetic": True},
                    {"source_id": "ET", "title": "合成额外招标范围", "path": str(extra_source), "sha256": file_sha256(extra_source),
                     "locator": "document", "kind": "tender", "provenance": "synthetic", "synthetic": True}],
        "requirements": requirements,
        "sections": [{"section_id": "TECH", "title": "合成技术方案", "kind": "technical", "source_refs": ["T"],
                      "requirement_refs": [f"R-{i}" for i in range(9)], "writing_tasks": tasks},
                     {"section_id": "CONTROL", "title": "待核原文", "kind": "commercial", "source_refs": ["ET"],
                      "requirement_refs": ["R-CONTEXT"], "render_body": False}],
    })


def provider_options():
    return {"mode": "chat_completions", "endpoint": "https://example.invalid/writing", "model": "synthetic-writing",
            "auth_env": "TEST_WRITING_REPLAY_KEY", "max_attempts": 1, "max_output_tokens": 512,
            "max_input_chars": 40000, "max_run_calls": 9, "max_run_usd": 1,
            "input_usd_per_million": .30, "output_usd_per_million": 1.20}


def candidate():
    return {"paragraphs": ["拟在取得作业许可后逐项检查合成对象，记录原始结果，对异常复核并移交记录。"],
            "steps": ["核对对象与许可，逐项记录并复核异常。"],
            "coverage": [{"item_index": 0, "paragraph_indices": [0], "step_indices": [0]}]}


def response():
    return {"id": "synthetic-writing-response", "choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps(candidate(), ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}}


def binding(run_id, unit_id):
    return {"alias": "original", "run_id": run_id, "node_key": "N4", "unit_id": unit_id}


def replay_config(root, run_id):
    return {"schema_version": "diagnostic-writing-replay-v1", "sources": {"original": str(root.resolve())},
            "bindings": {f"WRITE-{i}": binding(run_id, f"WRITE-{i}") for i in range(4)},
            "corrections": {f"WRITE-{i}": {**binding(run_id, f"WRITE-{i}"),
                "feedback": "原候选服务范围过宽，仅按附带的冻结服务范围校准，不新增事实或承诺。",
                "context_requirement_refs": ["R-CONTEXT"]} for i in range(4, 9)}}


def base_inputs(prepared, options):
    result = {}
    for section in prepared.sections:
        for task in section.writing_tasks:
            result[task.unit_id] = replay.build_writing_unit_input(prepared, section, task, options, None, {})
    return result


@pytest.fixture
def origin(tmp_path, monkeypatch):
    prepared = synthetic_prepared(tmp_path)
    options = provider_options()
    sent = []
    control = {"reject_next_response": False}
    monkeypatch.setenv(options["auth_env"], "synthetic-credential-never-a-real-key")

    class OfflineProvider(ChatProposalProvider):
        def __init__(self, config, *args, **kwargs):
            def transport(endpoint, payload, secret, timeout):
                sent.append(copy.deepcopy(payload))
                if control["reject_next_response"]:
                    control["reject_next_response"] = False
                    value = response()
                    invalid = candidate()
                    invalid["coverage"] = []
                    value["choices"][0]["message"]["content"] = json.dumps(invalid, ensure_ascii=False)
                    return value
                return response()
            super().__init__(config, transport=transport, sleep=lambda _: None)

    monkeypatch.setattr(pipeline, "ChatProposalProvider", OfflineProvider)
    monkeypatch.setattr(replay, "ChatProposalProvider", OfflineProvider)
    root = tmp_path / "runtime"
    settings = {"pipeline": "prepared", "provider": options, "run_purpose": "diagnostic_replay", "debug_capture": True}
    normal_n4 = next(node for node in pipeline.build_executor(root, settings).nodes if node.key == "N4")

    def n3(ctx, value):
        value = copy.deepcopy(value)
        refs = []
        for source in value["prepared"]["sources"]:
            ref = ctx.store_bytes("synthetic-original.txt", Path(source["path"]).read_bytes())
            source["path"] = str(root / ref["path"])
            refs.append(ref)
        return {**value, "source_artifacts": refs}

    executor = Executor(root, [NodeSpec("N3", "synthetic-n3-v1", n3, ()), normal_n4,
                               NodeSpec("N5", "synthetic-terminal-v1", lambda ctx, value: value, ())], settings)
    rid = executor.create_run({"prepared": prepared.model_dump(mode="json"), "proposals": {}, "source_artifacts": []})
    state = executor.execute(rid, stop_after="N4")
    assert state["status"] == "paused_debug", state.get("error_code")
    assert len(sent) == 9
    assert inspect_diagnostics(root, rid)["gaps"] == []
    value = executor.output(rid, "N3")
    return {"root": root, "run": rid, "executor": executor, "state": state, "value": value,
            "prepared": PreparedBid.model_validate(value["prepared"]), "options": options, "sent": sent,
            "control": control}


def target(origin, *, config=None, options=None, selected=None, damage_inputs=None,
           run_purpose="diagnostic_replay", debug_capture=True, interrupt_after_units=False):
    root = origin["root"]
    config = copy.deepcopy(config if config is not None else replay_config(root, origin["run"]))
    options = copy.deepcopy(options if options is not None else {**origin["options"], "mode": "diagnostic_writing_mixed"})
    settings = {"pipeline": "prepared", "provider": options, "writing_replay": config,
                "run_purpose": run_purpose, "debug_capture": debug_capture}
    provider = replay.DiagnosticWritingProvider(options, config)
    holder = {"provider": provider}
    interruption = {"pending": interrupt_after_units}
    prepared = origin["prepared"]
    inputs = base_inputs(prepared, options)
    if damage_inputs is not None:
        damage_inputs(inputs)

    def n4(ctx, value):
        active = holder["provider"]
        active.preflight(ctx, value["prepared"], inputs)
        output = {}
        for identity in selected if selected is not None else inputs:
            base = inputs[identity]
            unit_input = active.unit_input(identity, base)
            def write(base=base):
                proposal = active.generate(ctx, base["context"], base["requirements"])
                return {"proposal": proposal.model_dump(mode="json")}
            output[identity] = ctx.run_unit(identity, unit_input, write)
        if interruption["pending"]:
            interruption["pending"] = False
            raise RuntimeError("Synthetic interruption after durable units and before node summary")
        return {"outputs": output, "summary": active.summary()}

    executor = Executor(root, [NodeSpec("N3", "synthetic-target-prefix-v1", lambda ctx, value: value, ()),
                               NodeSpec("N4", "synthetic-target-n4-v1", n4, ("provider", "writing_replay"))], settings)
    rid = executor.create_run(origin["value"])
    state = executor.execute(rid)
    return {"executor": executor, "run": rid, "state": state, "provider": provider, "inputs": inputs,
            "holder": holder, "settings": settings, "config": config, "options": options}


def walk(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def test_correction_payload_selects_exact_frozen_requirement_and_preserves_base(tmp_path):
    prepared = synthetic_prepared(tmp_path)
    base = base_inputs(prepared, provider_options())["WRITE-4"]
    before = copy.deepcopy((base, prepared.model_dump(mode="json")))
    correction = {**binding("a" * 32, "WRITE-4"), "feedback": "仅校准越界范围，不新增承诺。",
                  "context_requirement_refs": ["R-CONTEXT"]}
    payload = replay.build_correction_payload(base, candidate(), correction, prepared.model_dump(mode="json"))
    expected = next(item.model_dump(mode="json") for item in prepared.requirements if item.requirement_id == "R-CONTEXT")
    exact_requirements = [item for item in walk(payload) if isinstance(item, dict) and item.get("requirement_id") == "R-CONTEXT"]
    assert exact_requirements == [expected]
    scope = payload["writing_correction"]["source_scope"]
    assert scope["role"] == "readonly_frozen_tender_requirements"
    assert scope["requirements"] == [expected]
    assert scope["sources"] == [{"source_id": "ET", "source_sha256": prepared.sources[1].sha256,
                                 "locator": prepared.sources[1].locator}]
    assert (base, prepared.model_dump(mode="json")) == before
    assert base["context"]["writing_task"]["must_cover"] == [prepared.requirements[4].text]
    wire = replay.build_correction_wire(provider_options(), base, candidate(), correction, prepared.model_dump(mode="json"))
    assert json.loads(wire["messages"][-1]["content"]) == payload
    assert wire["model"] == provider_options()["model"]


@pytest.mark.parametrize("damage", ["unknown_requirement", "duplicate_requirement", "quoted_extra_facts", "bad_candidate"])
def test_correction_builder_rejects_unfrozen_context_or_invalid_original(tmp_path, damage):
    prepared = synthetic_prepared(tmp_path)
    base = base_inputs(prepared, provider_options())["WRITE-4"]
    correction = {**binding("a" * 32, "WRITE-4"), "feedback": "原范围越界。", "context_requirement_refs": ["R-CONTEXT"]}
    old = candidate()
    if damage == "unknown_requirement":
        correction["context_requirement_refs"] = ["INVENTED"]
    elif damage == "duplicate_requirement":
        correction["context_requirement_refs"] *= 2
    elif damage == "quoted_extra_facts":
        correction["extra_requirements"] = [{"text": "伪造的独立储罐检测事实。"}]
    else:
        old = {"paragraphs": [], "coverage": []}
    with pytest.raises((ProviderFailure, ValidationError)):
        replay.build_correction_payload(base, old, correction, prepared.model_dump(mode="json"))


def test_complete_correction_wire_checks_input_budget_without_any_request(tmp_path):
    prepared = synthetic_prepared(tmp_path)
    base = base_inputs(prepared, provider_options())["WRITE-4"]
    correction = {**binding("a" * 32, "WRITE-4"), "feedback": "原范围越界。", "context_requirement_refs": ["R-CONTEXT"]}
    old = candidate()
    old["paragraphs"] = ["合成待校准旧响应。" * 6000]
    with pytest.raises(ProviderFailure, match="PROVIDER_INPUT_BUDGET_EXCEEDED"):
        replay.build_correction_wire(provider_options(), base, old, correction, prepared.model_dump(mode="json"))


def test_four_replays_need_no_credential_transport_or_new_reservation_and_link_original_trace(origin, monkeypatch):
    before = snapshot(origin["root"] / "runs" / origin["run"])
    monkeypatch.delenv(origin["options"]["auth_env"])
    result = target(origin, selected=[f"WRITE-{i}" for i in range(4)])
    assert result["state"]["status"] == "succeeded", result["state"].get("error_code")
    assert len(origin["sent"]) == 9
    assert result["state"].get("call_budget", {}).get("reservation_count", 0) == 0
    assert inspect_diagnostics(origin["root"], result["run"])["actual_call_attempts"] == 0
    assert snapshot(origin["root"] / "runs" / origin["run"]) == before
    spans = rows(result["executor"].store.run_dir(result["run"]) / "spans.jsonl")
    revalidation = [span for span in spans if span["name"] == "candidate.revalidation"]
    assert len(revalidation) == 4 and all(span.get("links") for span in revalidation)
    old_events = rows(origin["executor"].store.run_dir(origin["run"]) / "events.jsonl")
    original_calls = {(event["trace_id"], event["span_id"]) for event in old_events
                      if event["event"] == "call.completed" and event["unit_id"] in {f"WRITE-{i}" for i in range(4)}}
    assert all((link["trace_id"], link["span_id"]) in original_calls
               for span in revalidation for link in span["links"])
    events = rows(result["executor"].store.run_dir(result["run"]) / "events.jsonl")
    assert not any(event["event"].startswith("call.") for event in events)
    ordinary = json.dumps([spans, events, result["state"]], ensure_ascii=False)
    assert origin["prepared"].requirements[0].text not in ordinary
    assert "synthetic-credential-never-a-real-key" not in ordinary


def test_completed_source_run_is_also_a_valid_sealed_origin(origin):
    complete = origin["executor"].resume(origin["run"])
    assert complete["status"] == "succeeded"
    result = target(origin, selected=["WRITE-0"])
    assert result["state"]["status"] == "succeeded", result["state"].get("error_code")
    assert len(origin["sent"]) == 9


def test_exact_four_replays_and_five_corrections_only_make_five_new_calls(origin):
    result = target(origin)
    assert result["state"]["status"] == "succeeded", result["state"].get("error_code")
    assert len(origin["sent"]) == 14
    report = inspect_diagnostics(origin["root"], result["run"])
    assert report["actual_call_attempts"] == 5 and report["gaps"] == []
    assert result["state"]["call_budget"]["reservation_count"] == 5
    reservations = result["state"]["call_budget"]["reservations"]
    assert {item["unit_id"] for item in reservations} == {f"WRITE-{i}" for i in range(4, 9)}
    assert all(item["call_attempt"] == 1 for item in reservations)
    output = result["executor"].output(result["run"])
    for identity, value in output["outputs"].items():
        assert value["proposal"]["coverage"] == candidate()["coverage"]
        recorded = result["state"]["nodes"][1]["units"][identity]
        recorded_input = result["executor"].store.read_json(recorded["input_ref"])
        assert ("writing_correction" in recorded_input) is (identity in {f"WRITE-{i}" for i in range(4, 9)})


def test_production_n4_mixed_entry_uses_same_assembler_and_explicit_diagnostic_provenance(origin):
    options = {**origin["options"], "mode": "diagnostic_writing_mixed"}
    settings = {"pipeline": "prepared", "provider": options, "run_purpose": "diagnostic_replay", "debug_capture": True,
                "writing_replay": replay_config(origin["root"], origin["run"])}
    nodes = {node.key: node for node in pipeline.build_executor(origin["root"], settings).nodes}
    executor = Executor(origin["root"], [NodeSpec("N3", "synthetic-target-prefix-v1", lambda ctx, value: value, ()),
                                       nodes["N4"], nodes["N5"]], settings)
    rid = executor.create_run(origin["value"])
    state = executor.execute(rid)
    assert state["status"] == "succeeded", state.get("error_code")
    assert len(origin["sent"]) == 14
    output = executor.output(rid)
    assert output["generation_mode"] == "diagnostic_writing_mixed"
    assert output["external_model_verified"] is False
    assert output["chain_diagnostics"]["publication_status"] == "diagnostic_only"
    assert output["chain_diagnostics"]["writing_replay"]["revalidated_count"] == 4
    assert output["chain_diagnostics"]["writing_replay"]["correction_completed_count"] == 5
    assert len(output["writing_review"]) == 9
    correction_review = next(item for item in output["writing_review"] if item["unit_id"] == "WRITE-4")
    assert correction_review["correction_context_requirement_refs"] == ["R-CONTEXT"]
    expected_scope = replay.build_correction_payload(
        base_inputs(origin["prepared"], options)["WRITE-4"], candidate(),
        settings["writing_replay"]["corrections"]["WRITE-4"], origin["prepared"].model_dump(mode="json"),
    )["writing_correction"]["source_scope"]
    assert correction_review["correction_source_scope_sha256"] == digest(expected_scope)
    correction_blocks = [block for section in output["package"]["sections"] for block in section["blocks"]
                         if "R-4" in block["requirement_refs"]]
    assert correction_blocks and all({"T", "ET"} <= set(block["source_refs"]) for block in correction_blocks)
    assert state["artifact_scope"] == "diagnostic"
    assert inspect_diagnostics(origin["root"], rid)["gaps"] == []


@pytest.mark.parametrize("damage", ["block_sources", "context_refs", "scope_hash", "self_reported_scope"])
def test_n5_rebuilds_correction_scope_and_rejects_untraceable_aggregate_output(origin, damage):
    options = {**origin["options"], "mode": "diagnostic_writing_mixed"}
    settings = {"pipeline": "prepared", "provider": options, "run_purpose": "diagnostic_replay", "debug_capture": True,
                "writing_replay": replay_config(origin["root"], origin["run"])}
    nodes = {node.key: node for node in pipeline.build_executor(origin["root"], settings).nodes}

    def damaged_n4(ctx, value):
        output = nodes["N4"].handler(ctx, value)
        review = next(item for item in output["writing_review"] if item["unit_id"] == "WRITE-4")
        if damage == "block_sources":
            for section in output["package"]["sections"]:
                for block in section["blocks"]:
                    if "R-4" in block["requirement_refs"]:
                        block["source_refs"] = [ref for ref in block["source_refs"] if ref != "ET"]
        elif damage == "context_refs":
            review.pop("correction_context_requirement_refs")
        elif damage == "scope_hash":
            review["correction_source_scope_sha256"] = "0" * 64
        else:
            altered = copy.deepcopy(settings["writing_replay"]["corrections"]["WRITE-4"])
            altered["context_requirement_refs"] = ["R-0"]
            false_scope = replay.build_correction_payload(base_inputs(origin["prepared"], options)["WRITE-4"],
                candidate(), altered, origin["prepared"].model_dump(mode="json"))["writing_correction"]["source_scope"]
            review["correction_context_requirement_refs"] = ["R-0"]
            review["correction_source_scope_sha256"] = digest(false_scope)
        return output

    executor = Executor(origin["root"], [NodeSpec("N3", "synthetic-target-prefix-v1", lambda ctx, value: value, ()),
        NodeSpec("N4", "synthetic-aggregate-fault-injection-v1", damaged_n4, ("provider", "writing_replay")),
        nodes["N5"]], settings)
    rid = executor.create_run(origin["value"])
    state = executor.execute(rid)
    assert state["status"] == "failed"
    assert state["nodes"][1]["status"] == "succeeded"
    assert state["nodes"][2]["status"] == "failed"
    assert len(origin["sent"]) == 14


def test_current_system_prompt_drift_cannot_replay_or_turn_into_a_live_miss(origin, monkeypatch):
    monkeypatch.setattr(replay, "SYSTEM_PROMPT", replay.SYSTEM_PROMPT + "\nChanged frozen writing instructions.")
    result = target(origin)
    assert result["state"]["status"] == "failed"
    assert result["state"]["error_code"] == "WRITING_REPLAY_WIRE_MISMATCH"
    assert len(origin["sent"]) == 9


def test_a_failed_actual_correction_is_not_bought_again_after_provider_restart(origin):
    origin["control"]["reject_next_response"] = True
    result = target(origin, selected=["WRITE-4"])
    assert result["state"]["status"] == "failed"
    assert len(origin["sent"]) == 10 and result["state"]["call_budget"]["reservation_count"] == 1
    result["holder"]["provider"] = replay.DiagnosticWritingProvider(result["options"], result["config"])
    restarted = Executor(origin["root"], result["executor"].nodes, result["settings"])
    state = restarted.resume(result["run"])
    assert state["status"] == "failed"
    assert len(origin["sent"]) == 10
    assert state["call_budget"]["reservation_count"] == 1


def test_resume_reuses_successful_units_and_counts_completion_across_attempts(origin, monkeypatch):
    result = target(origin, interrupt_after_units=True)
    assert result["state"]["status"] == "failed"
    assert len(origin["sent"]) == 14
    assert all(unit["status"] == "succeeded" for unit in result["state"]["nodes"][1]["units"].values())
    checkpoint_refs = {identity: copy.deepcopy(unit["output_ref"])
                       for identity, unit in result["state"]["nodes"][1]["units"].items()}
    result["holder"]["provider"] = replay.DiagnosticWritingProvider(result["options"], result["config"])
    monkeypatch.delenv(origin["options"]["auth_env"])
    restarted = Executor(origin["root"], result["executor"].nodes, result["settings"])
    state = restarted.resume(result["run"])
    assert state["status"] == "succeeded", state.get("error_code")
    assert len(origin["sent"]) == 14
    summary = restarted.output(result["run"])["summary"]
    assert summary["revalidated_count"] == 4
    assert summary["correction_completed_count"] == 5
    assert summary["revalidated_this_attempt_count"] == summary["correction_completed_this_attempt_count"] == 0
    assert summary["replay_checkpoint_reuse_count"] == 4
    assert summary["correction_checkpoint_reuse_count"] == 5
    assert {identity: unit["output_ref"] for identity, unit in state["nodes"][1]["units"].items()} == checkpoint_refs


@pytest.mark.parametrize("damage", ["missing", "extra", "overlap", "wrong_source_unit", "unknown_source",
                                   "max_attempts", "purpose", "mode", "capture_disabled"])
def test_preflight_never_buys_a_live_call_for_an_invalid_declared_plan(origin, damage):
    config = replay_config(origin["root"], origin["run"])
    options = {**origin["options"], "mode": "diagnostic_writing_mixed"}
    purpose = "diagnostic_replay"
    capture = True
    if damage == "missing":
        del config["bindings"]["WRITE-0"]
    elif damage == "extra":
        config["bindings"]["UNDECLARED"] = binding(origin["run"], "UNDECLARED")
    elif damage == "overlap":
        config["bindings"]["WRITE-4"] = binding(origin["run"], "WRITE-4")
    elif damage == "wrong_source_unit":
        config["bindings"]["WRITE-0"]["unit_id"] = "WRITE-1"
    elif damage == "unknown_source":
        config["bindings"]["WRITE-0"]["alias"] = "UNKNOWN"
    elif damage == "max_attempts":
        options["max_attempts"] = 2
    elif damage == "mode":
        options["mode"] = "chat_completions"
    elif damage == "capture_disabled":
        capture = False
    else:
        purpose = "production"
    try:
        result = target(origin, config=config, options=options, run_purpose=purpose, debug_capture=capture)
    except ProviderFailure:
        result = None
    assert result is None or result["state"]["status"] == "failed"
    assert len(origin["sent"]) == 9


@pytest.mark.parametrize("damage", ["title", "source_hash", "tables", "provider_budget", "extra_base_field"])
def test_any_base_unit_input_change_is_fatal_without_live_fallback(origin, damage):
    def mutate(inputs):
        value = inputs["WRITE-0"]
        if damage == "title":
            value["context"]["title"] += " changed"
        elif damage == "source_hash":
            value["sources"][0]["sha256"] = "0" * 64
        elif damage == "tables":
            value["tables"] = [{"table_id": "INVENTED"}]
        elif damage == "provider_budget":
            value["provider"]["max_run_calls"] -= 1
        else:
            value["unrecognized"] = True
    result = target(origin, damage_inputs=mutate)
    assert result["state"]["status"] == "failed"
    assert len(origin["sent"]) == 9


@pytest.mark.parametrize("damage", ["checkpoint", "late_checkpoint", "request_capture", "trace", "usage", "running_source", "physical_source"])
def test_historical_evidence_damage_blocks_every_correction_before_http(origin, damage):
    directory = origin["executor"].store.run_dir(origin["run"])
    state_path = directory / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    node = next(node for node in state["nodes"] if node["key"] == "N4")
    if damage in {"checkpoint", "late_checkpoint"}:
        identity = "WRITE-8" if damage == "late_checkpoint" else "WRITE-0"
        (origin["root"] / node["units"][identity]["output_ref"]["path"]).write_bytes(b"corrupt checkpoint")
    elif damage == "request_capture":
        capture = next(item for attempt in node["attempts"] for item in attempt["captures"] if item["name"].startswith("request-"))
        (origin["root"] / capture["ref"]["path"]).write_bytes(b"corrupt capture")
    elif damage == "trace":
        spans_path = directory / "spans.jsonl"
        spans = rows(spans_path)
        span_id = node["units"]["WRITE-0"]["attempts"][0]["span_id"]
        spans_path.write_text("\n".join(json.dumps(span) for span in spans if span["span_id"] != span_id) + "\n", encoding="utf-8")
    elif damage == "usage":
        state["call_budget"]["reservations"][0]["usage"]["output_tokens"] += 1
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    elif damage == "running_source":
        state["status"] = "running"
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    else:
        origin["prepared"].sources[0].path.write_bytes(b"changed frozen source")
    result = target(origin)
    assert result["state"]["status"] == "failed"
    assert len(origin["sent"]) == 9
