"""Synthetic source-bound letters; no private inputs or real HTTP calls."""
from __future__ import annotations

import copy
import json
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from company_bid import commitment_drafting as commitments
from company_bid import pipeline
from company_bid import writing_candidate_import as imports
from company_bid.commitment_prompt import PENDING_LABEL, PROMPT_VERSION, SYSTEM_PROMPT
from company_bid.commitment_planning import commitment_nonwriting_heading_refs
from company_bid.models import PreparedBid, file_sha256
from company_bid.provider import ChatProposalProvider, ProviderFailure
from company_bid.runtime import Executor, NodeSpec
from company_bid.storage import digest
from test_writing_candidate_import import packet  # synthetic pinned 9/5/87 fixture


_GENERATE_JSON = ChatProposalProvider.generate_json
CLAUSES = {
    "Q": "9 质量保证承诺书\n（格式自拟，要求响应人法人代表或授权代表签字并加盖公章）",
    "A": "10 售后服务承诺\n（格式自拟，要求响应人法人代表或授权代表签字并加盖公章）",
    "W": "8.2本合同质量保证期为自技术服务成果验收合格后12个月。质量不符合标准，免费修改和改正并承担鉴定及其他一切有关费用；不修改和改正且不承担费用可扣除质量保证金（如有）并承担违约责任。",
    "C": "(6) 乙方不得拒绝与本技术服务项目有关的咨询，必须及时、明确书面或双方认可的方式答复。",
    "F": "(7) 项目结束后应为相关报告内容理解和实施问题提供免费服务，具体包括： 。",
    "R": "8.3技术服务成果验收之时返还甲方全部资料，不得任何形式留存，但履约必要且甲方书面同意的除外。",
}


def add_letters(packet):
    prepared = copy.deepcopy(packet["prepared"])
    source = packet["tmp"] / "synthetic-commitment-source.txt"
    source.write_text("\n".join(CLAUSES.values()), encoding="utf-8")
    prepared["sources"].append({"source_id": "COMMIT-SOURCE", "title": "Synthetic contract clauses",
        "path": str(source), "sha256": file_sha256(source), "locator": "synthetic/clauses",
        "kind": "tender", "provenance": "synthetic", "synthetic": True})
    for rid, text in CLAUSES.items():
        prepared["requirements"].append({"requirement_id": "R-COMMIT-" + rid, "text": text,
            "category": "commercial", "source_refs": ["COMMIT-SOURCE"], "response_mode": "fixed_text"})
    prepared["sections"].append({"section_id": "CONTRACT", "title": "Synthetic contract", "kind": "commercial",
        "requirement_refs": ["R-COMMIT-" + rid for rid in ("W", "C", "F", "R")],
        "fixed_content": [{"kind": "paragraph", "text": "\n".join(CLAUSES[rid] for rid in ("W", "C", "F", "R")),
                           "source_refs": ["COMMIT-SOURCE"]}]})
    for rid, title in (("Q", "9 质量保证承诺书"), ("A", "10 售后服务承诺")):
        prepared["sections"].append({"section_id": "LETTER-" + rid, "title": title, "kind": "facts",
                                     "requirement_refs": ["R-COMMIT-" + rid], "source_refs": ["COMMIT-SOURCE"]})
    plans = commitments.plan_commitment_tasks(prepared)
    for section in prepared["sections"]:
        if section["section_id"] in plans:
            task = plans[section["section_id"]]
            section.update(kind="technical", requirement_refs=task.requirement_refs,
                           writing_tasks=[task.model_dump(mode="json")])
    return PreparedBid.model_validate(prepared).model_dump(mode="json")


def candidate(payload):
    request = json.loads(payload["messages"][-1]["content"])
    task = request["section"]["writing_task"]
    identity = request["proposed_commitment"]["identity"]
    company, project = identity["company"]["value"]["name"], identity["project"]["value"]["name"]
    return {"paragraphs": [PENDING_LABEL + f"为{project}拟按以下条款作出承诺，待公司确认后签署。",
        *["拟按原文执行：" + text for text in task["must_cover"]],
        f"拟登记问题、分派责任岗位、复核结果并反馈。响应人：{company}；代表：【待签字】；日期：【待填写】；【待盖公章】"],
        "steps": [], "coverage": [{"item_index": i, "paragraph_indices": [i + 1], "step_indices": []}
                                   for i in range(len(task["must_cover"]))]}


@pytest.fixture
def hybrid(packet, monkeypatch):
    packet["prepared"] = add_letters(packet)
    options = {**packet["options"], "mode": commitments.MODE}
    direct = {**packet["options"], "mode": "chat_completions", "max_output_tokens": 4000}
    config = {"pipeline": "prepared", "provider": options, "writing_candidate_import": packet["config"],
        "run_purpose": "diagnostic_replay", "debug_capture": True,
        "commitment_drafting": {"schema_version": commitments.SCHEMA_VERSION, "profile": commitments.PROFILE,
            "allowed_units": commitments.commitment_allowlist(packet["prepared"]), "direct_provider": direct}}
    control = {"sent": [], "constructed": 0, "fail_second": False}
    monkeypatch.setenv(direct["auth_env"], "synthetic-never-real")

    class OfflineProvider(ChatProposalProvider):
        generate_json = _GENERATE_JSON

        def __init__(self, config):
            control["constructed"] += 1
            def transport(endpoint, payload, secret, timeout):
                control["sent"].append(copy.deepcopy(payload))
                if control["fail_second"] and len(control["sent"]) == 2:
                    raise ProviderFailure("SYNTHETIC_SECOND_CALL_FAILURE")
                return {"id": "synthetic-response", "choices": [{"finish_reason": "stop", "message": {
                    "content": json.dumps(control.get("candidate_factory", candidate)(payload), ensure_ascii=False)}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 300, "total_tokens": 400}}
            super().__init__(config, transport=transport, sleep=lambda _: None)

    monkeypatch.setattr(commitments, "ChatProposalProvider", OfflineProvider)
    return {**packet, "settings": config, "control": control}


def run(hybrid, *, n5_damage=None, stop_after=None):
    root, config = hybrid["tmp"] / "partition-runtime", hybrid["settings"]
    production = pipeline.build_executor(root, config)
    n4 = next(node for node in production.nodes if node.key == "N4")
    n5 = next(node for node in production.nodes if node.key == "N5")
    def check(ctx, value):
        if n5_damage:
            n5_damage(ctx, value)
        return n5.handler(ctx, value)
    # The production draft/check implementations, with a synthetic frozen N3.
    executor = Executor(root, [NodeSpec("N3", "synthetic-n3", lambda ctx, value: value, ()), n4,
                              NodeSpec("N5", n5.version, check, n5.config_keys)], config)
    rid = executor.create_run({"prepared": hybrid["prepared"], "proposals": {}, "source_artifacts": []})
    return executor, rid, executor.execute(rid, stop_after=stop_after)


def test_plan_is_source_bound_idempotent_and_preserves_nine_tasks(packet):
    old = copy.deepcopy(packet["prepared"]["sections"][0]["writing_tasks"])
    assert commitments.plan_commitment_tasks(packet["prepared"]) == {}
    prepared = add_letters(packet)
    plans = commitments.plan_commitment_tasks(prepared)
    assert len(plans) == 2
    assert prepared["sections"][0]["writing_tasks"] == old
    for task in plans.values():
        joined = "\n".join(task.must_cover)
        assert "12个月" in joined and "甲方书面同意" in joined and "具体包括： 。" in joined
        assert task.operation == "bounded_new_draft" and PENDING_LABEL in task.completion_criteria
        assert not set(task.requirement_refs) & {"R-COMMIT-" + ref for ref in ("W", "C", "F", "R")}
        readonly = commitments.commitment_source_scope(prepared, task)
        assert {"R-COMMIT-" + ref for ref in ("W", "C", "F", "R")} <= {
            item["requirement_id"] for item in readonly["requirements"]}
    changed = copy.deepcopy(prepared)
    next(item for item in changed["requirements"] if item["requirement_id"] == "R-COMMIT-F")["text"] = "无免费服务来源。"
    with pytest.raises(ProviderFailure, match="SOURCE_SCOPE_INCOMPLETE"):
        commitments.plan_commitment_tasks(changed)


def test_nine_imports_two_live_calls_have_independent_trace_cost_and_pending_status(hybrid):
    executor, rid, state = run(hybrid)
    assert state["status"] == "succeeded", state.get("error_code")
    output = executor.output(rid, "N5")
    assert len(hybrid["control"]["sent"]) == 2
    assert len(state["call_budget"]["reservations"]) == 2
    report = output["validation"]["commitment_partition"]
    assert report["historical_unit_count"] == 9 and report["new_unit_count"] == 2
    assert len(report["historical_correction_links"]) == 5
    assert len(report["new_actual_call_links"]) == 2
    assert report["usage"]["new_known_usage_estimate_usd"] == "0.00078"
    assert report["usage"]["historical_calls_charged_again"] == 0
    assert len(report["usage"]["historical_usage"]) == 9
    assert report["usage"]["new_unknown_reserved_upper_usd"] == "0"
    for payload in hybrid["control"]["sent"]:
        assert payload["messages"][0]["content"] == SYSTEM_PROMPT
        sent = json.loads(payload["messages"][-1]["content"])
        assert sent["coverage_contract"]["expected_item_indices"] == list(
            range(len(sent["section"]["writing_task"]["must_cover"])))
        assert len(sent["coverage_contract"]["expected_item_indices"]) > len(sent["requirements"])
    for review in output["writing_review"]:
        if review["unit_id"].startswith("COMMIT-"):
            assert review["commitment_drafting"]["company_confirmation"] == "pending"
            assert review["commitment_drafting"]["identity_sha256"] == digest(commitments.commitment_identity(hybrid["prepared"]))
            assert review["commitment_drafting"]["identity_source_refs"] == ["T"]
            assert set(review["commitment_drafting"]["readonly_requirement_refs"]) == {
                "R-COMMIT-" + ref for ref in ("W", "C", "F", "R")}
            assert review["commitment_drafting"]["readonly_context_satisfies_requirements"] is False
        else:
            assert "commitment_drafting" not in review


@pytest.mark.parametrize("damage", ["old_task", "unknown_extra", "new_task", "allowlist", "source", "candidate", "retry", "identity", "readonly_clause", "readonly_into_task"])
def test_partition_preflight_rejects_before_constructing_or_calling_live_provider(hybrid, damage):
    if damage == "old_task":
        hybrid["prepared"]["sections"][0]["writing_tasks"][0]["objective"] += "修改"
    elif damage == "unknown_extra":
        task = copy.deepcopy(hybrid["prepared"]["sections"][0]["writing_tasks"][0])
        task["unit_id"] = "WRITE-UNAUTHORIZED"
        hybrid["prepared"]["sections"][0]["writing_tasks"].append(task)
    elif damage == "new_task":
        hybrid["prepared"]["sections"][-1]["writing_tasks"][0]["objective"] += "修改"
    elif damage == "allowlist":
        hybrid["settings"]["commitment_drafting"]["allowed_units"]["UNKNOWN"] = {}
    elif damage == "source":
        Path(hybrid["prepared"]["sources"][-1]["path"]).write_text("changed source", encoding="utf-8")
    elif damage == "candidate":
        payload = json.loads(hybrid["path"].read_text(encoding="utf-8"))
        payload["entries"]["WRITE-0"]["candidate"]["paragraphs"][0] += "改写"
        hybrid["path"].write_text(json.dumps(payload), encoding="utf-8")
    elif damage == "retry":
        hybrid["settings"]["commitment_drafting"]["direct_provider"]["max_attempts"] = 2
    elif damage == "identity":
        hybrid["prepared"]["company"]["name"] = "Changed identity"
    elif damage == "readonly_clause":
        next(item for item in hybrid["prepared"]["requirements"] if item["requirement_id"] == "R-COMMIT-W")["text"] += "新条件"
    else:
        section = hybrid["prepared"]["sections"][-1]
        section["requirement_refs"].append("R-COMMIT-F")
        section["writing_tasks"][0]["requirement_refs"].append("R-COMMIT-F")
    _, _, state = run(hybrid)
    assert state["status"] == "failed"
    assert hybrid["control"]["constructed"] == 0
    assert hybrid["control"]["sent"] == []


def test_resume_does_not_repurchase_successful_letter_or_nine_imported_units(hybrid):
    hybrid["control"]["fail_second"] = True
    executor, rid, state = run(hybrid)
    assert state["status"] == "failed"
    n4 = next(node for node in state["nodes"] if node["key"] == "N4")
    first = next(uid for uid, record in n4["units"].items() if uid.startswith("COMMIT-") and record["status"] == "succeeded")
    saved = copy.deepcopy(n4["units"][first])
    state = executor.execute(rid)
    assert state["status"] == "succeeded", state.get("error_code")
    n4 = next(node for node in state["nodes"] if node["key"] == "N4")
    assert n4["units"][first]["output_ref"] == saved["output_ref"]
    assert len(n4["units"][first]["attempts"]) == 1
    assert len(hybrid["control"]["sent"]) == 3
    assert len(state["call_budget"]["reservations"]) == 3


def test_resume_rejects_changed_successful_unit_trace_before_any_more_http(hybrid):
    hybrid["control"]["fail_second"] = True
    executor, rid, state = run(hybrid)
    assert state["status"] == "failed"
    path = executor.store.run_dir(rid) / "spans.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    span = next(row for row in rows if row["name"] == "provider.call" and row["status"] != "ERROR")
    span["parent_span_id"] = "f" * 16
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    before = len(hybrid["control"]["sent"])
    state = executor.execute(rid)
    assert state["status"] == "failed"
    assert len(hybrid["control"]["sent"]) == before


def test_step_to_n4_then_resume_n5_preserves_both_successful_calls(hybrid):
    executor, rid, state = run(hybrid, stop_after="N4")
    assert state["status"] == "paused_debug"
    assert len(hybrid["control"]["sent"]) == 2
    state = executor.execute(rid)
    assert state["status"] == "succeeded", state.get("error_code")
    assert len(hybrid["control"]["sent"]) == 2


@pytest.mark.parametrize("damage", ["trace", "candidate", "review", "source", "identity"])
def test_n5_rejects_changed_live_evidence(hybrid, damage):
    def corrupt(ctx, value):
        uid = next(item["unit_id"] for item in value["writing_review"] if item["unit_id"].startswith("COMMIT-"))
        if damage == "trace":
            path = ctx.executor.store.run_dir(ctx.run_id) / "spans.jsonl"
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            span = next(row for row in rows if row["name"] == "provider.call" and row["attributes"].get("unit_id") == uid)
            span["attributes"]["prompt_version"] = "tampered"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        elif damage == "candidate":
            section = next(item for item in value["package"]["sections"] if item["section_id"] == "LETTER-Q")
            section["blocks"][1]["text"] += "篡改正文"
        elif damage == "review":
            next(item for item in value["writing_review"] if item["unit_id"] == uid)["commitment_drafting"]["company_confirmation"] = "confirmed"
        elif damage == "source":
            Path(hybrid["prepared"]["sources"][-1]["path"]).write_text("changed", encoding="utf-8")
        else:
            value["package"]["company"]["name"] = "Changed package identity"
    _, _, state = run(hybrid, n5_damage=corrupt)
    assert state["status"] == "failed"
    assert len(hybrid["control"]["sent"]) == 2


@pytest.mark.parametrize("extra", ["保证7×24服务", "我方已承诺", "保证48小时到场"])
def test_extra_unsupported_promise_rejected(hybrid, extra):
    prepared = PreparedBid.model_validate(hybrid["prepared"])
    section = prepared.sections[-1]
    task = section.writing_tasks[0]
    base = commitments.build_partition_unit_input(prepared, section, task, hybrid["settings"]["provider"], None,
                                                  {}, hybrid["settings"]["commitment_drafting"])
    config = commitments.ChatConfig(**{key: value for key, value in base["provider"].items() if key != "mode"})
    payload = commitments.build_json_request(config, commitments.commitment_payload(base), SYSTEM_PROMPT)
    value = candidate(payload)
    value["paragraphs"].append(extra)
    with pytest.raises(ProviderFailure):
        commitments.validate_commitment_candidate(base, value)


def test_known_company_is_not_replaced_by_an_unknown_marker(hybrid):
    prepared = PreparedBid.model_validate(hybrid["prepared"])
    section, task = prepared.sections[-1], prepared.sections[-1].writing_tasks[0]
    base = commitments.build_partition_unit_input(prepared, section, task, hybrid["settings"]["provider"], None,
                                                  {}, hybrid["settings"]["commitment_drafting"])
    config = commitments.ChatConfig(**{key: value for key, value in base["provider"].items() if key != "mode"})
    wire = commitments.build_json_request(config, commitments.commitment_payload(base), SYSTEM_PROMPT)
    value = candidate(wire)
    value["paragraphs"] = [text.replace(prepared.company.name, "【待公司确认】") for text in value["paragraphs"]]
    with pytest.raises(ProviderFailure, match="KNOWN_IDENTITY_REQUIRED"):
        commitments.validate_commitment_candidate(base, value)


def test_only_independently_proved_project_constraint_suffix_is_accepted(hybrid):
    prepared = hybrid["prepared"]
    new_tasks = [task for section in prepared["sections"] for task in section["writing_tasks"]
                 if task["unit_id"].startswith("COMMIT-")]
    provenance = {"plan": {"tasks": {task["unit_id"]: [{"constraints": [
        {"forbidden_field": "Synthetic independently proved prohibition"}]}] for task in new_tasks}}}
    for task in new_tasks:
        task["forbidden_fields"].append("Synthetic independently proved prohibition")
    config = hybrid["settings"]["commitment_drafting"]
    config["allowed_units"] = commitments.commitment_allowlist(prepared)
    assert len(commitments.checked_commitment_partition(prepared, config, provenance)) == 2
    new_tasks[0]["forbidden_fields"].append("Unproved extra instruction")
    config["allowed_units"] = commitments.commitment_allowlist(prepared)
    with pytest.raises(ProviderFailure, match="TASK_CHANGED"):
        commitments.checked_commitment_partition(prepared, config, provenance)


def test_only_exact_source_hashed_format_heading_is_nonwriting(hybrid):
    prepared = copy.deepcopy(hybrid["prepared"])
    section = next(item for item in prepared["sections"] if item["section_id"] == "LETTER-Q")
    section.update(kind="facts", writing_tasks=[])
    text = section["title"]
    source = {**prepared["sources"][-1], "source_id": "HEADING-SOURCE",
        "locator": f"synthetic/heading; basis=native_block_text_v1; chars[0:{len(text)}]; quote_sha256=" +
                   hashlib.sha256(text.encode()).hexdigest()}
    prepared["sources"].append(source)
    requirement = {"requirement_id": "R-FORM-SYNTHETIC", "text": text, "category": "procedure",
                   "response_mode": "fixed_text", "source_refs": ["HEADING-SOURCE"]}
    prepared["requirements"].append(requirement)
    section["requirement_refs"].append(requirement["requirement_id"])
    assert commitment_nonwriting_heading_refs(prepared)["LETTER-Q"] == ["R-FORM-SYNTHETIC"]
    task = commitments.plan_commitment_tasks(prepared)["LETTER-Q"]
    assert "R-FORM-SYNTHETIC" not in task.requirement_refs and text in task.must_cover
    requirement["text"] += "：包含实际填写义务"
    assert commitment_nonwriting_heading_refs(prepared)["LETTER-Q"] == []
    requirement["text"] = text
    source["locator"] = source["locator"].replace(hashlib.sha256(text.encode()).hexdigest(), "f" * 64)
    with pytest.raises(ProviderFailure, match="FORMAT_HEADING_PROOF_INVALID"):
        commitment_nonwriting_heading_refs(prepared)


def _review_operation(base):
    if base["context"]["title"].startswith("售后服务"):
        return {"kind": "remove_exact_coverage_contract_echo_v1", "instruction": commitments.ECHO_INSTRUCTION}
    scope = base["commitment_drafting"]["source_scope"]["requirements"]
    return {"kind": "separate_acceptance_warranty_step_v1", "instruction": commitments.REVIEW_INSTRUCTION,
        "step_index": 3, "original_step": commitments.ORIGINAL_MERGED_STEP,
        "replacement_step": commitments.REVIEWED_STEP,
        "source_requirements": {"acceptance": next(item for item in scope if "验收程序" in item["text"]),
            "warranty": next(item for item in scope if "质量保证期" in item["text"] and "验收合格" in item["text"])}}


def _write_review_packet(executor, rid, path):
    """Read an immutable original run, retaining both successful and failed calls."""
    from company_bid.project_constraint_runtime import load_project_constraints
    state = executor.store.read_state(rid)
    ctx = SimpleNamespace(executor=executor, _state=state, run_id=rid, execution_config=state["execution_config"])
    evidence = commitments._current_evidence(ctx)
    origin = {"root": str(executor.store.root), "run_id": rid}
    for name in ("state", "events", "spans"):
        source = executor.store.run_dir(rid) / (name + (".json" if name == "state" else ".jsonl"))
        origin[name + "_sha256"] = file_sha256(source)
    frozen = executor.output(rid, "N3")
    prepared = PreparedBid.model_validate(frozen["prepared"])
    constraints = load_project_constraints(ctx, frozen["prepared"])
    cfg = state["execution_config"]
    entries = {}
    for section in prepared.sections:
        for task in section.writing_tasks:
            if task.unit_id not in cfg["commitment_drafting"]["allowed_units"]:
                continue
            base = commitments.build_partition_unit_input(prepared, section, task, cfg["provider"], constraints,
                                                          frozen["proposals"], cfg["commitment_drafting"])
            success = task.title.startswith("质量保证")
            node, record, actual_input, _, attempt = evidence.unit("N4", task.unit_id, successful=success)
            assert actual_input == base
            attempt = attempt or record["attempts"][-1]
            event = next(row for row in evidence.events if row.get("unit_attempt_id") == attempt["attempt_id"]
                         and row["event"] == ("call.completed" if success else "call.failed"))
            relation = evidence.call(node, task.unit_id, attempt, event["call_id"], 1, passed=success)
            operation = _review_operation(base)
            edited = commitments._reviewed_transform(base, relation["candidate"], operation)
            entries[task.unit_id] = {"raw_candidate": relation["candidate"], "raw_candidate_sha256": digest(relation["candidate"]),
                "candidate": edited, "candidate_sha256": digest(edited), "operation": operation,
                "unit_input_hash": record["input_hash"], "request_sha256": digest(relation["request"]),
                "response_sha256": digest(relation["response"]), "origin_status": record["status"],
                **{key: event[key] for key in ("call_id", "trace_id", "span_id")}}
    packet = {"schema_version": commitments.REVIEW_PACKET_SCHEMA, "origin": origin, "entries": entries}
    path.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
    return packet


@pytest.fixture
def reviewed(hybrid, monkeypatch):
    prepared = hybrid["prepared"]
    warranty = next(item for item in prepared["requirements"] if item["requirement_id"] == "R-COMMIT-W")
    warranty["text"] = ("8.2本合同质量保证期为自技术服务成果验收合格后12个月。甲方在质量保证期间发现"
        "服务成果不符合本合同规定的质量标准，乙方免费修改和改正并承担鉴定及其他一切有关费用；"
        "不修改和改正且不承担费用可扣除质量保证金（如有）并承担违约责任。")
    acceptance = {"requirement_id": "R-COMMIT-E", "text": "8.1(2)验收程序：验收不合格，乙方按甲方意见免费进行修改或重新制作或编制，并按合同约定期限重新提请验收。",
                  "category": "commercial", "response_mode": "fixed_text", "source_refs": ["COMMIT-SOURCE"]}
    prepared["requirements"].append(acceptance)
    contract = next(item for item in prepared["sections"] if item["section_id"] == "CONTRACT")
    contract["requirement_refs"].append(acceptance["requirement_id"])
    contract["fixed_content"][0]["text"] = "\n".join(item["text"] for item in prepared["requirements"]
                                                      if item["requirement_id"] in contract["requirement_refs"])
    source = prepared["sources"][-1]
    Path(source["path"]).write_text("\n".join(item["text"] for item in prepared["requirements"]
                                              if "COMMIT-SOURCE" in item["source_refs"]), encoding="utf-8")
    source["sha256"] = file_sha256(Path(source["path"]))
    plans = commitments.plan_commitment_tasks(prepared)
    for section in prepared["sections"]:
        if section["section_id"] in plans:
            section["writing_tasks"] = [plans[section["section_id"]].model_dump(mode="json")]
    hybrid["prepared"] = prepared = PreparedBid.model_validate(prepared).model_dump(mode="json")
    hybrid["settings"]["commitment_drafting"]["allowed_units"] = commitments.commitment_allowlist(prepared)
    def original(payload):
        result = candidate(payload)
        request = json.loads(payload["messages"][-1]["content"])
        if request["section"]["title"].startswith("质量保证"):
            result["steps"] = ["接收登记。", "分派责任岗位。", "核对来源。", commitments.ORIGINAL_MERGED_STEP]
        else:
            result["coverage_contract"] = request["coverage_contract"]
        return result
    hybrid["control"]["candidate_factory"] = original
    executor, rid, state = run(hybrid)
    assert state["status"] == "failed" and state["error_code"] == "PROVIDER_SCHEMA_INVALID"
    path = hybrid["tmp"] / "reviewed-candidates.json"
    packet = _write_review_packet(executor, rid, path)
    config = copy.deepcopy(state["execution_config"])
    config["commitment_drafting"]["reviewed_candidates"] = {"path": str(path), "sha256": file_sha256(path)}
    def forbid(*args, **kwargs):
        pytest.fail("Reviewed import must not construct a live provider or read its key")
    monkeypatch.setattr(commitments, "ChatProposalProvider", forbid)
    monkeypatch.delenv(config["commitment_drafting"]["direct_provider"]["auth_env"])
    return {**hybrid, "executor": executor, "origin_id": rid, "origin_state": state,
            "review_packet": packet, "review_path": path, "review_config": config}


def run_reviewed(reviewed, *, n5_damage=None, stop_after=None):
    original = reviewed["executor"]
    cfg = reviewed["review_config"]
    production = pipeline.build_executor(original.store.root, cfg)
    n4, n5 = (next(node for node in production.nodes if node.key == key) for key in ("N4", "N5"))
    def check(ctx, value):
        if n5_damage:
            n5_damage(ctx, value)
        return n5.handler(ctx, value)
    executor = Executor(original.store.root, [original.nodes[0], n4,
        NodeSpec("N5", n5.version, check, n5.config_keys)], cfg)
    rid = executor.fork(reviewed["origin_id"], "N4")
    return executor, rid, executor.execute(rid, stop_after=stop_after)


def test_reviewed_two_exact_edits_are_offline_pending_and_keep_failed_origin(reviewed):
    executor, rid, state = run_reviewed(reviewed, stop_after="N4")
    assert state["status"] == "paused_debug", state.get("error_code")
    saved = copy.deepcopy(next(node for node in state["nodes"] if node["key"] == "N4")["units"])
    state = executor.execute(rid)
    assert state["status"] == "succeeded", state.get("error_code")
    assert next(node for node in state["nodes"] if node["key"] == "N4")["units"] == saved
    assert len(reviewed["control"]["sent"]) == 2 and reviewed["control"]["constructed"] == 1
    assert not state.get("call_budget", {}).get("reservations")
    report = executor.output(rid, "N5")["validation"]["commitment_partition"]
    assert report["new_actual_call_links"] == [] and len(report["reviewed_new_unit_links"]) == 2
    assert report["historical_unit_count"] == 9
    assert report["usage"]["reviewed_imports"]["new_external_calls"] == 0
    assert report["usage"]["reviewed_imports"]["historical_calls_charged_again"] == 0
    assert {row["origin_status"] for row in report["reviewed_new_unit_links"]} == {"succeeded", "failed"}
    for link in report["reviewed_new_unit_links"]:
        assert link["company_confirmation"] == "pending" and link["satisfaction_status"] == "not_determined"
    assert executor.store.read_state(reviewed["origin_id"]) == reviewed["origin_state"]


def _reassembled_reviewed(reviewed, mutation=None, *, rebind=True):
    original = reviewed["executor"]
    frozen = original.output(reviewed["origin_id"], "N3")
    prepared = frozen["prepared"]
    # A non-writing response table is repaired independently of all 11 tasks.
    prepared["tables"].append({"table_id": "REPAIRED-HISTORY", "section_id": "CONTRACT",
        "title": "Historical project statistics", "columns": ["项目名称"],
        "rows": [[{"text": "待补历史项目，不能填本次项目名", "fact_refs": [], "issue_refs": []}]],
        "source_refs": ["COMMIT-SOURCE"], "requirement_refs": ["R-COMMIT-W"]})
    next(section for section in prepared["sections"] if section["section_id"] == "CONTRACT")["table_refs"].append("REPAIRED-HISTORY")
    if mutation:
        mutation(prepared)
    frozen["prepared"] = PreparedBid.model_validate(prepared).model_dump(mode="json")
    n3 = NodeSpec("N3", original.nodes[0].version, lambda ctx, value: frozen, ())
    executor = Executor(original.store.root, [n3, *original.nodes[1:]], reviewed["review_config"])
    child = executor.create_run(frozen)
    state = executor.execute(child, stop_after="N3")
    assert state["status"] == "paused_debug"
    config = copy.deepcopy(reviewed["review_config"])
    if rebind:
        config["commitment_drafting"]["reviewed_candidates"]["source_rebind"] = {
            "version": "exact-writing-inputs-v1",
            "origin_n3_output_sha256": reviewed["origin_state"]["nodes"][0]["output_hash"],
            "current_n3_output_sha256": state["nodes"][0]["output_hash"]}
    return {**reviewed, "executor": executor, "origin_id": child, "review_config": config}


def test_reviewed_rebind_preserves_candidates_after_unrelated_table_repair(reviewed):
    changed = _reassembled_reviewed(reviewed)
    executor, rid, state = run_reviewed(changed)
    assert state["status"] == "succeeded", state.get("error_code")
    links = executor.output(rid, "N5")["validation"]["commitment_partition"]["reviewed_new_unit_links"]
    assert len(links) == 2
    for link in links:
        proof = link["source_rebind"]
        assert proof["origin_n3_output_sha256"] != proof["current_n3_output_sha256"]
        assert proof["unchanged_request_sha256"] == link["request_sha256"]
    assert len(reviewed["control"]["sent"]) == 2
    assert not state.get("call_budget", {}).get("reservations")
    assert executor.store.read_state(reviewed["origin_id"]) == reviewed["origin_state"]


def test_reviewed_changed_n3_requires_explicit_pinned_rebind(reviewed):
    changed = _reassembled_reviewed(reviewed, rebind=False)
    _, _, state = run_reviewed(changed)
    assert state["status"] == "failed" and state["error_code"] == "COMMITMENT_REVIEW_N3_PREFIX_CHANGED"


@pytest.mark.parametrize("damage", ["origin_pin", "current_pin", "version", "task", "identity", "clause", "config"])
def test_reviewed_rebind_rejects_changed_pins_and_writing_inputs(reviewed, damage):
    def mutate(prepared):
        if damage == "task":
            prepared["sections"][-1]["writing_tasks"][0]["objective"] += "改变写作目标"
        elif damage == "identity":
            prepared["company"]["name"] = "Changed company"
        elif damage == "clause":
            next(row for row in prepared["requirements"] if row["requirement_id"] == "R-COMMIT-W")["text"] += "新增承诺。"
    changed = _reassembled_reviewed(reviewed, mutate)
    spec = changed["review_config"]["commitment_drafting"]["reviewed_candidates"]["source_rebind"]
    if damage == "origin_pin":
        spec["origin_n3_output_sha256"] = "f" * 64
    elif damage == "current_pin":
        spec["current_n3_output_sha256"] = "f" * 64
    elif damage == "version":
        spec["version"] = "loose-reuse"
    elif damage == "config":
        changed["review_config"]["commitment_drafting"]["direct_provider"]["max_output_tokens"] += 1
    _, _, state = run_reviewed(changed)
    assert state["status"] == "failed"
    assert len(reviewed["control"]["sent"]) == 2
    assert not state.get("call_budget", {}).get("reservations")


def test_review_rebind_comparison_ignores_only_artifact_locations():
    old = {"project_constraint_artifact_refs": {"N3": "old"},
        "project_constraint_plan_sha256": "plan", "context": {"text": "required"}}
    current = copy.deepcopy(old)
    current["project_constraint_artifact_refs"] = {"N3": "new"}
    assert commitments._same_writing_input(old, current)
    current["project_constraint_plan_sha256"] = "changed"
    assert not commitments._same_writing_input(old, current)
    current["project_constraint_plan_sha256"] = "plan"
    current["context"]["text"] += "changed"
    assert not commitments._same_writing_input(old, current)


def test_reviewed_resume_reuses_first_success_without_another_import(reviewed, monkeypatch):
    original = commitments.CommitmentPartitionProvider.generate
    seen = []
    def interrupt_second(provider, ctx, context, requirements):
        if context["section_id"] in provider._reviewed:
            seen.append(context["section_id"])
            if len(seen) == 2:
                raise ProviderFailure("SYNTHETIC_OFFLINE_INTERRUPTION")
        return original(provider, ctx, context, requirements)
    monkeypatch.setattr(commitments.CommitmentPartitionProvider, "generate", interrupt_second)
    executor, rid, state = run_reviewed(reviewed)
    assert state["status"] == "failed" and state["error_code"] == "SYNTHETIC_OFFLINE_INTERRUPTION"
    first = seen[0]
    saved = copy.deepcopy(next(node for node in state["nodes"] if node["key"] == "N4")["units"][first])
    state = executor.execute(rid)
    assert state["status"] == "succeeded", state.get("error_code")
    assert seen.count(first) == 1 and len(seen) == 3
    assert next(node for node in state["nodes"] if node["key"] == "N4")["units"][first] == saved
    assert not state.get("call_budget", {}).get("reservations")
    assert len(reviewed["control"]["sent"]) == 2


@pytest.mark.parametrize("text", ['{"entries":{},"entries":{}}', '{"value":NaN}'])
def test_review_packet_rejects_ambiguous_json(text):
    with pytest.raises(ProviderFailure):
        commitments._strict_json(text)


@pytest.mark.parametrize("damage", ["raw", "raw_hash", "step", "paragraph", "coverage", "source", "operation", "schema", "origin_hash", "input_hash", "wire_hash", "config"])
def test_reviewed_packet_tamper_is_rejected_before_offline_execution_or_live_construction(reviewed, damage):
    packet = reviewed["review_packet"]
    entry = next(row for row in packet["entries"].values() if row["origin_status"] == "succeeded")
    if damage == "raw":
        entry["raw_candidate"]["paragraphs"][0] += "changed"
        entry["raw_candidate_sha256"] = digest(entry["raw_candidate"])
    elif damage == "raw_hash":
        entry["raw_candidate_sha256"] = "f" * 64
    elif damage == "step":
        entry["candidate"]["steps"][3] += "任何时候重新制作。"
    elif damage == "paragraph":
        entry["candidate"]["paragraphs"][1] += "changed"
    elif damage == "coverage":
        entry["candidate"]["coverage"][0]["paragraph_indices"] = [0]
    elif damage == "source":
        entry["operation"]["source_requirements"]["warranty"]["text"] += "changed"
    elif damage == "operation":
        entry["operation"]["replacement_step"] += "changed"
    elif damage == "schema":
        packet["schema_version"] += "changed"
    elif damage == "origin_hash":
        packet["origin"]["state_sha256"] = "f" * 64
    elif damage == "input_hash":
        entry["unit_input_hash"] = "f" * 64
    elif damage == "wire_hash":
        entry["request_sha256"] = "f" * 64
    else:
        reviewed["review_config"]["commitment_drafting"]["direct_provider"]["max_output_tokens"] += 1
    entry["candidate_sha256"] = digest(entry["candidate"])
    reviewed["review_path"].write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
    reviewed["review_config"]["commitment_drafting"]["reviewed_candidates"]["sha256"] = file_sha256(reviewed["review_path"])
    _, _, state = run_reviewed(reviewed)
    assert state["status"] == "failed"
    assert len(reviewed["control"]["sent"]) == 2
    assert not state.get("call_budget", {}).get("reservations")


@pytest.mark.parametrize("damage", ["trace", "candidate", "review", "origin_response"])
def test_n5_independently_rejects_reviewed_import_tampering(reviewed, damage):
    def corrupt(ctx, value):
        uid = next(item["unit_id"] for item in value["writing_review"] if item["unit_id"].startswith("COMMIT-"))
        if damage == "trace":
            path = ctx.executor.store.run_dir(ctx.run_id) / "spans.jsonl"
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            next(row for row in rows if row["name"] == "candidate.reviewed_import")["links"][0]["span_id"] = "f" * 16
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        elif damage == "candidate":
            next(item for item in value["package"]["sections"] if item["section_id"] == "LETTER-Q")["blocks"][1]["text"] += "changed"
        elif damage == "review":
            next(item for item in value["writing_review"] if item["unit_id"] == uid)["commitment_drafting"]["reviewed_candidate"]["origin_status"] = "confirmed"
        else:
            node = next(item for item in reviewed["origin_state"]["nodes"] if item["key"] == "N4")
            capture = next(item for attempt in node["attempts"] for item in attempt["captures"]
                           if item["name"].startswith("response-"))
            path = ctx.executor.store.root / capture["ref"]["path"]
            path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    _, _, state = run_reviewed(reviewed, n5_damage=corrupt)
    assert state["status"] == "failed"
    assert len(reviewed["control"]["sent"]) == 2
