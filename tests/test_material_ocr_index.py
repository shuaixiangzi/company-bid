"""Selected-image OCR boundaries and real Executor recovery, without engine calls."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from docx import Document
from PIL import Image, ImageDraw
import pytest

from company_bid import material_content_index, material_local_ocr, material_ocr_index
from company_bid.evidence import prepare_evidence
from company_bid.models import file_sha256
from company_bid.runtime import Executor, NodeSpec
from company_bid.storage import digest


FINGERPRINT = {"engine": "synthetic-local-ocr", "version": "test-v1", "language": "en-US"}
OPTIONS = {"expected_engine_fingerprint": FINGERPRINT}


def _source(path):
    return {"source_id": "SOURCE", "title": "Synthetic selected original", "path": str(path),
            "sha256": file_sha256(path), "kind": "company_material", "locator": "document",
            "provenance": "synthetic", "synthetic": True}


def _pdf(tmp_path, count=1):
    path = tmp_path / "selected.pdf"
    images = []
    for index in range(count):
        image = Image.new("RGB", (180, 260), "white")
        drawing = ImageDraw.Draw(image)
        drawing.rectangle((8, 12, 78 + index * 20, 110), fill="red" if index == 0 else "blue")
        drawing.rectangle((100, 150, 170, 250), fill="green")
        images.append(image)
    images[0].save(path, "PDF", save_all=True, append_images=images[1:])
    for image in images:
        image.close()
    return path


def _manifest(path, *, pages=None, parts=None, crop=None, rotation=0):
    source = _source(path)
    selection = {"selection_id": "SELECTED", "source_id": source["source_id"],
        "source_sha256": source["sha256"], "title": "Synthetic selected proof",
        "selection_reason": "Offline scope regression; not a qualification assertion.",
        "format": "docx_images" if parts else "pdf_pages", "pages": [] if parts else pages or [1],
        "package_parts": parts or [], "render_dpi": 160, "rotate_clockwise": rotation,
        "crop_box_normalized": crop}
    return {"sources": [source], "evidence_selections": [selection], "attachments": [], "material_records": []}


def _engine(monkeypatch, *, text="ISO14001 selected OCR", fail_target_once=None):
    calls = []
    failed = set()
    monkeypatch.setattr(material_local_ocr, "engine_fingerprint", lambda options: copy.deepcopy(FINGERPRINT))

    def recognize(ctx, *, image_path, image_sha256, identity, options):
        assert file_sha256(Path(image_path)) == image_sha256
        calls.append(copy.deepcopy(identity))
        target = identity["target"]
        if target == fail_target_once and target not in failed:
            failed.add(target)
            raise material_content_index.MaterialContentFailure("OCR_SYNTHETIC_INTERRUPTION")
        return {"image_sha256": image_sha256, "text": text,
                "lines": [{"text": text, "words": [{"text": text, "box": [0.0, 0.0, 40.0, 15.0]}]}] if text else [],
                "call_id": "synthetic-ocr-" + str(len(calls)), "api_cost_estimate_usd": 0}

    monkeypatch.setattr(material_local_ocr, "recognize", recognize)
    return calls


def _executor(root, *, mutate=None, parsed=None):
    def proof(ctx, value):
        manifest = copy.deepcopy(value["manifest"])
        result = prepare_evidence(ctx, manifest["sources"], manifest["evidence_selections"], root)
        manifest["sources"].extend(result["sources"])
        manifest["attachments"].extend(result["attachments"])
        return {"manifest": manifest}

    def ocr(ctx, value):
        manifest = copy.deepcopy(value["manifest"])
        index = material_content_index.build_material_content_index(ctx, manifest, parsed or {"documents": []})
        if mutate:
            mutate(manifest, index)
        result = material_ocr_index.transcribe_selected_index(ctx, manifest, index, copy.deepcopy(OPTIONS))
        return {"manifest": manifest, "native": index, "index": result}

    return Executor(root, [NodeSpec("N0", "synthetic-proof-v1", proof),
                           NodeSpec("N3", "synthetic-ocr-index-v1", ocr)], {})


def _execute(tmp_path, manifest, *, mutate=None, parsed=None):
    executor = _executor(tmp_path / "runtime", mutate=mutate, parsed=parsed)
    run_id = executor.create_run({"manifest": manifest})
    state = executor.execute(run_id)
    return executor, run_id, state


def test_selected_pdf_crop_rotation_and_actual_png_are_bound(tmp_path, monkeypatch):
    path = _pdf(tmp_path)
    calls = _engine(monkeypatch)
    executor, run_id, state = _execute(tmp_path, _manifest(path, crop=(0.1, 0.2, 0.8, 0.7), rotation=90))
    assert state["status"] == "succeeded", state.get("error_code")
    output = executor.output(run_id)
    image, = output["manifest"]["attachments"]
    selection, = output["manifest"]["evidence_selections"]
    call, = calls
    assert call["source_sha256"] == file_sha256(path)
    assert call["target"] == 1
    assert list(call["crop_box_normalized"]) == [0.1, 0.2, 0.8, 0.7]
    assert call["rotate_clockwise"] == 90 and call["render_dpi"] == 160
    assert call["selection_sha256"] == digest(selection)
    assert call["image_sha256"] == image["sha256"] == file_sha256(Path(image["path"]))
    with Image.open(image["path"]) as selected:
        # The source crop is portrait; the selected 90 degree rotation is landscape.
        assert selected.width > selected.height
    index = output["index"]
    meta, = index["units"]
    assert meta["transcription_scope"] == "selected_crop"
    assert meta["transcription_status"] == "ocr_transcribed_unverified"
    assert index["coverage"]["ocr_transcribed_crop_count"] == 1
    assert index["coverage"]["status_counts"]["read"] == 0
    assert index["coverage"]["native_text_char_count"] == 0
    assert index["coverage"]["ocr_text_char_count"] > 0
    assert index["summary"]["qualification_result"] == "not_determined"
    assert state.get("call_budget", {}).get("actual_calls", 0) == 0


def test_same_original_page_with_distinct_rotations_has_distinct_ocr_units(tmp_path, monkeypatch):
    path = _pdf(tmp_path)
    value = _manifest(path, rotation=0)
    rotated = copy.deepcopy(value["evidence_selections"][0])
    rotated.update(selection_id="ROTATED", rotate_clockwise=90)
    value["evidence_selections"].append(rotated)
    calls = _engine(monkeypatch)
    executor, run_id, state = _execute(tmp_path, value)
    assert state["status"] == "succeeded", state.get("error_code")
    assert len(calls) == 2
    assert {call["rotate_clockwise"] for call in calls} == {0, 90}
    assert len({call["image_sha256"] for call in calls}) == 2
    assert len({call["selection_sha256"] for call in calls}) == 2
    index = executor.output(run_id)["index"]
    assert len({meta["ocr_unit_id"] for meta in index["units"]}) == 2
    assert index["coverage"]["ocr_transcribed_unverified_count"] == 2


def test_docx_ocr_uses_only_selected_part_and_never_parent_text(tmp_path, monkeypatch):
    first = tmp_path / "selected.png"; Image.new("RGB", (90, 140), "blue").save(first)
    second = tmp_path / "unselected.png"; Image.new("RGB", (100, 150), "red").save(second)
    path = tmp_path / "parent.docx"
    document = Document()
    document.add_paragraph("PARENT-NATIVE-ISO9001 must never prove the selected certificate")
    document.add_picture(str(first)); document.add_picture(str(second)); document.save(path)
    calls = _engine(monkeypatch, text="SELECTED-ISO14001")
    parsed = {"documents": [{"source_id": "SOURCE", "sha256": file_sha256(path), "role": "reference_bid",
        "units": [{"unit_id": "PARENT-U", "source_id": "SOURCE", "locator": "docx/body/p[0]",
            "coverage": {"status": "read"}, "blocks": [
                {"block_id": "PARENT-TEXT", "source_id": "SOURCE", "locator": "docx/body/p[0]/text",
                 "kind": "paragraph", "text": "PARENT-NATIVE-ISO9001", "origin": "native"},
                {"block_id": "SELECTED-IMAGE", "source_id": "SOURCE", "locator": "docx/body/p[1]/image[0]",
                 "kind": "image", "origin": "native", "image_ref": {
                     "package_part": "word/media/image1.png", "sha256": file_sha256(first)}}]}]}]}
    executor, run_id, state = _execute(tmp_path, _manifest(path, parts=["word/media/image1.png"]), parsed=parsed)
    assert state["status"] == "succeeded", state.get("error_code")
    output = executor.output(run_id)
    unit, = output["index"]["parsed"]["documents"][0]["units"]
    text = material_content_index._text(unit)
    assert "SELECTED-ISO14001" in text and "PARENT-NATIVE" not in text and "ISO9001" not in text
    assert len(calls) == 1 and calls[0]["target"] == "word/media/image1.png"
    assert output["index"]["summary"]["parent_docx_text_read"] is False
    assert unit["coverage"]["status"] == "needs_ocr"
    assert all(block["origin"] == "ocr" for block in unit["blocks"] if block.get("text"))
    assert not any(block["kind"] == "table" for block in unit["blocks"])


@pytest.mark.parametrize("changed", ["source", "attachment", "declared_image_hash"])
def test_changed_original_or_selected_png_is_rejected_before_ocr(tmp_path, monkeypatch, changed):
    path = _pdf(tmp_path)
    calls = _engine(monkeypatch)

    def mutate(manifest, index):
        if changed == "source":
            with path.open("ab") as handle:
                handle.write(b"\nchanged-source")
        elif changed == "attachment":
            Path(manifest["attachments"][0]["path"]).write_bytes(b"changed-image")
        else:
            manifest["attachments"][0]["sha256"] = "f" * 64

    _, _, state = _execute(tmp_path, _manifest(path), mutate=mutate)
    assert state["status"] == "failed"
    expected = "MATERIAL_CONTENT_SOURCE_HASH_MISMATCH" if changed == "source" else "MATERIAL_OCR_ATTACHMENT_HASH_INVALID"
    assert state["error_code"] == expected
    assert calls == []


def test_wrong_selected_target_cannot_borrow_another_page(tmp_path, monkeypatch):
    path = _pdf(tmp_path, 2)
    calls = _engine(monkeypatch)

    def mutate(manifest, index):
        index["units"][0]["target"] = 2

    _, _, state = _execute(tmp_path, _manifest(path, pages=[1]), mutate=mutate)
    assert state["status"] == "failed" and state["error_code"] == "MATERIAL_OCR_SELECTION_SOURCE_INVALID"
    assert calls == []


@pytest.mark.parametrize("changed", ["source", "attachment"])
def test_source_and_selected_png_are_rechecked_after_recognition(tmp_path, monkeypatch, changed):
    path = _pdf(tmp_path)
    calls = _engine(monkeypatch)
    recognize = material_local_ocr.recognize

    def changed_during_call(ctx, **kwargs):
        result = recognize(ctx, **kwargs)
        target = path if changed == "source" else Path(kwargs["image_path"])
        with target.open("ab") as handle:
            handle.write(b"\nchanged-during-recognition")
        return result

    monkeypatch.setattr(material_local_ocr, "recognize", changed_during_call)
    _, _, state = _execute(tmp_path, _manifest(path))
    assert state["status"] == "failed"
    expected = "MATERIAL_CONTENT_SOURCE_HASH_MISMATCH" if changed == "source" else "MATERIAL_OCR_IMAGE_CHANGED_DURING_CALL"
    assert state["error_code"] == expected
    assert len(calls) == 1


def test_rebuilt_image_must_match_the_frozen_selection_png(tmp_path, monkeypatch):
    path = _pdf(tmp_path)
    calls = _engine(monkeypatch)

    def mutate(manifest, index):
        # Both files are valid immutable images; only the declared selection has changed.
        manifest["evidence_selections"][0]["rotate_clockwise"] = 90

    _, _, state = _execute(tmp_path, _manifest(path), mutate=mutate)
    assert state["status"] == "failed"
    assert state["error_code"] == "MATERIAL_OCR_RENDERED_SCOPE_HASH_MISMATCH"
    assert calls == []


def test_empty_ocr_response_remains_unresolved_and_keeps_zero_native_text(tmp_path, monkeypatch):
    path = _pdf(tmp_path)
    calls = _engine(monkeypatch, text="")
    executor, run_id, state = _execute(tmp_path, _manifest(path))
    assert state["status"] == "succeeded", state.get("error_code")
    index = executor.output(run_id)["index"]
    unit, = index["parsed"]["documents"][0]["units"]
    assert len(calls) == 1
    assert unit["transcription_status"] == "ocr_no_text_unresolved"
    assert unit["coverage"]["status"] == "needs_ocr"
    assert not unit["material_content_ocr_candidate_readable"]
    assert index["coverage"]["ocr_no_text_count"] == 1
    assert index["coverage"]["pending_ocr_unit_count"] == 1
    assert index["coverage"]["native_text_char_count"] == index["coverage"]["ocr_text_char_count"] == 0


def test_recreated_executor_resumes_without_repeating_successful_ocr(tmp_path, monkeypatch):
    path = _pdf(tmp_path, 2)
    calls = _engine(monkeypatch, fail_target_once=2)
    root = tmp_path / "runtime"
    executor = _executor(root)
    run_id = executor.create_run({"manifest": _manifest(path, pages=[1, 2])})
    failed = executor.execute(run_id)
    assert failed["status"] == "failed" and failed["error_code"] == "OCR_SYNTHETIC_INTERRUPTION"
    assert [call["target"] for call in calls] == [1, 2]
    previous = {key: copy.deepcopy(unit["output_ref"]) for key, unit in failed["nodes"][1]["units"].items()
                if key.startswith("material-ocr-") and unit["status"] == "succeeded"}
    assert len(previous) == 1
    # This is a new Executor object reading durable checkpoints, not an in-memory cache.
    executor = _executor(root)
    resumed = executor.resume(run_id)
    assert resumed["status"] == "succeeded", resumed.get("error_code")
    assert [call["target"] for call in calls] == [1, 2, 2]
    assert all(resumed["nodes"][1]["units"][key]["output_ref"] == reference for key, reference in previous.items())
    index = executor.output(run_id)["index"]
    assert index["coverage"]["ocr_transcribed_unverified_count"] == 2
    assert index["coverage"]["native_text_char_count"] == 0
    events = [json.loads(line) for line in (root / "runs" / run_id / "events.jsonl").read_text().splitlines()]
    assert any(row["event"] == "unit.reused" and row.get("unit_id") in previous for row in events)
    assert len(resumed["nodes"][1]["units"][next(key for key in resumed["nodes"][1]["units"]
        if key.startswith("material-ocr-") and key not in previous)]["attempts"]) == 2
