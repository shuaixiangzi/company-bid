import io
import json

from docx import Document
from PIL import Image
import pytest

from company_bid import material_deepseek_ocr as official
from company_bid.diagnostics import inspect_diagnostics
from company_bid.ocr import (DEEPSEEK_OCR_VERSION, RasterOptions, _Request, _prepare_image, create_ocr,
                             renderer_version)
from company_bid.reading import read_documents
from company_bid.runtime import Executor, NodeSpec, call_budget_summary
from company_bid.storage import json_bytes
from test_ocr import pdf, request, source


KEY = "test-official-credential-32791584"


def options(**overrides):
    value = {"engine": "deepseek_ocr", "max_run_usd": "0.009", "max_run_calls": 6,
             "reserve_usd_per_page": "0.0015", "expected_renderer_version": renderer_version(), **overrides}
    value["expected_engine_fingerprint"] = official.engine_fingerprint({"engine": "deepseek_ocr"})
    return value


def region(text, kind="text", box=(10, 20, 900, 950)):
    return f"<｜ref｜>{kind}<｜/ref｜><｜box｜>{json.dumps([box])}<｜/box｜>\n{text}\n"


def payload(text, finish_reason="stop"):
    return {"id": "ocr-tender-synthetic", "object": "ocr.result", "created": 1794816000,
            "purpose": "rich_layout_recognition", "page_count": 1,
            "pages": [{"index": 0, "text": text, "finish_reason": finish_reason}],
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}}


def stub(monkeypatch, raw):
    calls = []
    monkeypatch.setenv("COMPANY_BID_DEEPSEEK_API_KEY", KEY)

    def transport(body, media, secret, timeout):
        assert secret == KEY
        assert KEY.encode() not in body
        calls.append(body)
        if isinstance(raw, Exception):
            raise raw
        return 200, {}, raw

    monkeypatch.setattr(official, "_transport", transport)
    return calls


def executor(tmp_path, path, config):
    def read(ctx, value):
        # This is also how chain.py forwards its inherited chat settings. The
        # official adapter must use the frozen OCR options from this context.
        ocr = create_ocr(ctx, {**config, "provider": {"model": "unused"}})
        return read_documents(ctx, [source(path)], {"ocr_version": DEEPSEEK_OCR_VERSION}, ocr)

    return Executor(tmp_path / "runtime", [NodeSpec("N1", "tender-ocr-test-v1", read)],
                    {"pipeline": "automatic", "ocr": config, "debug_capture": True})


def target(path, *, page=1, **extra):
    value = request(path, page=page, ocr_version=DEEPSEEK_OCR_VERSION)
    _, raster = _prepare_image(_Request.model_validate(value), path.read_bytes(), RasterOptions())
    return {"source_sha256": value["sha256"], "locator": value["locator"], "page": page,
            "object_kind": "pdf_page", "rendered_sha256": raster["image_sha256"], **extra}


def rendered_image(body):
    png = body.split(b"Content-Type: image/png\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
    with Image.open(io.BytesIO(png)) as image:
        return image.size, image.getpixel((image.width // 2, image.height // 2))


def test_automatic_pdf_official_provider_checkpoint_and_private_witness(monkeypatch, tmp_path):
    path = pdf(tmp_path / "scan.pdf")
    recognized = "原件 核查词１０号。 核查词１０号。 勾选：□"
    calls = stub(monkeypatch, json_bytes(payload(region(recognized))))
    config = options(live_targets=[target(path, required_text=["核查词10号", "核查词10号"])])
    ex = executor(tmp_path, path, config)
    run = ex.create_run({})
    state = ex.execute(run)
    assert state["status"] == "succeeded", state.get("error_code")
    assert len(calls) == 1 and rendered_image(calls[0])[1] == (0, 0, 0)
    assert "核查词10号".encode() not in calls[0]
    parsed = ex.output(run)
    assert parsed["coverage"]["required_complete"]
    assert parsed["documents"][0]["units"][0]["blocks"][0]["text"] == recognized
    assert call_budget_summary(state["call_budget"])["effective_budget_charge_usd"] == "0.0015"
    assert ex.resume(run)["status"] == "succeeded" and len(calls) == 1
    diagnostic = inspect_diagnostics(ex.store.root, run)
    assert diagnostic["actual_call_attempts"] == 1 and not diagnostic["gaps"]
    runtime_dir = ex.store.run_dir(run)
    events = (runtime_dir / "events.jsonl").read_text(encoding="utf-8")
    assert any(row.get("operation") == "tender_ocr" and row["event"] == "call.started"
               for row in map(json.loads, events.splitlines()))
    assert recognized not in events and KEY not in events


@pytest.mark.parametrize("kind", ["pdf_region", "docx_image"])
def test_automatic_reads_exact_embedded_objects(monkeypatch, tmp_path, kind):
    if kind == "pdf_region":
        path = pdf(tmp_path / "region.pdf", image=True)
        expected_pixel = (0, 120, 180)
    else:
        image_path = tmp_path / "embedded.png"
        Image.new("RGB", (128, 64), (50, 65, 90)).save(image_path)
        document = Document()
        document.add_paragraph().add_run().add_picture(str(image_path))
        path = tmp_path / "original.docx"
        document.save(path)
        expected_pixel = (50, 65, 90)
    calls = stub(monkeypatch, json_bytes(payload(region("招标截图文字"))))
    ex = executor(tmp_path, path, options())
    run = ex.create_run({})
    state = ex.execute(run)
    assert state["status"] == "succeeded", state.get("error_code")
    assert len(calls) == 1 and rendered_image(calls[0])[1] == expected_pixel
    captures = state["nodes"][0]["attempts"][0]["captures"]
    prep = next(ex.store.read_json(c["ref"]) for c in captures if c["name"] == "ocr-preparation")
    assert prep["identity"]["object_kind"] == kind
    assert prep["identity"]["source_sha256"] == source(path)["sha256"]
    assert prep["image"]["sha256"] == prep["identity"]["image_sha256"]
    if kind == "pdf_region":
        assert prep["renderer"]["region"]["bbox"] == [20.0, 50.0, 170.0, 200.0]
    else:
        assert prep["identity"]["embedded_image_sha256"] == prep["renderer"]["embedded_image_sha256"]


@pytest.mark.parametrize(("text", "finish", "code"), [
    ("", "stop", "OCR_BLANK_UNCONFIRMED"),
    ("looks readable without a location", "stop", "OCR_LAYOUT_UNLOCATED"),
    (region("", "image"), "stop", "OCR_IMAGE_ONLY_UNCONFIRMED"),
    (region("禁止") + region("", "image", (10, 10, 20, 20)), "stop", "OCR_IMAGE_REGION_REVIEW_REQUIRED"),
    (region("<table><tr><td>报价</td></tr></table>", "table"), "stop", "OCR_TABLE_SOURCE_REVIEW_REQUIRED"),
    (region("<table><tr><td>报价</td>"), "stop", "OCR_MARKUP_INVALID"),
    (region("abc <｜ref"), "stop", "OCR_CONTENT_UNREADABLE"),
    (region("abc") * 2, "stop", "OCR_REPETITIVE_OUTPUT"),
    (region("abc"), "length", "OCR_OUTPUT_INCOMPLETE"),
])
def test_unread_content_preserves_raw_and_never_passes(monkeypatch, tmp_path, text, finish, code):
    path = pdf(tmp_path / "scan.pdf")
    raw = json_bytes(payload(text, finish))
    calls = stub(monkeypatch, raw)
    ex = executor(tmp_path, path, options())
    run = ex.create_run({})
    state = ex.execute(run)
    assert state["status"] == "failed" and state["error_code"] == code and len(calls) == 1
    captures = state["nodes"][0]["attempts"][0]["captures"]
    response = next(ex.store.read_json(c["ref"]) for c in captures if c["name"].startswith("response-"))
    assert ex.store.read_bytes(response["raw_response_ref"]) == raw


def test_witness_cannot_use_one_occurrence_for_two_required_results(monkeypatch, tmp_path):
    path = pdf(tmp_path / "scan.pdf")
    calls = stub(monkeypatch, json_bytes(payload(region("结果名称"))))
    ex = executor(tmp_path, path, options(live_targets=[target(path, required_text=["结果名称", "结果名称"])]))
    run = ex.create_run({})
    state = ex.execute(run)
    assert state["status"] == "failed" and state["error_code"] == "OCR_REVIEWED_TEXT_MISSING" and len(calls) == 1
    captures = state["nodes"][0]["attempts"][0]["captures"]
    validation = next(ex.store.read_json(c["ref"]) for c in captures if c["name"].startswith("validation-"))
    missing = validation["details"]["missing_witnesses"]
    assert len(missing) == 1 and missing[0]["expected_count"] == 2 and missing[0]["observed_count"] == 1


def test_stale_rendered_hash_and_legacy_route_fail_without_transport(monkeypatch, tmp_path):
    path = pdf(tmp_path / "scan.pdf")
    calls = stub(monkeypatch, json_bytes(payload(region("unused"))))
    ex = executor(tmp_path, path, options(live_targets=[target(path, rendered_sha256="0" * 64)]))
    state = ex.execute(ex.create_run({}))
    assert state["status"] == "failed" and state["error_code"] == "OCR_TARGET_RENDERED_HASH_MISMATCH"
    legacy = executor(tmp_path, path, {"provider": {"model": "legacy"}})
    legacy_state = legacy.execute(legacy.create_run({}))
    assert legacy_state["status"] == "failed" and legacy_state["error_code"] == "OCR_LEGACY_PROVIDER_DISABLED"
    assert calls == []


def test_unknown_outcome_blocks_later_dispatch_and_resume(monkeypatch, tmp_path):
    path = pdf(tmp_path / "scan.pdf", pages=2)
    calls = stub(monkeypatch, official.MaterialDeepSeekOCRFailure("MATERIAL_OCR_TIMEOUT_OUTCOME_UNKNOWN"))
    ex = executor(tmp_path, path, options())
    run = ex.create_run({})
    first = ex.execute(run)
    assert first["status"] == "failed" and first["error_code"] == "OCR_TIMEOUT_OUTCOME_UNKNOWN"
    assert len(calls) == 1
    again = ex.resume(run)
    assert again["status"] == "failed" and again["error_code"] == "OCR_PURCHASE_ALREADY_RESERVED"
    assert len(calls) == call_budget_summary(again["call_budget"])["reservation_count"] == 1


def test_succeeded_target_is_reused_when_sibling_fails(monkeypatch, tmp_path):
    path = pdf(tmp_path / "two.pdf", pages=2)
    monkeypatch.setenv("COMPANY_BID_DEEPSEEK_API_KEY", KEY)
    calls = []

    def transport(body, media, secret, timeout):
        calls.append(body)
        if len(calls) == 2:
            raise official.MaterialDeepSeekOCRFailure("MATERIAL_OCR_TIMEOUT_OUTCOME_UNKNOWN")
        return 200, {}, json_bytes(payload(region("first target")))

    monkeypatch.setattr(official, "_transport", transport)
    config = options()

    def read(ctx, _):
        ocr = create_ocr(ctx, config)

        def read_both():
            first = ocr(request(path, page=1, ocr_version=DEEPSEEK_OCR_VERSION))
            assert first["blocks"][0]["text"] == "first target"
            return ocr(request(path, page=2, ocr_version=DEEPSEEK_OCR_VERSION))

        return ctx.run_unit("containing-unit", {}, read_both)

    ex = Executor(tmp_path / "runtime", [NodeSpec("N1", "tender-ocr-test-v1", read)],
                  {"pipeline": "automatic", "ocr": config, "debug_capture": True})
    run = ex.create_run({})
    first = ex.execute(run)
    assert first["status"] == "failed" and first["error_code"] == "OCR_TIMEOUT_OUTCOME_UNKNOWN"
    before = next(unit for unit in first["nodes"][0]["units"].values() if unit["status"] == "succeeded")
    again = ex.resume(run)
    assert again["status"] == "failed" and again["error_code"] == "OCR_PURCHASE_ALREADY_RESERVED"
    assert again["nodes"][0]["units"][before["unit_id"]] == before
    assert len(calls) == call_budget_summary(again["call_budget"])["reservation_count"] == 2


def test_duplicate_json_keys_fail(monkeypatch, tmp_path):
    path = pdf(tmp_path / "scan.pdf")
    raw = json_bytes(payload(region("content"))).replace(b'"page_count":1', b'"page_count":1,"page_count":1')
    calls = stub(monkeypatch, raw)
    ex = executor(tmp_path, path, options())
    state = ex.execute(ex.create_run({}))
    assert state["status"] == "failed" and state["error_code"] == "OCR_RESPONSE_NOT_JSON" and len(calls) == 1
