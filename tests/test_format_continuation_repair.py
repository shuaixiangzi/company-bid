"""Bounded continuation correction through actual Executor and Provider paths.

All source bytes, N1 contracts, transport responses and credentials are synthetic.
The N2 node processes one selected window; no full extraction is claimed.
"""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from company_bid.diagnostics import inspect_diagnostics
from company_bid.extraction import _index, _windows, extraction_window_payload, run_extraction_window, validate_live_window
from company_bid.runtime import Executor, NodeContext, NodeSpec, call_budget_summary
from company_bid.structured import JsonTaskGateway
from company_bid.storage import digest
from test_diagnostic_replay import snapshot, target_run
from test_source_bound_material_repair import captured
from test_source_selected_citations import block, fixture
from test_structured_validation_repair import response
from test_targeted_validation_repair import assert_two_distinct_settled_calls


LETTER = "11 合成承诺函"
TRUE_FORMAT = "12 合成其他说明"


def source_data(tmp_path, *, other=False):
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"

    def line(identity, text):
        value = block(parsed, identity, text, origin="native")
        unit = parsed["documents"][0]["units"][0]
        value["locator"] = f"pdf/page[1]/line[{len(unit['blocks']) - 1}]"
        return value

    line("REGION", "第五章 响应文件格式")
    for i in range(6):
        line(f"PAD-{i}", f"合成前置格式读取背景第{i}段。")
    line("OWNER", LETTER)
    line("INTRO", "我公司郑重承诺：")
    line("LEAD-1", "在（项目名称）投标中保持原文件条件，")
    line("LEAD-2", "并确保原始资料真实。我公司：")
    for i in range(1, 16):
        line(f"ITEM-{i}", f"{i}. 我公司对合成事项{i}作出原格式承诺；")
    line("SIGN", "响应人：（盖单位章）")
    line("LEGAL", "法定代表人：（签字）")
    line("DATE", "年 月 日")
    if other:
        line("TRUE-HEAD", TRUE_FORMAT)
        line("TRUE-BODY", "本合成说明的实际原文必须保留，不能用续文纠正删除。")
    line("NEXT-CHAPTER", "第六章 合同条款")
    line("CONTRACT", "合成合同来源不属于原承诺函。")
    for unit in parsed["documents"][0]["units"]:
        unit["coverage"]["image_count"] = 0
    path = Path(manifest["sources"][0]["path"])
    path.write_text("Synthetic continuation source bytes; native N1 is supplied by the test.", encoding="utf-8")
    manifest["sources"][0]["sha256"] = parsed["documents"][0]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"manifest": manifest, "parsed": parsed, "source_artifacts": [], "input_kind": "automatic"}


def candidates(window, *, other=False, false_last=False):
    selected = [*(f"ITEM-{i}" for i in range(6, 16)), "SIGN", "LEGAL", "DATE"]
    requirement = {"citation": {"block_ids": selected}, "category": "qualification", "response_mode": "fixed_text",
        "format_status": "prescribed", "format_title": LETTER, "mandatory": True, "confidence": "high",
        "required_fact_labels": ["合成事项6"], "attachment_keywords": [], "reference_keywords": []}
    false_format = {"title": LETTER, "level": 2, "citation": {"block_ids": selected},
                    "content_block_ids": selected, "requirement_indices": [0], "status": "prescribed"}
    first = {"window_id": window["window_id"], "processing_declaration": "processed_not_semantically_accepted",
             "unresolved_block_ids": [], "requirements": [requirement], "format_chapters": [false_format],
             "format_status": "specified"}
    if other:
        first["requirements"].append({"citation": {"block_ids": ["TRUE-HEAD", "TRUE-BODY"]},
            "category": "qualification", "response_mode": "fixed_text", "format_status": "prescribed",
            "format_title": TRUE_FORMAT, "mandatory": True, "confidence": "high", "required_fact_labels": [],
            "attachment_keywords": [], "reference_keywords": []})
        true_format = {"title": TRUE_FORMAT, "level": 2, "citation": {"block_ids": ["TRUE-HEAD", "TRUE-BODY"]},
                       "content_block_ids": ["TRUE-HEAD", "TRUE-BODY"], "requirement_indices": [1], "status": "prescribed"}
        first["format_chapters"] = [true_format, false_format] if false_last else [false_format, true_format]
    second = copy.deepcopy(first)
    second["format_chapters"] = [fmt for fmt in second["format_chapters"] if fmt["title"] != LETTER]
    second["format_status"] = "specified" if second["format_chapters"] else "none_verified"
    return first, second


def setup_source(tmp_path, monkeypatch, *, other=False, false_last=False, capture=True, mutate_second=None, on_first=None):
    value = source_data(tmp_path, other=other)
    config = {"window_max_chars": 14000, "window_max_blocks": 16}
    options = {"mode": "chat_completions", "endpoint": "https://example.invalid/chat", "model": "synthetic",
        "auth_env": "TEST_CONTINUATION_REPAIR_KEY", "max_attempts": 1, "validation_repair_attempts": 1,
        "max_output_tokens": 512, "max_input_chars": 100000, "max_run_calls": 5, "max_run_usd": 1,
        "input_usd_per_million": .3, "output_usd_per_million": 1.2}
    monkeypatch.setenv(options["auth_env"], "synthetic-not-a-real-credential")
    sent, replies, holder = [], [], {}

    def transport(endpoint, wire, secret, timeout):
        sent.append(copy.deepcopy(wire))
        raw = copy.deepcopy(holder["first"] if len(sent) == 1 else holder["second"])
        if len(sent) == 1 and on_first:
            on_first(holder["executor"], holder["run"])
        if len(sent) == 2 and mutate_second:
            mutate_second(raw, holder)
        result = response(raw)
        replies.append(copy.deepcopy(result))
        return result

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
        windows, _ = _windows(docs, blocks, config)
        window = next(w for w in windows if w["blocks"][0]["block_id"] == "ITEM-6")
        holder["window"] = copy.deepcopy(window)
        holder["first"], holder["second"] = candidates(window, other=other, false_last=false_last)
        assert not {"OWNER", "INTRO", "LEAD-1"} & {b["block_id"] for b in window["blocks"]}
        result = run_extraction_window(ctx, window, config, gateway)
        return {"selected_window_result": result, "whole_N2_complete": False}

    executor = Executor(tmp_path / "source-runtime", [NodeSpec("N0", "synthetic-continuation-freeze", freeze, ()),
        NodeSpec("N1", "synthetic-continuation-N1", lambda ctx, data: data, ()),
        NodeSpec("N2", "synthetic-continuation-window-only", extract, ("structured_provider", "extraction"))],
        {"structured_provider": options, "extraction": config, "debug_capture": capture})
    run = executor.create_run(value)
    holder.update(executor=executor, run=run)
    return {"executor": executor, "run": run, "options": options, "tmp_path": tmp_path,
            "sent": sent, "replies": replies, "holder": holder, "extraction": config}


@pytest.mark.parametrize("other,false_last", [(False, False), (True, False), (True, True)])
def test_v9_deletes_only_the_false_new_format_with_two_accounted_actual_calls(tmp_path, monkeypatch, other, false_last):
    source = setup_source(tmp_path, monkeypatch, other=other, false_last=false_last)
    state = source["executor"].execute(source["run"])
    assert state["status"] == "succeeded", state.get("error_code")
    assert len(source["sent"]) == 2
    relation, = [value for _, value in captured(source, state, "validation-repair-result-")]
    assert relation["schema_version"] == "validation-repair-v9" and relation["status"] == "completed"
    assert_two_distinct_settled_calls(state, relation)
    first, second = source["holder"]["first"], source["holder"]["second"]
    assert first["requirements"] == second["requirements"]
    assert json.loads(source["replies"][0]["choices"][0]["message"]["content"]) == first
    assert relation["first_candidate_hash"] == digest(first)
    output = source["executor"].output(source["run"])
    assert output["whole_N2_complete"] is False
    assert output["selected_window_result"]["raw_model_candidate"] == second
    assert output["selected_window_result"]["candidate"]["format_status"] == ("specified" if other else "none_verified")
    assert len(second["format_chapters"]) == (1 if other else 0)
    report = inspect_diagnostics(source["executor"].store.root, source["run"])
    assert report["actual_call_attempts"] == 2 and report["gaps"] == []
    assert [call["outcome"] for call in report["calls"]] == ["failed", "completed"]


@pytest.mark.parametrize("damage", ["route_old", "delete_text", "change_label", "delete_true_format", "keep_false_format", "wrong_root"])
def test_v9_cannot_change_existing_requirements_or_other_format_content_and_never_buys_third_candidate(tmp_path, monkeypatch, damage):
    def mutate(raw, holder):
        if damage == "route_old":
            raw["requirements"][0].update(category="procedure", response_mode="procedure")
        elif damage == "delete_text":
            raw["requirements"][0]["citation"]["block_ids"].pop()
        elif damage == "change_label":
            raw["requirements"][0]["required_fact_labels"] = ["合成事项7"]
        elif damage == "delete_true_format":
            raw["format_chapters"] = []
            raw["format_status"] = "none_verified"
        elif damage == "keep_false_format":
            raw["format_chapters"] = copy.deepcopy(holder["first"]["format_chapters"])
        else:
            raw["format_status"] = "conflict"
    source = setup_source(tmp_path, monkeypatch, other=True, mutate_second=mutate)
    state = source["executor"].execute(source["run"])
    assert state["status"] == "failed" and state["error_code"] == "VALIDATION_REPAIR_COVERAGE_LOST"
    assert len(source["sent"]) == 2
    resumed = source["executor"].resume(source["run"])
    assert resumed["status"] == "failed" and resumed["error_code"] == "VALIDATION_REPAIR_ALREADY_ATTEMPTED"
    assert len(source["sent"]) == 2 and call_budget_summary(resumed["call_budget"])["reservation_count"] == 2


@pytest.mark.parametrize("damage", ["json_content", "corrupt_bytes"])
def test_v9_n1_damage_after_first_response_stops_before_second_call(tmp_path, monkeypatch, damage):
    def corrupt(executor, run):
        n1 = next(node for node in executor.store.read_state(run)["nodes"] if node["key"] == "N1")
        path = executor.store.resolve(n1["output_ref"]["path"])
        if damage == "corrupt_bytes":
            path.write_bytes(b"damaged-synthetic-N1")
        else:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            envelope["data"]["parsed"]["documents"][0]["units"][0]["blocks"][8]["text"] += "不可信改写"
            path.write_text(json.dumps(envelope), encoding="utf-8")
    source = setup_source(tmp_path, monkeypatch, on_first=corrupt)
    state = source["executor"].execute(source["run"])
    assert state["status"] == "failed" and state["error_code"] == "CHECKPOINT_INVALID"
    assert len(source["sent"]) == 1 and call_budget_summary(state["call_budget"])["reservation_count"] == 1


def test_v9_capture_disabled_stops_after_first_call(tmp_path, monkeypatch):
    source = setup_source(tmp_path, monkeypatch, capture=False)
    state = source["executor"].execute(source["run"])
    assert state["status"] == "failed" and len(source["sent"]) == 1
    assert call_budget_summary(state["call_budget"])["reservation_count"] == 1


def test_v9_capture_failure_stops_after_first_call(tmp_path, monkeypatch):
    original = NodeContext.capture
    def fail(self, name, value, **kwargs):
        if name.startswith("validation-") and not name.startswith("validation-repair-"):
            raise OSError("synthetic capture failure")
        return original(self, name, value, **kwargs)
    monkeypatch.setattr(NodeContext, "capture", fail)
    source = setup_source(tmp_path, monkeypatch)
    state = source["executor"].execute(source["run"])
    assert state["status"] == "failed" and len(source["sent"]) == 1
    assert call_budget_summary(state["call_budget"])["reservation_count"] == 1


def completed_source(tmp_path, monkeypatch):
    source = setup_source(tmp_path, monkeypatch)
    state = source["executor"].execute(source["run"])
    assert state["status"] == "succeeded", state.get("error_code")
    source["value"] = source["executor"].output(source["run"], "N1")
    source["unit_id"] = source["holder"]["window"]["window_id"]
    return source


def test_v9_actual_history_revalidates_through_diagnostic_mixed_at_zero_new_calls(tmp_path, monkeypatch):
    source = completed_source(tmp_path, monkeypatch)
    before = snapshot(source["executor"].store.root)
    monkeypatch.delenv(source["options"]["auth_env"])
    executor, run, state, sent, _ = target_run(source,
        validator=lambda candidate: validate_live_window(candidate, source["holder"]["window"]))
    assert state["status"] == "succeeded", state.get("error_code")
    assert sent == [] and "call_budget" not in state
    origin, = executor.output(run)["diagnostic_replay"]["origins"]
    assert len(origin["calls"]) == 2
    assert inspect_diagnostics(executor.store.root, run)["actual_call_attempts"] == 0
    assert snapshot(source["executor"].store.root) == before


@pytest.mark.parametrize("change", ["owner_intro", "remote_condition"])
def test_v9_replay_cannot_hide_changed_remote_owner_conditions_behind_identical_window(tmp_path, monkeypatch, change):
    source = completed_source(tmp_path, monkeypatch)
    before = snapshot(source["executor"].store.root)
    def change_current(value):
        key = "INTRO" if change == "owner_intro" else "LEAD-1"
        line = next(b for unit in value["parsed"]["documents"][0]["units"] for b in unit["blocks"] if b["block_id"] == key)
        line["text"] = ("经确认后我公司郑重承诺：" if change == "owner_intro" else
                        "在（项目名称）投标中仅经批准后方可履行原文件承诺，")
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


def test_v9_replay_accepts_non_source_container_metadata_without_ignoring_source_hashes(tmp_path, monkeypatch):
    source = completed_source(tmp_path, monkeypatch)
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
