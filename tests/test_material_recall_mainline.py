"""Semantic fallback through real N3 planning, using only offline transport."""
import copy
import json
from pathlib import Path
from urllib.error import URLError

import pytest
from PIL import Image

import company_bid.material_recall as recall
from company_bid.material_matching import material_candidate_report
from company_bid.models import file_sha256
from company_bid.planning import plan_bid
from company_bid.provider import ChatProposalProvider, ProviderFailure
from company_bid.runtime import Executor, NodeSpec
from test_planning import Context, add_unit, extract, fixture, no_call, requirement, response


CANARY = "synthetic-mainline-secret-must-not-serialize"


def settings(**overrides):
    return {"enabled": True, "max_queries": 12, "provider": {
        "endpoint": "https://offline.invalid/chat", "model": "synthetic",
        "auth_env": "MATERIAL_MAINLINE_TEST_KEY", "max_attempts": 1,
        "max_output_tokens": 1000, "max_run_calls": 8, "max_run_usd": 1}, **overrides}


def source_case(tmp_path, texts, titles=(("BUS", "营业执照"),)):
    manifest, parsed = fixture(tmp_path)
    candidates = {}
    for index, text in enumerate(texts):
        block = add_unit(parsed, f"U{index}", text)
        candidates[block["block_id"]] = requirement(block, category="qualification", mode="attachment")
    for source in manifest["sources"]:
        content = "\n".join(texts) if source["source_id"] == "tender" else "虚构测试公司；注册地址：测试路1号（虚构）"
        Path(source["path"]).write_text(content, encoding="utf-8")
        source["sha256"] = file_sha256(Path(source["path"]))
    parsed["documents"][0]["sha256"] = manifest["sources"][0]["sha256"]
    for index, (aid, title) in enumerate(titles):
        path = tmp_path / (aid + ".png")
        Image.new("RGB", (12, 12), (index * 17 % 255, 20, 30)).save(path)
        source = {"source_id": aid, "kind": "company_material", "title": title,
            "path": str(path), "sha256": file_sha256(path), "locator": "image:1",
            "provenance": "synthetic", "synthetic": True}
        manifest["sources"].append(source)
        manifest["source_roles"][aid] = "company_material"
        manifest["attachments"].append({"attachment_id": aid, "section_id": "unassigned",
            "title": title, "description": "独立合成证明图像", "path": str(path), "sha256": source["sha256"],
            "source_refs": [aid], "requirement_refs": ["unassigned"], "synthetic": True})
    manifest["evidence_selections"] = []
    extracted = extract(Context(), parsed, manifest,
        lambda payload: response(payload, [candidates[b["block_id"]] for b in payload["window"]["blocks"]]))
    return {"manifest": manifest, "parsed": parsed, "extracted": extracted}


def candidate_reply(payload, gid="BUS"):
    item = next(row for row in payload["catalog"] if row["group_id"] == gid)
    return {"query_id": payload["query_id"], "catalog_sha256": payload["catalog_sha256"],
        "candidates": [{"group_id": gid, "item_sha256": item["item_sha256"],
            "reason": "登记名称相关，原件和条件未核验。", "evidence": [item["evidence"][0]]}],
        "unresolved_conditions": ["需要核验原件与有效期"]}


def install_transport(monkeypatch, decide=None):
    monkeypatch.setenv("MATERIAL_MAINLINE_TEST_KEY", CANARY)
    calls = []
    def transport(endpoint, wire, secret, timeout):
        assert endpoint == "https://offline.invalid/chat" and secret == CANARY
        payload = json.loads(wire["messages"][1]["content"])
        calls.append(payload["query_id"])
        answer = decide(payload, len(calls)) if decide else candidate_reply(payload)
        return {"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(answer, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 25, "total_tokens": 65}}
    monkeypatch.setattr(recall, "ChatProposalProvider", lambda config: ChatProposalProvider(
        config, transport=transport, sleep=lambda _: None))
    return calls


def executor(root, options, organize=False):
    config = {"debug_capture": True, "planning": {"organize_response": organize,
                                               "semantic_material_recall": options}}
    def handler(ctx, data):
        return {"prepared": plan_bid(ctx, data["extracted"], data["parsed"], data["manifest"],
                                    no_call, ctx.execution_config["planning"])}
    return Executor(root, [NodeSpec("N3", "semantic-mainline-test-v1", handler, config_keys=("planning",))], config)


def matching_report(ex, run):
    state = ex.inspect(run)
    captures = state["nodes"][0]["attempts"][-1]["captures"]
    ref = next(c["ref"] for c in captures if c["name"] == "n3-material-matching")
    return ex.store.read_json(ref)


@pytest.mark.parametrize("organize", [False, True])
def test_real_n3_semantic_candidate_preserves_missing_without_assembly(tmp_path, monkeypatch, organize):
    data = source_case(tmp_path, ["请附证明企业依法登记存续的文件。"])
    original = copy.deepcopy(data)
    calls = install_transport(monkeypatch)
    ex = executor(tmp_path / "runtime", settings(), organize)
    run = ex.create_run(data)
    assert ex.execute(run)["status"] == "succeeded"
    prepared = ex.output(run)["prepared"]
    assert data == original and prepared["attachments"] == []
    missing = [item for item in prepared["issues"] if item["severity"] == "missing"]
    assert missing and any("语义补找的待核候选：营业执照" in item["reason"] for item in missing)
    assert all("原缺项继续保留" in item["reason"] for item in missing if "语义补找" in item["reason"])
    report = matching_report(ex, run)
    row, = report["semantic_recall"]["queries"]
    candidate, = row["candidates"]
    assert len(calls) == 1 and report["assembly_candidates"] == []
    assert row["status"] == "candidates_pending_original_checks"
    assert candidate["qualification_result"] == "not_determined" and not candidate["automatic_assembly_allowed"]


@pytest.mark.parametrize("organize", [False, True])
@pytest.mark.parametrize("damage,expected", [
    ("wrong_type", "known_requested_type_mismatch"),
    ("source_unbound", "frozen_source_scope_not_bound"),
])
def test_real_n3_program_rejects_wrong_type_or_unbound_source(tmp_path, monkeypatch, organize, damage, expected):
    text = "须提供质量体系证书。" if damage == "wrong_type" else "请附证明企业依法登记存续的文件。"
    data = source_case(tmp_path, [text])
    if damage == "source_unbound":
        # The existing source ref is valid, but it cannot bind this independent image.
        data["manifest"]["attachments"][0]["source_refs"] = ["company"]
    calls = install_transport(monkeypatch)
    ex = executor(tmp_path / "runtime", settings(), organize)
    run = ex.create_run(data)
    assert ex.execute(run)["status"] == "succeeded"
    report = matching_report(ex, run)
    row, = report["semantic_recall"]["queries"]
    candidate, = row["candidates"]
    assert len(calls) == 1 and candidate["decision"] == "blocked_candidate"
    assert expected in candidate["blockers"] and row["status"] == "all_candidates_blocked"
    assert not ex.output(run)["prepared"]["attachments"]
    assert any(i["severity"] == "missing" for i in ex.output(run)["prepared"]["issues"])


def test_real_n3_changed_source_fails_before_model(tmp_path, monkeypatch):
    data = source_case(tmp_path, ["请附证明企业依法登记存续的文件。"])
    calls = install_transport(monkeypatch)
    Path(data["manifest"]["attachments"][0]["path"]).write_bytes(b"damaged proof")
    ex = executor(tmp_path / "runtime", settings())
    run = ex.create_run(data)
    state = ex.execute(run)
    assert state["status"] == "failed" and state["error_code"] == "SEMANTIC_SOURCE_FILE_CHANGED"
    assert calls == []


def report_for(requirements, manifest):
    return material_candidate_report(requirements, manifest.get("material_records", []),
        attachments=manifest["attachments"], sources=manifest["sources"],
        evidence_selections=manifest["evidence_selections"])


@pytest.mark.parametrize("mode,category", [("factual_response", "commercial"), ("table", "qualification"), ("writing", "technical")])
def test_non_material_business_and_table_scope_never_spends(tmp_path, mode, category):
    data = source_case(tmp_path, ["合同价款及付款日期由双方约定。"])
    req = {"requirement_id": "OUTSIDE", "text": "合同价款及付款日期由双方约定。",
           "response_mode": mode, "category": category}
    plan = recall.plan_recall_scope([req], report_for([req], data["manifest"]), settings())
    assert plan["counts"]["selected"] == plan["counts"]["eligible"] == 0
    assert plan["queries"][0]["status"] == "outside_material_recall_scope"


def test_current_signed_document_and_existing_ten_performances_skip_purchase(tmp_path, monkeypatch):
    titles = [(f"PERF{i}", f"虚构历史项目{i}合同") for i in range(10)]
    data = source_case(tmp_path, ["须提交本次签章承诺书。", "须提供类似项目合同主要页。"], titles=titles)
    data["manifest"]["material_records"] = [{"record_id": "REC-" + aid, "kind": "historical_performance",
        "status": "usable_provisional", "fields": {"project_name": {"value": title}},
        "citations": [{"source_id": aid, "source_sha256": next(s["sha256"] for s in data["manifest"]["sources"] if s["source_id"] == aid)}]}
        for aid, title in titles]
    calls = install_transport(monkeypatch)
    ex = executor(tmp_path / "runtime", settings())
    run = ex.create_run(data)
    state = ex.execute(run)
    assert state["status"] == "succeeded", state.get("error_code")
    report = matching_report(ex, run)
    rows = report["semantic_recall"]["queries"]
    assert {r["status"] for r in rows} == {"requires_current_signed_document", "existing_performance_candidates_require_original_checks"}
    history = next(q for q in report["proof_queries"] if q["request"].get("historical_package_candidates"))
    assert len(history["candidate_group_ids"]) == 10 and calls == []
    assert not ex.output(run)["prepared"]["attachments"]


@pytest.mark.parametrize("bad_scope", ["unknown", "duplicate", "outside"])
def test_invalid_explicit_scope_rejected(tmp_path, bad_scope):
    data = source_case(tmp_path, ["请附证明企业依法登记存续的文件。"])
    reqs = [{"requirement_id": "GOOD", "text": "请附证明企业依法登记存续的文件。", "response_mode": "attachment", "category": "qualification"},
            {"requirement_id": "OUT", "text": "合同价款待双方约定。", "response_mode": "factual_response", "category": "commercial"}]
    selected = {"unknown": ["NO_SUCH_REQUIREMENT"], "duplicate": ["GOOD", "GOOD"], "outside": ["OUT"]}[bad_scope]
    with pytest.raises(ProviderFailure, match="SEMANTIC_SELECTED_SCOPE_INVALID"):
        recall.plan_recall_scope(reqs, report_for(reqs, data["manifest"]), settings(requirement_ids=selected))


def test_real_n3_explicit_scope_does_not_override_no_proof_instruction(tmp_path, monkeypatch):
    data = source_case(tmp_path, ["无需提供营业执照。"])
    rid = data["extracted"]["requirements"][0]["requirement_id"]
    calls = install_transport(monkeypatch)
    ex = executor(tmp_path / "runtime", settings(requirement_ids=[rid]))
    run = ex.create_run(data)
    assert ex.execute(run)["status"] == "succeeded" and calls == []
    report = matching_report(ex, run)
    row, = report["semantic_recall"]["queries"]
    assert row["status"] == "skip_explicit_instruction_guard"
    assert report["semantic_recall"]["counts"]["selected"] == 0
    assert not ex.output(run)["prepared"]["attachments"]
    assert not any("匹配" in item["title"] or "内容待补" in item["title"]
                   for item in ex.output(run)["prepared"]["issues"])


def test_real_n3_query_limit_fails_before_calls_instead_of_silent_truncation(tmp_path, monkeypatch):
    data = source_case(tmp_path, ["请附证明企业依法登记存续的文件。", "请提供本单位经营主体登记凭据。"])
    calls = install_transport(monkeypatch)
    ex = executor(tmp_path / "runtime", settings(max_queries=1))
    run = ex.create_run(data)
    state = ex.execute(run)
    assert state["status"] == "failed" and state["error_code"] == "SEMANTIC_PLANNED_QUERY_LIMIT_EXCEEDED"
    assert calls == []


def test_real_n3_unselected_queries_remain_deferred_not_absent(tmp_path, monkeypatch):
    data = source_case(tmp_path, ["请附证明企业依法登记存续的文件。", "请提供本单位经营主体登记凭据。"])
    selected = data["extracted"]["requirements"][0]["requirement_id"]
    calls = install_transport(monkeypatch)
    ex = executor(tmp_path / "runtime", settings(requirement_ids=[selected]))
    run = ex.create_run(data)
    assert ex.execute(run)["status"] == "succeeded"
    report = matching_report(ex, run)["semantic_recall"]
    assert calls == [selected] and report["status"] == "completed_with_deferred_queries"
    assert report["counts"]["completed"] == report["counts"]["deferred"] == 1
    deferred, = [row for row in report["queries"] if row["status"] == "deferred_not_searched"]
    assert deferred["absence_conclusion"] == "not_established"
    assert any("尚未完成补找" in item["reason"] for item in ex.output(run)["prepared"]["issues"])


def test_real_n3_restart_only_retries_failed_query_and_preserves_log_boundaries(tmp_path, monkeypatch):
    data = source_case(tmp_path, ["请附证明企业依法登记存续的文件。", "请提供本单位经营主体登记凭据。"])
    expected = [r["requirement_id"] for r in data["extracted"]["requirements"]]
    def decide(payload, count):
        if count == 2:
            raise URLError(CANARY + " 本单位经营主体登记凭据")
        return candidate_reply(payload)
    calls = install_transport(monkeypatch, decide)
    root = tmp_path / "runtime"
    ex = executor(root, settings())
    run = ex.create_run(data)
    failed = ex.execute(run)
    assert failed["status"] == "failed" and calls == expected
    successes = {key: unit["output_hash"] for key, unit in failed["nodes"][0]["units"].items()
                 if key.startswith("semantic-query-") and unit["status"] == "succeeded"}
    assert len(successes) == 1
    restarted = executor(root, settings())
    done = restarted.resume(run)
    assert done["status"] == "succeeded" and calls == [*expected, expected[1]]
    assert all(done["nodes"][0]["units"][key]["output_hash"] == value for key, value in successes.items())
    ordinary = "\n".join(path.read_text(encoding="utf-8") for path in (root / "runs" / run).glob("*.jsonl"))
    assert CANARY not in ordinary and "登记凭据" not in ordinary and "依法登记" not in ordinary
    artifacts = "\n".join(path.read_text(encoding="utf-8") for path in (root / "runs" / run / "artifacts").glob("*.json"))
    assert CANARY not in artifacts
    assert matching_report(restarted, run)["semantic_recall"]["counts"]["completed"] == 2


def test_selected_proof_manifest_is_separate_from_n1_and_passes_real_n4_preflight(tmp_path, monkeypatch):
    from company_bid.pipeline import build_executor
    from test_material_source_selection import fixture as selection_fixture

    data = selection_fixture(tmp_path)
    original = copy.deepcopy(data)
    config = {"pipeline": "automatic", "provider": {"mode": "prepared"}, "debug_capture": True,
              "planning": {"semantic_material_recall": {
                  "enabled": False, "additional_evidence": data["selections"]}}}
    calls = install_transport(monkeypatch)
    root = tmp_path / "runtime"
    production = build_executor(root, config)

    def extract_node(ctx, value):
        extracted = extract(ctx, value["parsed"], value["manifest"], lambda payload: response(payload,
            [requirement(block, category="qualification", mode="attachment")
             for block in payload["window"]["blocks"]]))
        return {**value, "extracted": extracted}

    # N1 supplies real DOCX parsing. N2 uses the validated offline candidate
    # adapter. N3 and N4 are the actual production handlers and fingerprints.
    nodes = [NodeSpec("N1", "source-selection-real-docx-fixture", lambda ctx, value: value),
             NodeSpec("N2", "source-selection-offline-extraction", extract_node)]
    nodes.extend(node for node in production.nodes if node.key in {"N3", "N4"})
    ex = Executor(root, nodes, config)
    initial = {"manifest": data["manifest"], "parsed": data["parsed"], "source_artifacts": [],
               "proposals": {}, "input_kind": "automatic"}
    run = ex.create_run(initial)
    done = ex.execute(run)
    assert done["status"] == "succeeded", done.get("error_code")
    assert calls == [] and data == original
    by_node = {node["key"]: node for node in done["nodes"]}
    plan = by_node["N3"]["units"]["plan-assemble"]
    plan_input = ex.store.read_json(plan["input_ref"])
    n1 = ex._checkpoint_data(by_node["N1"])
    assert plan_input["manifest"] == n1["manifest"] == original["manifest"]
    derived = plan_input["material_manifest"]
    assert len(derived["attachments"]) == 2 and not plan_input["manifest"]["attachments"]
    assert derived["facts"] == plan_input["manifest"]["facts"]
    assert derived["sources"][:len(original["manifest"]["sources"])] == original["manifest"]["sources"]
    assert all(derived["source_roles"][source["source_id"]] == "provenance_only"
               for source in derived["sources"][len(original["manifest"]["sources"]):])
    n3 = ex._checkpoint_data(by_node["N3"])
    assert len(n3["prepared"]["attachments"]) == 2
    assert all(file_sha256(Path(item["path"])) == item["sha256"] for item in derived["attachments"])
    preflight, = [c for c in by_node["N4"]["attempts"][-1]["captures"]
                  if c["name"] == "n4-project-constraint-preflight"]
    assert preflight["status"] == "captured"
    assert ex.store.read_json(preflight["ref"])["status"] == "passed"
    assert ex.output(run)["input_kind"] == "automatic"
