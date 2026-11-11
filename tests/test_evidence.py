import copy
import io
from pathlib import Path
import zipfile

from docx import Document
from PIL import Image
from pypdf import PdfWriter

from company_bid.diagnostics import inspect_diagnostics
from company_bid.models import file_sha256
from company_bid.pipeline import build_executor
import pytest


def manifest(tmp_path, *, docx=False):
    tender = tmp_path / "tender.pdf"
    writer = PdfWriter(); writer.add_blank_page(width=200, height=300); writer.write(tender)
    proof = tmp_path / ("proof.docx" if docx else "proof.pdf")
    if docx:
        image = tmp_path / "test.png"; Image.new("RGB", (90, 120), "red").save(image)
        document = Document(); document.add_picture(str(image)); document.save(proof)
    else:
        writer = PdfWriter()
        for _ in range(2): writer.add_blank_page(width=200, height=300)
        writer.write(proof)
    def source(sid, path, kind):
        return {"source_id": sid, "title": "合成原件", "path": str(path), "sha256": file_sha256(path),
                "locator": "document", "kind": kind, "provenance": "synthetic", "synthetic": True}
    return {"project": {"project_id": "P", "name": "合成项目", "source_refs": ["T"]},
            "company": {"company_id": "C", "name": "合成公司", "source_refs": ["P"]},
            "sources": [source("T", tender, "tender"), source("P", proof, "company_material")],
            "source_roles": {"T": "tender", "P": "provenance_only"}, "synthetic": True,
            "preparation_note": "合成空白页/色块只验证指定范围转换，不是真实证明或资格验收。",
            "evidence_selections": [{"selection_id": "LICENSE", "source_id": "P", "source_sha256": file_sha256(proof),
                "title": "合成营业执照复印件", "selection_reason": "离线选定测试范围", "format": "docx_images" if docx else "pdf_pages",
                **({"package_parts": ["word/media/image1.png"]} if docx else {"pages": [1, 2]})}]}


def frozen_output(executor, state):
    return executor.store.read_json(state["nodes"][0]["output_ref"])["data"]


def test_all_selected_pdf_pages_become_ordered_traceable_bundle(tmp_path):
    value = manifest(tmp_path)
    original_hash = file_sha256(Path(value["sources"][1]["path"]))
    root = tmp_path / "runtime"
    executor = build_executor(root, {"pipeline": "automatic", "provider": {"mode": "prepared"}})
    rid = executor.create_run({"manifest": value})
    state = executor.execute(rid, stop_after="N0")
    assert state["status"] == "paused_debug", state.get("error_code")
    data = frozen_output(executor, state)
    attachments = data["manifest"]["attachments"]
    assert [(a["page_index"], a["page_count"]) for a in attachments] == [(1, 2), (2, 2)]
    assert {a["bundle_id"] for a in attachments} == {"LICENSE"}
    for index, attachment in enumerate(attachments, 1):
        assert file_sha256(Path(attachment["path"])) == attachment["sha256"]
        source = next(s for s in data["manifest"]["sources"] if s["source_id"] in attachment["source_refs"])
        assert source["locator"] == f"pdf/page[{index}]" and source["sha256"] == original_hash
    assert file_sha256(Path(value["sources"][1]["path"])) == original_hash
    assert inspect_diagnostics(root, rid)["actual_call_attempts"] == 0


def test_failed_proof_conversion_resumes_without_recopy_or_redo_successful_page(tmp_path, monkeypatch):
    import company_bid.evidence as module
    original = module._png; calls = []
    def convert(image):
        calls.append(1)
        if len(calls) == 2:
            raise module.EvidenceFailure("SYNTHETIC_RENDER_INTERRUPTION")
        return original(image)
    monkeypatch.setattr(module, "_png", convert)
    executor = build_executor(tmp_path / "runtime", {"pipeline": "automatic", "provider": {"mode": "prepared"}})
    rid = executor.create_run({"manifest": manifest(tmp_path)})
    failed = executor.execute(rid, stop_after="N0")
    assert failed["error_code"] == "SYNTHETIC_RENDER_INTERRUPTION"
    successes = {k: copy.deepcopy(v["output_ref"]) for k, v in failed["nodes"][0]["units"].items() if v["status"] == "succeeded"}
    resumed = executor.resume(rid, stop_after="N0")
    assert resumed["status"] == "paused_debug", resumed.get("error_code")
    assert len(calls) == 3
    assert all(resumed["nodes"][0]["units"][key]["output_ref"] == ref for key, ref in successes.items())


def test_exact_docx_embedded_image_is_prepared_without_claiming_a_physical_page(tmp_path):
    executor = build_executor(tmp_path / "runtime", {"pipeline": "automatic", "provider": {"mode": "prepared"}})
    rid = executor.create_run({"manifest": manifest(tmp_path, docx=True)})
    state = executor.execute(rid, stop_after="N0")
    assert state["status"] == "paused_debug", state.get("error_code")
    data = frozen_output(executor, state)
    assert len(data["manifest"]["attachments"]) == 1
    assert data["manifest"]["sources"][-1]["locator"] == "docx/word/media/image1.png"


def test_invalid_selected_pdf_page_is_a_technical_failure(tmp_path):
    value = manifest(tmp_path); value["evidence_selections"][0]["pages"] = [3]
    executor = build_executor(tmp_path / "runtime", {"pipeline": "automatic", "provider": {"mode": "prepared"}})
    rid = executor.create_run({"manifest": value})
    state = executor.execute(rid)
    assert state["status"] == "failed" and state["error_code"] == "EVIDENCE_PAGE_OUT_OF_RANGE"
    assert state["cursor"] == 0 and "output_ref" not in state["nodes"][0]


def test_proof_transforms_are_explicit_frozen_and_bounded(tmp_path):
    from company_bid.evidence import EvidenceSelection
    value = manifest(tmp_path)
    selection = value["evidence_selections"][0]
    selection.update(crop_box_normalized=[0.1, 0.2, 0.9, 0.8], rotate_clockwise=90)
    executor = build_executor(tmp_path / "runtime", {"pipeline": "automatic", "provider": {"mode": "prepared"}})
    rid = executor.create_run({"manifest": value})
    state = executor.execute(rid, stop_after="N0")
    assert state["status"] == "paused_debug"
    data = frozen_output(executor, state)
    assert "rotate_clockwise" in data["manifest"]["sources"][-1]["locator"]
    selection["crop_box_normalized"] = [-0.1, 0, 1, 1]
    with pytest.raises(ValueError, match="crop must stay"):
        EvidenceSelection.model_validate(selection)


def _refresh_proof_hash(value):
    proof = Path(value["sources"][1]["path"])
    sha256 = file_sha256(proof)
    value["sources"][1]["sha256"] = sha256
    value["evidence_selections"][0]["source_sha256"] = sha256
    return sha256


def _replace_selected_image(value, data):
    proof = Path(value["sources"][1]["path"])
    with zipfile.ZipFile(io.BytesIO(proof.read_bytes())) as original:
        parts = [(entry, original.read(entry)) for entry in original.infolist()]
    with zipfile.ZipFile(proof, "w") as changed:
        for entry, blob in parts:
            changed.writestr(entry, data if entry.filename == "word/media/image1.png" else blob)
    return _refresh_proof_hash(value)


def test_duplicate_exact_docx_image_part_is_rejected_before_any_image_is_adopted(tmp_path):
    value = manifest(tmp_path, docx=True)
    proof = Path(value["sources"][1]["path"])
    blue = io.BytesIO(); Image.new("RGB", (90, 120), "blue").save(blue, format="PNG")
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(proof, "a") as archive:
            archive.writestr("word/media/image1.png", blue.getvalue())
    original_hash = _refresh_proof_hash(value)
    executor = build_executor(tmp_path / "runtime", {"pipeline": "automatic", "provider": {"mode": "prepared"}})
    rid = executor.create_run({"manifest": value}); state = executor.execute(rid, stop_after="N0")
    assert state["status"] == "failed" and state["error_code"] == "EVIDENCE_DOCX_PART_AMBIGUOUS"
    assert state["cursor"] == 0 and "output_ref" not in state["nodes"][0]
    assert not any(unit["status"] == "succeeded" for key, unit in state["nodes"][0]["units"].items() if key.startswith("proof-"))
    assert file_sha256(proof) == original_hash


@pytest.mark.parametrize("kind,code", [
    ("animated", "EVIDENCE_ANIMATED_IMAGE_UNSUPPORTED"),
    ("oversized", "EVIDENCE_IMAGE_LIMIT_EXCEEDED"),
])
def test_original_frame_count_and_dimensions_are_checked_before_crop_or_transpose(tmp_path, monkeypatch, kind, code):
    import company_bid.evidence as module
    value = manifest(tmp_path, docx=True)
    content = io.BytesIO()
    if kind == "animated":
        Image.new("RGB", (90, 120), "red").save(content, format="PNG", save_all=True,
            append_images=[Image.new("RGB", (90, 120), "blue")], duration=100, loop=0)
        with Image.open(io.BytesIO(content.getvalue())) as image:
            assert image.n_frames == 2
    else:
        Image.new("RGB", (6001, 1), "red").save(content, format="PNG")
    original_hash = _replace_selected_image(value, content.getvalue())
    # This crop would hide the source dimensions and both transformations would
    # lose the source's animation metadata if validation happened afterwards.
    value["evidence_selections"][0]["crop_box_normalized"] = [0.0, 0.0, 0.1, 1.0]
    value["evidence_selections"][0]["rotate_clockwise"] = 90
    def no_pixel_transform(*args, **kwargs):
        pytest.fail("Original-image validation must precede pixel transformation")
    monkeypatch.setattr(module.ImageOps, "exif_transpose", no_pixel_transform)
    executor = build_executor(tmp_path / "runtime", {"pipeline": "automatic", "provider": {"mode": "prepared"}})
    rid = executor.create_run({"manifest": value}); state = executor.execute(rid, stop_after="N0")
    assert state["status"] == "failed" and state["error_code"] == code
    assert state["cursor"] == 0 and "output_ref" not in state["nodes"][0]
    assert file_sha256(Path(value["sources"][1]["path"])) == original_hash
