"""Contract source-role review guards; synthetic native sources, zero HTTP."""
import copy
import hashlib

import pytest
from docx import Document

from company_bid.contract_pending_plan import contract_pending_plan, contract_source_review_plan
from company_bid.contract_review_slots import contract_review_slot_catalog
from company_bid.diagnostics import inspect_diagnostics
from company_bid.pipeline import build_executor
from company_bid.project_constraint_runtime import _contract_pending_requirements
from company_bid.provider import ChatProposalProvider, PreparedProposalProvider
from company_bid.runtime import Executor, NodeSpec
from company_bid.structured import JsonTaskGateway
from test_contract_pending_plan import requirement
from test_contract_review_slots import ip_source, source
from test_source_selected_citations import fixture


def review_source(number=17, *, old_pending=False):
    return source([["第一章 技术要求", "应编写本项目实施方案。", "第六章 合同条款", f"第{number}条 履约条件",
        f"{number}.1遵守甲方操作规程：乙方进入甲方工作区域应遵守安全操作规程。",
        "如有违反，乙方承担由此产生的全部法律责任；本条不表示责任已经被承诺或履行。",
        f"{number}.2设备合格：除非本合同另有明文规定，乙方所用设备应符合约定标准。",
        "未经甲方同意，乙方应使用本合同约定设备，不得自行扩大更换范围。",
        f"{number}.3若乙方存在第0条以外的其他违约行为，甲方可按本条约定处理。",
        "本条保留通知、纠正及例外条件，不解释第0条的引用对象。",
        f"{number}.4其他：" if old_pending else f"{number}.4固定合成合同条款。",
        f"第{number + 1}条 其他事项", f"{number + 1}.1合成后续正文。"]])


def clauses(parsed, manifest):
    return [item for item in contract_review_slot_catalog(parsed, manifest)["uncovered_clauses"]
            if item["clause_number"].split(".")[1] in {"1", "2", "3"} and len(item["block_ids"]) == 2]


def requirements(parsed, manifest, *, partial=False):
    return [requirement(parsed, manifest, item["block_ids"][:1] if partial else item["block_ids"],
        rid=f"R-{index}", category="technical" if index < 2 else "commercial", mode="writing" if index < 2 else "procedure")
        for index, item in enumerate(clauses(parsed, manifest))]


@pytest.mark.parametrize("number", [6, 17, 28])
def test_contract_role_guards_writing_and_zero_reference_without_number_or_requirement_id_rules(number):
    parsed, manifest = review_source(number)
    original = requirements(parsed, manifest)
    frozen = copy.deepcopy((original, parsed, manifest))
    result = contract_pending_plan(original, parsed, manifest)
    review = result["source_review"]
    assert not result["errors"] and len(review["items"]) == 3
    assert set(review["bindings"]) == {"R-0", "R-1", "R-2"}
    assert not review["derived_requirements"]
    assert all(item["satisfaction"] == "not_determined" and not item["values_inferred"] for item in review["items"])
    assert [item["literal_zero_reference_count"] for item in review["items"]] == [0, 0, 2]
    assert all(item["citation"]["quote"] == item["text"] and len(item["block_ids"]) == 2 for item in review["items"])
    assert "除非" in review["items"][1]["text"] and "未经" in review["items"][1]["text"]
    assert review["items"][2]["resolved_reference"] is None
    assert (original, parsed, manifest) == frozen


def test_partial_original_requirements_are_guarded_and_full_source_is_derived():
    parsed, manifest = review_source()
    original = requirements(parsed, manifest, partial=True)
    frozen = copy.deepcopy(original)
    review = contract_pending_plan(original, parsed, manifest)["source_review"]
    assert len(review["derived_requirements"]) == 3
    assert set(review["bindings"]) >= {"R-0", "R-1", "R-2"}
    for item in review["derived_requirements"]:
        assert item["requirement_id"].startswith("R-CONTRACT-REVIEW-")
        assert item["category"] == "commercial" and item["response_mode"] == "factual_response"
        assert len(item["citations"][0]["block_ids"]) == 2
        assert item["derivation"]["not_model_output"] and not item["derivation"]["n2_candidate_mutation"]
    assert original == frozen


def test_orphan_zero_reference_gets_complete_derived_review_and_duplicates_keep_both_bindings():
    parsed, manifest = review_source()
    orphan = contract_source_review_plan([], parsed, manifest)
    assert len(orphan["items"]) == len(orphan["derived_requirements"]) == 1
    assert orphan["items"][0]["literal_zero_reference_count"] == 2
    original = requirements(parsed, manifest)
    duplicate = {**copy.deepcopy(original[-1]), "requirement_id": "R-SECOND-ZERO"}
    review = contract_source_review_plan([*original, duplicate], parsed, manifest)
    assert review["bindings"]["R-2"] == review["bindings"]["R-SECOND-ZERO"]
    assert not review["derived_requirements"]


def test_noncontract_technical_requirement_still_has_no_contract_review_binding():
    parsed, manifest = review_source()
    block = parsed["documents"][0]["units"][0]["blocks"][1]
    item = requirement(parsed, manifest, [block["block_id"]], category="technical", mode="writing")
    review = contract_source_review_plan([item], parsed, manifest)
    assert item["requirement_id"] not in review["bindings"]
    assert all("contract_terms_are_not_standalone_technical_drafting" not in row["reasons"] for row in review["items"])


def test_existing_ip_pending_handler_keeps_its_three_zero_references_without_duplicate_disposition():
    parsed, manifest = ip_source()
    result = contract_pending_plan([], parsed, manifest)
    assert not result["errors"] and not result["source_review"]["items"]
    assert any(item["blocking_issues"] for items in result["bindings"].values() for item in items)


@pytest.mark.parametrize("damage", ["boundary", "unread", "quote", "origin"])
def test_unproved_contract_sources_never_become_review_dispositions(damage):
    parsed, manifest = review_source()
    original = requirements(parsed, manifest)
    body = parsed["documents"][0]["units"][0]["blocks"]
    if damage == "boundary":
        body[6]["text"] = "17.8设备合格：边界编号不连续。"
    elif damage == "unread":
        parsed["documents"][0]["units"][0]["coverage"]["status"] = "needs_ocr"
    elif damage == "quote":
        original[0]["text"] = original[0]["citations"][0]["quote"] = original[0]["text"].replace("全部法律责任", "部分责任")
    else:
        body[4]["origin"] = "ocr"
    result = contract_pending_plan(original, parsed, manifest)
    assert result["errors"]


def runtime(tmp_path, monkeypatch, *, partial=False, normal_technical=False, old_pending=False):
    parsed, _ = review_source(old_pending=old_pending)
    manifest, _ = fixture(tmp_path)
    manifest["sources"][0]["source_id"] = "S"
    manifest["source_roles"] = {"S": "tender"}
    manifest["project"]["source_refs"] = manifest["company"]["source_refs"] = ["S"]
    path = tmp_path / "synthetic-contract-source.txt"
    path.write_text("\n".join(b["text"] for d in parsed["documents"] for u in d["units"] for b in u["blocks"]), encoding="utf8")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest["sources"][0].update(path=str(path), sha256=sha)
    parsed["documents"][0]["sha256"] = sha
    selected = requirements(parsed, manifest, partial=partial)
    if normal_technical:
        block = parsed["documents"][0]["units"][0]["blocks"][1]
        selected.append(requirement(parsed, manifest, [block["block_id"]], rid="R-NORMAL", category="technical", mode="writing"))
    calls = []

    def gateway(self, ctx, task_type, unit_id, payload, validate):
        calls.append(task_type)
        assert task_type == "extract_requirements"
        targets = {b["block_id"] for b in payload["window"]["blocks"]}
        candidates = [{"citation": {"block_ids": r["citations"][0]["block_ids"]}, "category": r["category"],
            "response_mode": r["response_mode"], "mandatory": True, "confidence": "low", "format_status": "not_prescribed"}
            for r in selected if set(r["citations"][0]["block_ids"]) <= targets]
        return validate({"window_id": payload["window"]["window_id"], "processing_declaration": "processed_not_semantically_accepted",
            "unresolved_block_ids": [], "requirements": candidates, "format_chapters": [], "format_status": "none_verified"})

    def forbidden(*args, **kwargs):
        pytest.fail("Contract source review cannot adopt a fact or call a writing model")

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
    return executor, run


def planning_report(executor, state):
    return next(executor.store.read_json(c["ref"]) for node in state["nodes"] if node["key"] == "N3"
        for attempt in node["attempts"] for c in attempt["captures"] if c["name"] == "n3-planning-report")


def test_production_n3_keeps_real_noncontract_technical_drafting_task(tmp_path, monkeypatch):
    executor, run = runtime(tmp_path, monkeypatch, normal_technical=True)
    done = executor.execute(run, stop_after="N3")
    assert done["status"] == "paused_debug", done.get("error_code")
    prepared = executor.output(run, "N3")["prepared"]
    normal = next(r for r in prepared["requirements"] if r["text"] == "应编写本项目实施方案。")
    report = planning_report(executor, done)
    assert normal["requirement_id"] not in report["contract_pending_plan"]["source_review"]["bindings"]
    assert any(normal["requirement_id"] in task["requirement_refs"] for section in prepared["sections"] for task in section["writing_tasks"])


def test_production_n4_rebuild_keeps_pending_and_source_review_derivations_in_the_same_order(tmp_path, monkeypatch):
    executor, run = runtime(tmp_path, monkeypatch, partial=True, old_pending=True)
    n2 = executor.execute(run, stop_after="N2")
    assert n2["status"] == "paused_debug", n2.get("error_code")
    frozen = copy.deepcopy(executor.output(run, "N2"))
    done = executor.resume(run, stop_after="N4")
    assert done["status"] == "paused_debug", done.get("error_code")
    assert done["nodes"][3]["status"] == done["nodes"][4]["status"] == "succeeded"
    assert executor.output(run, "N2") == frozen
    report = planning_report(executor, done)
    assert report["contract_pending_plan"]["derived_requirements"]
    assert report["contract_pending_plan"]["source_review"]["derived_requirements"]
    assert executor.output(run, "N4")["package"]["sections"]
    diagnostics = inspect_diagnostics(executor.store.root, run)
    assert diagnostics["actual_call_attempts"] == 0 and diagnostics["gaps"] == []


@pytest.mark.parametrize("partial", [False, True])
def test_production_n3_n6_preserves_contract_clauses_and_zero_references_without_paid_writing(tmp_path, monkeypatch, partial):
    executor, run = runtime(tmp_path, monkeypatch, partial=partial)
    n2 = executor.execute(run, stop_after="N2")
    assert n2["status"] == "paused_debug", n2.get("error_code")
    frozen = copy.deepcopy(executor.output(run, "N2"))
    ref = copy.deepcopy(n2["nodes"][2]["output_ref"])
    done = executor.resume(run, stop_after="N6")
    assert done["status"] == "succeeded", done.get("error_code")
    assert executor.output(run, "N2") == frozen and done["nodes"][2]["output_ref"] == ref
    prepared = executor.output(run, "N3")["prepared"]
    report = planning_report(executor, done)
    review = report["contract_pending_plan"]["source_review"]
    assert len(review["items"]) == 3 and report["contract_source_review_handling"]
    assert all(not section["writing_tasks"] and not section["fact_refs"] for section in prepared["sections"])
    bound_ids = set(review["bindings"])
    assert all(row["status"] == "pending_manual" for row in executor.output(run, "N5")["validation"]["requirement_coverage"] if row["requirement_id"] in bound_ids)
    files = executor.output(run)["files"]
    for name in ["bid_docx", "issues_docx"]:
        document = Document(executor.store.root / files[name]["path"])
        text = "\n".join(p.text for p in document.paragraphs)
        assert all(item["text"] in text for item in review["items"])
        assert "第0条回指待澄清" in text and "除非" in text and "未经" in text
    diagnostics = inspect_diagnostics(executor.store.root, run)
    assert diagnostics["actual_call_attempts"] == 0 and diagnostics["gaps"] == []


@pytest.mark.parametrize("damage", ["source_text", "source_ref", "issue", "writing", "fact", "report_proof", "zero_resolution", "count_type"])
def test_downstream_rebuild_checks_actual_source_disposition_and_rejects_tampering(tmp_path, monkeypatch, damage):
    executor, run = runtime(tmp_path, monkeypatch)
    done = executor.execute(run, stop_after="N3")
    assert done["status"] == "paused_debug", done.get("error_code")
    prepared = executor.output(run, "N3")["prepared"]
    report = planning_report(executor, done)
    row = report["contract_source_review_handling"][0]
    section = next(s for s in prepared["sections"] if s["section_id"] == row["section_id"])
    block = next(b for b in section["fixed_content"] if b.get("text", "").startswith("合同条款原文"))
    if damage == "source_text": block["text"] = block["text"].replace("全部法律责任", "部分责任")
    elif damage == "source_ref": block["source_refs"] = []
    elif damage == "issue": next(i for i in prepared["issues"] if i["issue_id"] == row["issue_ids"][0])["source_requirement"] = "已满足"
    elif damage == "writing": section["writing_tasks"].append({"requirement_refs": [row["requirement_id"]]})
    elif damage == "fact": section["fact_refs"].append("F-FORGED")
    elif damage == "report_proof": report["contract_pending_plan"]["source_review"]["items"][0]["source_proof_sha256"] = "0" * 64
    elif damage == "zero_resolution": report["contract_pending_plan"]["source_review"]["items"][-1]["resolved_reference"] = "17.1"
    else: row["literal_zero_reference_count"] = False
    with pytest.raises(Exception, match="PROJECT_CONSTRAINT_CONTRACT_(?:PENDING|SOURCE_REVIEW)_CHANGED"):
        _contract_pending_requirements(report, prepared, executor.output(run, "N1"), executor.output(run, "N2")["extracted"]["requirements"])
