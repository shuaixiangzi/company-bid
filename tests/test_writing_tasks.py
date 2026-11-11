"""Writing-unit contracts and cost-saving recovery, using offline transports."""
from collections import Counter
from copy import deepcopy
import hashlib
import json

import pytest
from docx import Document
from pydantic import ValidationError

from company_bid import pipeline
from company_bid.models import PreparedBid, ProposalContent, ReferenceExcerpt
from company_bid.provider import ChatProposalProvider
from company_bid.sample import create_synthetic_sample


def input_with_tasks(tmp_path):
    data = create_synthetic_sample(tmp_path / "sources").model_dump(mode="json")
    excerpt = "合成参考方法：按设备编号记录检查条件，异常复测后整理交付记录。"
    data["reference_excerpts"] = [{
        "reference_id": "REF-1", "source_id": "S-TENDER", "locator": "synthetic prepared method",
        "text": excerpt, "text_sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
        "applicability": "合成参考仅验证方法改编合同，不是资质证明。",
        "retained_points": ["逐对象记录、异常复测"], "excluded_points": ["不继承历史价格、人员和设备数量"],
    }]
    tasks = []
    proposals = {}
    for index in range(2):
        unit_id = f"UNIT-{index + 1}"
        tasks.append({"unit_id": unit_id, "title": f"2.{index + 1} 记录与复核", "objective": "形成可以复核的拟实施方法",
                      "must_cover": ["检查前提和对象", "记录与异常处理"], "requirement_refs": ["R-002"],
                      "reference_refs": ["REF-1"], "forbidden_fields": ["确定人员设备与价格"],
                      "completion_criteria": ["两项均有实际正文位置，保留作业授权边界"],
                      "operation": "reference_adaptation"})
        proposals[unit_id] = proposal()
    data["sections"][1]["writing_tasks"] = tasks
    data["sections"][1]["table_refs"] = ["T-RECORD"]
    data["sections"][1]["writing_tasks"][0]["table_refs"] = ["T-RECORD"]
    data["tables"].append({"table_id": "T-RECORD", "section_id": "S2", "title": "拟用检查记录空表",
                           "columns": ["对象编号", "检查条件", "实测及复核结果"],
                           "rows": [[{"text": ""}, {"text": ""}, {"text": ""}]],
                           "source_refs": ["S-TENDER"], "requirement_refs": ["R-002"],
                           "column_widths": [1, 2, 3]})
    return {"prepared": PreparedBid.model_validate(data).model_dump(mode="json"), "proposals": proposals}


def proposal():
    return {"paragraphs": ["拟按设备编号核对作业许可、测点和检查条件。", "拟记录实测条件，异常先保留原始值并复测，超授权事项报告业主。"],
            "steps": [], "coverage": [{"item_index": 0, "paragraph_indices": [0]}, {"item_index": 1, "paragraph_indices": [1]}]}


def config():
    return {"debug_capture": True, "provider": {"mode": "chat_completions", "endpoint": "https://offline.invalid/chat/completions",
            "model": "offline", "auth_env": "WRITING_TEST_SECRET", "max_attempts": 1, "max_run_calls": 5, "max_run_usd": 1}}


def inject(monkeypatch, transport):
    monkeypatch.setenv("WRITING_TEST_SECRET", "WRITING_SECRET_CANARY")
    monkeypatch.setattr(pipeline, "ChatProposalProvider", lambda options: ChatProposalProvider(options, transport=transport, sleep=lambda _: None))


def response(content):
    return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 34, "total_tokens": 46}}


def test_same_section_failed_unit_resumes_without_recalling_successful_unit(tmp_path, monkeypatch):
    initial = input_with_tasks(tmp_path)
    calls = Counter()

    def transport(endpoint, payload, secret, timeout):
        request = json.loads(payload["messages"][-1]["content"])
        unit = request["section"]["section_id"]
        assert request["section"]["reference_excerpts"][0]["reference_id"] == "REF-1"
        assert len(request["requirements"]) == 1
        assert "path" not in request["section"]["reference_excerpts"][0]
        calls[unit] += 1
        if unit == "UNIT-2" and calls[unit] == 1:
            raise TimeoutError("unknown provider outcome")
        return response(proposal())

    inject(monkeypatch, transport)
    root = tmp_path / "runtime"
    runner = pipeline.build_executor(root, config())
    run = runner.create_run(initial)
    first = runner.execute(run)
    assert first["status"] == "failed"
    ref = first["nodes"][1]["units"]["UNIT-1"]["output_ref"]
    restarted = pipeline.build_executor(root, config())
    finished = restarted.resume(run, stop_after="N5")
    assert finished["status"] == "paused_debug"
    assert calls == {"UNIT-1": 1, "UNIT-2": 2}
    assert finished["nodes"][1]["units"]["UNIT-1"]["output_ref"] == ref
    output = restarted.output(run, "N5")
    assert len(output["validation"]["writing_units"]) == 2
    assert output["validation"]["semantic_fact_support"] == "requires_source_review"
    section = output["package"]["sections"][1]
    kinds = [block["kind"] for block in section["blocks"]]
    assert kinds == ["heading", "paragraph", "paragraph", "table", "heading", "paragraph", "paragraph"]
    events = [json.loads(line) for line in (root / "runs" / run / "events.jsonl").read_text().splitlines()]
    assert sum(item["event"] == "call.started" for item in events) == 3


@pytest.mark.parametrize("format_status", ["prescribed", "unread", "conflict"])
def test_unknown_or_prescribed_format_never_calls_prose_provider(tmp_path, monkeypatch, format_status):
    initial = input_with_tasks(tmp_path)
    initial["prepared"]["sections"][1]["writing_tasks"][0]["format_status"] = format_status
    calls = []
    inject(monkeypatch, lambda *args: calls.append(1) or response(proposal()))
    runner = pipeline.build_executor(tmp_path / "runtime", config())
    result = runner.execute(runner.create_run(initial))
    assert result["status"] == "failed" and result["error_code"] == "WRITING_FORMAT_NOT_READY"
    assert calls == []


def test_omitted_must_cover_is_saved_as_failed_call_not_success(tmp_path, monkeypatch):
    initial = input_with_tasks(tmp_path)
    invalid = proposal()
    invalid["coverage"].pop()
    inject(monkeypatch, lambda *args: response(invalid))
    root = tmp_path / "runtime"
    runner = pipeline.build_executor(root, config())
    run = runner.create_run(initial)
    result = runner.execute(run)
    assert result["status"] == "failed" and result["error_code"] == "WRITING_COVERAGE_INCOMPLETE"
    assert result["nodes"][1]["units"]["UNIT-1"]["status"] == "failed"
    events = (root / "runs" / run / "events.jsonl").read_text()
    assert '"call.failed"' in events and '"call.completed"' not in events
    assert "WRITING_SECRET_CANARY" not in events


def test_style_only_child_reuses_frozen_writing_and_preserves_original_docx(tmp_path, monkeypatch):
    calls = []
    inject(monkeypatch, lambda *args: calls.append(1) or response(proposal()))
    root = tmp_path / "runtime"
    runner = pipeline.build_executor(root, config())
    run = runner.create_run(input_with_tasks(tmp_path))
    assert runner.execute(run)["status"] == "succeeded"
    output = runner.output(run)
    original = (root / output["files"]["bid_docx"]["path"]).read_bytes()
    new_config = {**config(), "document_style": {"profile_id": "body-spacing-review", "body_line_spacing": 1.25}}
    child = runner.fork(run, "N6", execution_config=new_config)
    child_runner = pipeline.build_executor(root, new_config)
    assert child_runner.execute(child)["status"] == "succeeded"
    assert len(calls) == 2
    assert (root / output["files"]["bid_docx"]["path"]).read_bytes() == original
    assert child_runner.inspect(child)["nodes"][1]["reused_from_run_id"] == run
    child_file = root / child_runner.output(child)["files"]["bid_docx"]["path"]
    assert Document(child_file).styles["Normal"].paragraph_format.line_spacing == 1.25


@pytest.mark.parametrize("mutation", ["missing_ref", "duplicate_unit", "repeated_table", "missing_requirement"])
def test_plan_rejects_invalid_source_or_assembly_contract(tmp_path, mutation):
    data = input_with_tasks(tmp_path)["prepared"]
    tasks = data["sections"][1]["writing_tasks"]
    if mutation == "missing_ref":
        tasks[0]["reference_refs"] = ["REF-NOT-FOUND"]
    elif mutation == "duplicate_unit":
        tasks[1]["unit_id"] = tasks[0]["unit_id"]
    elif mutation == "repeated_table":
        tasks[1]["table_refs"] = ["T-RECORD"]
    else:
        data["sections"][1]["requirement_refs"].append("R-001")
    with pytest.raises(ValidationError):
        PreparedBid.model_validate(data)


def test_reference_hash_and_coverage_indices_fail_closed(tmp_path):
    reference = input_with_tasks(tmp_path)["prepared"]["reference_excerpts"][0]
    reference["text"] += "changed"
    with pytest.raises(ValidationError, match="hash mismatch"):
        ReferenceExcerpt.model_validate(reference)
    bad = deepcopy(proposal())
    bad["coverage"][0]["paragraph_indices"] = [99]
    with pytest.raises(ValidationError, match="location does not exist"):
        ProposalContent.model_validate(bad)
