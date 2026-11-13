"""Production N3-N6 contract pending paths with synthetic N1 and zero HTTP."""
import copy
import hashlib
from pathlib import Path

import pytest
from docx import Document

from company_bid.diagnostics import inspect_diagnostics
from company_bid.contract_review_slots import contract_review_slot_catalog
from company_bid.pipeline import build_executor
from company_bid.provider import ChatProposalProvider, PreparedProposalProvider
from company_bid.runtime import Executor, NodeSpec
from company_bid.structured import JsonTaskGateway
from test_contract_review_slots import acceptance, attachment_tail, fees, ip_source, rights
from test_source_selected_citations import fixture


def text(document):
    return "\n".join([*(p.text for p in document.paragraphs),
                      *(cell.text for table in document.tables for row in table.rows for cell in row.cells)])


def runtime(tmp_path, monkeypatch, factory, *, mode="procedure", category="commercial", damage=None):
    parsed, _ = factory()
    manifest, _ = fixture(tmp_path)
    manifest["sources"][0]["source_id"] = "S"
    manifest["source_roles"] = {"S": "tender"}
    manifest["project"]["source_refs"] = manifest["company"]["source_refs"] = ["S"]
    path = tmp_path / "synthetic-contract-source.txt"
    path.write_text("\n".join(b["text"] for d in parsed["documents"] for u in d["units"] for b in u["blocks"]), encoding="utf8")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest["sources"][0].update(path=str(path), sha256=sha)
    parsed["documents"][0]["sha256"] = sha
    catalog = contract_review_slot_catalog(parsed, manifest)
    selected = next((item for item in catalog["items"] if item["slot_forms"] != ["other_terms"]), catalog["items"][0])
    if damage:
        damage(parsed)
    calls = []

    def gateway(self, ctx, task_type, unit_id, payload, validate):
        calls.append(task_type)
        assert task_type == "extract_requirements", "Contract pending items must not ask for model mapping"
        targets = {block["block_id"] for block in payload["window"]["blocks"]}
        requirements = []
        if set(selected["block_ids"]) <= targets:
            requirements.append({"citation": {"block_ids": selected["block_ids"]}, "category": category,
                "response_mode": mode, "mandatory": True, "confidence": "low", "format_status": "not_prescribed",
                "required_fact_labels": ["含税总额"] if mode == "factual_response" else []})
        return validate({"window_id": payload["window"]["window_id"],
            "processing_declaration": "processed_not_semantically_accepted", "unresolved_block_ids": [],
            "requirements": requirements, "format_chapters": [], "format_status": "none_verified"})

    def forbidden(*args, **kwargs):
        pytest.fail("Contract pending or clarification must not call a model, map facts, or guess a value")

    monkeypatch.setattr(JsonTaskGateway, "__call__", gateway)
    monkeypatch.setattr(ChatProposalProvider, "generate", forbidden)
    monkeypatch.setattr(ChatProposalProvider, "generate_json", forbidden)
    monkeypatch.setattr(PreparedProposalProvider, "generate", forbidden)
    monkeypatch.setattr("company_bid.planning._label_facts", forbidden)
    config = {"pipeline": "automatic", "debug_capture": True, "provider": {"mode": "prepared"},
              "planning": {"map_table_fields": False}, "extraction": {"window_max_chars": 14000, "window_max_blocks": 60}}
    root = tmp_path / "runtime"
    production = build_executor(root, config)

    def freeze(ctx, value):
        value = copy.deepcopy(value)
        ref = ctx.store_bytes("synthetic-contract-source.txt", path.read_bytes())
        value["manifest"]["sources"][0]["path"] = str(ctx.executor.store.root / ref["path"])
        value["source_artifacts"] = [ref]
        return value

    executor = Executor(root, [NodeSpec("N0", "synthetic-freeze-v1", freeze, ()),
        NodeSpec("N1", "synthetic-native-reading-v1", lambda ctx, value: value, ()), *production.nodes[2:]], config)
    run = executor.create_run({"manifest": manifest, "parsed": parsed, "input_kind": "automatic",
                               "source_artifacts": [], "proposals": {}, "recorded_candidates": {}})
    return executor, run, calls, selected


def capture(executor, state, name):
    return [executor.store.read_json(c["ref"]) for node in state["nodes"] if node["key"] == "N3"
            for attempt in node["attempts"] for c in attempt["captures"] if c["name"] == name][0]


@pytest.mark.parametrize("factory, expected", [(rights, "甲方待约定"), (fees, "付款方式选项"),
                                               (acceptance, "连续不合格次数"), (ip_source, "第0条回指待澄清"),
                                               (attachment_tail, "附件五")])
def test_production_n3_n6_preserves_full_obligations_and_pending_contract_fields_without_http(tmp_path, monkeypatch, factory, expected):
    executor, run, calls, selected = runtime(tmp_path, monkeypatch, factory)
    n2_state = executor.execute(run, stop_after="N2")
    assert n2_state["status"] == "paused_debug", n2_state.get("error_code")
    frozen_n2 = copy.deepcopy(executor.output(run, "N2"))
    ref = n2_state["nodes"][2]["output_ref"]
    checkpoint_hash = hashlib.sha256((executor.store.root / ref["path"]).read_bytes()).hexdigest()
    done = executor.resume(run, stop_after="N6")
    assert done["status"] == "succeeded", done.get("error_code")
    assert done["nodes"][2]["output_ref"] == ref
    assert hashlib.sha256((executor.store.root / ref["path"]).read_bytes()).hexdigest() == checkpoint_hash
    assert executor.output(run, "N2") == frozen_n2
    prepared = executor.output(run, "N3")["prepared"]
    report = capture(executor, done, "n3-planning-report")
    pending = report["contract_pending_plan"]
    handling = report["contract_pending_handling"]
    assert pending["errors"] == [] and pending["catalog_summary"]["exhaustive"] is False
    assert handling and len({(row["requirement_id"], row["catalog_pending_id"]) for row in handling}) == len(handling)
    assert all(not row["values_inferred"] and row["physical_blank_status"] == row["satisfaction_status"] == "not_determined" for row in handling)
    assert all(not section["writing_tasks"] and not section["fact_refs"] for section in prepared["sections"])
    assert all(not row["adopted_fact_ids"] and not row["writing_unit_ids"] for row in handling)
    assert not report["factual_responses"]
    existing = frozen_n2["extracted"]["requirements"][0]
    planned_existing = next(item for item in prepared["requirements"] if item["requirement_id"] == existing["requirement_id"])
    assert planned_existing["text"] == existing["text"] and planned_existing["response_mode"] == existing["response_mode"]
    n5 = executor.output(run, "N5")
    bound_ids = set(pending["bindings"])
    assert all(row["status"] == "pending_manual" and row.get("satisfaction_status") not in {"satisfied", "accepted"}
               for row in n5["validation"]["requirement_coverage"] if row["requirement_id"] in bound_ids)
    assert n5["validation"]["writing_units"] == []
    files = executor.output(run)["files"]
    bid = text(Document(executor.store.root / files["bid_docx"]["path"]))
    issues = text(Document(executor.store.root / files["issues_docx"]["path"]))
    assert selected["text"] in bid and selected["text"] in issues
    assert expected in bid and expected in issues
    assert "合同待约定原文（已读文本未给值）" in bid and "物理留白未判定" in issues
    for row in handling:
        for issue_id in row["issue_ids"]:
            assert issue_id in bid and issue_id in issues
    if factory is rights:
        assert all(any(raw["text"] == item["text"] for raw in pending["derived_requirements"])
                   for items in pending["bindings"].values() for item in items if item["owner_party"] == "甲方" and item["subclause_number"] in {"2", "3"})
    if factory is acceptance:
        complete = next(item for items in pending["bindings"].values() for item in items if "acceptance_deadlines" in item["slot_forms"])
        assert complete["text"] in bid and "停止支付" in bid and "扣减费用" in bid
    if factory is ip_source:
        assert "第0条" in bid and "50%" in bid
        assert any(row.get("blocking_issues") for row in handling)
        assert not any(item["owner_party"] for items in pending["bindings"].values() for item in items if item["blocking_issues"])
    diagnostics = inspect_diagnostics(executor.store.root, run)
    assert diagnostics["actual_call_attempts"] == 0 and diagnostics["gaps"] == []
    assert calls == ["extract_requirements"]


def test_existing_factual_contract_fields_do_not_adopt_facts_or_spawn_mapping_calls(tmp_path, monkeypatch):
    executor, run, calls, _ = runtime(tmp_path, monkeypatch, fees, mode="factual_response")
    done = executor.execute(run, stop_after="N6")
    assert done["status"] == "succeeded", done.get("error_code")
    report = capture(executor, done, "n3-planning-report")
    assert not report["factual_responses"] and calls == ["extract_requirements"]
    assert all(not section["fact_refs"] and not section["writing_tasks"] for section in executor.output(run, "N3")["prepared"]["sections"])


@pytest.mark.parametrize("category, mode", [("technical", "writing"), ("commercial", "writing")])
def test_production_source_overlap_blocks_n3_before_any_paid_writing(tmp_path, monkeypatch, category, mode):
    executor, run, calls, _ = runtime(tmp_path, monkeypatch, rights, category=category, mode=mode)
    done = executor.execute(run, stop_after="N6")
    assert done["status"] == "failed" and done["error_code"] == "CONTRACT_PENDING_REQUIRES_SOURCE_REVIEW"
    assert all(node["status"] == "pending" for node in done["nodes"] if node["key"] in {"N4", "N5", "N6"})
    diagnostic = capture(executor, done, "n3-contract-pending-plan")
    assert any(item["code"] == "requires_source_review" for item in diagnostic["errors"])
    assert calls == ["extract_requirements"]


def test_production_missing_expected_page_evidence_stops_n3_instead_of_using_old_blank_default(tmp_path, monkeypatch):
    executor, run, calls, _ = runtime(tmp_path, monkeypatch, fees,
                                    damage=lambda parsed: parsed["documents"][0].pop("expected_units"))
    done = executor.execute(run, stop_after="N6")
    assert done["status"] == "failed" and done["error_code"] == "CONTRACT_PENDING_REQUIRES_SOURCE_REVIEW"
    assert not done["nodes"][3].get("output_ref") and calls == ["extract_requirements"]


def test_pending_planner_code_invalidates_all_runtime_consumers_and_not_n2(tmp_path, monkeypatch):
    config = {"pipeline": "automatic", "provider": {"mode": "prepared"}}
    before = {node.key: node.version for node in build_executor(tmp_path, config).nodes}
    original = Path.read_text

    def changed(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        return value + "\n# changed N3 source-only pending planner\n" if path.name == "contract_pending_plan.py" else value

    monkeypatch.setattr(Path, "read_text", changed)
    after = {node.key: node.version for node in build_executor(tmp_path, config).nodes}
    assert {key for key in before if before[key] != after[key]} == {"N3", "N4", "N5", "N6"}


@pytest.mark.parametrize("change", ["derived_text", "derived_mode", "report_proof", "delete_binding", "prepared_text",
    "prepared_mode", "prepared_source", "delete_issue", "issue_source", "owner", "adopt_fact", "claim_satisfied",
    "writing_task", "source_unread", "n2_text"])
def test_runtime_independently_rebuilds_pending_source_and_rejects_report_or_output_tampering(tmp_path, monkeypatch, change):
    from company_bid.project_constraint_runtime import _contract_pending_requirements
    from company_bid.project_constraints import ProjectConstraintError

    executor, run, _, _ = runtime(tmp_path, monkeypatch, fees)
    done = executor.execute(run, stop_after="N3")
    assert done["status"] == "paused_debug", done.get("error_code")
    n1 = executor.output(run, "N1")
    original = executor.output(run, "N2")["extracted"]["requirements"]
    prepared = executor.output(run, "N3")["prepared"]
    report = capture(executor, done, "n3-planning-report")
    derived = report["contract_pending_plan"]["derived_requirements"][0]
    rid = derived["requirement_id"]
    actual = next(item for item in prepared["requirements"] if item["requirement_id"] == rid)
    row = next(item for item in report["contract_pending_handling"] if item["requirement_id"] == rid)
    assert _contract_pending_requirements(report, prepared, n1, original)
    if change == "derived_text": derived["text"] += "猜定合成值。"
    elif change == "derived_mode": derived["response_mode"] = "procedure"
    elif change == "report_proof": report["contract_pending_plan"]["catalog_summary"]["catalog_sha256"] = "0" * 64
    elif change == "delete_binding": report["contract_pending_plan"]["bindings"].pop(rid)
    elif change == "prepared_text": actual["text"] += "猜定合成值。"
    elif change == "prepared_mode": actual["response_mode"] = "writing"
    elif change == "prepared_source":
        next(item for item in prepared["sources"] if item["source_id"] == actual["source_refs"][0])["locator"] += "; guessed"
    elif change == "delete_issue": prepared["issues"] = [item for item in prepared["issues"] if item["issue_id"] != row["issue_ids"][0]]
    elif change == "issue_source": next(item for item in prepared["issues"] if item["issue_id"] == row["issue_ids"][0])["source_requirement"] = "删去完整义务。"
    elif change == "owner": row["owner_party"] = "猜定主体"
    elif change == "adopt_fact": row["adopted_fact_ids"] = ["FAKE"]
    elif change == "claim_satisfied": row["satisfaction_status"] = "satisfied"
    elif change == "writing_task": prepared["sections"][0]["writing_tasks"].append({"requirement_refs": [rid]})
    elif change == "source_unread": n1["parsed"]["documents"][0]["units"][0]["coverage"]["status"] = "failed"
    elif change == "n2_text": original[0]["text"] += "修改原候选。"
    with pytest.raises(ProjectConstraintError, match="PROJECT_CONSTRAINT_CONTRACT_PENDING_CHANGED"):
        _contract_pending_requirements(report, prepared, n1, original)


def test_runtime_cannot_resolve_literal_zero_reference_via_a_tampered_handling_row(tmp_path, monkeypatch):
    from company_bid.project_constraint_runtime import _contract_pending_requirements
    from company_bid.project_constraints import ProjectConstraintError

    executor, run, _, _ = runtime(tmp_path, monkeypatch, ip_source)
    done = executor.execute(run, stop_after="N3")
    assert done["status"] == "paused_debug", done.get("error_code")
    report = capture(executor, done, "n3-planning-report")
    row = next(item for item in report["contract_pending_handling"] if item.get("blocking_issues"))
    row["blocking_issues"][0]["resolved_reference"] = "假定第11.1条"
    with pytest.raises(ProjectConstraintError, match="PROJECT_CONSTRAINT_CONTRACT_PENDING_CHANGED"):
        _contract_pending_requirements(report, executor.output(run, "N3")["prepared"], executor.output(run, "N1"),
                                       executor.output(run, "N2")["extracted"]["requirements"])
