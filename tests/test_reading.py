"""Native reading and coverage with synthetic documents; no real OCR/provider."""
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from PIL import Image
import pytest
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject, DecodedStreamObject, RectangleObject

from company_bid.reading import ReadingConfig, ReadingFailure, SourceDocument, _read_pdf_page, read_documents
from company_bid.runtime import Executor, NodeSpec


class Context:
    def __init__(self):
        self.outputs = {}
        self.calls = Counter()
        self.captures = []

    def run_unit(self, unit_id, inputs, fn):
        key = json.dumps(inputs, sort_keys=True)
        if unit_id in self.outputs:
            assert self.outputs[unit_id][0] == key
            return deepcopy(self.outputs[unit_id][1])
        self.calls[unit_id] += 1
        value = fn()
        self.outputs[unit_id] = (key, deepcopy(value))
        return value

    def capture(self, name, payload, **kwargs):
        self.captures.append((name, payload, kwargs))
        return {"status": "captured"}


def source(path, role="tender", source_id="SRC-1"):
    return {"source_id": source_id, "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "role": role, "title": "Synthetic input"}


def pdf(path, contents, password=None, *, separator_font=False, page_origin=(0, 0)):
    writer = PdfWriter()
    for content in contents:
        page = writer.add_blank_page(width=300, height=220)
        origin_x, origin_y = page_origin
        if page_origin != (0, 0):
            page.mediabox = RectangleObject((origin_x, origin_y, origin_x + 300, origin_y + 220))
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        if separator_font:
            # Different font bounds at the same text matrix baseline reproduce
            # the ownership conflict without changing the drawing coordinates.
            descriptor = DictionaryObject({NameObject("/Type"): NameObject("/FontDescriptor"),
                NameObject("/FontName"): NameObject("/SyntheticSeparator"), NameObject("/Descent"): NumberObject(-1000),
                NameObject("/FontBBox"): ArrayObject([NumberObject(x) for x in [0, -1000, 500, 0]])})
            font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/SyntheticSeparator"), NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
                NameObject("/FirstChar"): NumberObject(47), NameObject("/LastChar"): NumberObject(47),
                NameObject("/Widths"): ArrayObject([NumberObject(500)]), NameObject("/FontDescriptor"): writer._add_object(descriptor)})
            page["/Resources"]["/Font"][NameObject("/F2")] = writer._add_object(font)
        if content == "IMAGE":
            image = DecodedStreamObject()
            image.set_data(bytes([100, 140, 180]) * 64 * 64)
            image.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
                          NameObject("/Width"): NumberObject(64), NameObject("/Height"): NumberObject(64),
                          NameObject("/ColorSpace"): NameObject("/DeviceRGB"), NameObject("/BitsPerComponent"): NumberObject(8)})
            page["/Resources"][NameObject("/XObject")] = DictionaryObject({NameObject("/Im1"): writer._add_object(image)})
            command = "q 150 0 0 150 20 20 cm /Im1 Do Q"
        else:
            command = f"q 1 0 0 1 {origin_x} {origin_y} cm\n{content}\nQ" if page_origin != (0, 0) else content
        stream = DecodedStreamObject(); stream.set_data(command.encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    if password:
        writer.encrypt(password)
    writer.write(path)
    return path


def text(value, x=30, y=170):
    return f"BT /F1 11 Tf {x} {y} Td ({value}) Tj ET\n"


def echo_ocr(request):
    return {k: request[k] for k in ("source_id", "unit_id", "locator", "page")} | {"text": "Scanned requirement."}


def test_pdf_table_grid_and_native_text_are_not_counted_twice(tmp_path):
    commands = "30 70 m 270 70 l S\n30 110 m 270 110 l S\n30 150 m 270 150 l S\n30 70 m 30 150 l S\n150 70 m 150 150 l S\n270 70 m 270 150 l S\n"
    commands += text("Label", 40, 125) + text("Value", 160, 125) + text("Row", 40, 85) + text("", 160, 85) + text("Outside", 30, 190)
    path = pdf(tmp_path / "frozen-object", [commands])
    result = read_documents(Context(), [source(path)], {})
    unit = result["documents"][0]["units"][0]
    table = next(b for b in unit["blocks"] if b["kind"] == "table")
    assert table["rows"] == [["Label", "Value"], ["Row", ""]]
    assert len(table["column_widths"]) == 2 and all(w > 0 for w in table["column_widths"])
    assert [b["text"] for b in unit["blocks"] if b["kind"] == "paragraph"] == ["Outside"]
    assert unit["page"] == 1 and result["coverage"]["complete"]


def separator_table_pdf(path, *, ambiguous=False, page_origin=(0, 0)):
    edges = "30 70 m 270 70 l S\n30 110 m 270 110 l S\n30 150 m 270 150 l S\n30 70 m 30 150 l S\n150 70 m 150 150 l S\n270 70 m 270 150 l S\n"
    header = "BT /F1 11 Tf 165 114 Td (A) Tj /F2 11 Tf (/) Tj"
    if not ambiguous:
        header += " /F1 11 Tf (B) Tj"
    return pdf(path, [edges + text("Name", 40, 125) + header + " ET\n" + text("/", 40, 85)],
               separator_font=True, page_origin=page_origin)


@pytest.mark.parametrize("page_origin", [(0, 0), (17, 23)])
def test_cross_row_separator_follows_bracketing_glyphs_on_its_native_baseline(tmp_path, page_origin):
    path = separator_table_pdf(tmp_path / "source.pdf", page_origin=page_origin)
    unit = read_documents(Context(), [source(path)], {})["documents"][0]["units"][0]
    table = next(block for block in unit["blocks"] if block["kind"] == "table")
    assert table["rows"] == [["Name", "A/B"], ["/", ""]]
    decision, = unit["coverage"]["transformations"]
    assert decision["kind"] == "native_pdf_table_separator_ownership"
    assert decision["center_cell_bbox"][1] == decision["owning_cell_bbox"][3] == 110 - page_origin[1]
    assert decision["baseline_top"] == 106 - page_origin[1]


def test_cross_row_separator_with_one_unproven_neighbor_fails_required_read(tmp_path):
    path = separator_table_pdf(tmp_path / "ambiguous.pdf", ambiguous=True)
    with pytest.raises(ReadingFailure) as failure:
        read_documents(Context(), [source(path)], {})
    unresolved, = failure.value.result["coverage"]["required_unresolved_units"]
    assert unresolved["error_code"] == "PDF_TABLE_CELL_OWNERSHIP_AMBIGUOUS"


def test_pdf_horizontal_merge_is_preserved(tmp_path):
    commands = "30 70 m 270 70 l S\n30 110 m 270 110 l S\n30 150 m 270 150 l S\n30 70 m 30 150 l S\n150 70 m 150 110 l S\n270 70 m 270 150 l S\n"
    commands += text("Merged", 40, 125) + text("A", 40, 85) + text("B", 160, 85)
    p = pdf(tmp_path / "merge.pdf", [commands])
    data = read_documents(Context(), [source(p)], {})
    table = next(b for b in data["documents"][0]["units"][0]["blocks"] if b["kind"] == "table")
    assert table["rows"] == [["Merged", ""], ["A", "B"]]
    assert table["merges"] == [{"row": 0, "column": 0, "row_span": 1, "col_span": 2}]


def test_docx_order_merges_headers_and_footer_keep_native_locators(tmp_path):
    d = Document(); d.add_paragraph("Before table")
    t = d.add_table(rows=3, cols=3)
    t.cell(0, 0).merge(t.cell(0, 1)).text = "Wide header"
    t.cell(1, 0).merge(t.cell(2, 0)).text = "Vertical"
    t.cell(1, 1).text = "A"; t.cell(2, 2).text = "B"
    repeat = OxmlElement("w:tblHeader");t.rows[0]._tr.get_or_add_trPr().append(repeat)
    d.add_paragraph("After table");d.sections[0].header.paragraphs[0].text = "Header requirement"
    d.sections[0].footer.paragraphs[0].text = "Footer requirement"
    path = tmp_path / "input.docx"; d.save(path)
    result = read_documents(Context(), [source(path)], {})
    units = result["documents"][0]["units"]
    assert all(u["page"] is None for u in units)
    body = [u for u in units if u["locator"].startswith("word/document.xml")]
    assert [u["blocks"][0]["kind"] for u in body] == ["paragraph", "table", "paragraph"]
    table = body[1]["blocks"][0]
    assert table["rows"][0] == ["Wide header", "", ""]
    assert table["rows"][2][0] == ""
    assert {tuple(m.values()) for m in table["merges"]} == {(0, 0, 1, 2), (1, 0, 2, 1)}
    assert table["header_rows"] == [0]
    assert any("header" in u["locator"] and u["blocks"][0]["text"] == "Header requirement" for u in units)
    assert any("footer" in u["locator"] and u["blocks"][0]["text"] == "Footer requirement" for u in units)


def test_missing_ocr_keeps_complete_denominator_and_successful_later_pages(tmp_path):
    path = pdf(tmp_path / "mixed.pdf", [text("First native page"), "IMAGE", text("Third native page")])
    ctx = Context()
    with pytest.raises(ReadingFailure, match="OCR_REQUIRED") as failure:
        read_documents(ctx, [source(path)], {})
    result = failure.value.result
    assert result["coverage"]["unit_count"] == 3
    assert result["coverage"]["counts"] == {"read": 2, "empty": 0, "needs_ocr": 1, "failed": 0, "unsupported": 0}
    assert len(ctx.outputs) == 2 and ctx.captures[-1][0] == "reading-coverage"
    assert result["documents"][0]["units"][1]["coverage"]["unread_objects"] == ["pdf/page[2]/image[0]"]


def test_ocr_failure_retries_only_failed_page(tmp_path):
    path = pdf(tmp_path / "mixed.pdf", [text("First native page"), "IMAGE", text("Third native page")])
    calls = []
    def ocr(request):
        calls.append(request["page"])
        if len(calls) == 1:
            raise TimeoutError("Sensitive provider details must not escape")
        return echo_ocr(request)
    ctx = Context(); config = {"ocr_version": "stub-v1"}
    with pytest.raises(ReadingFailure) as failed:
        read_documents(ctx, [source(path)], config, ocr)
    assert "Sensitive" not in str(failed.value)
    succeeded = read_documents(ctx, [source(path)], config, ocr)
    assert succeeded["coverage"]["complete"] and calls == [2, 2]
    units = succeeded["documents"][0]["units"]
    assert [ctx.calls[u["unit_id"]] for u in units] == [1, 2, 1]
    assert units[1]["coverage"]["origin"] == "ocr"


@pytest.mark.parametrize("changed", ["source_id", "unit_id", "locator", "page"])
def test_ocr_identity_cannot_change(changed, tmp_path):
    path = pdf(tmp_path / "scan.pdf", ["IMAGE"])
    def ocr(request):
        result = echo_ocr(request);result[changed] = 99 if changed == "page" else "WRONG"
        return result
    with pytest.raises(ReadingFailure) as failure:
        read_documents(Context(), [source(path)], {"ocr_version": "stub"}, ocr)
    assert failure.value.result["coverage"]["counts"]["failed"] == 1


def test_blank_native_page_skips_ocr_and_remains_empty(tmp_path):
    p = pdf(tmp_path / "empty.pdf", [""])
    calls = []
    data = read_documents(Context(), [source(p)], {}, lambda req: calls.append(req))
    assert data["coverage"]["counts"]["empty"] == 1 and calls == []


def test_optional_reference_images_remain_partial_without_spending_ocr(tmp_path):
    tender = pdf(tmp_path / "tender.pdf", [text("Tender native requirement")])
    ref = pdf(tmp_path / "reference.pdf", ["IMAGE"])
    calls = []
    output = read_documents(Context(), [source(tender), source(ref, "reference_bid", "REF")], {}, lambda req: calls.append(req))
    assert output["coverage"]["required_complete"] and not output["coverage"]["complete"]
    assert output["coverage"]["status"] == "partial" and calls == []
    assert output["documents"][1]["units"][0]["coverage"]["status"] == "needs_ocr"


def test_explicit_decorative_ignore_is_scoped_and_keeps_reason(tmp_path):
    p = pdf(tmp_path / "logo.pdf", ["IMAGE"])
    cfg = {"ignored_objects": {source(p)["sha256"] + "::SRC-1::pdf/page[1]/image[0]": "Verified decorative logo only"}}
    output = read_documents(Context(), [source(p)], cfg)
    coverage = output["documents"][0]["units"][0]["coverage"]
    assert output["coverage"]["complete"] and coverage["status"] == "empty"
    assert coverage["ignored_objects"][0]["reason"] == "Verified decorative logo only"


def test_docx_image_and_unrecognized_object_are_not_silently_read(tmp_path):
    image = tmp_path / "image.png";Image.new("RGB", (20, 30), "white").save(image)
    d = Document();d.add_paragraph("Native before");d.add_picture(str(image));d.add_paragraph("Native after")
    path = tmp_path / "scan.docx";d.save(path)
    with pytest.raises(ReadingFailure) as failure:
        read_documents(Context(), [source(path)], {})
    assert failure.value.result["coverage"]["counts"]["read"] == 2
    output = read_documents(Context(), [source(path)], {"ocr_version": "stub"}, echo_ocr)
    unit = next(u for u in output["documents"][0]["units"] if u["coverage"]["image_count"])
    assert unit["page"] is None and unit["blocks"][0]["image_ref"]["package_part"].startswith("word/media/")
    d = Document();d.add_paragraph("Readable");d.element.body.insert(1, OxmlElement("w:altChunk"));d.save(path)
    with pytest.raises(ReadingFailure) as failure:
        read_documents(Context(), [source(path)], {})
    assert failure.value.result["coverage"]["counts"]["unsupported"] == 1


@pytest.mark.parametrize("kind", ["hash", "password", "corrupt"])
def test_bad_or_changed_sources_fail_closed(tmp_path, kind):
    path = pdf(tmp_path / "input.pdf", [text("Test")], password="secret" if kind == "password" else None)
    document = source(path)
    if kind == "hash":document["sha256"] = "0" * 64
    if kind == "corrupt":path.write_bytes(b"%PDF-1.7\nbroken");document = source(path)
    with pytest.raises(ReadingFailure):read_documents(Context(), [document], {})


def test_runtime_checkpoint_and_normal_events_do_not_contain_source_text(tmp_path):
    path = pdf(tmp_path / "input.pdf", [text("READING_SOURCE_CANARY")])
    runner = Executor(tmp_path / "runtime", [NodeSpec("N1", "native-test", lambda ctx, value: read_documents(ctx, value["documents"], {}))], execution_config={"debug_capture": True})
    run = runner.create_run({"documents": [source(path)]})
    assert runner.execute(run)["status"] == "succeeded"
    assert runner.output(run)["coverage"]["complete"]
    events = (tmp_path / "runtime/runs" / run / "events.jsonl").read_text(encoding="utf-8")
    assert "READING_SOURCE_CANARY" not in events


class GlyphPage:
    def __init__(self, symbol="\uf0a3", font="FAAAFE+Wingdings2"):
        self.chars = [{"text": symbol, "fontname": font, "x0": 10, "x1": 20, "top": 30, "bottom": 40}]
        self.images = []; self.curves = []; self.lines = []; self.rects = []

    def find_tables(self, **kwargs):
        return []

    def extract_words(self, *, return_chars=False):
        return [{**self.chars[0], **({"chars": self.chars} if return_chars else {})}]


def glyph_config(document, **changes):
    rule = {"source_sha256": document["sha256"], "font": "FAAAFE+Wingdings2", "codepoint": "U+F0A3",
            "replacement": "□", "pages": [1], "reason": "Synthetic visual decision: unchecked box"}
    return ReadingConfig(glyph_maps=[{**rule, **changes}])


def test_scoped_glyph_conversion_retains_exact_evidence_and_source_bytes(tmp_path):
    path = pdf(tmp_path / "glyph-source.pdf", [text("Native")]);document = source(path);original = path.read_bytes()
    unit = _read_pdf_page(SourceDocument.model_validate(document), GlyphPage(), 1, glyph_config(document), None)
    assert unit.coverage.status == "read" and unit.blocks[0].text == "□"
    evidence = unit.coverage.transformations[0]
    assert evidence["codepoint"] == "U+F0A3" and evidence["font"] == "FAAAFE+Wingdings2"
    assert evidence["source_sha256"] == document["sha256"] and evidence["bbox"] == [10, 30, 20, 40]
    assert evidence["original_symbol"] == "\uf0a3" and evidence["replacement"] == "□"
    assert path.read_bytes() == original


@pytest.mark.parametrize("change", [{"source_sha256": "0" * 64}, {"font": "AnotherFont"}, {"pages": [2]}, {"codepoint": "U+F0A4"}])
def test_unknown_or_mismatched_glyph_rule_never_becomes_read(tmp_path, change):
    path = pdf(tmp_path / "input.pdf", [text("Native")]);document = source(path)
    with pytest.raises(ReadingFailure, match="OCR_REQUIRED"):
        _read_pdf_page(SourceDocument.model_validate(document), GlyphPage(), 1, glyph_config(document, **change), None)


def test_full_page_ocr_replaces_native_text_instead_of_double_counting(tmp_path):
    path = pdf(tmp_path / "input.pdf", [text("Native")]);document = SourceDocument.model_validate(source(path))
    unit = _read_pdf_page(document, GlyphPage(), 1, ReadingConfig(ocr_version="stub"), echo_ocr)
    assert [b.text for b in unit.blocks if b.kind == "paragraph"] == ["Scanned requirement."]
    assert unit.coverage.origin == "ocr"


def test_ignore_cannot_carry_over_to_changed_bytes_same_source_id(tmp_path):
    old = pdf(tmp_path / "old.pdf", ["IMAGE"]);new = pdf(tmp_path / "new.pdf", ["IMAGE", ""])
    config = {"ignored_objects": {source(old)["sha256"] + "::SRC-1::pdf/page[1]/image[0]": "Decorative in old source only"}}
    with pytest.raises(ReadingFailure, match="OCR_REQUIRED"):
        read_documents(Context(), [source(new)], config)


def test_real_executor_restart_only_retries_failed_ocr_unit(tmp_path, monkeypatch):
    import company_bid.reading as reading
    path = pdf(tmp_path / "mixed.pdf", [text("First"), "IMAGE", text("Third")])
    native_calls = Counter();ocr_calls = []
    original = reading._read_pdf_page
    def counted(source, page, number, config, ocr):
        native_calls[number] += 1
        return original(source, page, number, config, ocr)
    monkeypatch.setattr(reading, "_read_pdf_page", counted)
    def ocr(request):
        ocr_calls.append(request["page"])
        if len(ocr_calls) == 1:raise TimeoutError("transient")
        return echo_ocr(request)
    def runner():
        return Executor(tmp_path / "runtime", [NodeSpec("N1", "restart-native-test",
            lambda ctx, value: read_documents(ctx, value["documents"], {"ocr_version": "stub"}, ocr))], execution_config={"debug_capture": True})
    first = runner();run = first.create_run({"documents": [source(path)]});failed = first.execute(run)
    assert failed["status"] == "failed"
    units = failed["nodes"][0]["units"]
    saved = {key: value["output_ref"] for key, value in units.items() if value["status"] == "succeeded"}
    assert len(saved) == 2
    assert any(c["name"] == "reading-coverage" for a in failed["nodes"][0]["attempts"] for c in a.get("captures", []))
    restarted = runner();done = restarted.resume(run)
    assert done["status"] == "succeeded" and ocr_calls == [2, 2] and native_calls == {1: 1, 2: 2, 3: 1}
    assert all(done["nodes"][0]["units"][key]["output_ref"] == value for key, value in saved.items())


def test_empty_ocr_response_cannot_complete_a_scanned_page(tmp_path):
    path = pdf(tmp_path / "scan.pdf", ["IMAGE"])
    def ocr(request):return {key: request[key] for key in ("source_id", "unit_id", "locator", "page")} | {"text": ""}
    with pytest.raises(ReadingFailure):read_documents(Context(), [source(path)], {"ocr_version": "stub"}, ocr)


def test_unknown_glyph_cannot_be_hidden_with_ignore_rule(tmp_path):
    path = pdf(tmp_path / "input.pdf", [text("Native")]);document = source(path)
    with pytest.raises(ValueError, match="never an ignore"):
        ReadingConfig(ignored_objects={document["sha256"] + "::SRC-1::pdf/page[1]/unmapped-glyphs": "not allowed"})


def test_docx_empty_body_has_an_explicit_empty_unit(tmp_path):
    path = tmp_path / "empty.docx";Document().save(path)
    result = read_documents(Context(), [source(path)], {})
    assert result["coverage"]["unit_count"] == 1 and result["coverage"]["counts"]["empty"] == 1
    assert result["documents"][0]["expected_units"] == 1


def test_unknown_content_in_docx_table_cell_is_not_silently_lost(tmp_path):
    d = Document();table = d.add_table(rows=1, cols=1)
    control = OxmlElement("w:sdt");content = OxmlElement("w:sdtContent")
    paragraph = OxmlElement("w:p");run = OxmlElement("w:r");text_node = OxmlElement("w:t");text_node.text = "Hidden requirement"
    run.append(text_node);paragraph.append(run);content.append(paragraph);control.append(content);table.cell(0, 0)._tc.append(control)
    path = tmp_path / "cell-control.docx";d.save(path)
    with pytest.raises(ReadingFailure) as failure:read_documents(Context(), [source(path)], {})
    assert failure.value.result["coverage"]["counts"]["unsupported"] == 1


def test_ocr_boolean_page_identity_is_invalid(tmp_path):
    path = pdf(tmp_path / "scan.pdf", ["IMAGE"])
    def ocr(request):return {**echo_ocr(request), "page": True}
    with pytest.raises(ReadingFailure):read_documents(Context(), [source(path)], {"ocr_version": "stub"}, ocr)


@pytest.mark.parametrize("application_code,expected", [
    ("PROVIDER_RUN_BUDGET_EXCEEDED", "PROVIDER_RUN_BUDGET_EXCEEDED"),
    ("PROVIDER_AUTH_FAILED", "PROVIDER_AUTH_FAILED"),
    ("DIAGNOSTICS_CAPTURE_FAILED", "DIAGNOSTICS_CAPTURE_FAILED"),
    ("OCR_CONTENT_UNREADABLE", "OCR_CONTENT_UNREADABLE"),
    ("OTHER_PRIVATE_VALUE", "OCR_FAILED_OR_IDENTITY_INVALID"),
    ("OCR_private_text", "OCR_FAILED_OR_IDENTITY_INVALID"),
    ("OCR_PRIVATE VALUE", "OCR_FAILED_OR_IDENTITY_INVALID"),
    ("OCR_" + "X" * 80, "OCR_FAILED_OR_IDENTITY_INVALID"),
    (401, "OCR_FAILED_OR_IDENTITY_INVALID"),
])
def test_ocr_preserves_only_bounded_application_codes_and_complete_ledger(tmp_path, application_code, expected):
    path = pdf(tmp_path / "mixed.pdf", [text("Readable first"), "IMAGE", text("Readable third")])
    class AdapterError(Exception):
        code = application_code
    def fail(request):
        raise AdapterError("EXCEPTION-BODY-PRIVATE-CANARY")
    ctx = Context()
    with pytest.raises(ReadingFailure) as failure:
        read_documents(ctx, [source(path)], {"ocr_version": "stub"}, fail)
    assert failure.value.code == expected
    coverage = failure.value.result["coverage"]
    assert coverage["unit_count"] == 3 and coverage["counts"]["read"] == 2
    assert coverage["required_unresolved_units"][0]["error_code"] == expected
    assert ctx.captures[-1][0] == "reading-coverage"
    assert "EXCEPTION-BODY-PRIVATE-CANARY" not in str(ctx.captures)
    assert "EXCEPTION-BODY-PRIVATE-CANARY" not in str(failure.value)


def _observed_ocr_failure(tmp_path, monkeypatch, *, authentication):
    import io
    from urllib.error import HTTPError
    from company_bid.ocr import OCR_VERSION, create_ocr
    from company_bid.provider import ChatProposalProvider
    from company_bid.diagnostics import inspect_diagnostics
    monkeypatch.setenv("TEST_READING_OCR_KEY", "reading-credential-canary")
    path = pdf(tmp_path / "scan.pdf", ["IMAGE"])
    calls = []
    def transport(endpoint, payload, secret, timeout):
        calls.append(payload)
        if authentication:
            raise HTTPError(endpoint, 401, "private reason", {}, io.BytesIO(b"reading-credential-canary PRIVATE-HTTP-BODY"))
        pytest.fail("Budget exhaustion must happen before transport")
    original = ChatProposalProvider.__init__
    monkeypatch.setattr(ChatProposalProvider, "__init__", lambda self, config, **kwargs:
                        original(self, config, transport=transport, sleep=lambda _: None))
    common = {"mode": "chat_completions", "endpoint": "https://example.invalid/chat", "model": "synthetic-vision",
              "auth_env": "TEST_READING_OCR_KEY", "max_attempts": 1, "max_run_calls": 2,
              "max_run_usd": 1 if authentication else 0.000000001}
    def run(ctx, value):
        return read_documents(ctx, [source(path)], {"ocr_version": OCR_VERSION}, create_ocr(ctx, {"provider": common}))
    executor = Executor(tmp_path / "runtime", [NodeSpec("N1", "observed-ocr-errors", run)],
                        {"provider": common, "debug_capture": True})
    rid = executor.create_run({}); state = executor.execute(rid)
    report = inspect_diagnostics(executor.store.root, rid)
    return executor, rid, state, report, calls


def test_ocr_budget_exhaustion_is_explicit_at_node_with_zero_http_attempts(tmp_path, monkeypatch):
    executor, rid, state, report, calls = _observed_ocr_failure(tmp_path, monkeypatch, authentication=False)
    assert state["error_code"] == "PROVIDER_RUN_BUDGET_EXCEEDED" and calls == []
    assert report["actual_call_attempts"] == 0
    assert next(iter(state["nodes"][0]["units"].values()))["attempts"][-1]["error_code"] == state["error_code"]
    captures = state["nodes"][0]["attempts"][0]["captures"]
    reasons = [executor.store.read_json(capture["ref"]) for capture in captures if capture["name"] == "ocr-failure"]
    assert reasons[0]["error_code"] == state["error_code"]
    assert any(capture["name"] == "reading-coverage" for capture in captures)


def test_ocr_auth_failure_matches_actual_call_unit_and_node_code(tmp_path, monkeypatch):
    executor, rid, state, report, calls = _observed_ocr_failure(tmp_path, monkeypatch, authentication=True)
    assert state["error_code"] == "PROVIDER_AUTH_FAILED" and len(calls) == 1
    assert report["actual_call_attempts"] == 1
    assert report["calls"][0]["error_code"] == state["error_code"]
    assert report["calls"][0]["outcome"] == "failed"
    assert next(iter(state["nodes"][0]["units"].values()))["attempts"][-1]["error_code"] == state["error_code"]
    directory = executor.store.run_dir(rid)
    for name in ("events.jsonl", "spans.jsonl"):
        ordinary = (directory / name).read_text(encoding="utf-8")
        assert "reading-credential-canary" not in ordinary and "PRIVATE-HTTP-BODY" not in ordinary
    assert not any(b"reading-credential-canary" in path.read_bytes() for path in directory.rglob("*") if path.is_file())
