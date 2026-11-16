"""Unverified OCR can recall frozen proof candidates, never adopt their facts."""
import copy

import pytest

from company_bid import material_content_index, material_content_matching, material_local_ocr
from company_bid import material_ocr_index, material_recall
from company_bid.material_matching import material_candidate_report
from company_bid.storage import digest
from test_material_content_index import Context as IndexContext, manifest, pdf_file
from test_material_content_matching import Context
from test_material_ocr_index import _engine, _execute, _manifest, _pdf


@pytest.fixture(autouse=True)
def no_real_ocr(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Matching tests must never invoke an OCR engine or provider")
    monkeypatch.setattr(material_local_ocr, "recognize", forbidden)
    monkeypatch.setattr(material_local_ocr, "engine_fingerprint", forbidden)


def requirement(text="须提供营业执照。"):
    return {"requirement_id": "R-OCR", "text": text, "response_mode": "attachment",
            "category": "qualification"}


def report(value, index, req=None):
    return material_candidate_report([req or requirement()], [], attachments=value["attachments"],
        sources=value["sources"], evidence_selections=value["evidence_selections"],
        parsed=index["parsed"])


def _block(unit, text, origin):
    return {"block_id": unit["unit_id"] + "-" + origin, "source_id": unit["source_id"],
            "kind": "paragraph", "origin": origin, "text": text, "rows": [],
            "locator": unit["locator"] + ("/ocr/line[0]" if origin == "ocr" else "/p[0]")}


def indexed_case(tmp_path, text="工商营业执照", *, title="合成扫描证明", crop=None,
                 native_text=None, mixed=False):
    """Use a real synthetic source and selected binding, then project fake OCR."""
    path = tmp_path / "proof.pdf"
    count = 2 if native_text and not mixed else 1
    pdf_file(path, ["SYNTHETIC"] * count)
    value = manifest(path, pages=list(range(1, count + 1)), title=title, crop=crop)
    index = material_content_index.build_material_content_index(IndexContext(), value, {"documents": []})
    units = index["parsed"]["documents"][0]["units"]
    for number, unit in enumerate(units):
        native_only = native_text is not None and not mixed and number == 0
        unit["blocks"] = [_block(unit, native_text, "native")] if native_only else [_block(unit, text, "ocr")]
        if native_text and mixed:
            unit["blocks"].insert(0, _block(unit, native_text, "native"))
        unit["coverage"].update(status="read" if native_only else "needs_ocr",
            origin="native" if native_only else "mixed" if mixed else "ocr",
            native_text_chars=len(native_text or "") if native_only or mixed else 0)
        # A mixed scan retains its reader-owned native channel after transcription.
        unit["material_content_native_readable"] = native_only or bool(native_text and mixed)
        unit["material_content_ocr_candidate_readable"] = not native_only and bool(text)
        if not native_only:
            unit["transcription_status"] = "ocr_transcribed_unverified" if text else "ocr_no_text_unresolved"
    read = int(bool(native_text and not mixed))
    index["coverage"].update(selected_unit_count=count,
        status_counts={"read": read, "empty": 0, "needs_ocr": count - read, "unsupported": 0},
        unread_unit_count=count - read, ocr_processed_unit_count=1,
        ocr_transcribed_unverified_count=int(bool(text)), ocr_no_text_count=int(not text),
        ocr_transcribed_crop_count=int(bool(text and crop)), pending_ocr_unit_count=int(not text))
    index["parsed"]["coverage"] = copy.deepcopy(index["coverage"])
    index["output_sha256"] = digest({k: v for k, v in index.items() if k != "output_sha256"})
    return value, index


@pytest.mark.parametrize("text,requested,relation", [
    ("工商营业执照", "须提供营业执照。", "equivalent_alias"),
    ("质量体系证书", "须提供质量管理体系认证证书。", "equivalent_alias"),
    ("ISO 9001:2015", "须提供质量管理体系认证证书。", "related_standard_requires_validation"),
])
def test_ocr_alias_or_standard_recalls_candidate_without_reading_or_qualification(tmp_path, text, requested, relation):
    value, index = indexed_case(tmp_path, text)
    before = copy.deepcopy((value, index))
    result = report(value, index, requirement(requested))
    group, = result["proof_groups"]
    query, = result["proof_queries"]
    assert query["candidate_group_ids"] == ["G"]
    assert group["read_scope_status"] == "unread_or_incomplete"
    assert query["read_scope_status"] == "incomplete" and query["unread_scope_present"]
    assert group["qualification_result"] == query["qualification_result"] == "not_determined"
    basis, = group["read_text_basis"]
    assert basis["candidate_only"] and basis["content_coverage"] == "ocr_transcribed_unverified"
    assert any(hit["relation"] == relation for hit in basis["hits"])
    assert group["machine_findings_pending_review"] == [basis]
    assert result["assembly_candidates"] == []
    assert (value, index) == before


@pytest.mark.parametrize("text", ["尚未提供营业执照。", "本文件仅为营业执照申请说明。", "安全生产许可证"])
def test_ocr_denial_or_wrong_type_cannot_remove_original_catalog_attachment(tmp_path, text):
    value, index = indexed_case(tmp_path, text, title="营业执照")
    before = copy.deepcopy((value, index))
    baseline = material_candidate_report([requirement()], [], attachments=value["attachments"],
        sources=value["sources"], evidence_selections=value["evidence_selections"])
    assert baseline["assembly_candidates"]
    result = report(value, index)
    group, = result["proof_groups"]
    assert group["role_exclusions"] == [] and group["can_assemble"]
    assert group["machine_findings_pending_review"]
    assert result["assembly_candidates"] == baseline["assembly_candidates"]
    assert result["proof_queries"][0]["excluded_group_ids"] == []
    assert (value, index) == before


@pytest.mark.parametrize("native_text,expected", [
    ("尚未提供营业执照。", "proof_not_provided_or_not_obtained"),
    ("安全生产许可证", "conflicting_read_document_type"),
])
@pytest.mark.parametrize("mixed", [False, True], ids=["separate-native-page", "mixed-native-and-ocr-page"])
def test_native_explicit_counterevidence_still_blocks_assembly_after_ocr(tmp_path, native_text, expected, mixed):
    value, index = indexed_case(tmp_path, "工商营业执照", title="营业执照",
                                native_text=native_text, mixed=mixed)
    result = report(value, index)
    group, = result["proof_groups"]
    assert expected in group["role_exclusions"]
    assert not group["can_assemble"] and result["assembly_candidates"] == []
    assert result["proof_queries"][0]["candidate_group_ids"] == []
    assert group["qualification_result"] == "not_determined"


def test_transcription_preserves_authenticated_partial_native_channel_and_counterevidence(tmp_path, monkeypatch):
    path = _pdf(tmp_path)
    value = _manifest(path)
    value["evidence_selections"][0]["title"] = "营业执照"
    calls = _engine(monkeypatch, text="工商营业执照")
    denial = "尚未提供营业执照。"

    def native_reader_projection(manifest_value, index):
        # Simulate the native reader reporting its own text beside an unread scan.
        # The OCR adapter must preserve this authenticated reader-owned channel.
        unit, = index["parsed"]["documents"][0]["units"]
        assert unit["coverage"]["status"] == "needs_ocr"
        unit["material_content_native_readable"] = True
        unit["blocks"].append(_block(unit, denial, "native"))
        unit["coverage"]["native_text_chars"] = len(denial)

    ex, run_id, state = _execute(tmp_path, value, mutate=native_reader_projection)
    assert state["status"] == "succeeded", state.get("error_code")
    output = ex.output(run_id)
    native, = output["native"]["parsed"]["documents"][0]["units"]
    transcribed, = output["index"]["parsed"]["documents"][0]["units"]
    assert len(calls) == 1
    assert native["material_content_native_readable"] is True
    assert transcribed["material_content_native_readable"] is True
    assert transcribed["material_content_ocr_candidate_readable"] is True
    assert transcribed["coverage"]["status"] == "needs_ocr"
    assert [b for b in transcribed["blocks"] if b.get("origin") == "native"] == native["blocks"]
    assert any(b.get("origin") == "ocr" for b in transcribed["blocks"])
    result = report(output["manifest"], output["index"])
    group, = result["proof_groups"]
    assert "proof_not_provided_or_not_obtained" in group["role_exclusions"]
    assert not group["can_assemble"] and result["assembly_candidates"] == []
    assert any(not b.get("candidate_only") for b in group["read_text_basis"])
    assert group["machine_findings_pending_review"]


@pytest.mark.parametrize("damage", ["document_source", "document_hash", "unit_source", "block_source",
                                    "unit_group", "group_binding", "selection_crop",
                                    "selection_rotation", "selection_dpi", "missing_selection_hash",
                                    "wrong_selection_hash"])
def test_ocr_cannot_be_borrowed_without_exact_source_group_or_crop_scope(tmp_path, damage):
    value, index = indexed_case(tmp_path, crop=(0.1, 0.1, 0.5, 0.5))
    assert report(value, index)["proof_queries"][0]["candidate_group_ids"] == ["G"]
    document = index["parsed"]["documents"][0]
    unit, = document["units"]
    if damage == "document_source":
        document["source_id"] = "OTHER"
    elif damage == "document_hash":
        document["sha256"] = "f" * 64
    elif damage == "unit_source":
        unit["source_id"] = "OTHER"
    elif damage == "block_source":
        unit["blocks"][0]["source_id"] = "OTHER"
    elif damage == "unit_group":
        unit["material_content_group_ids"] = ["OTHER"]
    elif damage == "group_binding":
        index["parsed"]["group_bindings"][0]["attachment_ids"] = ["OTHER"]
    elif damage == "selection_crop":
        # The same page and selection name cannot lend text to another crop.
        value["evidence_selections"][0]["crop_box_normalized"] = [0.5, 0.5, 0.9, 0.9]
    elif damage == "selection_rotation":
        value["evidence_selections"][0]["rotate_clockwise"] = 90
    elif damage == "selection_dpi":
        value["evidence_selections"][0]["render_dpi"] = 180
    elif damage == "missing_selection_hash":
        index["parsed"].pop("group_selection_sha256s", None)
    else:
        index["parsed"]["group_selection_sha256s"]["G"] = "f" * 64
    result = report(value, index)
    assert result["proof_queries"][0]["candidate_group_ids"] == []
    assert result["proof_groups"][0]["read_text_basis"] == []
    assert result["assembly_candidates"] == []


def _install_projected_index(monkeypatch, index):
    native = copy.deepcopy(index)
    for document in native["parsed"]["documents"]:
        for unit in document["units"]:
            unit["blocks"] = [b for b in unit["blocks"] if b.get("origin") != "ocr"]
            unit.pop("transcription_status", None)
            unit["material_content_ocr_candidate_readable"] = False
    monkeypatch.setattr(material_content_index, "build_material_content_index", lambda *_: copy.deepcopy(native))
    calls = []

    def transcribe(ctx, manifest_value, native_value, options):
        assert native_value == native and options == {"enabled": True}
        calls.append(True)
        return copy.deepcopy(index)

    monkeypatch.setattr(material_ocr_index, "transcribe_selected_index", transcribe)
    return calls


def test_integration_preserves_catalog_assembly_when_ocr_wrongly_denies_it(tmp_path, monkeypatch):
    value, index = indexed_case(tmp_path, "尚未提供营业执照。", title="营业执照")
    catalog = material_candidate_report([requirement()], [], attachments=value["attachments"],
        sources=value["sources"], evidence_selections=value["evidence_selections"])
    before = copy.deepcopy((value, index, catalog))
    calls = _install_projected_index(monkeypatch, index)
    result = material_content_matching.integrate_original_content(Context(), [requirement()], value,
        {"documents": []}, catalog, {"enabled": True})
    assert calls == [True]
    assert result["assembly_candidates"] == catalog["assembly_candidates"]
    assert result["ocr_candidate_assembly_guard"]["withheld_candidates"] == []
    assert result["ocr_candidate_assembly_guard"]["automatic_adoption"] is False
    assert (value, index, catalog) == before


def test_integration_guard_withholds_new_assembly_without_prior_catalog_decision(tmp_path, monkeypatch):
    value, index = indexed_case(tmp_path, "工商营业执照", title="营业执照")
    catalog = material_candidate_report([requirement()], [], attachments=value["attachments"],
        sources=value["sources"], evidence_selections=value["evidence_selections"])
    # A historical catalog snapshot approved no assembly for this requirement.
    # Even a refreshed program candidate must not bypass that frozen decision.
    catalog["assembly_candidates"] = []
    before = copy.deepcopy(catalog)
    _install_projected_index(monkeypatch, index)
    result = material_content_matching.integrate_original_content(Context(), [requirement()], value,
        {"documents": []}, catalog, {"enabled": True})
    assert result["proof_queries"][0]["candidate_group_ids"] == ["G"]
    assert result["assembly_candidates"] == []
    guard = result["ocr_candidate_assembly_guard"]
    assert guard["automatic_adoption"] is False and guard["catalog_decision_count"] == 0
    withheld, = guard["withheld_candidates"]
    assert withheld["group_id"] == "G" and withheld["qualification_result"] == "not_determined"
    assert catalog == before


def test_issue_transcription_counts_preserve_original_missing_and_requirement_text(tmp_path):
    value, index = indexed_case(tmp_path, "工商营业执照", crop=(0.1, 0.1, 0.5, 0.5))
    requested = requirement("须提供有效营业执照，准确字段与期限仍须人工核验。")
    result = report(value, index, requested)
    result["original_content_index"] = {k: v for k, v in index.items() if k != "parsed"}
    result["original_content_index"]["coverage"].update(selected_unit_count=3,
        status_counts={"read": 0, "empty": 0, "needs_ocr": 3}, unread_unit_count=3,
        ocr_processed_unit_count=3, ocr_transcribed_unverified_count=2,
        ocr_transcribed_crop_count=1, ocr_no_text_count=1, pending_ocr_unit_count=1)
    result["semantic_recall"] = {"queries": [{"requirement_id": "R-OCR",
        "status": "no_candidate_in_frozen_catalog", "candidates": [], "condition_findings": [],
        "qualification_result": "not_determined"}]}
    issues = [{"issue_id": "M-OCR", "severity": "missing", "requirement_refs": ["R-OCR"],
               "reason": "原缺项及有效期条件必须保留。", "action": "补齐并核验原件。",
               "source_requirement": requested["text"]},
              {"issue_id": "M-UNRELATED", "severity": "missing", "requirement_refs": ["OTHER"],
               "reason": "不相关原缺项。"}]
    before = copy.deepcopy((issues, result, requested))
    change, = material_recall.annotate_material_issues(issues, result)
    assert len(issues) == 2 and issues[1] == before[0][1]
    assert issues[0]["reason"].startswith(before[0][0]["reason"] + "\n")
    assert all(issues[0][k] == v for k, v in before[0][0].items() if k != "reason")
    assert "全部所选3页/图" in issues[0]["reason"]
    assert "2项已有本地OCR转录文字（含1项裁剪区域）" in issues[0]["reason"]
    assert "1项仍未取得可用识别文字" in issues[0]["reason"]
    assert "机器转录不等于原件完整读取或资格核验" in issues[0]["reason"]
    assert "未做OCR" not in issues[0]["reason"] and "原缺项继续保留" in issues[0]["reason"]
    assert change["before"] == before[0][0] and change["after"] == issues[0]
    assert change["requirement_contexts"][0]["original_requirement"] == requested["text"]
    assert result == before[1] and requested == before[2]
