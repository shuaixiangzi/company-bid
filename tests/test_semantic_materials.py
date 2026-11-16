"""Real executor/provider with offline transport for bounded semantic recall."""
import copy
import json

import pytest
from PIL import Image

from company_bid.diagnostics import inspect_diagnostics
from company_bid.material_matching import material_candidate_report
from company_bid.models import file_sha256
from company_bid.provider import ProviderFailure
from company_bid.runtime import call_budget_summary
from company_bid.semantic_materials import (build_catalog, build_probe_executor, validate_response,
    assess_candidates, verify_material_files)


def inputs(tmp_path, texts):
    sources, attachments = [], []
    for index, (aid, title) in enumerate([("BUS", "营业执照"), ("SAFE", "安全生产许可证"),
                                        ("APPLICATION", "营业执照办理申请说明"), ("PERSON", "李四")]):
        path = tmp_path / (aid + ".png")
        Image.new("RGB", (12, 12), (index * 30, 20, 10)).save(path)
        source = {"source_id": aid, "kind": "company_material", "title": title, "path": str(path),
                  "sha256": file_sha256(path)}
        sources.append(source)
        attachments.append({"attachment_id": aid, "title": title, "description": "合成测试材料",
            "path": str(path), "sha256": source["sha256"], "source_refs": [aid]})
    person = {"record_id": "REC-P", "kind": "personnel_declaration", "status": "usable_provisional",
        "fields": {"name": {"value": "李四"}, "certificate_declarations": [
            {"declaration_id": "CP", "status": "usable_provisional", "certificate_name": {"value": "AMPP CP3"}}]},
        "citations": [{"source_id": "PERSON", "source_sha256": sources[-1]["sha256"]}]}
    manifest = {"sources": sources, "attachments": attachments, "evidence_selections": [], "material_records": [person]}
    return {"manifest": manifest, "requirements": [{"requirement_id": f"Q{i}", "text": text,
        "response_mode": "attachment"} for i, text in enumerate(texts)]}


def config():
    return {"debug_capture": True, "run_purpose": "diagnostic_replay", "semantic_provider": {
        "endpoint": "https://example.invalid/chat", "model": "synthetic", "auth_env": "SEMANTIC_TEST_KEY",
        "max_attempts": 1, "max_output_tokens": 1000, "max_run_calls": 6, "max_run_usd": 1}}


def reply(payload, gid="BUS"):
    item = next(i for i in payload["catalog"] if i["group_id"] == gid)
    return {"query_id": payload["query_id"], "catalog_sha256": payload["catalog_sha256"],
        "candidates": [{"group_id": gid, "item_sha256": item["item_sha256"], "reason": "登记证明的候选，需核对原件。",
            "evidence": [{"evidence_id": "title", "quote": item["evidence"][0]["quote"]}]}],
        "unresolved_conditions": ["有效期及适用范围未核验"]}


def wire(response):
    return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(response, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50}}


def test_control_and_negative_instruction_skip_llm(tmp_path):
    def transport(*args):
        pytest.fail("A deterministic or negative control cannot purchase a model call")
    value = inputs(tmp_path, ["须提供营业执照。", "无需提供营业执照。"])
    ex = build_probe_executor(tmp_path / "runtime", config(), transport=transport)
    rid = ex.create_run(value)
    assert ex.execute(rid)["status"] == "succeeded"
    rows = ex.output(rid)["queries"]
    assert [r["route"] for r in rows] == ["deterministic_candidate_available", "skip_explicit_instruction_guard"]
    assert all(not r["model_called"] and r["qualification_result"] == "not_determined" for r in rows)
    assert rows[1]["pending_checks"] == rows[1]["condition_findings"] == []
    assert rows[1]["response_instruction_status"] == "not_required"
    assert inspect_diagnostics(tmp_path / "runtime", rid)["actual_call_attempts"] == 0


def test_semantic_recall_sees_whole_catalog_but_never_grants_qualification(tmp_path, monkeypatch):
    monkeypatch.setenv("SEMANTIC_TEST_KEY", "semantic-secret-canary")
    calls = []
    def transport(endpoint, payload, secret, timeout):
        user = json.loads(payload["messages"][1]["content"])
        assert {i["group_id"] for i in user["catalog"]} == {"BUS", "SAFE", "APPLICATION", "PERSON"}
        assert all(set(e) == {"evidence_id", "quote"} for i in user["catalog"] for e in i["evidence"])
        calls.append(user)
        return wire(reply(user))
    value = inputs(tmp_path, ["请附证明企业依法登记存续的文件。"])
    ex = build_probe_executor(tmp_path / "runtime", config(), transport=transport)
    rid = ex.create_run(value)
    assert ex.execute(rid)["status"] == "succeeded"
    output = ex.output(rid)
    candidate, = output["queries"][0]["candidates"]
    assert candidate["decision"] == "semantic_candidate_pending_review"
    assert candidate["qualification_result"] == "not_determined" and not candidate["automatic_assembly_allowed"]
    assert output["assembly_candidates_added"] == 0 and len(calls) == 1
    diagnostics = inspect_diagnostics(tmp_path / "runtime", rid)
    assert diagnostics["actual_call_attempts"] == 1 and diagnostics["evidence_status"] == "available"
    events = (tmp_path / "runtime" / "runs" / rid / "events.jsonl").read_text(encoding="utf-8")
    assert "依法登记" not in events and "李四" not in events and "semantic-secret-canary" not in events


@pytest.mark.parametrize("damage,code", [
    ("unknown_id", "SEMANTIC_CANDIDATE_ID_INVALID"),
    ("item_hash", "SEMANTIC_CANDIDATE_HASH_MISMATCH"),
    ("catalog_hash", "SEMANTIC_RESPONSE_SCOPE_MISMATCH"),
    ("quote", "SEMANTIC_QUOTE_NOT_IN_CATALOG"),
    ("qualified", "SEMANTIC_RESPONSE_SCHEMA_INVALID"),
    ("duplicate", "SEMANTIC_CANDIDATE_ID_INVALID"),
    ("old_quote_shape", "SEMANTIC_RESPONSE_SCHEMA_INVALID"),
])
def test_model_invented_binding_or_extra_qualification_fails_and_keeps_usage(tmp_path, monkeypatch, damage, code):
    monkeypatch.setenv("SEMANTIC_TEST_KEY", "semantic-secret-canary")
    def transport(endpoint, payload, secret, timeout):
        user = json.loads(payload["messages"][1]["content"]); result = reply(user)
        if damage == "unknown_id": result["candidates"][0]["group_id"] = "FABRICATED"
        elif damage == "item_hash": result["candidates"][0]["item_sha256"] = "0" * 64
        elif damage == "catalog_hash": result["catalog_sha256"] = "0" * 64
        elif damage == "quote": result["candidates"][0]["evidence"][0]["quote"] = "原件确认已满足全部要求"
        elif damage == "qualified": result["qualification_result"] = "passed"
        elif damage == "old_quote_shape":
            evidence = result["candidates"][0]["evidence"][0]
            evidence["text"] = evidence.pop("quote"); evidence["kind"] = "maintained_label"
        else: result["candidates"].append(copy.deepcopy(result["candidates"][0]))
        return wire(result)
    ex = build_probe_executor(tmp_path / "runtime", config(), transport=transport)
    rid = ex.create_run(inputs(tmp_path, ["请附证明企业依法登记存续的文件。"])); state = ex.execute(rid)
    assert state["status"] == "failed" and state["error_code"] == code
    budget = call_budget_summary(state["call_budget"])
    assert budget["known_usage_estimate_count"] == 1
    assert float(budget["effective_budget_charge_usd"]) > 0
    if damage == "old_quote_shape":
        captures = state["nodes"][0]["attempts"][-1]["captures"]
        errors, = [c for c in captures if c["name"].startswith("semantic-schema-errors-")]
        detail = ex.store.read_json(errors["ref"])
        assert any(e["type"] == "missing" and e["location"][-1] == "quote" for e in detail["issues"])


@pytest.mark.parametrize("gid,text,blocker", [
    ("APPLICATION", "请附证明企业依法登记存续的文件。", "application_explanation_or_non_proof_role"),
    ("SAFE", "须提供质量体系证书。", "known_requested_type_mismatch"),
    ("PERSON", "须提供同类项目合同主要页。", "known_requested_type_mismatch"),
    ("BUS", "须提供类似项目证明。", "known_requested_type_mismatch"),
])
def test_model_candidate_cannot_override_program_guards(tmp_path, monkeypatch, gid, text, blocker):
    monkeypatch.setenv("SEMANTIC_TEST_KEY", "semantic-secret-canary")
    def transport(endpoint, payload, secret, timeout):
        return wire(reply(json.loads(payload["messages"][1]["content"]), gid))
    ex = build_probe_executor(tmp_path / "runtime", config(), transport=transport)
    rid = ex.create_run(inputs(tmp_path, [text])); assert ex.execute(rid)["status"] == "succeeded"
    candidate, = ex.output(rid)["queries"][0]["candidates"]
    assert candidate["decision"] == "blocked_candidate" and blocker in candidate["blockers"]


def test_named_person_conflict_skips_purchase_and_rejects_forced_model_suggestion(tmp_path):
    value = inputs(tmp_path, ["项目负责人张三须提供AMPP CP3证书。"])
    ex = build_probe_executor(tmp_path / "runtime", config(), transport=lambda *args: pytest.fail("must not call"))
    rid = ex.create_run(value); assert ex.execute(rid)["status"] == "succeeded"
    output = ex.output(rid); row, = output["queries"]
    assert row["route"] == "skip_named_person_conflict" and not row["model_called"]
    payload = {"query_id": "Q0", "catalog_sha256": output["catalog"]["catalog_sha256"], "catalog": output["catalog"]["items"]}
    forced = validate_response(reply(payload, "PERSON"), payload)
    assessed, = assess_candidates(forced, output["deterministic_report"], output["deterministic_report"]["proof_queries"][0])
    assert assessed["decision"] == "blocked_candidate" and "requested_person_identity_mismatch" in assessed["blockers"]


def test_source_corruption_stops_before_model(tmp_path):
    value = inputs(tmp_path, ["请附证明企业依法登记存续的文件。"])
    from pathlib import Path
    Path(value["manifest"]["sources"][0]["path"]).write_bytes(b"corrupt")
    ex = build_probe_executor(tmp_path / "runtime", config(), transport=lambda *args: pytest.fail("must not call"))
    rid = ex.create_run(value); state = ex.execute(rid)
    assert state["status"] == "failed" and state["error_code"] == "SEMANTIC_SOURCE_FILE_CHANGED"
    assert inspect_diagnostics(tmp_path / "runtime", rid)["actual_call_attempts"] == 0


def test_resume_keeps_successful_query_and_retries_only_failed_unit(tmp_path, monkeypatch):
    monkeypatch.setenv("SEMANTIC_TEST_KEY", "semantic-secret-canary")
    calls, fail = [], True
    def transport(endpoint, payload, secret, timeout):
        user = json.loads(payload["messages"][1]["content"]); calls.append(user["query_id"])
        if user["query_id"] == "Q1" and fail:
            raise ProviderFailure("SYNTHETIC_TRANSIENT")
        return wire(reply(user))
    value = inputs(tmp_path, ["请附证明企业依法登记存续的文件。", "请附能够证明公司完成合法登记的资料。"])
    ex = build_probe_executor(tmp_path / "runtime", config(), transport=transport)
    rid = ex.create_run(value); assert ex.execute(rid)["status"] == "failed"
    units = ex.inspect(rid)["nodes"][0]["units"]
    successful = {key: u["output_hash"] for key,u in units.items() if u["status"] == "succeeded"}
    fail = False
    resumed = build_probe_executor(tmp_path / "runtime", config(), transport=transport)
    assert resumed.resume(rid)["status"] == "succeeded"
    assert calls == ["Q0", "Q1", "Q1"]
    assert all(resumed.inspect(rid)["nodes"][0]["units"][k]["output_hash"] == v for k,v in successful.items())


def test_catalog_overflow_is_explicit_not_a_lexical_prefilter(tmp_path):
    value = inputs(tmp_path, ["需要证明"]); manifest = value["manifest"]
    report = material_candidate_report(value["requirements"], manifest["material_records"],
        attachments=manifest["attachments"], sources=manifest["sources"], evidence_selections=[])
    with pytest.raises(ProviderFailure, match="SEMANTIC_CATALOG_LIMIT_EXCEEDED"):
        build_catalog(report, max_groups=2)


@pytest.mark.parametrize("code,status", [("CP3", "declared_code_matches_but_unverified"),
    ("CP4", "requested_credential_not_established_by_declaration")])
def test_person_credential_condition_is_explicit_without_changing_source_or_buying_call(tmp_path, code, status):
    value = inputs(tmp_path, [f"项目技术负责人李四须提供AMPP {code}证书。"])
    before = copy.deepcopy(value)
    ex = build_probe_executor(tmp_path / "runtime", config(), transport=lambda *args: pytest.fail("must not call"))
    rid = ex.create_run(value); assert ex.execute(rid)["status"] == "succeeded"
    row, = ex.output(rid)["queries"]; finding, = row["condition_findings"]
    assert finding["status"] == status and finding["requested_codes"] == [code]
    assert finding["declared_codes"] == ["CP3"] and code in finding["reason"]
    assert not finding["grade_ordering_performed"] and not finding["document_absence_established"]
    assert row["qualification_result"] == "not_determined" and value == before
