"""One source obligation can own two proved native deviation forms, offline."""
import copy
import pytest
from docx import Document

from company_bid import pipeline
from company_bid.diagnostics import inspect_diagnostics
from company_bid.extraction import PlanningError, extract_requirements
from company_bid.models import file_sha256
from company_bid.planning import plan_bid, _index, _citation, _pdf_footer_skips, _format_plan, _joint_response_formats
from company_bid.provider import ChatProposalProvider, PreparedProposalProvider
from company_bid.runtime import Executor, NodeSpec
from test_n3_source_roles import line
from test_source_selected_citations import Context, block, fixture, no_call


JOINT = "响应文件对采购文件的全部偏差，均应在响应文件的商务和技术偏差表中列明。"
BUSINESS = "5 商务条款偏离表"
TECHNICAL = "6 技术规格偏离表"
BUSINESS_ROWS = [
    ["询比文件的商务条款", "响应文件的商务条款", "偏离", "说明"],
    ["合成条款甲", "合成响应甲", "", "保留商务原注：项"],
    ["合成商务补充原注", "", "", ""],
]
TECHNICAL_ROWS = [
    ["序号", "询比规格", "响应文件规格", "偏离", "说明"],
    ["1", "合成测点范围", "合成响应范围", "", "保留技术原注"],
]
BUSINESS_MERGES = [{"row": 2, "column": 0, "row_span": 1, "col_span": 4}]
# Synthetic public clause structure; these are not copied tender continuation
# blocks. The different terms keep the test independent of the private sample.
FOLLOW_UP_PARTS = [
    "响应文件偏差表中未列明的事项，将视为响应人响应采购文件的要求；但如在采购活动",
    "中发现响应文件的其他部分与商务和技术偏差表的记载不一致或响应人的响应缺乏支持性材",
    "料，则评审小组有权要求响应人对相关事项进行澄清，并根据澄清结果对响应人的响应",
    "文件进行评审。",
]


def native_grid(parsed, identity, rows, *, page, merges=None, headers=None, widths=None):
    value = block(parsed, identity, "", kind="table", page=page, origin="native",
                  rows=copy.deepcopy(rows), merges=copy.deepcopy(merges or []),
                  header_rows=headers or [0], column_widths=widths or [1] * len(rows[0]))
    unit = next(unit for unit in parsed["documents"][0]["units"] if unit["page"] == page)
    index = sum(item["kind"] == "table" for item in unit["blocks"]) - 1
    value["locator"] = f"pdf/page[{page}]/table[{index}]"
    return value


def source_case(tmp_path, *, change=None, declaration=JOINT, status="prescribed", mode="table",
                duplicate=False, missing_second=False, missing_grid=False, mixed_source=False,
                format_status="prescribed", source_format="pdf", bodies=True, candidate_requests=None,
                shared_grid=False, declaration_parts=None, include_short_requirement=False):
    manifest, parsed = fixture(tmp_path)
    document = parsed["documents"][0]
    document["format"] = source_format
    parts = declaration_parts or [declaration]
    obligation_ids = ["OBLIGATION" if i == 0 else f"OBLIGATION_{i}" for i in range(len(parts))]
    for identity, text in zip(obligation_ids, parts):
        line(parsed, identity, text)
    line(parsed, "REGION", "第六章 响应文件格式", page=2)
    line(parsed, "BUSINESS", BUSINESS, page=2)
    native_grid(parsed, "BUSINESS_GRID", BUSINESS_ROWS, page=2, merges=BUSINESS_MERGES,
                headers=[0], widths=[3, 3, 1, 3])
    forms = [("BUSINESS", BUSINESS, "BUSINESS_GRID")]
    if not missing_second:
        line(parsed, "TECHNICAL", TECHNICAL, page=3)
        if missing_grid:
            line(parsed, "TECHNICAL_NOTE", "合成技术表的原生网格尚未提供。", page=3)
        else:
            native_grid(parsed, "TECHNICAL_GRID", TECHNICAL_ROWS, page=3,
                        headers=[0], widths=[1, 3, 3, 1, 2])
        forms.append(("TECHNICAL", TECHNICAL, "TECHNICAL_NOTE" if missing_grid else "TECHNICAL_GRID"))
    if duplicate:
        line(parsed, "DUPLICATE", "7 商务条款偏离表", page=4)
        native_grid(parsed, "DUPLICATE_GRID", BUSINESS_ROWS, page=4, merges=BUSINESS_MERGES,
                    headers=[0], widths=[3, 3, 1, 3])
        forms.append(("DUPLICATE", "7 商务条款偏离表", "DUPLICATE_GRID"))
    line(parsed, "END", "第七章 合成合同事项", page=5)

    if mixed_source:
        unit = next(unit for unit in document["units"] if unit["page"] == 3)
        document["units"].remove(unit)
        unit.update(source_id="OTHER", unit_id="OTHER-U3")
        for value in unit["blocks"]:
            value["source_id"] = "OTHER"
        parsed["documents"].append({"source_id": "OTHER", "role": "tender", "format": "pdf",
                                    "units": [unit], "sha256": "b" * 64})
        manifest["sources"].append({**manifest["sources"][0], "source_id": "OTHER"})
        manifest["source_roles"]["OTHER"] = "tender"
    if change:
        change(manifest, parsed)
    for source_document in parsed["documents"]:
        path = tmp_path / f"synthetic-{source_document['source_id']}-joint-deviations.txt"
        path.write_text("\n".join("\n".join("\t".join(row) for row in value["rows"])
                                  if value["kind"] == "table" else value.get("text", "")
                                  for unit in source_document["units"] for value in unit["blocks"]), encoding="utf-8")
        digest = file_sha256(path)
        source_document["sha256"] = digest
        source = next(source for source in manifest["sources"] if source["source_id"] == source_document["source_id"])
        source.update(path=str(path), sha256=digest)

    def candidate(ctx, task, unit, payload, validate):
        if candidate_requests is not None:
            candidate_requests.append(task)
        values = {value["block_id"]: value for value in payload["window"]["blocks"]}
        requirements = []
        if "OBLIGATION" in values:
            if include_short_requirement:
                requirements.append({"citation": {"block_ids": ["OBLIGATION"]}, "category": "commercial",
                    "response_mode": mode, "format_status": status, "format_title": None})
            requirements.append({"citation": {"block_ids": obligation_ids}, "category": "commercial",
                "response_mode": mode, "format_status": status, "format_title": None})
        chapters = [{"title": values[identity]["text"], "level": 1,
            "citation": {"block_ids": [identity]},
            "content_block_ids": ["BUSINESS_GRID" if shared_grid and identity == "TECHNICAL" else grid] if bodies else [],
            "requirement_indices": [], "status": format_status}
            for identity, title, grid in forms if identity in values]
        return validate({"window_id": payload["window"]["window_id"],
            "processing_declaration": "processed_not_semantically_accepted", "unresolved_block_ids": [],
            "requirements": requirements, "format_chapters": chapters,
            "format_status": "specified" if chapters else "none_verified"})

    extracted = extract_requirements(Context(), parsed, manifest, candidate, {})
    return manifest, parsed, extracted


def association_items(ctx):
    capture = ctx.captures["n3-format-associations"]
    assert capture["errors"] == []
    return capture["items"]


def assert_joint_plan(prepared, extracted):
    canonical, = extracted["requirements"]
    rid = canonical["requirement_id"]
    assert canonical["text"] == JOINT
    assert canonical["response_mode"] == "table" and canonical["format_title"] is None
    assert len([item for item in prepared["requirements"] if item["requirement_id"] == rid]) == 1
    assert len(prepared["sections"]) == len(prepared["tables"]) == 2
    assert len({table["table_id"] for table in prepared["tables"]}) == 2
    for section in prepared["sections"]:
        assert rid in section["requirement_refs"]
        tables = [table for table in prepared["tables"] if table["section_id"] == section["section_id"]]
        table, = tables
        assert rid in table["requirement_refs"] and table["table_id"] in section["table_refs"]
        assert not section["writing_tasks"] and not section["fact_refs"]
    assert not prepared["review_actions"]
    return rid


def test_one_canonical_obligation_keeps_both_proved_native_table_edges(tmp_path):
    manifest, parsed, extracted = source_case(tmp_path)
    original = copy.deepcopy((manifest, parsed, extracted))
    ctx = Context()
    prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {"map_table_fields": False})
    rid = assert_joint_plan(prepared, extracted)
    proof, = association_items(ctx)
    assert proof["rule"] == "joint_business_technical_deviation_pair_v1"
    assert proof["requirement_id"] == rid and proof["source_id"] == "T"
    assert proof["status"] == "source_proven" and proof["satisfaction_status"] == "not_determined"
    assert proof["n2_candidate_mutation"] is False and proof["proof_sha256"]
    assert {target["source_title"] for target in proof["targets"]} == {BUSINESS, TECHNICAL}
    assert len({target["role"] for target in proof["targets"]}) == 2
    assert all(target["tables"] and target["source_blocks"] for target in proof["targets"])
    assert ctx.captures["n3-planning-report"]["format_associations"] == [proof]
    assert (manifest, parsed, extracted) == original


def test_source_proven_native_continuation_remains_eligible_without_model_echoing_body_ids(tmp_path):
    manifest, parsed, extracted = source_case(tmp_path, bodies=False)
    ctx = Context()
    prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {"map_table_fields": False})
    assert_joint_plan(prepared, extracted)
    assert any(row["status"] == "structural_continuation" for row in ctx.captures["n3-format-coverage"]["blocks"])


def test_native_header_wraps_are_used_for_matching_but_not_rewritten(tmp_path):
    def wrapped_headers(manifest, parsed):
        values = [value for unit in parsed["documents"][0]["units"] for value in unit["blocks"]]
        next(value for value in values if value["block_id"] == "BUSINESS_GRID")["rows"][0][0] = "询比文件的\n商务条款"
        next(value for value in values if value["block_id"] == "TECHNICAL_GRID")["rows"][0][1] = "询比\n规格"
    manifest, parsed, extracted = source_case(tmp_path, change=wrapped_headers)
    ctx = Context()
    prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {"map_table_fields": False})
    assert_joint_plan(prepared, extracted)
    business = next(table for table in prepared["tables"] if table["title"] == BUSINESS)
    technical = next(table for table in prepared["tables"] if table["title"] == TECHNICAL)
    assert business["rows"][0][0]["text"] == "询比文件的\n商务条款"
    assert technical["rows"][0][1]["text"] == "询比\n规格"


@pytest.mark.parametrize("wrapped", [False, True])
def test_complete_presumption_and_clarification_follow_up_keeps_source_and_pair(tmp_path, wrapped):
    parts = [JOINT, *FOLLOW_UP_PARTS] if wrapped else [JOINT + "".join(FOLLOW_UP_PARTS)]
    manifest, parsed, extracted = source_case(tmp_path, declaration_parts=parts)
    original = copy.deepcopy((parsed, extracted))
    ctx = Context()
    prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {"map_table_fields": False})
    requirement, = extracted["requirements"]
    proof, = association_items(ctx)
    assert requirement["text"] == "\n".join(parts)
    assert len(requirement["citations"][0]["block_ids"]) == len(parts)
    assert proof["requirement_citations"] == requirement["citations"]
    assert proof["satisfaction_status"] == "not_determined"
    assert len(prepared["tables"]) == 2
    assert all(requirement["requirement_id"] in table["requirement_refs"] for table in prepared["tables"])
    assert (parsed, extracted) == original


def test_short_and_complete_obligations_sharing_anchor_keep_both_ids_and_pair_edges(tmp_path):
    manifest, parsed, extracted = source_case(tmp_path, declaration_parts=[JOINT, *FOLLOW_UP_PARTS],
                                            include_short_requirement=True)
    original = copy.deepcopy(extracted)
    ctx = Context()
    prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {"map_table_fields": False})
    short, complete = extracted["requirements"]
    ids = {short["requirement_id"], complete["requirement_id"]}
    assert len(ids) == 2
    assert short["citations"][0]["block_id"] == complete["citations"][0]["block_id"]
    assert len(short["citations"][0]["block_ids"]) == 1
    assert len(complete["citations"][0]["block_ids"]) == 5
    assert {item["requirement_id"] for item in association_items(ctx)} == ids
    assert all(sum(item["requirement_id"] == rid for item in prepared["requirements"]) == 1 for rid in ids)
    assert len(prepared["sections"]) == len(prepared["tables"]) == 2
    assert len({table["table_id"] for table in prepared["tables"]}) == 2
    assert all(ids <= set(item["requirement_refs"]) for item in [*prepared["sections"], *prepared["tables"]])
    assert extracted == original


@pytest.mark.parametrize("suffix", [
    "上述联合填写要求无效。",
    "本项目不需要遵守上述联合填写要求。",
    "经采购人书面确认后，上述联合填写要求方可执行。",
    "本项目以商务偏差表为最终依据。",
])
@pytest.mark.parametrize("wrapped", [False, True])
def test_unknown_waiver_condition_or_substitute_suffix_cannot_acquire_pair(tmp_path, suffix, wrapped):
    if wrapped:
        # Five complete adjacent native blocks, with the unsupported suffix
        # crossing four blocks after the positive statement.
        cuts = [len(suffix) * i // 4 for i in range(5)]
        parts = [JOINT, *(suffix[a:b] for a, b in zip(cuts, cuts[1:]))]
    else:
        parts = [JOINT + suffix]
    manifest, parsed, extracted = source_case(tmp_path, declaration_parts=parts)
    original = copy.deepcopy(extracted)
    with pytest.raises(PlanningError, match="REQUIRED_FORMAT_UNRESOLVED"):
        plan_bid(Context(), extracted, parsed, manifest, no_call, {"map_table_fields": False})
    assert extracted == original


def test_unsupported_follow_up_is_not_admitted_by_its_precedence_word_alone(tmp_path):
    declaration = JOINT + "若响应文件各部分存在不一致，以评审小组的澄清结果为准。"
    manifest, parsed, extracted = source_case(tmp_path, declaration=declaration)
    with pytest.raises(PlanningError, match="REQUIRED_FORMAT_UNRESOLVED"):
        plan_bid(Context(), extracted, parsed, manifest, no_call, {"map_table_fields": False})


def test_positive_substring_cannot_hide_unknown_native_source_suffix(tmp_path):
    manifest, parsed, extracted = source_case(tmp_path, declaration=JOINT + "本项目不需要遵守上述联合填写要求。")
    blocks, units, documents = _index(parsed, manifest)
    formats, ledger, errors = _format_plan(extracted, blocks, documents)
    assert errors == []
    requirement = copy.deepcopy(extracted["requirements"][0])
    source = blocks["OBLIGATION"]
    requirement["text"] = JOINT
    requirement["citations"] = [_citation({"source_id": source["source_id"], "block_id": source["block_id"],
        "locator": source["locator"], "quote": JOINT}, blocks, _pdf_footer_skips(blocks))]
    proof, error = _joint_response_formats(requirement, formats, ledger, blocks, units, documents, extracted["coverage"])
    assert proof is None and error["reason"] == "joint_deviation_format_pair_unresolved"


@pytest.mark.parametrize("declaration", [
    JOINT.replace("均应", "不得"),
    "仅经采购人书面确认后，" + JOINT,
    JOINT + "本项目不适用上述联合填写要求。",
    "响应人可以任选商务或技术偏差表登记所发现的偏差。",
])
def test_negated_conditional_or_different_joint_rule_cannot_acquire_pair(tmp_path, declaration):
    manifest, parsed, extracted = source_case(tmp_path, declaration=declaration)
    original = copy.deepcopy(extracted)
    with pytest.raises(PlanningError, match="REQUIRED_FORMAT_UNRESOLVED"):
        plan_bid(Context(), extracted, parsed, manifest, no_call, {"map_table_fields": False})
    assert extracted == original


@pytest.mark.parametrize("option", ["missing_second", "missing_grid", "mixed_source", "duplicate", "shared_grid"])
def test_both_roles_must_be_unique_same_source_and_backed_by_complete_grids(tmp_path, option):
    manifest, parsed, extracted = source_case(tmp_path, **{option: True})
    original = copy.deepcopy(extracted)
    with pytest.raises(PlanningError, match="REQUIRED_FORMAT_UNRESOLVED"):
        plan_bid(Context(), extracted, parsed, manifest, no_call, {"map_table_fields": False})
    assert extracted == original


@pytest.mark.parametrize("change", ["unknown_name", "missing_header", "ocr_table", "bad_locator"])
def test_unknown_or_non_native_form_evidence_is_not_guessed(tmp_path, change):
    def mutate(manifest, parsed):
        values = [value for unit in parsed["documents"][0]["units"] for value in unit["blocks"]]
        if change == "unknown_name":
            next(value for value in values if value["block_id"] == "TECHNICAL")["text"] = "6 不明规格记录表"
        else:
            grid = next(value for value in values if value["block_id"] == "TECHNICAL_GRID")
            if change == "missing_header":
                grid["rows"][0][2] = "未知原始列"
            elif change == "ocr_table":
                grid["origin"] = "ocr"
            else:
                grid["locator"] = "pdf/page[3]/table[unknown]"
    manifest, parsed, extracted = source_case(tmp_path, change=mutate)
    with pytest.raises(PlanningError, match="REQUIRED_FORMAT_UNRESOLVED"):
        plan_bid(Context(), extracted, parsed, manifest, no_call, {"map_table_fields": False})


@pytest.mark.parametrize("options", [
    {"status": "unread"}, {"status": "conflict"},
    {"format_status": "unread"}, {"format_status": "conflict"}, {"source_format": "docx"},
])
def test_joint_binding_does_not_rescue_unread_conflict_or_unproved_source_format(tmp_path, options):
    manifest, parsed, extracted = source_case(tmp_path, **options)
    with pytest.raises(PlanningError, match="REQUIRED_FORMAT_UNRESOLVED"):
        plan_bid(Context(), extracted, parsed, manifest, no_call, {"map_table_fields": False})


def test_incomplete_native_reading_stops_before_any_candidate_request(tmp_path):
    def incomplete(manifest, parsed):
        next(unit for unit in parsed["documents"][0]["units"] if unit["page"] == 3)["coverage"]["status"] = "needs_ocr"
    requests = []
    with pytest.raises(PlanningError, match="TENDER_READING_INCOMPLETE"):
        source_case(tmp_path, change=incomplete, candidate_requests=requests)
    assert requests == []


def table_text(table):
    return "\n".join(cell.text for row in table.rows for cell in row.cells)


def test_production_n3_n6_renders_both_original_tables_once_without_model_calls(tmp_path, monkeypatch):
    manifest, parsed, extracted = source_case(tmp_path)
    original = copy.deepcopy(extracted)
    def forbidden(*args, **kwargs):
        pytest.fail("Native fixed tables must not call a writing/model provider")
    monkeypatch.setattr(PreparedProposalProvider, "generate", forbidden)
    monkeypatch.setattr(ChatProposalProvider, "generate", forbidden)
    monkeypatch.setattr(ChatProposalProvider, "generate_json", forbidden)
    config = {"provider": {"mode": "prepared"}, "debug_capture": True}
    base = pipeline.build_executor(tmp_path / "runtime", config)
    def n3(ctx, value):
        prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {"map_table_fields": False})
        return {"prepared": prepared, "proposals": {}, "source_artifacts": [], "input_kind": "automatic"}
    nodes = [NodeSpec("N3", "synthetic-joint-deviation-planning", n3, ())]
    nodes.extend(node for node in base.nodes if node.key in {"N4", "N5", "N6"})
    executor = Executor(base.store.root, nodes, config)
    run = executor.create_run({"synthetic": True})
    state = executor.execute(run)
    assert state["status"] == "succeeded", state.get("error_code")
    prepared = executor.output(run, "N3")["prepared"]
    rid = assert_joint_plan(prepared, extracted)
    package = executor.output(run, "N4")["package"]
    blocks = [block for section in package["sections"] for block in section["blocks"] if block["kind"] == "table"]
    assert len(blocks) == len({block["table"]["table_id"] for block in blocks}) == 2
    assert all(rid in block["table"]["requirement_refs"] and rid in block["requirement_refs"] for block in blocks)
    n5 = executor.output(run, "N5")["validation"]
    assert n5["table_count"] == 2 and n5["writing_units"] == []
    canonical_row, = [row for row in n5["requirement_coverage"] if row["requirement_id"] == rid]
    assert canonical_row["status"] == "pending_manual"
    rendered = executor.store.root / executor.output(run)["files"]["bid_docx"]["path"]
    document = Document(rendered)
    assert len(document.tables) == 2
    business, = [table for table in document.tables if "保留商务原注：项" in table_text(table)]
    technical, = [table for table in document.tables if "保留技术原注" in table_text(table)]
    assert len(business.rows) == 3 and len(business.columns) == 4
    assert business.cell(2, 0)._tc.grid_span == 4
    assert len(technical.rows) == 2 and len(technical.columns) == 5
    for row in BUSINESS_ROWS[1:]:
        for text in row:
            if text:
                assert text in table_text(business)
    for row in TECHNICAL_ROWS:
        for text in row:
            if text:
                assert text in table_text(technical)
    assert extracted == original
    assert inspect_diagnostics(executor.store.root, run)["actual_call_attempts"] == 0
