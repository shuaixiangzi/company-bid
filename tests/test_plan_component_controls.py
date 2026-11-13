"""Synthetic source controls through real N3/N4; no external services."""
import copy
import hashlib
from pathlib import Path

import pytest

from company_bid import pipeline
from company_bid.diagnostics import inspect_diagnostics
from company_bid.extraction import PlanningError, _citation, _index, _pdf_footer_skips, extract_requirements
from company_bid.models import PreparedBid
from company_bid.plan_component_controls import collect_plan_controls
from company_bid.planning import _native_source_span, plan_bid
from company_bid.provider import ChatProposalProvider
from company_bid.runtime import Executor, NodeSpec
from test_joint_deviation_formats import native_grid
from test_n3_source_roles import line
from test_source_selected_citations import Context, fixture, no_call, selected


# Anonymous standard-form contract examples, with no project/company facts.
COMPOSITION = ["3.1 响应文件的组成", "3.1.1 响应文件应包括下列内容：", "（1）响应函；",
    "（2）授权委托书（如有）；", "（3）联合体协议书（如有）；", "（4）商务和技术偏差表；",
    "（5）报价表；", "（6）资格审查资料；", "（7）响应方案；", "（8）响应人须知前附表要求的其他资料。",
    "响应人在评审过程中作出的符合采购文件要求的澄清、说明和补正，构成响应文件的组",
    "成部分。"]
GLOBAL = ["1.10.1 技术标准和要求和合同条款及格式中的关键条款均以“*”符号标记。响应文件",
    "应当对技术标准和要求和合同条款及格式中的关键条款作出满足性或更有利于采购人的响应，",
    "否则，响应人的响应文件将被视为无效。"]
EXCEPTION = ("3.1.2 响应人的法定代表人（单位负责人）亲自签署响应文件、亲自参加询比的，响应文",
    "件不包括第3.1.1（2）目所指的授权委托书。第一章“询比采购公告”规定不接受联合体的，",
    "或响应人没有组成联合体的，响应文件不包括第3.1.1（3）目所指的联合体协议书。响应人",
    "须知前附表未要求响应人递交响应保证金的，响应文件不包括第3.1.1（4）目所指的响应保",
    "证金。")


def source_case(tmp_path, *, composition=True, global_rule=True, conflict=True, duplicate=False,
                forms=False, global_suffix="", origin="native", source_format="pdf", global_fields=None):
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = source_format
    source_path = Path(manifest["sources"][0]["path"])
    source_path.write_text("Synthetic native source contract only.", encoding="utf-8")
    source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    manifest["sources"][0]["sha256"] = parsed["documents"][0]["sha256"] = source_hash
    component_blocks = [line(parsed, f"COMP-{i}", text) for i, text in enumerate(COMPOSITION)] if composition else []
    global_blocks = [line(parsed, f"GLOBAL-{i}", text + (global_suffix if i == 2 else ""), page=2)
                     for i, text in enumerate(GLOBAL)] if global_rule else []
    for value in global_blocks:
        value["origin"] = origin
    conflict_blocks = [line(parsed, f"EXCEPTION-{i}", text, page=3) for i, text in enumerate(EXCEPTION)] if conflict else []
    formats = []
    if forms:
        line(parsed, "REGION", "第五章 响应文件格式", page=4)
        for i, title in enumerate(("3 报价表", "4 分项报价明细表", "5 商务条款偏离表", "6 技术规格偏离表"), 4):
            heading = line(parsed, f"FORM-{i}", title, page=i)
            grid = native_grid(parsed, f"GRID-{i}", [["合成响应内容", "待填"], ["合成原标签", ""]], page=i)
            formats.append((heading, grid))
        line(parsed, "END", "第六章 合成合同事项", page=8)

    def call(ctx, task, unit, payload, validate):
        available = {block["block_id"] for block in payload["window"]["blocks"]}
        requirements = []
        if component_blocks and component_blocks[0]["block_id"] in available:
            refs = [component_blocks, component_blocks[1:10]] if duplicate else [component_blocks]
            requirements.extend({"citation": selected(*values), "category": "procedure", "response_mode": "procedure",
                                 "format_status": "not_prescribed"} for values in refs)
        if global_blocks and global_blocks[0]["block_id"] in available:
            requirements.append({"citation": selected(*global_blocks), "category": "technical", "response_mode": "writing",
                                 "format_status": "not_prescribed", **(global_fields or {})})
        if conflict_blocks and conflict_blocks[0]["block_id"] in available:
            requirements.append({"citation": selected(*conflict_blocks), "category": "procedure", "response_mode": "procedure",
                                 "format_status": "not_prescribed"})
        actual_formats = [{"title": heading["text"], "level": 1, "citation": selected(heading),
                           "content_block_ids": [grid["block_id"]], "requirement_indices": []}
                          for heading, grid in formats if heading["block_id"] in available]
        raw = {"window_id": payload["window"]["window_id"], "processing_declaration": "processed_not_semantically_accepted",
               "unresolved_block_ids": [], "requirements": requirements, "format_chapters": actual_formats,
               "format_status": "specified" if actual_formats else "none_verified"}
        return validate(raw)

    extracted = extract_requirements(Context(), parsed, manifest, call, {"window_max_blocks": 100})
    return manifest, parsed, extracted


def planned(tmp_path, **options):
    manifest, parsed, extracted = source_case(tmp_path, **options)
    ctx = Context()
    result = plan_bid(ctx, extracted, parsed, manifest, no_call, {"map_table_fields": False})
    return result, ctx, (manifest, parsed, extracted)


def test_eight_components_have_visible_individual_pending_handlers_and_immutable_n2(tmp_path):
    manifest, parsed, extracted = source_case(tmp_path, duplicate=True)
    before = copy.deepcopy((manifest, parsed, extracted))
    ctx = Context()
    result = plan_bid(ctx, extracted, parsed, manifest, no_call, {"map_table_fields": False})
    PreparedBid.model_validate(result)
    matrix = ctx.captures["n3-component-dispositions"]["items"]
    assert len(matrix) == 8 and len({row["group_id"] for row in matrix}) == 1
    assert {row["number"] for row in matrix} == set(range(1, 9))
    for row in matrix:
        section = next(item for item in result["sections"] if item["section_id"] == row["section_id"])
        issue = next(item for item in result["issues"] if item["issue_id"] == row["issue_id"])
        assert section["render_body"] and section["kind"] == "facts" and not section["writing_tasks"]
        assert issue["source_requirement"] == row["source_text"]
        assert row["status"] == "pending_manual" and row["satisfaction_status"] == "not_determined"
        assert row["source_parts"] and row["proof_sha256"] and row["source_ref"] in section["source_refs"]
    assert len(matrix[0]["requirement_refs"]) == 2
    assert all("适用性待核对" in item["title"] and "不能据资料缺少推定不适用" in item["reason"]
               for item in result["issues"] if "授权委托书" in item["title"] or "联合体协议书" in item["title"])
    assert (manifest, parsed, extracted) == before
    other = next(item for item in result["issues"] if "范围及完整性待核对" in item["title"])
    assert other["severity"] == "warning" and "不能证明存在额外证明要求" in other["reason"]


def test_number_name_conflict_is_visible_without_removing_either_deviation_or_quotation_table(tmp_path):
    result, ctx, (_, _, extracted) = planned(tmp_path, forms=True)
    assert len(result["tables"]) == 4
    assert {table["title"] for table in result["tables"]} == {
        "3 报价表", "4 分项报价明细表", "5 商务条款偏离表", "6 技术规格偏离表"}
    assert any(issue["title"] == "响应函响应内容待补" for issue in result["issues"])
    matrix = ctx.captures["n3-component-dispositions"]["items"]
    for key in ("deviation_tables", "quotation_tables"):
        row = next(item for item in matrix if item["component"] == key)
        assert len(row["format_candidates"]) == 2
        assert all(item["table_refs"] and item["existing_issue_ids"] and item["response_status"] == "pending_source_review"
                   for item in row["format_candidates"])
        assert row["status"] == "pending_manual"
    conflict, = ctx.captures["n3-component-dispositions"]["number_name_conflicts"]
    warning = next(item for item in result["issues"] if item["issue_id"] == conflict["issue_id"])
    assert "商务和技术偏差表" in warning["source_requirement"] and "目所指的响应保" in warning["source_requirement"]
    assert warning["severity"] == "warning" and conflict["listed_name"] != conflict["exception_name"]
    assert all(item["text"] in [raw["text"] for raw in result["requirements"]] for item in extracted["requirements"])


def test_pure_global_control_retains_original_requirement_and_visible_source_without_writing(tmp_path):
    result, ctx, (_, _, extracted) = planned(tmp_path, composition=False, conflict=False)
    raw, = extracted["requirements"]
    actual, = result["requirements"]
    assert {key: actual[key] for key in ("requirement_id", "text", "category", "mandatory", "response_mode")} == {
        key: raw[key] for key in ("requirement_id", "text", "category", "mandatory", "response_mode")}
    section, = result["sections"]
    assert section["kind"] == "facts" and section["render_body"] and not section["writing_tasks"]
    assert section["fixed_content"][0]["text"].endswith(raw["text"])
    control, = ctx.captures["n3-global-critical-controls"]["handling"]
    assert control["issue_id"] in {issue["issue_id"] for issue in result["issues"]}
    assert ctx.captures["n3-global-critical-controls"]["target_interpretation"].startswith("pending_source_review")
    assert control["satisfaction_status"] == "not_determined"


@pytest.mark.parametrize("suffix", ["同时应编制合成测点复测方案。", "但不适用于本项目。", "请同时提供合成证明。"])
def test_mixed_global_rule_cannot_be_downgraded_into_a_pure_control(tmp_path, suffix):
    result, ctx, _ = planned(tmp_path, composition=False, conflict=False, global_suffix=suffix)
    assert len(result["sections"][0]["writing_tasks"]) == 1
    assert ctx.captures["n3-global-critical-controls"]["handling"] == []


@pytest.mark.parametrize("options", [{"origin": "ocr"}, {"source_format": "docx"}])
def test_exact_rule_with_unproven_native_source_stops_before_planning_calls(tmp_path, options):
    manifest, parsed, extracted = source_case(tmp_path, composition=False, conflict=False, **options)
    with pytest.raises(PlanningError, match="PLAN_CONTROL_SOURCE_UNPROVEN"):
        plan_bid(Context(), extracted, parsed, manifest, no_call, {"map_table_fields": False})


def test_partial_source_selection_cannot_hide_mixed_substantive_tail(tmp_path):
    manifest, parsed, extracted = source_case(tmp_path, composition=False, conflict=False,
                                            global_suffix="同时应提供合成测点证明。")
    blocks, units, documents = _index(parsed, manifest)
    requirement = copy.deepcopy(extracted["requirements"][0])
    requirement["text"] = "\n".join(GLOBAL)
    citation = {**selected(*(blocks[f"GLOBAL-{i}"] for i in range(3))), "quote": requirement["text"]}
    with pytest.raises(PlanningError, match="CITATION_SPAN_MUST_QUOTE_ALL_BLOCKS"):
        _citation(citation, blocks, _pdf_footer_skips(blocks))
    # Even a forged already-normalized citation cannot weaken the local proof.
    requirement["citations"][0].update(quote=requirement["text"], end=len(requirement["text"]))
    with pytest.raises(PlanningError, match="PLAN_CONTROL_SOURCE_UNPROVEN"):
        collect_plan_controls([requirement], blocks, units, documents, _native_source_span)


def test_pure_rule_does_not_release_a_declared_unknown_format(tmp_path):
    manifest, parsed, extracted = source_case(tmp_path, composition=False, conflict=False,
        global_fields={"format_status": "prescribed", "format_title": "技术标准和要求"})
    with pytest.raises(PlanningError, match="REQUIRED_FORMAT_UNRESOLVED"):
        plan_bid(Context(), extracted, parsed, manifest, no_call, {})


def test_production_n4_and_n5_keep_components_pending_and_make_zero_http_requests(tmp_path, monkeypatch):
    manifest, parsed, extracted = source_case(tmp_path, forms=True, duplicate=True)
    calls = []

    def transport(*args, **kwargs):
        calls.append((args, kwargs))
        pytest.fail("Pure controls and pending components must not buy prose")

    def provider(options):
        return ChatProposalProvider(options, transport=transport, sleep=lambda _: None)

    monkeypatch.setattr(pipeline, "ChatProposalProvider", provider)
    config = {"debug_capture": True, "provider": {"mode": "chat_completions", "model": "offline-controls",
        "endpoint": "https://offline.invalid/chat/completions", "max_attempts": 1, "max_run_calls": 1}}
    base = pipeline.build_executor(tmp_path / "runtime", config)

    def n3(ctx, value):
        prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {"map_table_fields": False})
        return {"prepared": prepared, "proposals": {}, "source_artifacts": [], "input_kind": "automatic"}

    executor = Executor(base.store.root, [NodeSpec("N3", "synthetic-component-controls", n3, ()),
        *(node for node in base.nodes if node.key in {"N4", "N5"})], config)
    run = executor.create_run({"synthetic": True})
    state = executor.execute(run)
    assert state["status"] == "succeeded", state.get("error_code")
    output = executor.output(run, "N5")
    assert calls == [] and output["validation"]["writing_units"] == []
    assert output["validation"]["table_count"] == 4
    assert all(row["status"] == "pending_manual" for row in output["validation"]["requirement_coverage"])
    assert inspect_diagnostics(executor.store.root, run)["actual_call_attempts"] == 0
    markers = [block["issue_id"] for section in output["package"]["sections"] for block in section["blocks"]
               if block["kind"] == "issue"]
    assert set(markers) == {issue["issue_id"] for issue in output["package"]["issues"]}
