"""Original screenshot transcription through the same N1 reader and checkpoint path."""

from copy import deepcopy
import hashlib

import pytest
from pypdf import PdfWriter

from company_bid.ocr import DEEPSEEK_OCR_VERSION, RasterOptions, _prepare_image, _Request
from company_bid.provider import ProviderFailure
from company_bid.reading import ReadingFailure, read_documents
from company_bid import reading_review_origin
from test_reading import Context, echo_ocr, pdf, source, text


class ReviewContext(Context):
    artifact_scope = "diagnostic"

    def __init__(self):
        super().__init__()
        self.execution_config = {"run_purpose": "diagnostic_replay",
                                 "ocr": {"engine": "deepseek_ocr", "recorded_targets": []}}
        self.images = {}

    def store_bytes(self, name, blob, media_type):
        checksum = hashlib.sha256(blob).hexdigest()
        self.images[checksum] = blob
        return {"artifact_id": checksum[:32], "path": name, "size_bytes": len(blob),
                "sha256": checksum, "media_type": media_type}

    def read_artifact(self, ref):
        return self.images[ref["sha256"]]


@pytest.fixture
def original(tmp_path):
    path = pdf(tmp_path / "base.pdf", ["IMAGE"])
    out = tmp_path / "original.pdf"
    writer = PdfWriter(clone_from=path)
    content = (text("Preserved heading", 25, 207) + "q 150 0 0 150 20 20 cm /Im1 Do Q\n" +
               "q 65 0 0 65 220 55 cm /Im1 Do Q\n" + text("Native footer", 25, 10))
    writer.pages[0]["/Contents"].set_data(content.encode("ascii"))
    writer.write(out)
    original_source = {**source(out), "provenance": "original", "kind": "tender", "synthetic": False}
    unit_id = "N1-" + hashlib.sha256(original_source["source_id"].encode()).hexdigest()[:16] + "-pdf-p00001"
    locator = "pdf/page[1]/image[0]"
    bbox = [20.0, 50.0, 170.0, 200.0]
    request = _Request(source_id=original_source["source_id"], sha256=original_source["sha256"], path=str(out),
                       unit_id=unit_id, locator=locator, page=1, bbox=bbox,
                       object_kind="pdf_region", ocr_version=DEEPSEEK_OCR_VERSION)
    _, renderer = _prepare_image(request, out.read_bytes(), RasterOptions(render_dpi=160))
    review = {"source_id": request.source_id, "source_sha256": request.sha256, "page": request.page,
              "locator": locator, "bbox": bbox, "image_sha256": renderer["image_sha256"],
              "width_px": renderer["width"], "height_px": renderer["height"], "reviewed_by": "Fixture reviewer",
              "entries": [{"text": "Open the portal", "pixel_bbox": [20, 20, 110, 35], "role": "instruction"}],
              "unknown": [{"pixel_bbox": [106, 22, 123, 37], "reason": "Clipped UI badge", "critical": False}],
              "origin": {"run_id": "1" * 32, "unit_id": "tender-ocr-" + "a" * 64,
                         "call_id": "2" * 32, "raw_response_sha256": "3" * 64}}
    return original_source, review, unit_id


@pytest.fixture
def history(monkeypatch):
    calls = []

    def authenticate(ctx, binding, **identity):
        calls.append((binding, identity))
        corrections = identity.get("corrections", [])
        return {"origin": binding, "historical_status": "succeeded" if corrections else "failed",
                "historical_error_code": None if corrections else "OCR_IMAGE_REGION_REVIEW_REQUIRED",
                "historical_required_text": ["Open the p0rtal" if corrections else "Open the portal"],
                "historical_billing": {"provider_returned_pages": 1}}

    monkeypatch.setattr(reading_review_origin, "verify_review_origin", authenticate)
    return calls


def read(context, original, ocr=echo_ocr, **overrides):
    document, review, _ = original
    return read_documents(context, [document], {"pdf_region_reviews": [review], "ocr_version": DEEPSEEK_OCR_VERSION,
                                                **overrides}, ocr)


def _one_review(parsed):
    unit, = parsed["documents"][0]["units"]
    record, = [row for row in unit["coverage"]["transformations"] if row["kind"] == "pdf_region_source_review"]
    return unit, record


def test_certified_review_is_independent_of_ocr_and_preserves_native_and_neighbor_regions(original, history):
    ctx, seen = ReviewContext(), []

    def recorded(request):
        seen.append(request["locator"])
        return echo_ocr(request)

    parsed = read(ctx, original, recorded)
    unit, proof = _one_review(parsed)
    assert parsed["coverage"]["publication_status"] == "diagnostic_only"
    assert parsed["coverage"]["pdf_region_source_review"]["confirmed_count"] == 1
    assert unit["coverage"]["origin"] == "mixed" and unit["coverage"]["status"] == "read"
    assert unit["coverage"]["unread_objects"] == []
    assert [block["text"] for block in unit["blocks"] if block["kind"] == "paragraph"] == [
        "Preserved heading", "Open the portal", "Scanned requirement.", "Native footer"]
    rec, = [block for block in unit["blocks"] if block["origin"] == "source_review"]
    assert rec["locator"] == proof["entries"][0]["locator"]
    assert proof["entries"][0]["role"] == "instruction" and proof["unknown"] == original[1]["unknown"]
    assert proof["reviewed_image"]["sha256"] == original[1]["image_sha256"]
    assert proof["historical_ocr"]["historical_error_code"] == "OCR_IMAGE_REGION_REVIEW_REQUIRED"
    assert proof["historical_ocr"]["historical_billing"]["provider_returned_pages"] == 1
    assert [(block["locator"], block["origin"]) for block in unit["blocks"] if block["kind"] == "image"] == [
        ("pdf/page[1]/image[0]", "native"), ("pdf/page[1]/image[1]", "native")]
    assert seen == ["pdf/page[1]/image[1]"]
    assert len(history) == 1 and history[0][1]["bbox"] == original[1]["bbox"]
    assert read(ctx, original, recorded) == parsed
    assert len(history) == 2 and seen == ["pdf/page[1]/image[1]"]


def test_critical_unknown_preserves_both_confirmed_text_and_failed_read(original, history):
    original[1]["unknown"][0]["critical"] = True
    with pytest.raises(ReadingFailure, match="READING_SOURCE_REVIEW_CRITICAL_UNREAD") as error:
        read(ReviewContext(), original)
    unit, record = _one_review(error.value.result)
    assert not error.value.result["coverage"]["required_complete"]
    assert unit["coverage"]["unread_objects"] == [original[1]["locator"]]
    assert record["status"] == "unresolved" and record["unknown"] == original[1]["unknown"]
    assert [block["text"] for block in unit["blocks"] if block["origin"] == "source_review"] == ["Open the portal"]


@pytest.mark.parametrize("change,code", [
    ({"image_sha256": "4" * 64}, "READING_SOURCE_REVIEW_RASTER_MISMATCH"),
    ({"width_px": 300}, "READING_SOURCE_REVIEW_RASTER_MISMATCH"),
    ({"bbox": [20.5, 50, 170, 200]}, "READING_SOURCE_REVIEW_RENDER_INVALID"),
    ({"locator": "pdf/page[1]/image[9]"}, "READING_SOURCE_REVIEW_OBJECT_MISMATCH"),
])
def test_modified_raster_or_locator_cannot_resolve_a_screenshot(original, history, change, code):
    original[1].update(change)
    with pytest.raises(ReadingFailure, match=code) as error:
        read(ReviewContext(), original)
    unit, record = _one_review(error.value.result)
    assert not history and record["status"] == "failed"
    assert original[1]["locator"] in unit["coverage"]["unread_objects"]
    assert all(block["origin"] != "source_review" for block in unit["blocks"])


def test_known_success_correction_is_kept_as_source_review_only(original, history):
    correction = {"ocr_text": "p0rtal", "source_text": "portal", "pixel_bbox": [75, 21, 109, 34]}
    original[1]["corrections"] = [correction]
    _, record = _one_review(read(ReviewContext(), original))
    assert record["corrections"] == history[0][1]["corrections"] == [correction]
    assert record["historical_ocr"]["historical_status"] == "succeeded"
    assert not record["missing_historical_witnesses"]


def test_historical_witnesses_do_not_bridge_boxes_or_reuse_one_occurrence(original, monkeypatch):
    monkeypatch.setattr(reading_review_origin, "verify_review_origin", lambda *args, **kw: {
        "historical_required_text": ["Open the portal", "Open", "Open"]})
    original[1]["entries"] = [{"text": "Open the", "pixel_bbox": [20, 20, 60, 35], "role": "instruction"},
                              {"text": "portal", "pixel_bbox": [61, 20, 110, 35], "role": "interface"}]
    with pytest.raises(ReadingFailure, match="READING_SOURCE_REVIEW_WITNESS_MISSING") as error:
        read(ReviewContext(), original)
    _, record = _one_review(error.value.result)
    assert record["missing_historical_witnesses"] == ["Open the portal", "Open"]


def test_tampered_page_checkpoint_cannot_change_reviewed_text(original, history):
    ctx = ReviewContext()
    read(ctx, original)
    old = ctx.outputs[original[2]][1]
    block, = [entry for entry in old["blocks"] if entry["origin"] == "source_review"]
    block["text"] = "Modified checkpoint text"
    with pytest.raises(ReadingFailure, match="READING_SOURCE_REVIEW_CHECKPOINT_INVALID"):
        read(ctx, original)
    assert len(history) == 2


def test_tampered_raster_checkpoint_or_departed_origin_invalidates_page_cache(original, history, monkeypatch):
    ctx = ReviewContext()
    read(ctx, original)
    previous = deepcopy(ctx.images)
    ctx.images[original[1]["image_sha256"]] = b"tampered"
    with pytest.raises(ReadingFailure, match="READING_SOURCE_REVIEW_CHECKPOINT_INVALID"):
        read(ctx, original)
    ctx.images = previous

    def reject(*args, **kwargs):
        raise ProviderFailure("OCR_REPLAY_SOURCE_INVALID")

    monkeypatch.setattr(reading_review_origin, "verify_review_origin", reject)
    with pytest.raises(ReadingFailure, match="READING_SOURCE_REVIEW_CHECKPOINT_INVALID"):
        read(ctx, original)


@pytest.mark.parametrize("settings", [
    {"run_purpose": "standard"},
    {"debug_capture": False},
    {"ocr": {"engine": "deepseek_ocr", "live_targets": [], "recorded_targets": []}},
    {"ocr": {"engine": "deepseek_ocr"}},
])
def test_review_scope_rejects_paid_or_unsealed_execution(original, history, settings):
    ctx = ReviewContext()
    ctx.execution_config.update(settings)
    with pytest.raises(ReadingFailure, match="READING_SOURCE_REVIEW_DIAGNOSTIC_SCOPE_REQUIRED"):
        read(ctx, original)
    assert not history and not ctx.outputs


def test_review_cannot_be_obscured_by_scoped_ignore(original, history):
    document, review, _ = original
    key = f"{document['sha256']}::{document['source_id']}::{review['locator']}"
    with pytest.raises(ReadingFailure, match="READING_INPUT_INVALID"):
        read(ReviewContext(), original, ignored_objects={key: "synthetic bypass"})
    assert not history


def test_same_box_conflicting_text_and_out_of_bounds_box_are_invalid(original, history):
    review = original[1]
    review["entries"].append({**review["entries"][0], "text": "Conflicting exact position"})
    with pytest.raises(ReadingFailure, match="READING_INPUT_INVALID"):
        read(ReviewContext(), original)
    review["entries"].pop()
    review["unknown"][0]["pixel_bbox"][2] = review["width_px"] + 1
    with pytest.raises(ReadingFailure, match="READING_INPUT_INVALID"):
        read(ReviewContext(), original)
    assert not history
