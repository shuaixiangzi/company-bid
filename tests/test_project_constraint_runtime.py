"""Executor N3/N4 provenance gates and synthetic HTTP, never real candidates.

N1/N2 data are explicitly synthetic; N2 is locally source-validated before it
is frozen by the executor. These cases make no claim about whole real N2.
"""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from company_bid import pipeline, project_constraint_runtime
from company_bid.diagnostics import inspect_diagnostics
from company_bid.extraction import extract_requirements
from company_bid.runtime import CheckpointInvalidError, Executor, NodeSpec
from company_bid.storage import digest
from test_joint_deviation_formats import native_grid
from test_n3_source_roles import line
from test_source_selected_citations import Context, fixture, selected


def source_case(tmp_path, *, option="☑不允许偏差", methods=1, derived=False):
    if derived:
        from test_preparation_rule_pipeline import fixture as preparation_fixture
        return preparation_fixture(tmp_path)
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"
    native_grid(parsed, "CHOICES", [["条款号", "条款名称", "编列内容"],
        ["1.9", "分包", "☑不允许"], ["1.10.2", "对非关键条款的偏差", option],
        ["2.1", "其他事项", "保留原始行"]], page=1, widths=[50.0, 120.0, 280.0])
    for index in range(methods):
        line(parsed, f"METHOD-{index}", f"应编制合成对象{index + 1}的巡检方法，说明检查、异常复测和记录移交。", page=index + 2)
    path = Path(manifest["sources"][0]["path"])
    path.write_text("Synthetic N1 native-row fixture; no real tender or parser claim.", encoding="utf-8")
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest["sources"][0]["sha256"] = parsed["documents"][0]["sha256"] = source_hash

    def call(ctx, task, unit, payload, validate):
        window = payload["window"]
        requirements = []
        for row in window.get("source_table_row_catalog", []):
            if row["quote"].startswith(("1.9\t", "1.10.2\t")):
                requirements.append({"citation": {key: row[key] for key in
                    ("source_id", "block_id", "locator", "block_ids", "table_row_id")},
                    "category": "commercial", "response_mode": "procedure", "format_status": "not_prescribed"})
        for block in window["blocks"]:
            if block["block_id"].startswith("METHOD-"):
                requirements.append({"citation": selected(block), "category": "technical", "response_mode": "writing",
                                     "format_status": "not_prescribed"})
        return validate({"window_id": window["window_id"], "processing_declaration": "processed_not_semantically_accepted",
            "unresolved_block_ids": [], "requirements": requirements, "format_chapters": [], "format_status": "none_verified"})

    extracted = extract_requirements(Context(), parsed, manifest, call, {"window_max_chars": 50000, "window_max_blocks": 100})
    return manifest, parsed, extracted


def executor_case(tmp_path, monkeypatch, *, option="☑不允许偏差", methods=1, derived=False,
                  fail_call=None, n4_output_damage=None):
    manifest, parsed, extracted = source_case(tmp_path, option=option, methods=methods, derived=derived)
    calls = []
    attempts = []
    failure = {"enabled": fail_call is not None}
    monkeypatch.setenv("PROJECT_CONSTRAINT_TEST_KEY", "offline-synthetic-key")

    def transport(endpoint, payload, secret, timeout):
        assert endpoint == "https://offline.invalid/chat/completions"
        assert secret == "offline-synthetic-key"
        data = json.loads(payload["messages"][1]["content"])
        attempts.append(data["section"]["section_id"])
        if failure["enabled"] and len(attempts) == fail_call:
            failure["enabled"] = False
            raise TimeoutError("Synthetic transport interruption")
        calls.append({"wire": copy.deepcopy(data), "system": payload["messages"][0]["content"]})
        result = {"paragraphs": ["拟对合成对象逐项检查，异常按授权范围复测，复核记录后移交。"], "steps": [],
            "coverage": [{"item_index": index, "paragraph_indices": [0], "step_indices": []}
                         for index in range(len(data["section"]["writing_task"]["must_cover"]))]}
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}

    actual_provider = pipeline.ChatProposalProvider

    def provider(config):
        return actual_provider(config, transport=transport, sleep=lambda _: None)

    monkeypatch.setattr(pipeline, "ChatProposalProvider", provider)
    config = {"pipeline": "automatic", "debug_capture": True, "planning": {"map_table_fields": False},
        "provider": {"mode": "chat_completions", "endpoint": "https://offline.invalid/chat/completions",
            "model": "offline-synthetic", "auth_env": "PROJECT_CONSTRAINT_TEST_KEY", "max_attempts": 1,
            "max_run_calls": 10, "max_run_usd": 5, "max_input_chars": 100000}}
    root = tmp_path / "runtime"
    base = pipeline.build_executor(root, config)

    def n1(ctx, value):
        return {"manifest": manifest, "parsed": parsed, "source_artifacts": [], "proposals": {}, "input_kind": "automatic"}

    def n2(ctx, value):
        return {**value, "extracted": extracted}

    nodes = [NodeSpec("N1", "synthetic-n1-v1", n1, ()), NodeSpec("N2", "synthetic-source-validated-n2-v1", n2, ()),
             *(node for node in base.nodes if node.key in {"N3", "N4", "N5"})]
    if n4_output_damage:
        original = next(node for node in nodes if node.key == "N4")

        def damaged_n4(ctx, value):
            output = original.handler(ctx, value)
            output["writing_review"][0].pop("project_constraint_bindings")
            if n4_output_damage == "review_and_expected":
                output["expected_project_constraint_bindings"] = {}
            return output

        nodes[nodes.index(original)] = NodeSpec("N4", original.version, damaged_n4, original.config_keys)
    executor = Executor(root, nodes, config)
    run = executor.create_run({"synthetic": True, "scope": "Synthetic N1/N2 and actual N3/N4 handlers only"})
    state = executor.execute(run, stop_after="N3")
    assert state["status"] == "paused_debug", state.get("error_code")
    assert attempts == calls == []
    return executor, run, calls, attempts, (manifest, parsed, extracted), failure


def plan_artifacts(executor, run):
    state = executor.inspect(run)
    node = next(item for item in state["nodes"] if item["key"] == "N3")
    unit = node["units"]["plan-assemble"]
    return unit, executor.store.read_json(unit["input_ref"]), executor._checkpoint_data(unit)


def test_actual_n4_http_wire_has_independent_n3_proofs_and_source_target_hashes(tmp_path, monkeypatch):
    executor, run, calls, attempts, (_, parsed, extracted), _ = executor_case(tmp_path, monkeypatch)
    unit, plan_input, assembled = plan_artifacts(executor, run)
    report = assembled["report"]["project_constraints"]
    assert report["parsed_sha256"] == digest(parsed)
    assert report["requirements_sha256"] == digest(extracted["requirements"])
    assert plan_input["extracted"] == extracted
    assert executor.execute(run, stop_after="N4")["status"] == "paused_debug"
    assert len(calls) == len(attempts) == 1
    wire = calls[0]["wire"]
    task = wire["section"]["writing_task"]
    bindings = wire["section"]["project_constraints"]
    assert bindings == report["tasks"][task["unit_id"]]
    assert len(bindings) == 1 and len(bindings[0]["constraints"]) == 2
    assert bindings[0]["unresolved"] == []
    assert bindings[0]["source_set_sha256"] == report["sources"][0]["source_set_sha256"]
    assert bindings[0]["source_reading_sha256"] == digest(parsed["documents"][0])
    for target in bindings[0]["target_requirements"]:
        source_req = next(item for item in extracted["requirements"] if item["requirement_id"] == target["requirement_id"])
        assert target["requirement_sha256"] == digest(source_req)
    for constraint in bindings[0]["constraints"]:
        assert constraint["forbidden_field"] in task["forbidden_fields"]
        assert constraint["source"]["source_sha256"] == parsed["documents"][0]["sha256"]
        assert constraint["source"]["parent_grid"]["rows"][-1][-1] == "保留原始行"
    assert "不得自动填写无偏差" in calls[0]["system"]
    n4 = next(item for item in executor.inspect(run)["nodes"] if item["key"] == "N4")
    unit_input = executor.store.read_json(n4["units"][task["unit_id"]]["input_ref"])
    assert unit_input["project_constraint_artifact_refs"]["N3_plan_output"] == unit["output_ref"]
    assert unit_input["project_constraint_artifact_refs"]["N3_plan_input"] == unit["input_ref"]
    assert unit_input["project_constraint_plan_sha256"] == digest(report)
    assert unit_input["context"]["project_constraints"] == bindings
    assert inspect_diagnostics(executor.store.root, run)["gaps"] == []


def test_actual_wire_keeps_one_prohibition_and_the_unsupported_sibling_partition(tmp_path, monkeypatch):
    executor, run, calls, _, _, _ = executor_case(tmp_path, monkeypatch, option="□不允许偏差\n☑允许偏差")
    assert executor.execute(run, stop_after="N4")["status"] == "paused_debug"
    binding, = calls[0]["wire"]["section"]["project_constraints"]
    assert [item["operator"] for item in binding["constraints"]] == ["prohibit_subcontracting"]
    assert binding["unresolved"][0]["operator"] == "prohibit_noncritical_deviation"
    assert binding["unresolved"][0]["detection_status"] == "unsupported"
    assert binding["applicability_status"] == "not_checked"
    assert binding["source_reading_sha256"] and binding["source_set_sha256"]


def test_n3_derived_source_rules_are_retained_in_the_independent_n4_reconstruction(tmp_path, monkeypatch):
    executor, run, calls, _, (manifest, _, extracted), _ = executor_case(tmp_path, monkeypatch, derived=True)
    _, _, assembled = plan_artifacts(executor, run)
    rules = assembled["report"]["preparation_source_rules"]
    derived = [item["derived_requirement"] for item in rules if item.get("derived_requirement")]
    assert derived and all(item["citations"] for item in derived)
    assert all(digest(item["source_role_proof"]) == item["source_role_proof_sha256"]
               for item in rules if item.get("derived_requirement"))
    prepared_requirements = {item["requirement_id"]: item for item in assembled["prepared"]["requirements"]}
    prepared_sources = {item["source_id"]: item for item in assembled["prepared"]["sources"]}
    for item in derived:
        actual = prepared_requirements[item["requirement_id"]]
        assert actual["text"] == item["text"]
        assert all(citation["quote"] == item["text"] for citation in item["citations"])
        assert all(prepared_sources[ref]["sha256"] == manifest["sources"][0]["sha256"] for ref in actual["source_refs"])
        quote_hash = hashlib.sha256(item["text"].encode()).hexdigest()
        assert any("quote_sha256=" + quote_hash in prepared_sources[ref]["locator"] for ref in actual["source_refs"])
    assert all(item["requirement_id"] not in {q["requirement_id"] for q in extracted["requirements"]} for item in derived)
    assert assembled["report"]["project_constraints"]["requirements_sha256"] == digest([*extracted["requirements"], *derived])
    assert executor.execute(run, stop_after="N4")["status"] == "paused_debug"
    assert len(calls) == 1
    binding, = calls[0]["wire"]["section"]["project_constraints"]
    assert binding["constraints"] == []
    assert [item["detection_status"] for item in binding["unresolved"]] == ["not_detected", "not_detected"]


@pytest.mark.parametrize("damage", ["proof", "unresolved", "forbidden", "source_reading_hash", "task_identity"])
def test_actual_context_serialization_loss_fails_before_http_and_keeps_independent_expected(tmp_path, monkeypatch, damage):
    executor, run, calls, attempts, _, _ = executor_case(tmp_path, monkeypatch, option="□不允许偏差")
    unit, _, frozen = plan_artifacts(executor, run)
    original_hash = hashlib.sha256((executor.store.root / unit["output_ref"]["path"]).read_bytes()).hexdigest()
    validate = project_constraint_runtime.validate_project_constraint_context
    observations = []

    def damage_actual(task, context, provenance):
        expected = copy.deepcopy(provenance["plan"]["tasks"][task["unit_id"]])
        if damage == "proof": context["project_constraints"][0]["constraints"].pop()
        elif damage == "unresolved": context["project_constraints"][0]["unresolved"] = []
        elif damage == "forbidden":
            prohibited = {item["forbidden_field"] for item in expected[0]["constraints"]}
            context["writing_task"]["forbidden_fields"] = [value for value in context["writing_task"]["forbidden_fields"]
                                                        if value not in prohibited]
        elif damage == "source_reading_hash": context["project_constraints"][0].pop("source_reading_sha256")
        elif damage == "task_identity": context["writing_task"]["unit_id"] = "wrong-task"
        observations.append(provenance["plan"]["tasks"][task["unit_id"]] == expected)
        return validate(task, context, provenance)

    monkeypatch.setattr(project_constraint_runtime, "validate_project_constraint_context", damage_actual)
    state = executor.execute(run, stop_after="N4")
    assert state["status"] == "failed"
    assert state["error_code"] == "PROJECT_CONSTRAINT_TASK_BINDING_MISSING_OR_CHANGED"
    assert calls == attempts == []
    assert state.get("call_budget", {}).get("reservation_count", 0) == 0
    assert observations == [True]
    assert inspect_diagnostics(executor.store.root, run)["actual_call_attempts"] == 0
    assert hashlib.sha256((executor.store.root / unit["output_ref"]["path"]).read_bytes()).hexdigest() == original_hash
    assert executor._checkpoint_data(unit) == frozen


@pytest.mark.parametrize("damage,code", [
    ("n1_reading", "PROJECT_CONSTRAINT_PLAN_INPUT_CHANGED"),
    ("n2_source_requirement", "PROJECT_CONSTRAINT_PLAN_INPUT_CHANGED"),
    ("n3_report_missing", "PROJECT_CONSTRAINT_PLAN_INVALID"),
    ("n3_proof_removed", "PROJECT_CONSTRAINT_PLAN_BINDING_CHANGED"),
    ("derived_requirement_changed", "PROJECT_CONSTRAINT_DERIVED_SOURCE_CHANGED"),
    ("derived_requirement_rehashed", "PROJECT_CONSTRAINT_DERIVED_SOURCE_CHANGED"),
])
def test_n4_rechecks_decoded_source_and_planunit_relations_before_http(tmp_path, monkeypatch, damage, code):
    # Fault-inject after the actual store read/integrity check. This isolates
    # the consumer's independent relationship guard from the storage checksum.
    executor, run, calls, attempts, (_, _, extracted), _ = executor_case(
        tmp_path, monkeypatch, derived=damage.startswith("derived_requirement"))
    decode = executor._checkpoint_data

    def decoded(record):
        value = decode(record)
        if damage == "n1_reading" and record.get("key") == "N1":
            value["parsed"]["documents"][0]["units"][0]["blocks"][0]["rows"][1][2] = "□不允许"
        elif damage == "n2_source_requirement" and record.get("key") == "N2":
            value["extracted"]["requirements"][0]["response_mode"] = "factual_response"
        elif record.get("unit_id") == "plan-assemble":
            if damage == "n3_report_missing": value["report"].pop("project_constraints")
            elif damage == "n3_proof_removed": value["report"]["project_constraints"]["sources"][0]["constraints"].pop()
            elif damage.startswith("derived_requirement"):
                derived = next(item["derived_requirement"] for item in value["report"]["preparation_source_rules"]
                               if item.get("derived_requirement"))
                derived["text"] += "伪造附加条件"
                if damage == "derived_requirement_rehashed":
                    # This paired change passed the old requirements-digest
                    # comparison. The original N1 and prepared requirement
                    # must remain independent authorities after rehashing.
                    all_derived = [rule["derived_requirement"] for rule in value["report"]["preparation_source_rules"]
                                   if rule.get("derived_requirement")]
                    value["report"]["project_constraints"]["requirements_sha256"] = digest(
                        [*extracted["requirements"], *all_derived])
        return value

    monkeypatch.setattr(executor, "_checkpoint_data", decoded)
    state = executor.execute(run, stop_after="N4")
    assert state["status"] == "failed" and state["error_code"] == code
    assert calls == attempts == []
    assert state.get("call_budget", {}).get("reservation_count", 0) == 0
    assert inspect_diagnostics(executor.store.root, run)["actual_call_attempts"] == 0


@pytest.mark.parametrize("damage", ["N1", "N2", "N3_plan_input", "N3_plan_output"])
def test_immutable_upstream_artifact_damage_is_rejected_by_real_executor_before_http(tmp_path, monkeypatch, damage):
    executor, run, calls, attempts, _, _ = executor_case(tmp_path, monkeypatch)
    state = executor.inspect(run)
    nodes = {node["key"]: node for node in state["nodes"]}
    if damage in {"N1", "N2"}:
        reference = nodes[damage]["output_ref"]
    else:
        reference = nodes["N3"]["units"]["plan-assemble"]["input_ref" if damage.endswith("input") else "output_ref"]
    path = executor.store.root / reference["path"]
    value = json.loads(path.read_text(encoding="utf-8"))
    if damage == "N1": value["data"]["parsed"]["documents"][0]["units"][0]["blocks"][0]["rows"][1][2] = "□不允许"
    elif damage == "N2": value["data"]["extracted"]["requirements"][0]["text"] += "改变原要求"
    elif damage.endswith("input"): value["extracted"]["requirements"] = []
    else: value["data"]["report"].pop("project_constraints")
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(CheckpointInvalidError):
        executor.execute(run, stop_after="N4")
    assert executor.inspect(run)["error_code"] == "CHECKPOINT_INVALID"
    assert executor.inspect(run).get("call_budget", {}).get("reservation_count", 0) == 0
    assert calls == attempts == []


def test_actual_executor_resume_reuses_successful_writing_unit_without_another_http_request(tmp_path, monkeypatch):
    executor, run, calls, attempts, _, _ = executor_case(tmp_path, monkeypatch, methods=2, fail_call=2)
    first = executor.execute(run, stop_after="N4")
    assert first["status"] == "failed" and len(calls) == 1 and len(attempts) == 2
    first_task, failed_task = attempts
    n4 = next(node for node in first["nodes"] if node["key"] == "N4")
    successful = copy.deepcopy(n4["units"][first_task])
    assert successful["status"] == "succeeded"
    restarted = Executor(executor.store.root, executor.nodes, executor.execution_config)
    resumed = restarted.resume(run, stop_after="N4")
    assert resumed["status"] == "paused_debug", resumed.get("error_code")
    assert attempts == [first_task, failed_task, failed_task]
    assert len(calls) == 2
    actual = next(node for node in resumed["nodes"] if node["key"] == "N4")["units"][first_task]
    assert actual == successful
    diagnostics = inspect_diagnostics(executor.store.root, run)
    assert diagnostics["actual_call_attempts"] == 3
    # The deliberately interrupted HTTP attempt remains honestly partial;
    # recovery must not erase that historical capture state.
    assert [gap["code"] for gap in diagnostics["gaps"]] == ["CAPTURE_INCOMPLETE"]
    assert diagnostics["gaps"][0]["name"].startswith("response-error-")
    assert diagnostics["gaps"][0]["status"] == "partial"
    assert restarted.resume(run)["status"] == "succeeded"
    assert len(attempts) == 3


@pytest.mark.parametrize("damage", ["review", "review_and_expected"])
def test_actual_n5_uses_n3_expected_when_n4_loses_review_and_its_claimed_expected(tmp_path, monkeypatch, damage):
    executor, run, calls, attempts, _, _ = executor_case(tmp_path, monkeypatch, n4_output_damage=damage)
    state = executor.execute(run)
    assert state["status"] == "failed"
    assert state["error_code"] == "PROJECT_CONSTRAINT_REVIEW_BINDING_MISSING_OR_CHANGED"
    assert len(calls) == len(attempts) == 1
    nodes = {node["key"]: node for node in state["nodes"]}
    assert nodes["N4"]["status"] == "succeeded" and nodes["N5"]["status"] == "failed"
    assert nodes["N3"]["units"]["plan-assemble"]["status"] == "succeeded"

