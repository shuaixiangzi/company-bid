from types import SimpleNamespace

import pytest

from company_bid.provider import ProviderFailure
from company_bid.pipeline import PipelineFailure, build_executor
from company_bid.reading_review_origin import verify_review_origin
from company_bid.storage import json_bytes
from test_ocr import pdf
from test_tender_deepseek_ocr import options, payload, region, stub
from test_tender_ocr_replay import chain, execute, no_http, recorded_targets


@pytest.mark.parametrize("ocr", [{"engine": "deepseek_ocr", "live_targets": []},
                                  {"engine": "deepseek_ocr", "recorded_targets": [], "live_targets": None}])
def test_executor_rejects_live_settings_before_creating_review_run(tmp_path, ocr):
    with pytest.raises(PipelineFailure, match="READING_SOURCE_REVIEW_OFFLINE_REQUIRED"):
        build_executor(tmp_path, {"pipeline": "automatic", "run_purpose": "diagnostic_replay",
                       "reading": {"pdf_region_reviews": [{}]}, "ocr": ocr})


@pytest.mark.parametrize(("layout", "finish", "prior_error"), [
    (region("", "image"), "stop", "OCR_IMAGE_ONLY_UNCONFIRMED"),
    (region("片段"), "length", "MATERIAL_OCR_OUTPUT_INCOMPLETE"),
    (region("原来错词"), "stop", None),
])
def test_review_checks_archived_source_and_outcome(tmp_path, monkeypatch, layout, finish, prior_error):
    source = pdf(tmp_path / "original.pdf", image=True)
    sent = stub(monkeypatch, json_bytes(payload(layout, finish_reason=finish)))
    executor = chain(tmp_path / "runtime", source, options())
    rid, state = execute(executor)
    assert len(sent) == 1
    bound, = recorded_targets(executor, rid)
    origin = {k: bound["origin"][k] for k in ("run_id", "unit_id", "call_id", "raw_response_sha256")}
    args = dict(source_id="S", source_sha256=bound["source_sha256"], page=1, locator=bound["locator"],
                bbox=bound["bbox"], image_sha256=bound["rendered_sha256"])
    ctx = SimpleNamespace(node_key="N1", artifact_scope="diagnostic", _state={"capture_policy": True},
                          execution_config={"run_purpose": "diagnostic_replay"}, run_id="new", executor=executor)
    pending = no_http(monkeypatch)
    correction = {"pixel_bbox": [35, 50, 60, 72], "ocr_text": "错词", "source_text": "异词"}
    corrections = [] if prior_error else [correction]
    verified = verify_review_origin(ctx, origin, **args, corrections=corrections)
    assert verified["historical_error_code"] == prior_error and verified["finish_reason"] == finish
    assert verified["historical_status"] == ("failed" if prior_error else "succeeded")
    if not prior_error:
        assert verified["source_corrections"] == [{**correction, "origin_region_index": 0}]
        with pytest.raises(ProviderFailure, match="CORRECTION_TARGET_MISMATCH"):
            verify_review_origin(ctx, origin, **args, corrections=[{**correction, "ocr_text": "原响应以外的字"}])
    with pytest.raises(ProviderFailure):
        verify_review_origin(ctx, origin, **args, corrections=[correction] if prior_error else [])
    with pytest.raises(ProviderFailure):
        verify_review_origin(ctx, {**origin, "raw_response_sha256": "0" * 64}, **args, corrections=corrections)
    with pytest.raises(ProviderFailure, match="REGION_MISMATCH"):
        verify_review_origin(ctx, origin, **{**args, "locator": "pdf/page[2]/image[0]"}, corrections=corrections)
    assert not pending and executor.store.read_state(rid) == state
