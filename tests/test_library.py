"""Selected native-company preparation; synthetic data and no remote calls."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from company_bid.library import LIBRARY_VERSION, LibraryError, prepare_library
from company_bid.models import file_sha256
from company_bid.storage import LocalStore, digest


class Context:
    def __init__(self, root):
        self.executor = SimpleNamespace(store=LocalStore(root))
        self.run_id = "1" * 32
        self.units, self.captures = {}, {}

    def run_unit(self, unit_id, inputs, fn):
        if unit_id in self.units:
            assert self.units[unit_id][0] == inputs
            return copy.deepcopy(self.units[unit_id][1])
        output = fn()
        self.units[unit_id] = (copy.deepcopy(inputs), copy.deepcopy(output))
        return output

    def store_bytes(self, name, content, media_type="application/octet-stream"):
        return self.executor.store.put_bytes(self.run_id, name, content, media_type)

    def capture(self, name, data, **kwargs):
        self.captures[name] = copy.deepcopy(data)


def source(tmp_path, source_id="S1", *, role="company_material", provenance="synthetic", material_type="company_profile"):
    path = tmp_path / (source_id + ".txt")
    path.write_text("Synthetic frozen source " + source_id, encoding="utf-8")
    return {"source_id": source_id, "title": "虚构公司资料", "path": str(path), "sha256": file_sha256(path),
            "locator": "document", "kind": "company_material", "provenance": provenance, "synthetic": True,
            "role": role, "material_type": material_type}


def document(src, rows=None, *, heading="2 公司基本情况", status="read", origin="native"):
    sid = src["source_id"]
    blocks = []
    if heading is not None:
        blocks.append({"block_id": sid + "-P", "source_id": sid, "locator": "word/document.xml/p[1]", "kind": "paragraph", "text": heading, "origin": "native"})
    if rows is not None:
        blocks.append({"block_id": sid + "-T", "source_id": sid, "locator": "word/document.xml/table[4]", "kind": "table", "text": "",
                       "rows": rows, "merges": [], "column_widths": [1] * len(rows[0]), "origin": origin})
    return {"source_id": sid, "role": src["role"], "sha256": src["sha256"], "format": "docx", "units": [
        {"unit_id": sid + "-U", "source_id": sid, "locator": "word/document.xml/unit[1]", "page": None,
         "coverage": {"status": status}, "blocks": blocks}]}


def prepare(ctx, sources, docs, config=None):
    return prepare_library(ctx, sources, {"documents": docs}, config or {})


def test_native_basic_table_cells_are_frozen_and_exactly_traceable(tmp_path):
    src = source(tmp_path)
    doc = document(src, [["企业名称", "虚构测试公司", "注册地址", "测试路1号"],
                         ["统一社会信用代码", "TEST-CODE", "注册资本（万元）", "100"]])
    ctx = Context(tmp_path / "runtime")
    snapshot = prepare(ctx, [src], [doc])
    assert snapshot["status"] == "prepared" and snapshot["counts"]["usable_facts"] == 4
    assert snapshot["counts"]["registered_sources"] == snapshot["counts"]["parsed_sources"] == 1
    address = next(fact for fact in snapshot["facts"] if fact["label"] == "注册地址")
    ref = next(ref for ref in snapshot["sources"] if ref["source_id"] == address["source_refs"][0])
    assert "word/document.xml/table[4]" in ref["locator"] and "r0c2,r0c3" in ref["locator"]
    assert ref["sha256"] == src["sha256"] and Path(ref["path"]).read_bytes() == Path(src["path"]).read_bytes()
    capital = next(fact for fact in snapshot["facts"] if fact["label"] == "注册资本")
    assert capital["value"] == "100 万元"
    assert next(c for c in snapshot["fact_candidates"] if c["field"] == "注册资本")["unit_basis"]["unit_quote"] == "万元"
    stored = ctx.executor.store.read_json(snapshot["snapshot_artifact"])
    assert stored["snapshot_id"] == snapshot["snapshot_id"] and "snapshot_artifact" not in stored
    original_snapshot = copy.deepcopy(snapshot)
    Path(src["path"]).write_text("changed outside original", encoding="utf-8")
    assert prepare(ctx, [src], [doc]) == original_snapshot
    assert hashlib.sha256(Path(ref["path"]).read_bytes()).hexdigest() == src["sha256"]


def test_registration_denominator_is_not_extraction_or_reading_success(tmp_path):
    src = source(tmp_path)
    registrations = [src]
    for index in range(2, 106):
        registrations.append({**src, "source_id": f"S{index}", "path": str(tmp_path / "never-read" / f"{index}.pdf"), "material_type": "personnel"})
    snapshot = prepare(Context(tmp_path / "runtime"), registrations, [document(src, [["企业名称", "虚构公司"]])])
    assert snapshot["status"] == "partial"
    assert snapshot["counts"]["registered_sources"] == 105 and snapshot["counts"]["parsed_sources"] == 1
    assert snapshot["counts"]["unparsed_sources"] == 104
    assert sum(state["status"] == "registered_unparsed" for state in snapshot["source_states"]) == 104
    assert all(entry["status"] == "listed_unprocessed" for entry in snapshot["listed_unprocessed"])


@pytest.mark.parametrize("role,provenance,authorized", [("reference_bid", "derived_from_bid", False), ("reference_bid", "original", True)])
def test_bid_fact_authorization_is_bound_to_original_hash_and_preserves_provisional_origin(tmp_path, role, provenance, authorized):
    src = source(tmp_path, role=role, provenance=provenance)
    doc = document(src, [["注册地址", "测试路1号"]])
    cfg = {"authorized_bid_fact_sources": {src["source_id"]: src["sha256"]}} if authorized else {}
    snapshot = prepare(Context(tmp_path / "runtime"), [src], [doc], cfg)
    assert bool(snapshot["facts"]) is authorized
    if authorized:
        assert snapshot["fact_candidates"][0]["status"] == "usable_provisional"
        assert snapshot["fact_candidates"][0]["fact_adoption_source_sha256"] == src["sha256"]
        assert all(ref["provenance"] == "derived_from_bid" for ref in snapshot["sources"])
    else:
        assert any(entry["reason"] == "bid_fact_authorization_missing" for entry in snapshot["listed_unprocessed"])


def test_authorization_for_different_original_hash_is_rejected(tmp_path):
    src = source(tmp_path, role="reference_bid", provenance="derived_from_bid")
    with pytest.raises(LibraryError, match="LIBRARY_AUTHORIZATION_HASH_MISMATCH"):
        prepare(Context(tmp_path / "runtime"), [src], [document(src, [["企业名称", "虚构公司"]])],
                {"authorized_bid_fact_sources": {src["source_id"]: "0" * 64}})


def test_contract_customer_address_is_not_adopted_as_company_fact(tmp_path):
    src = source(tmp_path, material_type="contract")
    doc = document(src, [["注册地址", "客户的地址"]], heading="第一章 合同双方信息")
    snapshot = prepare(Context(tmp_path / "runtime"), [src], [doc])
    assert not snapshot["facts"]
    assert any(entry["reason"] == "outside_company_profile_scope" for entry in snapshot["listed_unprocessed"])


def test_explicit_table_scope_can_select_authorized_basic_fields_without_title(tmp_path):
    src = source(tmp_path, role="reference_bid", provenance="derived_from_bid")
    doc = document(src, [["注册地址", "测试路1号"]], heading=None)
    snapshot = prepare(Context(tmp_path / "runtime"), [src], [doc], {
        "authorized_bid_fact_sources": {src["source_id"]: src["sha256"]}, "table_scopes": {src["source_id"]: ["word/document.xml/table[4]"]}})
    assert len(snapshot["facts"]) == 1
    assert snapshot["fact_candidates"][0]["scope"]["basis"] == "explicit_selected_table"


def test_conflicting_same_field_is_visible_and_excluded_from_usable_facts(tmp_path):
    a, b = source(tmp_path, "A"), source(tmp_path, "B")
    snapshot = prepare(Context(tmp_path / "runtime"), [a, b], [document(a, [["注册地址", "甲地址"]]), document(b, [["注册地址", "乙地址"]])])
    assert snapshot["status"] == "conflict" and not snapshot["facts"]
    assert snapshot["counts"]["conflicts"] == 1
    assert all(c["status"] == "conflict" for c in snapshot["fact_candidates"])
    assert all(state["status"] == "conflict" for state in snapshot["source_states"])


def test_identical_values_merge_sources_without_hiding_candidate_citations(tmp_path):
    a, b = source(tmp_path, "A"), source(tmp_path, "B")
    snapshot = prepare(Context(tmp_path / "runtime"), [a, b], [document(a, [["注册地址", "相同地址"]]), document(b, [["注册地址", "相同地址"]])])
    assert len(snapshot["facts"]) == 1 and len(snapshot["facts"][0]["source_refs"]) == 2
    assert len(snapshot["fact_candidates"]) == 2 and not snapshot["conflicts"]


def test_prices_personnel_bank_signature_and_performance_are_listed_not_adopted(tmp_path):
    src = source(tmp_path)
    doc = document(src, [["报价", "123", "人员姓名", "虚构人"], ["银行账号", "1234", "项目业绩", "虚构业绩"], ["盖章", "旧签章", "注册地址", "____"]])
    snapshot = prepare(Context(tmp_path / "runtime"), [src], [doc])
    assert not snapshot["facts"]
    assert {entry["category"] for entry in snapshot["listed_unprocessed"]} >= {"price", "personnel", "bank", "performance", "signature"}
    assert snapshot["fact_candidates"][0]["status"] == "missing_value"


def test_partial_reader_status_is_not_evidence_of_no_materials(tmp_path):
    src = source(tmp_path, material_type="personnel")
    doc = document(src, [["企业名称", "虚构公司"]], status="needs_ocr")
    snapshot = prepare(Context(tmp_path / "runtime"), [src], [doc])
    assert snapshot["status"] == "partial" and snapshot["source_states"][0]["status"] == "unusable"
    assert snapshot["source_states"][0]["unresolved_units"] == 1
    assert not snapshot["facts"] and any(entry["category"] == "personnel" for entry in snapshot["listed_unprocessed"])


def test_source_hash_failure_stops_preparation_and_is_captured(tmp_path):
    src = source(tmp_path)
    doc = document(src, [["企业名称", "虚构公司"]])
    Path(src["path"]).write_text("changed original", encoding="utf-8")
    ctx = Context(tmp_path / "runtime")
    with pytest.raises(LibraryError, match="LIBRARY_SOURCE_HASH_MISMATCH"):
        prepare(ctx, [src], [doc])
    assert any(capture.get("status") == "failed" for capture in ctx.captures.values())
    assert "library-snapshot" not in ctx.units


def test_pdf_proof_scope_is_exact_but_not_a_prepared_attachment(tmp_path):
    src = source(tmp_path, material_type="business_license")
    doc = document(src, None, heading=None)
    doc["format"] = "pdf"
    doc["units"] = [{"unit_id": f"P{page}", "source_id": src["source_id"], "locator": f"pdf/page[{page}]", "page": page,
                     "coverage": {"status": "read"}, "blocks": []} for page in (1, 2)]
    snapshot = prepare(Context(tmp_path / "runtime"), [src], [doc], {"proof_selections": [{"source_id": src["source_id"], "title": "营业执照", "pages": [2]}]})
    proof = snapshot["proof_entries"][0]
    assert proof["pages"] == [2] and proof["sha256"] == src["sha256"] and proof["scope_locators"] == ["pdf/page[2]"]
    assert proof["status"] == "parsed_scope_not_prepared" and not proof["prepared_attachment"]
    assert snapshot["counts"]["prepared_attachments"] == 0


def test_docx_image_scope_does_not_turn_unread_image_into_read_proof(tmp_path):
    src = source(tmp_path)
    doc = document(src, None, heading=None, status="needs_ocr")
    doc["units"][0]["blocks"] = [{"block_id": "IMAGE", "source_id": src["source_id"], "locator": "word/document.xml/p[2]/image[0]", "kind": "image",
        "image_ref": {"package_part": "word/media/image2.png", "sha256": "c" * 64}}]
    snapshot = prepare(Context(tmp_path / "runtime"), [src], [doc], {"proof_selections": [{"source_id": src["source_id"], "title": "证明图片", "package_parts": ["word/media/image2.png"]}]})
    proof = snapshot["proof_entries"][0]
    assert proof["status"] == "parsed_scope_unread" and not proof["content_read"] and not proof["prepared_attachment"]


def test_executor_persists_source_units_and_an_immutable_snapshot(tmp_path):
    from company_bid.runtime import Executor, NodeSpec
    src = source(tmp_path)
    parsed = {"documents": [document(src, [["注册地址", "测试路1号"]])]}
    runner = Executor(tmp_path / "runtime", [NodeSpec("LIBRARY", LIBRARY_VERSION,
        lambda ctx, value: prepare_library(ctx, value["sources"], value["parsed"], value["config"]))], {})
    run_id = runner.create_run({"sources": [src], "parsed": parsed, "config": {}})
    result = runner.execute(run_id)
    assert result["status"] == "succeeded"
    snapshot = runner.output(run_id)
    assert snapshot["facts"][0]["value"] == "测试路1号"
    assert len(result["nodes"][0]["units"]) == 2
    assert runner.store.read_json(snapshot["snapshot_artifact"])["content_hash"] == snapshot["content_hash"]


def test_real_native_docx_reader_profile_table_is_compatible(tmp_path):
    from docx import Document
    from company_bid.reading import read_documents
    src = source(tmp_path, role="reference_bid", provenance="derived_from_bid")
    docx = Document(); docx.add_paragraph("2 公司基本情况")
    table = docx.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "公司名称"; table.cell(0, 1).text = "虚构测试公司"
    table.cell(1, 0).text = "注册地址"; table.cell(1, 1).text = "测试路1号"
    path = tmp_path / "source.docx"; docx.save(path)
    src.update(path=str(path), sha256=file_sha256(path))
    ctx = Context(tmp_path / "runtime")
    parsed = read_documents(ctx, [{**src, "role": "reference_bid"}], {})
    snapshot = prepare_library(ctx, [src], parsed, {"authorized_bid_fact_sources": {src["source_id"]: src["sha256"]}})
    assert snapshot["counts"]["usable_facts"] == 2
    assert all(candidate["citation"]["page"] is None for candidate in snapshot["fact_candidates"])


@pytest.mark.parametrize("case,expected", [("other_cell", True), ("same_cell", False), ("merged_overlap", False), ("unknown_locator", False), ("failed", False)])
def test_partial_table_only_adopts_native_cells_provably_outside_ocr_scope(tmp_path, case, expected):
    src = source(tmp_path, role="reference_bid", provenance="derived_from_bid")
    doc = document(src, [["注册地址", "测试地址", "组织图", ""]], heading="2 公司基本情况")
    # Match real N1 ordering: a read paragraph unit followed by a table unit.
    heading_block, table = doc["units"][0]["blocks"]
    doc["units"] = [{"unit_id": "HEADING", "source_id": src["source_id"], "locator": heading_block["locator"], "page": None,
        "coverage": {"status": "read"}, "blocks": [heading_block]},
        {"unit_id": "TABLE", "source_id": src["source_id"], "locator": table["locator"], "page": None,
         "coverage": {"status": "needs_ocr", "error_code": "OCR_REQUIRED"}, "blocks": [table]}]
    suffix = "/row[0]/cell[3]/paragraph[0]/image[0]"
    if case == "same_cell":
        suffix = "/row[0]/cell[1]/paragraph[0]/image[0]"
    elif case == "merged_overlap":
        table["rows"] = [["注册地址", "测试地址", "", ""], ["", "", "", ""]]
        table["merges"] = [{"row": 0, "column": 1, "row_span": 2, "col_span": 1}]
        suffix = "/row[1]/cell[1]/paragraph[0]/image[0]"
    elif case == "unknown_locator":
        suffix = "/image[0]"
    elif case == "failed":
        doc["units"][1]["coverage"].update(status="failed", error_code="DOCX_PARSE_FAILED")
    doc["units"][1]["coverage"]["unread_objects"] = [table["locator"] + suffix]
    snapshot = prepare(Context(tmp_path / "runtime"), [src], [doc], {"authorized_bid_fact_sources": {src["source_id"]: src["sha256"]}})
    assert bool(snapshot["facts"]) is expected
    assert snapshot["status"] == "partial" and snapshot["source_states"][0]["status"] == "partial"
    assert snapshot["source_states"][0]["unresolved_unit_details"][0]["unread_objects"]
    if expected:
        candidate = snapshot["fact_candidates"][0]
        assert candidate["status"] == "usable_provisional"
        assert candidate["reading_evidence"]["table_reading_status"] == "partial"
        assert candidate["reading_evidence"]["excluded_unread_objects"]


@pytest.mark.parametrize("version,suffix,expected", [("native-reading-2", "/row[0]/cell[3]/paragraph[0]/image[0]", True),
    ("native-reading-1", "/row[0]/cell[3]/paragraph[0]/image[0]", False),
    ("native-reading-2", "/row[0]/cell[3]/unsupported-object", False),
    ("native-reading-2", "/row[0]/cell[1]/paragraph[0]/image[0]", False)])
def test_known_optional_reader_v2_null_error_is_compatible_only_for_exact_other_image_cells(tmp_path, version, suffix, expected):
    src = source(tmp_path)
    doc = document(src, [["注册地址", "测试地址", "组织图", ""]], heading=None, status="needs_ocr")
    table = doc["units"][0]["blocks"][0]
    doc["units"][0]["coverage"].update(error_code=None, unread_objects=[table["locator"] + suffix])
    snapshot = prepare_library(Context(tmp_path / "runtime"), [src], {"producer_version": version, "documents": [doc]},
        {"table_scopes": {src["source_id"]: [table["locator"]]}})
    assert bool(snapshot["facts"]) is expected
    assert snapshot["source_states"][0]["unresolved_unit_details"][0]["error_code"] is None
    if expected:
        evidence = snapshot["fact_candidates"][0]["reading_evidence"]
        assert evidence["error_code"] is None and evidence["compatibility_rule"] == "native-reading-2-optional-cell-image-v1"


def test_structured_history_is_in_snapshot_without_flat_price_or_person_facts(tmp_path):
    src = source(tmp_path, role="reference_bid", provenance="derived_from_bid")
    rows = [["序号", "项目名称", "项目负责人", "发包人", "服务地点", "服务日期", "合同额（万元）", "本项目拟投入人员是否参与"],
            ["1", "虚构历史项目", "测试负责人", "虚构客户", "测试地点", "2022年", "12.50", "是"]]
    doc = document(src, rows, heading=None)
    config = {"authorized_bid_fact_sources": {src["source_id"]: src["sha256"]}, "record_scopes": [{
        "kind": "historical_performance", "source_id": src["source_id"], "source_sha256": src["sha256"], "locator": "word/document.xml/table[4]"}]}
    snapshot = prepare(Context(tmp_path / "runtime"), [src], [doc], config)
    assert not snapshot["facts"] and snapshot["counts"]["usable_facts"] == 0
    assert snapshot["counts"]["historical_performance_records"] == 1
    record = snapshot["material_records"][0]
    assert record["fields"]["contract_amount"]["value"] == "12.50" and record["fields"]["contract_amount"]["unit"] == "万元"
    assert record["current_project_price"] is False and record["current_project_assignment"] is False
    assert snapshot["record_processing"][0]["status"] == "prepared"
    assert not any(entry.get("locator") == "word/document.xml/table[4]" for entry in snapshot["listed_unprocessed"])


def test_real_executor_keeps_structured_declaration_hashes_and_business_fields(tmp_path):
    from company_bid.pipeline import build_executor
    src = source(tmp_path, role="reference_bid", provenance="derived_from_bid")
    rows = [["职务", "姓名", "职称", "证书名称", "级别", "证号", "专业", "备注"],
            ["历史负责人", "虚构测试人", "工程师", "测试证书", "三级", "TEST-001", "检测", ""]]
    doc = document(src, rows, heading=None)
    config = {"pipeline": "library", "debug_capture": True, "library": {
        "authorized_bid_fact_sources": {src["source_id"]: src["sha256"]}, "record_scopes": [{
            "kind": "personnel_declaration", "source_id": src["source_id"], "source_sha256": src["sha256"], "locator": "word/document.xml/table[4]"}]}}
    runner = build_executor(tmp_path / "runtime", config)
    run_id = runner.create_run({"library_input": {"registered_sources": [src], "parsed": {"documents": [doc]}}})
    state = runner.execute(run_id)
    assert state["status"] == "succeeded"
    snapshot = runner.output(run_id)
    assert snapshot["material_records"][0]["fields"]["certificate_declarations"][0]["number"]["value"] == "TEST-001"
    assert snapshot["material_records"][0]["citations"][0]["source_sha256"] == src["sha256"]
    assert snapshot["source_artifacts"][0]["sha256"] == src["sha256"]
    assert snapshot["counts"]["material_records"] == 1 and not snapshot["facts"]
