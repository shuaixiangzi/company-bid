"""Real Executor/gateway integration for N1-bound material-slot correction.

Documents, provider responses and credentials are entirely synthetic. The N2
node executes one selected window, and does not claim complete extraction.
"""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from company_bid.diagnostics import inspect_diagnostics
from company_bid.extraction import _index, _windows, extraction_window_payload, run_extraction_window, validate_live_window
from company_bid.runtime import Executor, NodeContext, NodeSpec, call_budget_summary
from company_bid.storage import digest
from company_bid.structured import JsonTaskGateway, _MATERIAL_RESPONSE_FIELDS
from test_diagnostic_replay import make_source_run, snapshot, target_run
from test_source_selected_citations import block, fixture
from test_structured_validation_repair import response
from test_targeted_validation_repair import assert_two_distinct_settled_calls


def source_data(tmp_path):
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"

    def line(identity, text):
        value = block(parsed, identity, text, origin="native")
        unit = parsed["documents"][0]["units"][0]
        value["locator"] = f"pdf/page[1]/line[{sum(b['kind'] == 'paragraph' for b in unit['blocks']) - 1}]"
        return value

    line("REGION", "第五章 响应文件要求及格式")
    line("PARENT", "7 资格审查资料")
    line("NOTE", "注：所有材料按照“第三部分 询比要求”中要求提供")
    line("H1", "7.1、资质材料：")
    line("H2", "7.2、业绩材料：")
    line("CAPTION", "响应人业绩统计表（后附业绩材料）")
    table = block(parsed, "TABLE", "", kind="table", origin="native", rows=[["序号", "合成项目"], ["", ""]],
                  merges=[], column_widths=[1, 1], header_rows=[0])
    table["locator"] = "pdf/page[1]/table[0]"
    for index in range(5):
        line(f"H2-BODY-{index}", f"合成业绩表原注续文{index}应原样保留。")
    line("H3", "7.3、财务要求资料：")
    line("H4", "7.4、信誉要求资料：")
    line("H5", "7.5、人员要求资料：")
    line("H6", "7.6、其他资料：")
    line("FORM", "合成关联关系说明")
    line("BODY", "响应人应说明关联关系，不得删除本项实质原文。")
    line("NEXT", "8 技术服务方案")
    line("PLAN", "（格式自拟，根据技术方案及详细评审中的技术部分要求编制）")
    for unit in parsed["documents"][0]["units"]:
        unit["coverage"]["image_count"] = 0
    source = Path(manifest["sources"][0]["path"])
    source.write_text("Synthetic source bytes; native N1 contract is supplied by the test.", encoding="utf-8")
    manifest["sources"][0]["sha256"] = parsed["documents"][0]["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    return {"manifest": manifest, "parsed": parsed, "source_artifacts": [], "input_kind": "automatic"}


def candidates(window):
    first = {"window_id": window["window_id"], "processing_declaration": "processed_not_semantically_accepted",
        "unresolved_block_ids": ["H4", "H5"], "format_status": "specified", "requirements": [
            {"citation": {"block_ids": ["H6", "FORM", "BODY"]}, "category": "qualification", "response_mode": "fixed_text",
             "format_status": "prescribed", "format_title": "合成关联关系说明", "mandatory": True,
             "confidence": "high", "required_fact_labels": [], "attachment_keywords": [], "reference_keywords": []},
            {"citation": {"block_ids": ["NEXT", "PLAN"]}, "category": "technical", "response_mode": "writing",
             "format_status": "not_prescribed", "format_title": None, "mandatory": True,
             "confidence": "high", "required_fact_labels": [], "attachment_keywords": [], "reference_keywords": []}],
        "format_chapters": [{"title": "合成关联关系说明", "level": 2, "citation": {"block_ids": ["FORM", "BODY"]},
                             "content_block_ids": ["FORM", "BODY"], "requirement_indices": [0], "status": "prescribed"}]}
    second = copy.deepcopy(first)
    second["unresolved_block_ids"] = []
    second["requirements"].extend({"citation": {"block_ids": [key], "clause_id": None, "table_row_id": None},
                                    **copy.deepcopy(_MATERIAL_RESPONSE_FIELDS)} for key in ("H4", "H5"))
    return first, second


def setup_source(tmp_path, monkeypatch, *, capture=True, mutate_second=None, on_first=None):
    value = source_data(tmp_path)
    window_config = {"window_max_chars": 14000, "window_max_blocks": 13}
    options = {"mode": "chat_completions", "endpoint": "https://example.invalid/chat", "model": "synthetic",
        "auth_env": "TEST_SOURCE_BOUND_REPAIR_KEY", "max_attempts": 1, "validation_repair_attempts": 1,
        "max_output_tokens": 512, "max_input_chars": 100000, "max_run_calls": 5, "max_run_usd": 1,
        "input_usd_per_million": .30, "output_usd_per_million": 1.20}
    monkeypatch.setenv(options["auth_env"], "synthetic-not-a-real-credential")
    sent, replies, holder = [], [], {}

    def transport(endpoint, wire, secret, timeout):
        sent.append(copy.deepcopy(wire))
        raw = copy.deepcopy(holder["first"] if len(sent) == 1 else holder["second"])
        if len(sent) == 1 and on_first:
            on_first(holder["executor"], holder["run"])
        if len(sent) == 2 and mutate_second:
            mutate_second(raw)
        answer = response(raw)
        replies.append(copy.deepcopy(answer))
        return answer

    gateway = JsonTaskGateway(options, transport=transport)

    def freeze(ctx, data):
        data = copy.deepcopy(data)
        source = data["manifest"]["sources"][0]
        ref = ctx.store_bytes("synthetic-source.txt", Path(source["path"]).read_bytes())
        source["path"] = str(ctx.executor.store.root / ref["path"])
        data["source_artifacts"] = [ref]
        return data

    def extract(ctx, data):
        blocks, _, docs = _index(data["parsed"], data["manifest"])
        windows, _ = _windows(docs, blocks, window_config)
        window = next(w for w in windows if {"H4", "H5"} <= {b["block_id"] for b in w["blocks"]})
        holder["window"] = copy.deepcopy(window)
        holder["first"], holder["second"] = candidates(window)
        assert not {"PARENT", "NOTE"} & {b["block_id"] for b in window["blocks"]}
        result = run_extraction_window(ctx, window, window_config, gateway)
        return {"selected_window_result": result, "whole_N2_complete": False}

    executor = Executor(tmp_path / "source-runtime", [NodeSpec("N0", "synthetic-source-freeze", freeze, ()),
        NodeSpec("N1", "synthetic-native-N1", lambda ctx, data: data, ()),
        NodeSpec("N2", "synthetic-source-bound-window-only", extract, ("structured_provider", "extraction"))],
        {"structured_provider": options, "extraction": window_config, "debug_capture": capture})
    run = executor.create_run(value)
    holder.update(executor=executor, run=run)
    return {"executor": executor, "run": run, "options": options, "tmp_path": tmp_path,
            "sent": sent, "replies": replies, "holder": holder, "extraction": window_config}


def captured(source, state, prefix):
    node = next(n for n in state["nodes"] if n["key"] == "N2")
    return [(cap, source["executor"].store.read_json(cap["ref"])) for at in node["attempts"] for cap in at["captures"]
            if cap["name"].startswith(prefix) and cap.get("ref")]


def test_real_n0_n1_n2_correction_preserves_old_candidate_and_budgets_two_distinct_calls(tmp_path, monkeypatch):
    source = setup_source(tmp_path, monkeypatch)
    state = source["executor"].execute(source["run"])
    assert state["status"] == "succeeded", state.get("error_code")
    assert len(source["sent"]) == 2
    relation, = [value for _, value in captured(source, state, "validation-repair-result-")]
    assert relation["schema_version"] == "validation-repair-v8" and relation["status"] == "completed"
    assert_two_distinct_settled_calls(state, relation)
    first, second = source["holder"]["first"], source["holder"]["second"]
    assert json.loads(source["replies"][0]["choices"][0]["message"]["content"]) == first
    assert relation["first_candidate_hash"] == digest(first)
    first_wire, repair_wire = [json.loads(wire["messages"][-1]["content"]) for wire in source["sent"]]
    assert "validation_feedback" not in first_wire
    groups = repair_wire["validation_feedback"]["required_coverage"]["resolution_groups"]
    assert [group["block_ids"] for group in groups] == [["H4"], ["H5"]]
    assert all(group["resolution_kind"] == "source_bound_material_heading" for group in groups)
    assert all(group["source_proof"]["reference_resolution_status"] == "not_resolved" for group in groups)
    assert second["requirements"][:2] == first["requirements"] and second["format_chapters"] == first["format_chapters"]
    output = source["executor"].output(source["run"])
    assert output["whole_N2_complete"] is False
    assert len(output["selected_window_result"]["validated"]["requirements"]) == 4
    diagnostics = inspect_diagnostics(source["executor"].store.root, source["run"])
    assert diagnostics["actual_call_attempts"] == 2 and diagnostics["gaps"] == []
    assert [call["outcome"] for call in diagnostics["calls"]] == ["failed", "completed"]


@pytest.mark.parametrize("damage", ["json_content", "corrupt_bytes"])
def test_real_n1_checkpoint_damage_after_first_response_never_buys_a_second_request(tmp_path, monkeypatch, damage):
    def corrupt(executor, run):
        state = executor.store.read_state(run)
        n1 = next(node for node in state["nodes"] if node["key"] == "N1")
        path = executor.store.resolve(n1["output_ref"]["path"])
        if damage == "corrupt_bytes":
            path.write_bytes(b"corrupt-synthetic-N1")
        else:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            envelope["data"]["parsed"]["documents"][0]["units"][0]["blocks"][2]["text"] += "来源失真"
            path.write_text(json.dumps(envelope), encoding="utf-8")
    source = setup_source(tmp_path, monkeypatch, on_first=corrupt)
    state = source["executor"].execute(source["run"])
    assert state["status"] == "failed" and state["error_code"] == "CHECKPOINT_INVALID"
    assert len(source["sent"]) == 1
    assert call_budget_summary(state["call_budget"])["reservation_count"] == 1


@pytest.mark.parametrize("damage", ["clear_only", "change_old", "route_old_to_procedure", "procedure", "new_fact", "extra_item", "keep_unresolved"])
def test_v8_second_candidate_cannot_change_old_items_or_erase_unresolved_without_exact_additions(tmp_path, monkeypatch, damage):
    def mutate(raw):
        if damage == "clear_only":
            raw["requirements"] = raw["requirements"][:2]
        elif damage == "change_old":
            raw["requirements"][0]["mandatory"] = False
        elif damage == "route_old_to_procedure":
            raw["requirements"][0].update(category="procedure", response_mode="procedure")
        elif damage == "procedure":
            raw["requirements"][-1].update(category="procedure", response_mode="procedure")
        elif damage == "new_fact":
            raw["requirements"][-1]["required_fact_labels"] = ["虚构高级工程师"]
        elif damage == "extra_item":
            raw["requirements"].append(copy.deepcopy(raw["requirements"][-1]))
        else:
            raw["unresolved_block_ids"] = ["H4", "H5"]
    source = setup_source(tmp_path, monkeypatch, mutate_second=mutate)
    state = source["executor"].execute(source["run"])
    assert state["status"] == "failed" and state["error_code"] == "VALIDATION_REPAIR_COVERAGE_LOST"
    assert len(source["sent"]) == 2
    resumed = source["executor"].resume(source["run"])
    assert resumed["status"] == "failed" and resumed["error_code"] == "VALIDATION_REPAIR_ALREADY_ATTEMPTED"
    assert len(source["sent"]) == 2 and call_budget_summary(resumed["call_budget"])["reservation_count"] == 2


def test_v8_debug_capture_disabled_stops_after_first_call(tmp_path, monkeypatch):
    source = setup_source(tmp_path, monkeypatch, capture=False)
    state = source["executor"].execute(source["run"])
    assert state["status"] == "failed" and len(source["sent"]) == 1
    assert call_budget_summary(state["call_budget"])["reservation_count"] == 1


def test_v8_failed_capture_cannot_start_the_correction(tmp_path, monkeypatch):
    original = NodeContext.capture
    def fail(self, name, value, **kwargs):
        if name.startswith("validation-") and not name.startswith("validation-repair-"):
            raise OSError("synthetic validation capture failure")
        return original(self, name, value, **kwargs)
    monkeypatch.setattr(NodeContext, "capture", fail)
    source = setup_source(tmp_path, monkeypatch)
    state = source["executor"].execute(source["run"])
    assert state["status"] == "failed" and len(source["sent"]) == 1
    assert call_budget_summary(state["call_budget"])["reservation_count"] == 1


def test_v8_actual_call_history_revalidates_through_diagnostic_mixed_with_zero_new_calls(tmp_path, monkeypatch):
    source = setup_source(tmp_path, monkeypatch)
    state = source["executor"].execute(source["run"])
    assert state["status"] == "succeeded", state.get("error_code")
    source["value"] = source["executor"].output(source["run"], "N1")
    source["unit_id"] = source["holder"]["window"]["window_id"]
    before = snapshot(source["executor"].store.root)
    monkeypatch.delenv(source["options"]["auth_env"])
    result = target_run(source, validator=lambda candidate: validate_live_window(candidate, source["holder"]["window"]))
    executor, run, target_state, sent, _ = result
    assert target_state["status"] == "succeeded", target_state.get("error_code")
    assert sent == [] and "call_budget" not in target_state
    origin, = executor.output(run)["diagnostic_replay"]["origins"]
    assert len(origin["calls"]) == 2 and {call["run_id"] for call in origin["calls"]} == {source["run"]}
    assert inspect_diagnostics(executor.store.root, run)["actual_call_attempts"] == 0
    assert snapshot(source["executor"].store.root) == before


def test_v7_actual_literal_correction_history_remains_replayable_without_a_new_call(tmp_path, monkeypatch):
    source = make_source_run(tmp_path, monkeypatch, repaired=True)
    state = source["executor"].inspect(source["run"])
    relation, = [value for _, value in captured(source, state, "validation-repair-result-")]
    assert relation["schema_version"] == "validation-repair-v7"
    before = snapshot(source["executor"].store.root)
    monkeypatch.delenv(source["options"]["auth_env"])
    executor, run, state, sent, _ = target_run(source)
    assert state["status"] == "succeeded", state.get("error_code")
    assert sent == [] and "call_budget" not in state
    assert inspect_diagnostics(executor.store.root, run)["actual_call_attempts"] == 0
    assert snapshot(source["executor"].store.root) == before


@pytest.mark.parametrize("change", ["different_reference", "conditional_note"])
def test_v8_replay_rejects_changed_remote_n1_note_even_when_current_window_is_identical(tmp_path, monkeypatch, change):
    source = setup_source(tmp_path, monkeypatch)
    state = source["executor"].execute(source["run"])
    assert state["status"] == "succeeded", state.get("error_code")
    source["value"] = source["executor"].output(source["run"], "N1")
    source["unit_id"] = source["holder"]["window"]["window_id"]
    before = snapshot(source["executor"].store.root)

    def change_current(value):
        note = next(block for unit in value["parsed"]["documents"][0]["units"] for block in unit["blocks"]
                    if block["block_id"] == "NOTE")
        note["text"] = ("注：所有材料按照“第一章 合成询比要求”中要求提供" if change == "different_reference" else
                        "注：如有需要，所有材料按照“第三部分 询比要求”中要求提供")
        blocks, _, docs = _index(value["parsed"], value["manifest"])
        current = next(w for w in _windows(docs, blocks, source["extraction"])[0] if w["window_id"] == source["unit_id"])
        assert extraction_window_payload(current, source["extraction"]) == extraction_window_payload(
            source["holder"]["window"], source["extraction"])

    monkeypatch.delenv(source["options"]["auth_env"])
    executor, run, state, sent, _ = target_run(source, value_change=change_current,
        validator=lambda candidate: validate_live_window(candidate, source["holder"]["window"]))
    assert state["status"] == "failed" and state["error_code"] == "DIAGNOSTIC_REPLAY_SOURCE_CONTEXT_MISMATCH"
    assert sent == [] and "call_budget" not in state
    assert inspect_diagnostics(executor.store.root, run)["actual_call_attempts"] == 0
    assert snapshot(source["executor"].store.root) == before


def test_v8_replay_allows_only_non_source_n1_container_metadata_to_differ(tmp_path, monkeypatch):
    source = setup_source(tmp_path, monkeypatch)
    state = source["executor"].execute(source["run"])
    assert state["status"] == "succeeded", state.get("error_code")
    source["value"] = source["executor"].output(source["run"], "N1")
    source["unit_id"] = source["holder"]["window"]["window_id"]
    before = snapshot(source["executor"].store.root)

    def change_current(value):
        value["parsed"]["container_note"] = "synthetic fresh read container"
        value["parsed"]["documents"][0]["container_note"] = "synthetic fresh document container"

    monkeypatch.delenv(source["options"]["auth_env"])
    executor, run, state, sent, _ = target_run(source, value_change=change_current,
        validator=lambda candidate: validate_live_window(candidate, source["holder"]["window"]))
    assert state["status"] == "succeeded", state.get("error_code")
    assert sent == [] and "call_budget" not in state
    assert inspect_diagnostics(executor.store.root, run)["actual_call_attempts"] == 0
    assert snapshot(source["executor"].store.root) == before
