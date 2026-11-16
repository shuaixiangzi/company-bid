"""Source-bound inventory repair without rereading or rewriting N1/N2."""
import copy
import json
from pathlib import Path

from docx import Document
from PIL import Image
import pytest

from company_bid.diagnostics import inspect_diagnostics
from company_bid.extraction import _index
from company_bid.intake import IntakeManifest
from company_bid.material_matching import material_candidate_report
from company_bid.material_source_selection import extend_material_evidence
from company_bid.models import file_sha256
from company_bid.reading import read_documents
from company_bid.runtime import Executor, NodeSpec
from test_reading import Context


def fixture(tmp_path):
    tender = tmp_path / "tender.docx"
    document = Document(); document.add_paragraph("合成招标原文。须提供营业执照和安全生产许可证。")
    document.save(tender)
    proof = tmp_path / "reference.docx"
    document = Document()
    for i, (title, color) in enumerate((("7.1 营业执照", "red"), ("7.2 安全生产许可证", "blue"))):
        png = tmp_path / f"image-{i}.png"; Image.new("RGB", (40, 60), color).save(png)
        document.add_paragraph(title); document.add_picture(str(png))
    document.save(proof)
    def source(sid, path, kind):
        return {"source_id": sid, "title": "合成来源", "path": str(path), "sha256": file_sha256(path),
                "locator": "document", "kind": kind, "provenance": "synthetic", "synthetic": True}
    manifest = {"project": {"project_id": "P", "name": "合成项目", "source_refs": ["T"]},
        "company": {"company_id": "C", "name": "合成公司", "source_refs": ["R"]},
        "sources": [source("T", tender, "tender"), source("R", proof, "company_material")],
        "source_roles": {"T": "tender", "R": "reference_bid"}, "facts": [], "attachments": [],
        "evidence_selections": [], "synthetic": True, "preparation_note": "合成来源绑定测试，非真实材料。"}
    parsed = read_documents(Context(), [{**s, "role": manifest["source_roles"][s["source_id"]]}
        for s in manifest["sources"]], {})
    reference = next(d for d in parsed["documents"] if d["source_id"] == "R")
    selections = []
    for i in range(2):
        h = reference["units"][i * 2]["blocks"][0]
        image = reference["units"][i * 2 + 1]["blocks"][0]
        selections.append({"selection_id": f"EXPLICIT-{i}", "source_id": "R",
            "source_sha256": reference["sha256"], "heading_block_id": h["block_id"],
            "heading_locator": h["locator"], "heading_text": h["text"],
            "image_block_id": image["block_id"], "image_locator": image["locator"],
            "package_part": image["image_ref"]["package_part"], "image_sha256": image["image_ref"]["sha256"]})
    return {"manifest": manifest, "parsed": parsed, "selections": selections}


def executor(root):
    def extend(ctx, value):
        return {"manifest": extend_material_evidence(ctx, value["manifest"], value["parsed"], value["selections"]),
                "parsed": value["parsed"]}
    return Executor(root, [NodeSpec("N3", "synthetic-material-selection-v1", extend),
                           NodeSpec("N4", "synthetic-unused", lambda ctx, value: value)], {})


def run(tmp_path, value):
    ex = executor(tmp_path / "runtime"); rid = ex.create_run(value)
    return ex, rid, ex.execute(rid, stop_after="N3")


def test_exact_heading_image_adds_candidate_without_changing_facts_or_parsed(tmp_path):
    value = fixture(tmp_path); before = copy.deepcopy(value)
    ex, rid, state = run(tmp_path, value)
    assert state["status"] == "paused_debug", state.get("error_code")
    out = ex.store.read_json(state["nodes"][0]["output_ref"])["data"]
    manifest = out["manifest"]
    assert value == before and out["parsed"] == value["parsed"]
    assert manifest["facts"] == []
    assert manifest["sources"][:2] == value["manifest"]["sources"]
    assert len(manifest["sources"]) == 4 and len(manifest["attachments"]) == 2
    assert all(manifest["source_roles"][s["source_id"]] == "provenance_only" for s in manifest["sources"][2:])
    assert [a["title"] for a in manifest["attachments"]] == [s["heading_text"] for s in value["selections"]]
    for attachment in manifest["attachments"]:
        assert file_sha256(attachment["path"]) == attachment["sha256"]
    IntakeManifest.model_validate(manifest)
    assert _index(out["parsed"], manifest) == _index(value["parsed"], value["manifest"])
    report = material_candidate_report([], [], attachments=manifest["attachments"], sources=manifest["sources"],
        evidence_selections=manifest["evidence_selections"], parsed=out["parsed"])
    assert len(report["proof_groups"]) == 2
    assert all(g["source_bound"] and g["bundle_complete"] for g in report["proof_groups"])
    assert all(g["read_scope_status"] == "unread_or_incomplete" and g["qualification_result"] == "not_determined"
               for g in report["proof_groups"])
    assert inspect_diagnostics(ex.store.root, rid)["actual_call_attempts"] == 0
    events = (ex.store.root / "runs" / rid / "events.jsonl").read_text(encoding="utf-8")
    spans = (ex.store.root / "runs" / rid / "spans.jsonl").read_text(encoding="utf-8")
    assert "营业执照" not in events + spans and "安全生产许可证" not in events + spans


@pytest.mark.parametrize("mutation,code", [
    ("title", "MATERIAL_SELECTION_HEADING_INVALID"),
    ("image_hash", "MATERIAL_SELECTION_IMAGE_INVALID"),
    ("image_part", "MATERIAL_SELECTION_IMAGE_INVALID"),
    ("unread_heading", "MATERIAL_SELECTION_HEADING_INVALID"),
    ("wrong_source", "MATERIAL_SELECTION_SOURCE_INVALID"),
    ("wrong_image_block", "MATERIAL_SELECTION_IMAGE_INVALID"),
    ("remote_heading", "MATERIAL_SELECTION_ADJACENCY_INVALID"),
    ("new_path", "MATERIAL_SELECTION_INPUT_INVALID"),
    ("duplicate_selection", "MATERIAL_SELECTION_DUPLICATE"),
    ("duplicate_image", "MATERIAL_SELECTION_DUPLICATE"),
    ("changed_file", "MATERIAL_SELECTION_SOURCE_CHANGED"),
])
def test_invalid_scope_does_not_add_proof(tmp_path, mutation, code):
    value = fixture(tmp_path); selection = value["selections"][0]
    doc = next(d for d in value["parsed"]["documents"] if d["source_id"] == "R")
    if mutation == "title": selection["heading_text"] = "无来源的新标题"
    elif mutation == "image_hash": selection["image_sha256"] = "0" * 64
    elif mutation == "image_part": selection["package_part"] = "word/media/unknown.png"
    elif mutation == "unread_heading": doc["units"][0]["coverage"]["status"] = "unsupported"
    elif mutation == "wrong_source": selection["source_id"] = "T"
    elif mutation == "wrong_image_block": selection["image_block_id"] = value["selections"][1]["image_block_id"]
    elif mutation == "remote_heading":
        for key in ("heading_block_id", "heading_locator", "heading_text"):
            selection[key] = value["selections"][1][key]
    elif mutation == "new_path": selection["path"] = str(tmp_path / "external.png")
    elif mutation == "duplicate_selection": value["selections"][1]["selection_id"] = selection["selection_id"]
    elif mutation == "duplicate_image": value["selections"][1] = {**selection, "selection_id": "OTHER"}
    elif mutation == "changed_file": Path(value["manifest"]["sources"][1]["path"]).write_bytes(b"corrupt")
    before = copy.deepcopy(value)
    ex, rid, state = run(tmp_path, value)
    assert state["status"] == "failed" and state["error_code"] == code
    assert "output_ref" not in state["nodes"][0] and value == before
    assert inspect_diagnostics(ex.store.root, rid)["actual_call_attempts"] == 0


def test_forged_parsed_heading_cannot_override_native_docx_text(tmp_path):
    value = fixture(tmp_path)
    doc = next(d for d in value["parsed"]["documents"] if d["source_id"] == "R")
    doc["units"][0]["blocks"][0]["text"] = "7.1 高新技术企业证书"
    value["selections"][0]["heading_text"] = "7.1 高新技术企业证书"
    _, _, state = run(tmp_path, value)
    assert state["error_code"] == "MATERIAL_SELECTION_NATIVE_HEADING_MISMATCH"


def test_native_image_relationship_is_rechecked_even_if_parsed_metadata_is_forged(tmp_path):
    value = fixture(tmp_path)
    doc = next(d for d in value["parsed"]["documents"] if d["source_id"] == "R")
    other = doc["units"][3]["blocks"][0]["image_ref"]
    doc["units"][1]["blocks"][0]["image_ref"] = copy.deepcopy(other)
    value["selections"] = value["selections"][:1]
    value["selections"][0].update(package_part=other["package_part"], image_sha256=other["sha256"])
    _, _, state = run(tmp_path, value)
    assert state["error_code"] == "MATERIAL_SELECTION_NATIVE_IMAGE_MISMATCH"


def test_interrupted_second_image_reuses_first_image_after_restart(tmp_path, monkeypatch):
    import company_bid.evidence as evidence
    value = fixture(tmp_path); original = evidence._selected_png; calls = []
    def flaky(image, selection):
        calls.append(selection.selection_id)
        if len(calls) == 2:
            raise evidence.EvidenceFailure("SYNTHETIC_SECOND_IMAGE_INTERRUPTION")
        return original(image, selection)
    monkeypatch.setattr(evidence, "_selected_png", flaky)
    ex, rid, failed = run(tmp_path, value)
    assert failed["error_code"] == "SYNTHETIC_SECOND_IMAGE_INTERRUPTION"
    completed = {k: copy.deepcopy(u["output_ref"]) for k, u in failed["nodes"][0]["units"].items()
                 if u["status"] == "succeeded"}
    restarted = executor(tmp_path / "runtime"); result = restarted.resume(rid, stop_after="N3")
    assert result["status"] == "paused_debug", result.get("error_code")
    assert calls == ["EXPLICIT-0", "EXPLICIT-1", "EXPLICIT-1"]
    assert all(result["nodes"][0]["units"][k]["output_ref"] == ref for k, ref in completed.items())


def test_an_existing_image_selection_is_not_imported_again(tmp_path):
    value = fixture(tmp_path); ex, _, state = run(tmp_path, value)
    extended = ex.store.read_json(state["nodes"][0]["output_ref"])["data"]["manifest"]
    value["manifest"] = extended
    value["selections"][0]["selection_id"] = "DIFFERENT-ID-SAME-IMAGE"
    _, _, duplicate = run(tmp_path, value)
    assert duplicate["error_code"] == "MATERIAL_SELECTION_DUPLICATE"
