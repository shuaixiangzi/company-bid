"""Fully read administrative rules stay separate from N2 candidates and facts."""
import copy
import hashlib
from pathlib import Path

import pytest
from docx import Document
from pydantic import ValidationError

from company_bid import pipeline
from company_bid.extraction import PlanningError, _index
from company_bid.models import PreparedBid
from company_bid.planning import _instruction_placements, plan_bid
from company_bid.runtime import Executor, NodeSpec
from test_n3_source_roles import extract, line, sources
from test_source_selected_citations import Context, no_call, selected


OTHER_B = "（6）采购文件要求提供的其他商务类相关资料和供应商认为必要的其他内容。"
OTHER_T = "④采购文件要求提供的其他相关资料和供应商认为必要的其他内容。"
OUTER = "B．不带商务报价的技术部分包括："
PARENT = "（1）服务类适用"
TAIL = ("投标文件封面须法定代表人或授权代表签署并加盖单位公章，并在规定的时间内将投标",
        "文件递交至合成电子招标平台。")
PERMISSION = "2.投标文件部分文本格式（未提供格式的投标文件由投标人自定义格式）"


def fixture(tmp_path, *, mixed=False, missing_plan=False, summary=False, explicit_table=False,
            supplied_rules=False, tail_mode="procedure", forbidden_summary=False):
    manifest, parsed = sources(tmp_path)
    values = parsed["documents"][0]["units"][0]["blocks"]
    by_id = {item["block_id"]: item for item in values}
    by_id["PLAN"]["text"] = "①应编写合成测点的巡检方案。"
    by_id["PERMISSION"]["text"] = PERMISSION
    if summary:
        by_id["PROOF"]["text"] = "（2）合成检测证明文件汇总（如有）。"
        if explicit_table:
            by_id["PROOF"]["text"] += "同时提交检测项目清单。"
    if forbidden_summary:
        by_id["PROOF"]["text"] = "（2）不得提交合成检测证明文件汇总。"
    extras = {}
    for key, text in (("A", "A．商务材料包括："), ("OTHER_B", OTHER_B), ("B", OUTER),
                      ("PARENT", PARENT), ("OTHER_T", OTHER_T + ("另须提交检测证书。" if mixed else "")),
                      ("TAIL_0", TAIL[0]), ("TAIL_1", TAIL[1])):
        extras[key] = line(parsed, key, text)
    by_id.update(extras)
    values[:] = [by_id[key] for key in ("REGION", "COMPOSITION", "A", "PROOF", "OTHER_B", "B", "PARENT",
                                      "PLAN", "OTHER_T", "TAIL_0", "TAIL_1", "PERMISSION")]
    for i, item in enumerate(values):
        item["locator"] = f"pdf/page[1]/line[{i}]"
    path = Path(manifest["sources"][0]["path"])
    path.write_text("\n".join(item["text"] for item in values), encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest["sources"][0]["sha256"] = parsed["documents"][0]["sha256"] = digest

    def change(raw):
        if not raw["requirements"]:
            return
        raw["requirements"] = [item for item in raw["requirements"]
                               if item["response_mode"] != "procedure" and
                               (not missing_plan or item["response_mode"] != "writing")]
        raw["requirements"].append({"citation": {"block_ids": ["TAIL_0", "TAIL_1"]},
            "category": "procedure" if tail_mode == "procedure" else "technical",
            "response_mode": tail_mode, "format_status": "not_prescribed"})
        if supplied_rules:
            for key in ("OTHER_B", "OTHER_T", "PERMISSION"):
                raw["requirements"].append({"citation": selected(by_id[key]), "category": "procedure",
                    "response_mode": "procedure", "format_status": "not_prescribed"})
    extracted = extract(manifest, parsed, change=change)
    return manifest, parsed, extracted


def planned(tmp_path, **options):
    manifest, parsed, extracted = fixture(tmp_path, **options)
    frozen = copy.deepcopy(extracted)
    ctx = Context()
    result = plan_bid(ctx, extracted, parsed, manifest, no_call, {"map_table_fields": False})
    assert extracted == frozen
    return result, ctx, extracted


def test_missing_generic_candidates_become_explicit_n3_rules_not_n2_success(tmp_path):
    result, ctx, extracted = planned(tmp_path)
    derived = [item for item in result["requirements"] if item["requirement_id"].startswith("R-PREP-")]
    assert {item["text"] for item in derived} == {OTHER_B, OTHER_T, PERMISSION}
    assert all(item["response_mode"] == "procedure" for item in derived)
    assert not any(item["text"] in {OTHER_B, OTHER_T, PERMISSION} for item in extracted["requirements"])
    report = ctx.captures["n3-preparation-source-rules"]
    assert len(report["derived_requirements"]) == 3
    assert report["n2_candidate_mutation"] is False
    action = next(item for item in result["review_actions"] if item["source_requirement"] == "\n".join(TAIL))
    assert action["source_scope_status"] == "unresolved" and action["status"] == "pending_manual"
    assert [item["text"] for item in action["source_scope_contexts"]] == [OUTER, PARENT]
    assert not any(section["render_body"] for section in result["sections"] if
                   set(section["requirement_refs"]) & set(action["requirement_refs"]))


def test_already_selected_generic_rules_are_not_duplicated(tmp_path):
    result, ctx, extracted = planned(tmp_path, supplied_rules=True)
    assert not any(item["requirement_id"].startswith("R-PREP-") for item in result["requirements"])
    assert all(sum(item["text"] == text for item in result["requirements"]) == 1
               for text in (OTHER_B, OTHER_T, PERMISSION))


@pytest.mark.parametrize("options", [{"mixed": True}, {"missing_plan": True}, {"tail_mode": "writing"},
                                    {"summary": True, "explicit_table": True}, {"forbidden_summary": True}])
def test_generic_and_manual_rules_cannot_discharge_substantive_unknowns(tmp_path, options):
    manifest, parsed, extracted = fixture(tmp_path, **options)
    with pytest.raises(PlanningError, match="REQUIRED_FORMAT_UNRESOLVED"):
        plan_bid(Context(), extracted, parsed, manifest, no_call, {"map_table_fields": False})


def test_compilation_requires_real_attachment_or_missing_issue_without_guessing_table(tmp_path):
    result, ctx, _ = planned(tmp_path, summary=True)
    rid = next(item["requirement_id"] for item in result["requirements"] if "证明文件汇总" in item["text"])
    assert any(rid in item["requirement_refs"] for item in result["issues"])
    assert not any(rid in table["requirement_refs"] for table in result["tables"])
    handling = next(item for item in ctx.captures["n3-instruction-handling"]["items"] if item["requirement_id"] == rid)
    assert handling["proof_compilation_status"] == "pending_review"
    assert handling["satisfaction_status"] == "not_determined"


def test_scope_context_tampering_is_rejected(tmp_path):
    result, _, _ = planned(tmp_path)
    action = next(item for item in result["review_actions"] if item["source_scope_status"] == "unresolved")
    action["source_scope_contexts"][0]["text"] += "假冒范围"
    with pytest.raises(ValidationError, match="scope"):
        PreparedBid.model_validate(result)


def test_compilation_warning_alone_does_not_prove_an_attachment_handler():
    ledger = [{"instruction_requirements": [{"requirement_id": "PROOF", "response_mode": "attachment",
                                            "proof_compilation": True}]}]
    warning = {"issue_id": "M-001", "requirement_refs": ["PROOF"], "severity": "warning"}
    _, errors = _instruction_placements(ledger, [], [], [], [warning])
    assert errors == [{"reason": "preparation_instruction_handler_missing", "requirement_id": "PROOF"}]


def test_production_consumers_keep_pending_scope_and_render_the_context(tmp_path):
    manifest, parsed, extracted = fixture(tmp_path)
    config = {"debug_capture": True, "provider": {"mode": "prepared"}}
    base = pipeline.build_executor(tmp_path / "runtime", config)

    def n3(ctx, value):
        prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {"map_table_fields": False})
        proposals = {task["unit_id"]: {"paragraphs": ["拟按测点编号核对异常并复测，保留原始记录后提交复核。"],
                    "steps": [], "coverage": [{"item_index": i, "paragraph_indices": [0]}
                        for i in range(len(task["must_cover"]))]}
                    for section in prepared["sections"] for task in section["writing_tasks"]}
        return {"prepared": prepared, "proposals": proposals, "source_artifacts": [], "input_kind": "automatic"}

    executor = Executor(base.store.root, [NodeSpec("N3", "synthetic-preparation-rules", n3, ()),
                        *(node for node in base.nodes if node.key in {"N4", "N5", "N6"})], base.execution_config)
    run = executor.create_run({"synthetic": True})
    state = executor.execute(run)
    assert state["status"] == "succeeded", state.get("error_code")
    output = executor.output(run, "N5")
    action = next(item for item in output["package"]["review_actions"] if item["source_scope_status"] == "unresolved")
    row = next(row for row in output["validation"]["requirement_coverage"] if row["requirement_id"] in action["requirement_refs"])
    assert row["source_scope_status"] == "unresolved" and row["satisfaction_status"] == "not_determined"
    files = executor.output(run)["files"]
    issue_text = "\n".join(item.text for item in Document(base.store.root / files["issues_docx"]["path"]).paragraphs)
    assert "适用范围尚未确定" in issue_text and OUTER in issue_text and PARENT in issue_text
