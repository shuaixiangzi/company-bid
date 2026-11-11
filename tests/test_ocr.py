"""Real local rendering and the production gateway, with offline HTTP stubs."""
import base64
import hashlib
import io
import json
import zipfile

from docx import Document
from PIL import Image
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from company_bid.diagnostics import inspect_diagnostics
from company_bid.models import file_sha256
from company_bid.ocr import OCR_VERSION, PROMPT_VERSION, create_ocr, renderer_version
from company_bid.provider import ChatConfig, ChatProposalProvider, ProviderFailure
from company_bid.reading import read_documents
from company_bid.runtime import Executor, NodeSpec


def provider_options(**changes):
    return {"mode": "chat_completions", "endpoint": "https://example.invalid/chat", "model": "synthetic-vision",
            "auth_env": "TEST_OCR_KEY", "max_attempts": 1, "max_run_calls": 10,
            "max_run_usd": 10, **changes}


def pdf(path, *, pages=1, image=False):
    writer = PdfWriter()
    for index in range(pages):
        page = writer.add_blank_page(width=300, height=220)
        if image:
            content = DecodedStreamObject()
            content.set_data(bytes([index * 50, 120, 180]) * 32 * 32)
            content.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
                            NameObject("/Width"): NumberObject(32), NameObject("/Height"): NumberObject(32),
                            NameObject("/ColorSpace"): NameObject("/DeviceRGB"), NameObject("/BitsPerComponent"): NumberObject(8)})
            page[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"): DictionaryObject({NameObject("/Im1"): writer._add_object(content)})})
            command = b"q 150 0 0 150 20 20 cm /Im1 Do Q"
        else:
            command = f"q {index % 2} 0 0 rg 0 0 300 220 re f Q".encode()
        stream = DecodedStreamObject(); stream.set_data(command)
        page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(path)
    return path


def source(path, source_id="S"):
    return {"source_id": source_id, "path": str(path), "sha256": file_sha256(path), "role": "tender", "title": "Synthetic"}


def request(path, page=1, **changes):
    sid = "S"
    return {"source_id": sid, "path": str(path), "sha256": file_sha256(path),
            "unit_id": "N1-" + hashlib.sha256(sid.encode()).hexdigest()[:16] + f"-pdf-p{page:05d}",
            "locator": f"pdf/page[{page}]/vector-content", "page": page,
            "ocr_version": OCR_VERSION, "object_kind": "pdf_page", **changes}


def wire(payload, blocks=None):
    return {**payload["identity"], "source_sha256": payload["source_sha256"], "image_sha256": payload["image_sha256"],
            "status": "read", "blocks": blocks if blocks is not None else [{"kind": "paragraph", "text": "OCR-SOURCE-CANARY"}]}


def patch_transport(monkeypatch, calls, make=None):
    monkeypatch.setenv("TEST_OCR_KEY", "ocr-active-credential-canary")
    original = ChatProposalProvider.__init__
    def transport(endpoint, payload, secret, timeout):
        assert endpoint == "https://example.invalid/chat"
        content = payload["messages"][1]["content"]
        center_pixel = None
        if isinstance(content, list):
            candidate = json.loads(content[0]["text"])
            data_url = content[1]["image_url"]["url"]
            assert data_url.startswith("data:image/png;base64,")
            data = base64.b64decode(data_url.split(",", 1)[1])
            assert hashlib.sha256(data).hexdigest() == candidate["image_sha256"]
            with Image.open(io.BytesIO(data)) as image:
                assert image.size == (candidate["renderer"]["width"], candidate["renderer"]["height"])
                center_pixel = image.getpixel((image.width // 2, image.height // 2))
        else:
            candidate = json.loads(content)
        calls.append({"request": candidate, "payload": payload, "center_pixel": center_pixel})
        answer = make(candidate, len(calls)) if make else wire(candidate)
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer)}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}
    monkeypatch.setattr(ChatProposalProvider, "__init__", lambda self, config, **kwargs:
                        original(self, config, transport=transport, sleep=lambda _: None))


def execute_callback(tmp_path, req, *, provider=None, ocr_options=None):
    common = provider or provider_options()
    options = {"provider": common, **(ocr_options or {})}
    def run(ctx, value):
        callback = create_ocr(ctx, options)
        return ctx.run_unit(value["unit_id"], value, lambda: callback(value))
    executor = Executor(tmp_path / "runtime", [NodeSpec("N1", "ocr-test-v1", run)], {"provider": common, "debug_capture": True})
    rid = executor.create_run(req)
    return executor, rid, executor.execute(rid)


def test_pdf_exact_page_render_and_shared_gateway_private_evidence(tmp_path, monkeypatch):
    path = pdf(tmp_path / "source.pdf", pages=2)
    original_hash = file_sha256(path)
    calls = []; patch_transport(monkeypatch, calls)
    executor, rid, state = execute_callback(tmp_path, request(path, page=2), ocr_options={"model": "explicit-vision-model"})
    assert state["status"] == "succeeded", state.get("error_code")
    assert len(calls) == 1 and calls[0]["request"]["identity"]["page"] == 2
    assert calls[0]["payload"]["model"] == "explicit-vision-model"
    assert calls[0]["request"]["renderer"]["render_dpi"] == 160
    assert calls[0]["request"]["renderer"]["region"] is None
    assert calls[0]["center_pixel"] == (255, 0, 0)  # page 1 is black; page 2 is red
    assert calls[0]["request"]["renderer"]["renderer_version"] == renderer_version()
    assert executor.output(rid)["blocks"][0]["text"] == "OCR-SOURCE-CANARY"
    assert file_sha256(path) == original_hash
    report = inspect_diagnostics(executor.store.root, rid)
    assert report["actual_call_attempts"] == 1 and report["evidence_status"] == "available"
    assert report["calls"][0]["prompt_version"] == PROMPT_VERSION
    run_dir = executor.store.run_dir(rid)
    for name in ("events.jsonl", "spans.jsonl"):
        text = (run_dir / name).read_text(encoding="utf-8")
        assert "OCR-SOURCE-CANARY" not in text and "ocr-active-credential-canary" not in text
        assert "data:image" not in text and str(path) not in text
    private = [p.read_bytes() for p in run_dir.rglob("*") if p.is_file()]
    assert any(b"OCR-SOURCE-CANARY" in data for data in private)
    assert any(b"data:image/png;base64" in data for data in private)
    assert not any(b"ocr-active-credential-canary" in data for data in private)


def test_reader_pdf_region_uses_exact_original_image_bbox(tmp_path, monkeypatch):
    path = pdf(tmp_path / "image.pdf", image=True)
    calls = []; patch_transport(monkeypatch, calls)
    def run(ctx, value):
        return read_documents(ctx, [source(path)], {"ocr_version": OCR_VERSION}, create_ocr(ctx, {"provider": provider_options()}))
    executor = Executor(tmp_path / "runtime", [NodeSpec("N1", "reader-ocr-test", run)], {"debug_capture": True})
    rid = executor.create_run({})
    state = executor.execute(rid)
    assert state["status"] == "succeeded", state.get("error_code")
    assert len(calls) == 1
    detail = calls[0]["request"]["renderer"]
    assert detail["region"]["bbox"] == [20.0, 50.0, 170.0, 200.0]
    assert detail["width"] < detail["full_page_pixels"][0]
    assert calls[0]["center_pixel"] == (0, 120, 180)
    assert executor.output(rid)["coverage"]["required_complete"] is True


@pytest.mark.parametrize("location", ["body", "table", "header"])
def test_reader_docx_exact_embedded_image_identity(tmp_path, monkeypatch, location):
    image_path = tmp_path / "embedded.png"
    Image.new("RGB", (180, 120), "white").save(image_path)
    document = Document()
    if location == "body":
        paragraph = document.add_paragraph()
    elif location == "table":
        paragraph = document.add_table(rows=1, cols=1).cell(0, 0).paragraphs[0]
    else:
        paragraph = document.sections[0].header.paragraphs[0]
    paragraph.add_run().add_picture(str(image_path))
    path = tmp_path / "image.docx"; document.save(path)
    original_hash = file_sha256(path)
    calls = []; patch_transport(monkeypatch, calls)
    def run(ctx, value):
        return read_documents(ctx, [source(path)], {"ocr_version": OCR_VERSION}, create_ocr(ctx, {"provider": provider_options()}))
    executor = Executor(tmp_path / "runtime", [NodeSpec("N1", "docx-ocr-test", run)], {"debug_capture": True})
    rid = executor.create_run({}); state = executor.execute(rid)
    assert state["status"] == "succeeded", state.get("error_code")
    assert len(calls) == 1 and calls[0]["request"]["identity"]["page"] is None
    assert calls[0]["request"]["renderer"]["embedded_image_sha256"] == file_sha256(image_path)
    assert calls[0]["request"]["renderer"]["package_part"] == "word/media/image1.png"
    assert calls[0]["center_pixel"] == (255, 255, 255)
    assert file_sha256(path) == original_hash


def test_successful_reader_page_reused_after_later_ocr_validation_failure(tmp_path, monkeypatch):
    path = pdf(tmp_path / "pages.pdf", pages=2)
    calls = []
    def make(payload, number):
        result = wire(payload)
        if number == 2:
            result["page"] = 99
        return result
    patch_transport(monkeypatch, calls, make)
    def run(ctx, value):
        return read_documents(ctx, [source(path)], {"ocr_version": OCR_VERSION}, create_ocr(ctx, {"provider": provider_options(max_run_calls=3)}))
    executor = Executor(tmp_path / "runtime", [NodeSpec("N1", "reader-ocr-test", run)], {"debug_capture": True})
    rid = executor.create_run({})
    assert executor.execute(rid)["status"] == "failed"
    before = inspect_diagnostics(executor.store.root, rid)
    assert before["calls"][1]["error_code"] == "OCR_RESPONSE_IDENTITY_INVALID"
    assert executor.resume(rid)["status"] == "succeeded"
    assert [c["request"]["identity"]["page"] for c in calls] == [1, 2, 2]
    assert inspect_diagnostics(executor.store.root, rid)["actual_call_attempts"] == 3


@pytest.mark.parametrize("change,code", [
    ({"sha256": "0" * 64}, "OCR_SOURCE_HASH_MISMATCH"),
    ({"page": 2}, "OCR_PAGE_IDENTITY_INVALID"),
    ({"unit_id": "wrong"}, "OCR_UNIT_IDENTITY_INVALID"),
    ({"ocr_version": "unconfigured"}, "OCR_VERSION_MISMATCH"),
    ({"object_kind": "docx_image", "page": None, "locator": "word/document.xml/body/block[0]/image[0]"}, "OCR_SOURCE_FORMAT_INVALID"),
    ({"path": "https://example.invalid/image.pdf"}, "OCR_REMOTE_SOURCE_FORBIDDEN"),
    ({"object_kind": "pdf_region", "locator": "pdf/page[1]/image[0]", "bbox": [-1.0, 0.0, 100.0, 20.0]}, "OCR_REGION_IDENTITY_INVALID"),
])
def test_request_source_page_and_geometry_fail_before_transport(tmp_path, monkeypatch, change, code):
    path = pdf(tmp_path / "source.pdf", image=True)
    calls = []; patch_transport(monkeypatch, calls)
    _, _, state = execute_callback(tmp_path, {**request(path), **change})
    assert state["error_code"] == code
    assert not calls


@pytest.mark.parametrize("options,code", [
    ({"max_image_pixels": 100}, "OCR_IMAGE_DIMENSION_LIMIT"),
    ({"max_image_edge": 100}, "OCR_IMAGE_DIMENSION_LIMIT"),
    ({"max_image_bytes": 1}, "OCR_IMAGE_BYTE_LIMIT"),
    ({"max_source_bytes": 1}, "OCR_SOURCE_LIMIT_OR_MISSING"),
])
def test_limits_never_silently_reduce_resolution(tmp_path, monkeypatch, options, code):
    path = pdf(tmp_path / "source.pdf")
    calls = []; patch_transport(monkeypatch, calls)
    _, _, state = execute_callback(tmp_path, request(path), ocr_options=options)
    assert state["error_code"] == code and not calls


@pytest.mark.parametrize("mutate,code", [
    (lambda r: r.update(page=2), "OCR_RESPONSE_IDENTITY_INVALID"),
    (lambda r: r.update(source_sha256="0" * 64), "OCR_RESPONSE_HASH_MISMATCH"),
    (lambda r: r.update(image_sha256="0" * 64), "OCR_RESPONSE_HASH_MISMATCH"),
    (lambda r: r.update(status="unreadable", blocks=[]), "OCR_CONTENT_UNREADABLE"),
    (lambda r: r.update(blocks=[]), "OCR_EMPTY_OR_OVERSIZED_RESULT"),
    (lambda r: r.update(blocks=[{"kind": "paragraph", "text": "  "}]), "OCR_EMPTY_RESULT"),
    (lambda r: r.update(blocks=[{"kind": "paragraph", "text": "unresolved \ufffd"}]), "OCR_CONTENT_UNREADABLE"),
    (lambda r: r.update(blocks=[{"kind": "paragraph", "text": "X", "source_id": "invented"}]), "OCR_RESPONSE_SCHEMA_INVALID"),
    (lambda r: r.update(blocks=[{"kind": "table", "rows": [["A", "B"], ["C"]], "column_widths": [1.0, 1.0]}]), "OCR_RESPONSE_SCHEMA_INVALID"),
    (lambda r: r.update(blocks=[{"kind": "table", "rows": [["A", "B"]], "column_widths": [1.0, 1.0], "merges": [{"row": 0, "column": 0, "col_span": 2}]}]), "OCR_RESPONSE_SCHEMA_INVALID"),
])
def test_bad_model_result_is_a_failed_observed_call(tmp_path, monkeypatch, mutate, code):
    path = pdf(tmp_path / "source.pdf")
    calls = []
    def make(payload, number):
        result = wire(payload); mutate(result); return result
    patch_transport(monkeypatch, calls, make)
    executor, rid, state = execute_callback(tmp_path, request(path))
    assert state["error_code"] == code and len(calls) == 1
    report = inspect_diagnostics(executor.store.root, rid)
    assert report["calls"][0]["outcome"] == "failed"
    assert report["calls"][0]["error_code"] == code


def test_structured_merged_table_keeps_original_header_and_blank_continuation(tmp_path, monkeypatch):
    path = pdf(tmp_path / "source.pdf")
    table = {"kind": "table", "rows": [["Wide", ""], ["Label", "Value"]],
             "column_widths": [2.0, 3.0], "merges": [{"row": 0, "column": 0, "col_span": 2}], "header_rows": [0]}
    calls = []; patch_transport(monkeypatch, calls, lambda p, n: wire(p, [table]))
    executor, rid, state = execute_callback(tmp_path, request(path))
    assert state["status"] == "succeeded", state.get("error_code")
    block = executor.output(rid)["blocks"][0]
    assert block["rows"] == table["rows"] and block["header_rows"] == [0]
    assert block["merges"] == [{"row": 0, "column": 0, "row_span": 1, "col_span": 2}]


def test_ocr_and_text_calls_share_one_run_budget(tmp_path, monkeypatch):
    path = pdf(tmp_path / "source.pdf")
    calls = []; patch_transport(monkeypatch, calls)
    common = provider_options(max_run_calls=1)
    def run(ctx, value):
        result = create_ocr(ctx, {"provider": common})(request(path))
        provider = ChatProposalProvider(ChatConfig(**{k: v for k, v in common.items() if k != "mode"}))
        provider.generate_json(ctx, {}, unit_id="later", prompt_version="text-v1", system_prompt="test",
                               task_type="extract_requirements", validate=lambda value: value)
        return result
    executor = Executor(tmp_path / "runtime", [NodeSpec("N1", "budget-test", run)], {"provider": common, "debug_capture": True})
    rid = executor.create_run({}); state = executor.execute(rid)
    assert state["error_code"] == "PROVIDER_RUN_BUDGET_EXCEEDED"
    assert len(calls) == 1 and state["call_budget"]["reservation_count"] == 1


def docx_request(path, image_path):
    return {"source_id": "S", "path": str(path), "sha256": file_sha256(path),
            "unit_id": "N1-" + hashlib.sha256(b"S").hexdigest()[:16] + "-docx-body-b00000",
            "locator": "word/document.xml/body/block[0]/image[0]", "page": None, "ocr_version": OCR_VERSION,
            "object_kind": "docx_image", "package_part": "word/media/image1.png", "image_sha256": file_sha256(image_path)}


@pytest.mark.parametrize("change,code", [
    ({"image_sha256": "0" * 64}, "OCR_DOCX_IMAGE_HASH_MISMATCH"),
    ({"package_part": "word/media/image2.png"}, "OCR_DOCX_IMAGE_IDENTITY_INVALID"),
    ({"package_part": "../image.png"}, "OCR_DOCX_PART_INVALID"),
    ({"locator": "word/document.xml/body/block[0]/image[9]"}, "OCR_DOCX_LOCATOR_INVALID"),
    ({"unit_id": "wrong-object"}, "OCR_UNIT_IDENTITY_INVALID"),
    ({"page": 1}, "OCR_DOCX_IDENTITY_INVALID"),
])
def test_docx_wrong_part_hash_locator_or_page_never_calls_provider(tmp_path, monkeypatch, change, code):
    image = tmp_path / "object.png"; Image.new("RGB", (80, 60), "white").save(image)
    document = Document(); document.add_paragraph().add_run().add_picture(str(image))
    path = tmp_path / "object.docx"; document.save(path)
    calls = []; patch_transport(monkeypatch, calls)
    _, _, state = execute_callback(tmp_path, {**docx_request(path, image), **change})
    assert state["error_code"] == code and not calls


def test_external_docx_image_relationship_is_not_fetched(tmp_path, monkeypatch):
    image = tmp_path / "object.png"; Image.new("RGB", (80, 60), "white").save(image)
    document = Document(); document.add_paragraph().add_run().add_picture(str(image))
    raw = tmp_path / "original.docx"; document.save(raw)
    path = tmp_path / "external.docx"
    from docx.oxml import parse_xml
    from lxml import etree
    with zipfile.ZipFile(raw) as original, zipfile.ZipFile(path, "w") as changed:
        for name in original.namelist():
            content = original.read(name)
            if name == "word/_rels/document.xml.rels":
                root = parse_xml(content)
                rel = next(child for child in root if child.get("Type", "").endswith("/image"))
                rel.set("TargetMode", "External"); rel.set("Target", "https://example.invalid/private.png")
                content = etree.tostring(root)
            changed.writestr(name, content)
    calls = []; patch_transport(monkeypatch, calls)
    _, _, state = execute_callback(tmp_path, docx_request(path, image))
    assert state["error_code"] == "OCR_DOCX_RELATIONSHIP_INVALID" and not calls


def test_factory_cannot_override_the_shared_budget_before_first_call(tmp_path, monkeypatch):
    path = pdf(tmp_path / "source.pdf")
    calls = []; patch_transport(monkeypatch, calls)
    common = provider_options(max_run_calls=2)
    def run(ctx, value):
        return create_ocr(ctx, {"provider": provider_options(max_run_calls=10)})(request(path))
    executor = Executor(tmp_path / "runtime", [NodeSpec("N1", "budget-guard", run)], {"provider": common})
    rid = executor.create_run({}); state = executor.execute(rid)
    assert state["error_code"] == "OCR_SHARED_BUDGET_MISMATCH" and not calls


def test_password_protected_original_is_rejected_even_with_empty_user_password(tmp_path, monkeypatch):
    path = tmp_path / "encrypted.pdf"
    writer = PdfWriter(); writer.add_blank_page(width=300, height=220)
    writer.encrypt(user_password="", owner_password="synthetic-owner-password"); writer.write(path)
    calls = []; patch_transport(monkeypatch, calls)
    _, _, state = execute_callback(tmp_path, request(path))
    assert state["error_code"] == "OCR_SOURCE_PASSWORD_PROTECTED" and not calls


def test_corrupt_pdf_fails_locally_without_transport(tmp_path, monkeypatch):
    path = tmp_path / "broken.pdf"; path.write_bytes(b"%PDF-1.7\nnot-a-valid-pdf")
    calls = []; patch_transport(monkeypatch, calls)
    _, _, state = execute_callback(tmp_path, request(path))
    assert state["error_code"] == "OCR_LOCAL_CONVERSION_FAILED" and not calls
