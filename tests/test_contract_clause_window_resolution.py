"""Source-bound recovery of numbered contract clauses across different cuts."""

import copy
import threading

import pytest

from company_bid.contract_clause_resolution import INDEX_OUTPUT_KEY, RESOLUTION_KEY, WINDOW_CONTEXT_KEY
from company_bid.extraction import (
    PlanningError, _index, _windows, build_contract_clause_indexes, extraction_window_payload,
    extract_requirements, validate_live_window,
)
from company_bid.planning import plan_bid
from company_bid.runtime import Executor, NodeSpec
from test_source_selected_citations import Context, block, fixture, no_call


def _scenario(tmp_path, cap, *, optional_notice=False, technical_duty=False):
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"
    lines = [
        ("H", "第四部分 合同文本"),
        ("CH", "第一条 服务交接"),
        *[(f"I{i}", "本条各款均适用于合同履行期间。") for i in range(cap - 4)],
        ("P1", "1.1 乙方应保存交接记录。发生故障时，"),
        ("P2", "经双方确认的设施改动应由乙方登记，"),
        ("P3", "登记资料须随服务记录统一归档。"),
        ("R", "1.2 甲乙双方共同维护设备台账。" if cap == 4 else "1.2 双方应维护设备台账，"),
        *[(f"R{i}", "每次交接时须更新有关记录。") for i in range(cap - 4)],
        ("N1", "1.3 双方履行合同义务期间，若设备异常，乙方应" + (
            "编制作业技术方案，" if technical_duty else "记录故障，")),
        ("N2", "在【四（4）】小时内" + ("无需" if optional_notice else "以书面形式") + "通知甲方，"),
        ("N3", "并在三日内向甲方提交故障报告。"),
        ("N4", "未按时报告的，乙方应承担因此增加的费用。"),
        ("SUB", "1.4 乙方应按以下流程开展设施交接。"),
        ("TECH", "1.4.1 乙方应编制隔离顺序并实施逐台复核。"),
    ]
    for index, (identity, text) in enumerate(lines):
        x = 95 if identity in {"P1", "R", "N1", "SUB", "TECH"} else 75
        item = block(parsed, identity, text, origin="native", bbox=[x, 50 + index * 17, 450, 62 + index * 17])
        item["locator"] = f"pdf/page[1]/line[{index}]"
    config = {"window_max_blocks": cap}
    return manifest, parsed, config


def _requirement(ids, category="procedure", mode="procedure"):
    return {"citation": {"block_ids": ids}, "category": category, "response_mode": mode,
            "format_status": "not_prescribed", "confidence": "high"}


def _answer(window, *, predecessor="complete", successor="select", overlap=False, extra=False):
    ids = {item["block_id"] for item in window["blocks"]}
    unresolved, requirements = [], []
    if "P1" in ids:
        if predecessor != "missing":
            span = ["P1", "P2", "P3"] if predecessor == "complete" else ["P1", "P2"]
            requirements.append(_requirement(span))
    if "N2" in ids:
        unresolved = ["P3", "N1", "N2"]
        if extra:
            unresolved.append("R")
        requirements.append(_requirement(sorted(key for key in ids if key.startswith("R"))))
        if overlap:
            requirements.append(_requirement(["P3"]))
    if "N3" in ids:
        if successor == "unresolved":
            unresolved = ["N3", "N4"]
        else:
            category = "commercial" if successor == "select" else "procedure"
            requirements.append(_requirement(["N1", "N2", "N3", "N4"], category))
        requirements.append(_requirement(["TECH"], "technical", "writing"))
    return {"window_id": window["window_id"], "processing_declaration": "processed_not_semantically_accepted",
            "unresolved_block_ids": unresolved, "requirements": requirements,
            "format_chapters": [], "format_status": "none_verified"}


def _call(**options):
    return lambda _ctx, _task, _unit, payload, validate: validate(_answer(payload["window"], **options))


@pytest.mark.parametrize("cap", [4, 5])
@pytest.mark.parametrize("successor", ["select", "unresolved", "different_category"])
def test_public_extraction_and_planning_preserve_sources_and_independent_child(tmp_path, cap, successor):
    manifest, parsed, config = _scenario(tmp_path, cap)
    blocks, _, documents = _index(parsed, manifest)
    legacy, _ = _windows(documents, blocks, config, manifest=manifest)
    indexes = build_contract_clause_indexes(documents, blocks)
    enriched, _ = _windows(documents, blocks, config, manifest=manifest, contract_clause_indexes=indexes)
    assert WINDOW_CONTEXT_KEY in enriched[1]
    assert [extraction_window_payload(window, config) for window in enriched] == [
        extraction_window_payload(window, config) for window in legacy]
    context = Context()
    output = extract_requirements(context, parsed, manifest, _call(successor=successor), config)
    assert output[INDEX_OUTPUT_KEY] == indexes == context.captures["n2-contract-clause-indexes"]["items"]
    assert output["coverage"]["semantic_review"] == "requires_source_review"
    first, middle, last = output["window_results"]
    assert first["raw_model_candidate"] == _answer(legacy[0])
    assert middle["raw_model_candidate"] == _answer(legacy[1])
    assert middle["processing"]["unresolved_block_ids"] == ["P3", "N1", "N2"]
    assert len(middle["validated"]["requirements"]) == 1
    prior, added = middle[RESOLUTION_KEY]["items"]
    assert prior["witness"]["window_id"] == first["window_id"] and "requirement" not in prior
    assert added["requirement"]["citation"]["block_ids"] == ["N1", "N2", "N3", "N4"]
    assert added["requirement"]["citation"]["quote"] == "\n".join(blocks[key]["text"] for key in ("N1", "N2", "N3", "N4"))
    assert added["closing_window_id"] == last["window_id"]
    assert all(item["citations"][0]["block_id"] != "P3" for item in output["requirements"])
    same_clause = [item for item in output["requirements"] if item["citations"][0]["block_id"] == "N1"]
    if successor == "select":
        assert len(same_clause) == 1 and same_clause[0]["window_ids"] == [middle["window_id"], last["window_id"]]
    elif successor == "unresolved":
        assert last[RESOLUTION_KEY]["items"][0]["witness"]["basis"] == "validated_source_requirement"
        assert len(same_clause) == 1 and same_clause[0]["window_ids"] == [middle["window_id"]]
    else:
        assert {item["category"] for item in same_clause} == {"procedure", "commercial"}
    independent, = [item for item in output["requirements"] if item["citations"][0]["block_id"] == "TECH"]
    assert independent["response_mode"] == "writing" and independent["category"] == "technical"
    prepared = plan_bid(Context(), output, parsed, manifest, no_call, {})
    assert prepared["requirements"]


@pytest.mark.parametrize("scenario, call_options", [
    ({}, {"predecessor": "clipped"}), ({}, {"predecessor": "missing"}), ({}, {"overlap": True}),
    ({}, {"extra": True}), ({"optional_notice": True}, {}), ({"technical_duty": True}, {}),
])
def test_mixed_or_unproved_targets_still_stop_at_original_unresolved_gate(tmp_path, scenario, call_options):
    manifest, parsed, config = _scenario(tmp_path, 4, **scenario)
    context = Context()
    with pytest.raises(PlanningError, match="LIVE_REQUIRED_BLOCK_UNRESOLVED"):
        extract_requirements(context, parsed, manifest, _call(**call_options), config)
    assert len(context.units) == 1
    assert "n2-contract-clause-indexes" in context.captures


def test_recovery_without_prior_and_rehashed_index_changes_fail_closed(tmp_path):
    manifest, parsed, config = _scenario(tmp_path, 4)
    blocks, _, docs = _index(parsed, manifest)
    indexes = build_contract_clause_indexes(docs, blocks)
    windows, _ = _windows(docs, blocks, config, manifest=manifest, contract_clause_indexes=indexes)
    with pytest.raises(PlanningError, match="LIVE_REQUIRED_BLOCK_UNRESOLVED"):
        validate_live_window(_answer(windows[1]), windows[1])
    output = extract_requirements(Context(), parsed, manifest, _call(), config)
    tampered = copy.deepcopy(output)
    tampered[INDEX_OUTPUT_KEY][0]["entries"][0]["quote"] += "修改。"
    with pytest.raises(PlanningError, match="EXTRACTION_CONTRACT_CLAUSE_INDEX_MISMATCH"):
        plan_bid(Context(), tampered, parsed, manifest, no_call, {})
    tampered = copy.deepcopy(output)
    tampered["window_results"][1][RESOLUTION_KEY]["items"][0]["witness"]["requirement_sha256"] = "0" * 64
    with pytest.raises(PlanningError, match="EXTRACTION_CONTRACT_CLAUSE_RESOLUTION_MISMATCH"):
        plan_bid(Context(), tampered, parsed, manifest, no_call, {})


def test_parallel_recovery_waits_for_actual_preceding_checkpoint(tmp_path):
    manifest, parsed, config = _scenario(tmp_path, 4)
    middle_arrived = threading.Event()

    def call(_ctx, _task, _unit, payload, validate):
        window = payload["window"]
        ids = {item["block_id"] for item in window["blocks"]}
        if "P1" in ids:
            assert middle_arrived.wait(5), "Middle window never entered parallel validation."
        if "N2" in ids:
            middle_arrived.set()
        return validate(_answer(window))

    def run(ctx, value):
        return extract_requirements(ctx, value["parsed"], value["manifest"], call,
                                    {**config, "window_concurrency": 2})

    executor = Executor(tmp_path / "runtime", [NodeSpec("N2", "contract-recovery-test", run)], {"debug_capture": True})
    run_id = executor.create_run({"manifest": manifest, "parsed": parsed})
    state = executor.execute(run_id)
    assert state["status"] == "succeeded", state.get("error_code")
    result = executor.output(run_id)
    assert result["coverage"]["completed_window_count"] == 3
    assert result["window_results"][1][RESOLUTION_KEY]["resolved_block_ids"] == ["P3", "N1", "N2"]


def test_performance_evidence_keeps_both_origins_and_stays_out_of_bid_materials(tmp_path):
    from company_bid.material_matching import material_candidate_report
    from company_bid.material_recall import plan_recall_scope

    manifest, parsed, config = _scenario(tmp_path, 4)
    def select(_ctx, _task, _unit, payload, validate):
        answer = _answer(payload["window"])
        for candidate in answer["requirements"]:
            if candidate["citation"]["block_ids"] == ["N1", "N2", "N3", "N4"]:
                candidate["attachment_keywords"] = ["故障报告"]
        return validate(answer)

    output = extract_requirements(Context(), parsed, manifest, select, config)
    first, opening, closing = output["window_results"]
    scoped, = [row for row in output["requirements"] if "contract_evidence_scope" in row]
    assert scoped["window_ids"] == [opening["window_id"], closing["window_id"]]
    assert scoped["attachment_keywords"] == [] and scoped["contract_evidence_scope"]["document"] == "故障报告"
    assert closing["raw_model_candidate"]["requirements"][0]["attachment_keywords"] == ["故障报告"]
    assert len(scoped["citations"]) == 2 and scoped["citations"][1]["label_grounding"][0]["label"] == "故障报告"
    report = material_candidate_report([scoped], [], attachments=[])
    query, = report["proof_queries"]
    assert query["request"]["rule"] == "contract_performance_evidence_for_counterparty"
    assert query["status"] == "not_searched" and report["assembly_candidates"] == []
    assert plan_recall_scope([scoped], report, {})["queries"][0]["admission_route"] == "outside_material_recall_scope"
    prepared = plan_bid(Context(), output, parsed, manifest, no_call, {})
    assert prepared["requirements"] and not any(scoped["requirement_id"] in a["requirement_refs"] for a in prepared["attachments"])

    tampered = copy.deepcopy(output)
    next(row for row in tampered["requirements"] if "contract_evidence_scope" in row)["attachment_keywords"] = ["故障报告"]
    with pytest.raises(PlanningError, match="EXTRACTION_MERGE_MISMATCH"):
        plan_bid(Context(), tampered, parsed, manifest, no_call, {})


def test_performance_stage_requires_exact_contract_scope_and_matching_document(tmp_path):
    from company_bid.contract_clause_resolution import performance_evidence_scopes, performance_evidence_scope

    manifest, parsed, config = _scenario(tmp_path, 4)
    output = extract_requirements(Context(), parsed, manifest, _call(), config)
    scopes = performance_evidence_scopes(output[INDEX_OUTPUT_KEY])
    candidate = output["window_results"][-1]["validated"]["requirements"][0]
    assert performance_evidence_scope(candidate, scopes)
    for changed in ({"response_mode": "attachment"}, {"category": "qualification"},
                    {"attachment_keywords": ["设备交接记录"]},
                    {"citation": {**candidate["citation"], "source_id": "another-source"}}):
        assert performance_evidence_scope({**candidate, **changed}, scopes) is None
    manifest, parsed, config = _scenario(tmp_path, 4, optional_notice=True)
    blocks, _, docs = _index(parsed, manifest)
    assert not performance_evidence_scopes(build_contract_clause_indexes(docs, blocks))
