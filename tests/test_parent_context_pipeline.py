"""Synthetic native scope contexts survive real planning, writing and checks.

The source reader output is a synthetic N1 contract. Proposal transports are
in-process stubs; no HTTP, model, OCR or private source data is used.
"""
import copy
import hashlib
import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from company_bid import pipeline
from company_bid.extraction import PlanningError, extract_requirements
from company_bid.models import DocumentPackage, PreparedBid
from company_bid.planning import plan_bid
from company_bid.provider import ChatProposalProvider
from company_bid.runtime import Executor, NodeSpec
from test_source_selected_citations import Context, block, fixture, live_candidate, no_call, selected


OUTER = "B．技术部分不得包含商务报价，包括："
PARENT = "（1）合成服务适用"
FIRST = "①应编写合成检测方案，说明测点编号与复测步骤。"
SECOND = "②应编写合成记录方案，说明异常报告与交付记录。"
PERMISSION = "2.投标文件格式（未提供格式的投标文件可由投标人自定义格式）"
PROPOSAL = "拟逐一核对合成测点编号并记录检查条件，异常先保留记录、复测后按授权边界报告。"


def native_line(parsed, identity, text, *, page=1):
    value = block(parsed, identity, text, page=page, origin="native")
    unit = next(unit for unit in parsed["documents"][0]["units"] if unit["page"] == page)
    value["locator"] = f"pdf/page[{page}]/line[{len(unit['blocks']) - 1}]"
    return value


def source_data(tmp_path, *, sibling=False, unknown_parent=False, tail=False, child_procedure=False, cost_table=False):
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"
    native_line(parsed, "REGION", "第六章 投标文件格式")
    native_line(parsed, "COMPOSITION", "1.投标文件组成，包括以下合成材料：")
    native_line(parsed, "A", "A．商务材料包括：")
    native_line(parsed, "QUOTE", "（1）应填写技术成本控制表，记录30天的计划周期。" if cost_table else
                "（1）应按《合成报价表》提交报价表。")
    native_line(parsed, "B", OUTER)
    native_line(parsed, "PARENT", "（1）合成货物适用" if unknown_parent else PARENT)
    native_line(parsed, "FIRST", FIRST)
    if sibling:
        native_line(parsed, "SIBLING", "（2）测试服务适用")
    native_line(parsed, "SECOND", "①" + SECOND[1:] if sibling else SECOND)
    if tail:
        native_line(parsed, "TAIL", "合成文件须在约定时限内递交。")
    native_line(parsed, "PERMISSION", PERMISSION)
    heading = native_line(parsed, "FORM", "一、技术成本控制表" if cost_table else "一、合成报价表", page=2)
    native_line(parsed, "FORM_NOTE", "本合成模板保留记录说明。", page=2)
    block(parsed, "GRID", "", kind="table", page=2, origin="native",
          rows=[["控制项", "计划周期（天）"], ["合成复测", "30"]] if cost_table else [["报价（元）", ""]],
          merges=[], column_widths=[1, 2], header_rows=[])
    native_line(parsed, "END", "第七章 合同事项", page=3)
    source_path = Path(manifest["sources"][0]["path"])
    source_path.write_text("\n".join(value.get("text", "") for unit in parsed["documents"][0]["units"]
                                     for value in unit["blocks"]), encoding="utf-8")
    source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    manifest["sources"][0]["sha256"] = source_hash
    parsed["documents"][0]["sha256"] = source_hash

    def candidate(ctx, task, unit, payload, validate):
        values = {value["block_id"]: value for value in payload["window"]["blocks"]}
        raw = live_candidate(payload, selected(next(iter(values.values()))))
        raw["requirements"] = []
        for identity in ("QUOTE", "FIRST", "SECOND", "TAIL", "PERMISSION"):
            if identity not in values:
                continue
            item = {"citation": selected(values[identity]), "format_status": "not_prescribed"}
            if identity == "QUOTE":
                item.update(category="fixed_table" if cost_table else "commercial", response_mode="table", format_status="prescribed",
                            format_title=heading["text"])
            elif identity in {"FIRST", "SECOND"}:
                item.update(category="procedure" if child_procedure else "technical",
                            response_mode="procedure" if child_procedure else "writing")
            else:
                item.update(category="procedure", response_mode="procedure")
            raw["requirements"].append(item)
        raw["format_chapters"] = []
        if "FORM" in values:
            raw["format_chapters"] = [{"title": heading["text"], "level": 1,
                "citation": selected(values["FORM"]), "content_block_ids": ["FORM_NOTE", "GRID"],
                "requirement_indices": [i for i, item in enumerate(raw["requirements"])
                                        if item["response_mode"] == "table"]}]
        raw["format_status"] = "specified" if raw["format_chapters"] else "none_verified"
        return validate(raw)

    extracted = extract_requirements(Context(), parsed, manifest, candidate, {})
    return manifest, parsed, extracted


def planned(tmp_path, **options):
    manifest, parsed, extracted = source_data(tmp_path, **options)
    original = copy.deepcopy(extracted)
    result = plan_bid(Context(), extracted, parsed, manifest, no_call, {"map_table_fields": False})
    assert extracted == original
    return result, extracted


def writing_requirements(prepared):
    return [item for item in prepared["requirements"] if item["response_mode"] == "writing"]


def task_proposal(task):
    return {"paragraphs": [PROPOSAL], "steps": [],
            "coverage": [{"item_index": i, "paragraph_indices": [0]}
                         for i in range(len(task["must_cover"]))]}


def runner(tmp_path, monkeypatch, *, provider_mode="prepared", provider_change=None, output_change=None, cost_table=False):
    manifest, parsed, extracted = source_data(tmp_path, cost_table=cost_table)
    calls = []
    config = {"provider": {"mode": "prepared"}, "debug_capture": True}
    if provider_mode == "offline_chat":
        config["provider"] = {"mode": "chat_completions", "endpoint": "https://offline.invalid/chat/completions",
            "model": "synthetic-offline", "auth_env": "PARENT_CONTEXT_TEST_SECRET", "max_attempts": 1,
            "max_run_calls": 4, "max_run_usd": 1}
        monkeypatch.setenv("PARENT_CONTEXT_TEST_SECRET", "SYNTHETIC_NOT_A_CREDENTIAL")

        def transport(endpoint, payload, secret, timeout):
            request = json.loads(payload["messages"][-1]["content"])
            calls.append(copy.deepcopy(request))
            value = task_proposal(request["section"]["writing_task"])
            return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value, ensure_ascii=False)}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}}

        def factory(options):
            provider = ChatProposalProvider(options, transport=transport, sleep=lambda _: None)
            if provider_change:
                original = provider.generate

                def generate(ctx, section, requirements):
                    section, requirements = copy.deepcopy(section), copy.deepcopy(requirements)
                    provider_change(section, requirements)
                    return original(ctx, section, requirements)
                provider.generate = generate
            return provider
        monkeypatch.setattr(pipeline, "ChatProposalProvider", factory)

    base = pipeline.build_executor(tmp_path / "runtime", config)

    def n3(ctx, value):
        prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {"map_table_fields": False})
        proposals = {task["unit_id"]: task_proposal(task) for section in prepared["sections"]
                     for task in section["writing_tasks"]}
        return {"prepared": prepared, "proposals": proposals, "source_artifacts": [], "input_kind": "automatic"}

    nodes = [NodeSpec("N3", "synthetic-native-parent-context-N3", n3, ())]
    for node in base.nodes:
        if node.key not in {"N4", "N5"}:
            continue
        if node.key == "N4" and output_change:
            def changed(ctx, value, handler=node.handler):
                output = handler(ctx, value)
                output_change(output)
                return output
            nodes.append(NodeSpec(node.key, "synthetic-mutated-context-output", changed, node.config_keys))
        else:
            nodes.append(node)
    executor = Executor(base.store.root, nodes, base.execution_config)
    run = executor.create_run({"synthetic": True})
    return executor, run, calls


def test_n3_keeps_child_quotes_and_parent_source_refs_independent(tmp_path):
    prepared, extracted = planned(tmp_path)
    requirements = writing_requirements(prepared)
    assert [item["text"] for item in requirements] == [FIRST, SECOND]
    original = {item["requirement_id"]: item for item in extracted["requirements"]}
    assert prepared["source_context_proofs"]
    for item in requirements:
        assert item["text"] == original[item["requirement_id"]]["text"]
        contexts = item["source_contexts"]
        assert {context["operator"] for context in contexts} == {"exclude_commercial_quotation", "service_applicability"}
        assert {context["text"] for context in contexts} == {OUTER, PARENT}
        assert all(context["review_status"] == "not_checked" for context in contexts)
        assert all(set(context["source_refs"]).isdisjoint(item["source_refs"]) for context in contexts)
        assert all(context["text"] not in {fact["value"] for fact in prepared["facts"]} for context in contexts)
        task, = [task for section in prepared["sections"] for task in section["writing_tasks"]
                 if item["requirement_id"] in task["requirement_refs"]]
        assert task["must_cover"] == [item["text"]]
        assert set(task["source_context_refs"]) == {context["context_id"] for context in contexts}
        assert any(item["requirement_id"] in issue["requirement_refs"] and issue["severity"] == "warning"
                   for issue in prepared["issues"])
        assert all(item["requirement_id"] not in action["requirement_refs"] for action in prepared["review_actions"])


@pytest.mark.parametrize("provider_mode", ["prepared", "offline_chat"])
def test_contexts_survive_real_n3_n4_n5_and_remain_pending(tmp_path, monkeypatch, provider_mode):
    executor, run, calls = runner(tmp_path, monkeypatch, provider_mode=provider_mode)
    state = executor.execute(run)
    assert state["status"] == "succeeded", state.get("error_code")
    prepared = executor.output(run, "N3")["prepared"]
    output = executor.output(run, "N5")
    package = output["package"]
    assert package["source_context_proofs"] == prepared["source_context_proofs"]
    checks = output["validation"]
    assert checks["pending_context_review_count"] > 0
    children = writing_requirements(prepared)
    for item in children:
        actual = next(value for value in package["requirements"] if value["requirement_id"] == item["requirement_id"])
        assert actual["source_contexts"] == item["source_contexts"] and actual["text"] == item["text"]
        row = next(row for row in checks["requirement_coverage"] if row["requirement_id"] == item["requirement_id"])
        assert row["body_status"] == "drafted_for_review"
        assert row["applicability_status"] == "not_checked"
        assert row["satisfaction_status"] == "not_determined"
        assert row["status"] != "no_body_required"
        blocks = [value for section in package["sections"] for value in section["blocks"]
                  if value["kind"] == "paragraph" and value.get("content_role") == "proposal"
                  and item["requirement_id"] in value["requirement_refs"]]
        assert blocks
        parent_refs = {ref for context in item["source_contexts"] for ref in context["source_refs"]}
        assert all(parent_refs <= set(value["source_refs"]) for value in blocks)
    assert checks["source_context_review"]
    if provider_mode == "offline_chat":
        assert len(calls) == 2
        for request in calls:
            context_ids = {value["context_id"] for value in request["section"]["source_contexts"]}
            assert context_ids == set(request["section"]["writing_task"]["source_context_refs"])
            assert "container_members" not in json.dumps(request)
            assert all(value["review_status"] == "not_checked" for value in request["section"]["source_contexts"])
    else:
        assert calls == []


@pytest.mark.parametrize("mutation", ["drop_context", "change_text", "change_proof"])
def test_n4_context_tampering_fails_before_offline_transport(tmp_path, monkeypatch, mutation):
    def change(section, requirements):
        if mutation == "drop_context":
            section["source_contexts"].pop()
        elif mutation == "change_text":
            section["source_contexts"][0]["text"] += "未获证实的其他范围。"
        else:
            section["source_contexts"][0]["proof_sha256"] = "0" * 64
    executor, run, calls = runner(tmp_path, monkeypatch, provider_mode="offline_chat", provider_change=change)
    state = executor.execute(run)
    assert state["status"] == "failed"
    assert state["nodes"][1]["status"] == "failed"
    assert state["error_code"] == ("WRITING_SOURCE_CONTEXT_MISMATCH" if mutation == "drop_context" else "WRITING_CONTEXT_INVALID")
    assert calls == []


@pytest.mark.parametrize("mutation", ["parent_text", "missing_proof", "wrong_task_refs", "missing_task",
                                    "parent_locator", "child_locator", "parent_offset", "child_offset"])
def test_prepared_model_rejects_changed_parent_proof_or_task_binding(tmp_path, mutation):
    prepared, _ = planned(tmp_path)
    child = writing_requirements(prepared)[0]
    if mutation == "parent_text":
        child["source_contexts"][0]["text"] += "改变父条件。"
    elif mutation == "missing_proof":
        prepared["source_context_proofs"] = []
    elif mutation == "wrong_task_refs":
        task = next(task for section in prepared["sections"] for task in section["writing_tasks"])
        task["source_context_refs"] = []
    elif mutation == "missing_task":
        section = next(section for section in prepared["sections"] if section["writing_tasks"])
        section["writing_tasks"] = []
    else:
        identity = child["source_contexts"][0]["source_refs"][0] if mutation.startswith("parent") else child["source_refs"][0]
        source = next(source for source in prepared["sources"] if source["source_id"] == identity)
        if mutation.endswith("locator"):
            source["locator"] = "pdf/page[999]/line[0];" + source["locator"].split(";", 1)[1]
        else:
            changed, count = re.subn(r"chars\[\d+:\d+\]", "chars[1:2]", source["locator"])
            assert count == 1
            source["locator"] = changed
    with pytest.raises(ValidationError):
        PreparedBid.model_validate(prepared)


def test_a_context_from_a_sibling_condition_cannot_be_borrowed_by_another_task(tmp_path):
    prepared, _ = planned(tmp_path, sibling=True)
    tasks = [task for section in prepared["sections"] for task in section["writing_tasks"]]
    assert set(tasks[0]["source_context_refs"]) != set(tasks[1]["source_context_refs"])
    tasks[1]["source_context_refs"] = list(tasks[0]["source_context_refs"])
    with pytest.raises(ValidationError):
        PreparedBid.model_validate(prepared)


@pytest.mark.parametrize("source_option", ["unknown_parent", "tail"])
def test_unknown_parent_or_unnumbered_tail_still_blocks_n3(tmp_path, source_option):
    manifest, parsed, extracted = source_data(tmp_path, **{source_option: True})
    ctx = Context()
    with pytest.raises(PlanningError, match="REQUIRED_FORMAT_UNRESOLVED"):
        plan_bid(ctx, extracted, parsed, manifest, no_call, {"map_table_fields": False})
    errors = ctx.captures["n3-format-coverage"]["errors"]
    assert any("scope" in item["reason"] for item in errors)


def test_typed_parent_context_does_not_allow_procedure_to_replace_technical_body(tmp_path):
    manifest, parsed, extracted = source_data(tmp_path, child_procedure=True)
    ctx = Context()
    with pytest.raises(PlanningError, match="REQUIRED_FORMAT_UNRESOLVED"):
        plan_bid(ctx, extracted, parsed, manifest, no_call, {"map_table_fields": False})
    errors = ctx.captures["n3-format-coverage"]["errors"]
    assert any(item["reason"] == "preparation_instruction_response_role_invalid" for item in errors)


def test_n5_rejects_missing_context_after_a_successful_writing_step(tmp_path, monkeypatch):
    mutated = []
    def drop(output):
        child = next(value for value in output["package"]["requirements"] if value["response_mode"] == "writing")
        child["source_contexts"] = []
        mutated.append(copy.deepcopy(output["package"]))
    executor, run, _ = runner(tmp_path, monkeypatch, output_change=drop)
    state = executor.execute(run)
    assert state["status"] == "failed"
    assert state["nodes"][1]["status"] == "succeeded"
    assert state["nodes"][2]["status"] == "failed"
    assert state["error_code"] == "NODE_EXECUTION_FAILED"
    with pytest.raises(ValidationError, match="parents cannot disappear"):
        DocumentPackage.model_validate(mutated[0])


@pytest.mark.parametrize("quote_role", ["commercial", "fixed_table_table", "fixed_table_fixed_text"])
def test_n5_rejects_known_quotation_table_placed_in_a_technical_context(tmp_path, monkeypatch, quote_role):
    mutated = []
    def move_quote(output):
        package = output["package"]
        technical_id = next(item["requirement_id"] for item in package["requirements"] if item["response_mode"] == "writing")
        technical = next(section for section in package["sections"] if technical_id in section["requirement_refs"])
        origin = next(section for section in package["sections"] if any(value["kind"] == "table" for value in section["blocks"]))
        table = next(value for value in origin["blocks"] if value["kind"] == "table")
        if quote_role.startswith("fixed_table"):
            structural = next(item for item in package["requirements"] if item["category"] == "fixed_table")
            structural["response_mode"] = "table" if quote_role == "fixed_table_table" else "fixed_text"
            table["table"]["requirement_refs"] = [structural["requirement_id"]]
            table["requirement_refs"] = [structural["requirement_id"]]
        origin["blocks"].remove(table)
        table["table"]["section_id"] = technical["section_id"]
        technical["blocks"].append(table)
        mutated.append(copy.deepcopy(package))
    executor, run, _ = runner(tmp_path, monkeypatch, output_change=move_quote)
    state = executor.execute(run)
    assert state["status"] == "failed"
    assert state["nodes"][1]["status"] == "succeeded"
    assert state["nodes"][2]["status"] == "failed"
    assert state["error_code"] == "NODE_EXECUTION_FAILED"
    with pytest.raises(ValidationError, match="forbids commercial quotation"):
        DocumentPackage.model_validate(mutated[0])


def test_n5_does_not_confuse_technical_cost_control_or_schedule_numbers_with_a_quote(tmp_path, monkeypatch):
    def move_cost_table(output):
        package = output["package"]
        technical_id = next(item["requirement_id"] for item in package["requirements"] if item["response_mode"] == "writing")
        technical = next(section for section in package["sections"] if technical_id in section["requirement_refs"])
        origin = next(section for section in package["sections"] if any(value["kind"] == "table" for value in section["blocks"]))
        table = next(value for value in origin["blocks"] if value["kind"] == "table")
        assert table["table"]["title"] == "一、技术成本控制表"
        origin["blocks"].remove(table)
        table["table"]["section_id"] = technical["section_id"]
        technical["blocks"].append(table)
    executor, run, _ = runner(tmp_path, monkeypatch, output_change=move_cost_table, cost_table=True)
    state = executor.execute(run)
    assert state["status"] == "succeeded", state.get("error_code")
    result = executor.output(run, "N5")
    assert result["validation"]["pending_context_review_count"] > 0
    assert any(cell["text"] == "30" for section in result["package"]["sections"] for block in section["blocks"]
               if block["kind"] == "table" for row in block["table"]["rows"] for cell in row)
