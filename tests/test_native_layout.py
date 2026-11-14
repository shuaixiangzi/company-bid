"""Native response geometry remains source-bound without changing N1 contracts."""
import hashlib
from pathlib import Path

import pdfplumber
from pypdf import PdfReader, PdfWriter
import pytest
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from PIL import Image

from company_bid.native_layout import NativeLayoutError, read_native_format_layout
from company_bid.reading import SourceDocument, ReadingConfig, _read_pdf_page
from company_bid.extraction import _index
from company_bid.storage import digest


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def native_pdf(tmp_path, *, image=False, curve=False, rotated_text=False):
    path = tmp_path / "native.pdf"
    pdf = canvas.Canvas(str(path), pagesize=(600, 800))
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawCentredString(300, 745, "Native authorization form")
    pdf.setFont("Helvetica", 12)
    pdf.drawString(60, 710, "Company:")
    pdf.line(120, 707, 250, 707)
    # An uneven table with one horizontal and one vertical span.
    for x in (40, 200, 360):
        pdf.line(x, 400, x, 650)
    for y in (400, 500, 650):
        pdf.line(40, y, 360, y)
    pdf.line(40, 600, 200, 600)
    pdf.setFont("Helvetica", 12)
    pdf.drawString(50, 630, "Identity area")
    pdf.drawString(210, 630, "Tall right cell")
    pdf.drawString(50, 580, "Left continuation")
    pdf.drawCentredString(120, 445, "Bottom left")
    pdf.drawCentredString(280, 445, "Bottom right")
    pdf.drawCentredString(300, 20, "- 1 -")
    if image:
        pdf.drawImage(ImageReader(Image.new("RGB", (20, 20), "red")), 450, 600, 20, 20)
    if curve:
        pdf.circle(450, 600, 10)
    if rotated_text:
        pdf.saveState();pdf.translate(480, 600);pdf.rotate(90);pdf.drawString(0, 0, "Vertical");pdf.restoreState()
    pdf.save()
    return path


def test_measurements_fonts_rules_and_spans_match_same_native_n1_selection(tmp_path):
    path = native_pdf(tmp_path)
    before = path.read_bytes()
    result = read_native_format_layout(path, sha(path), [1])
    page = result["pages"][0]
    assert result["source_sha256"] == sha(path) and path.read_bytes() == before
    assert page["rotation"] == 0 and page["width_pt"] == 600 and page["height_pt"] == 800
    table, = page["tables"]
    assert table["bbox"] == pytest.approx([40, 150, 360, 400])
    assert table["column_widths_pt"] == [160, 160]
    assert table["row_heights_pt"] == [50, 100, 100]
    assert {"row": 0, "column": 1, "row_span": 2, "col_span": 1} in table["merges"]
    assert table["height_pt"] == 250
    assert any(rule["kind"] == "fillable_rule" and rule["length_pt"] == 130 for rule in page["horizontal_rules"])
    assert any(rule["kind"] == "table_boundary" for rule in page["horizontal_rules"])
    title = next(line for line in page["lines"] if line["text"] == "Native authorization form")
    assert title["runs"][0]["bold"] is True
    assert title["runs"][0]["font_size_pt"] == 14
    assert title["alignment_evidence"]["horizontal"] == "center"
    footer = next(line for line in page["lines"] if line["text"] == "- 1 -")
    assert footer["page_furniture_candidate"] == "page_number"
    # Furniture is retained, not silently stripped or called a form paragraph.
    assert footer["furniture_policy"] == "retained_for_cross_check_not_automatically_form_content"
    source = SourceDocument(source_id="T", path=path, sha256=sha(path), role="tender", title="Synthetic")
    with pdfplumber.open(path) as document:
        original = _read_pdf_page(source, document.pages[0], 1, ReadingConfig(), None)
    by_locator = {block.locator: block for block in original.blocks}
    for line in page["lines"]:
        frozen = by_locator[line["locator"]]
        assert frozen.text == line["text"] and frozen.bbox == line["bbox"]
    original_table = by_locator[table["locator"]]
    assert original_table.rows == table["rows"]
    assert [merge.model_dump() for merge in original_table.merges] == table["merges"]
    assert original_table.column_widths == table["column_widths_pt"]
    assert table["grid_text_sha256"] == digest(original_table.rows)
    # N1's stored table is raw rows; N3 sees the source-selected TSV view.
    # Layout text must match that actual consuming view, not its empty raw text.
    assert original_table.text == ""
    parsed = {"documents": [{"source_id": source.source_id, "sha256": source.sha256,
        "role": "tender", "format": "pdf", "units": [original.model_dump(mode="json")]}]}
    indexed, _, _ = _index(parsed, {"sources": [source.model_dump(mode="json")],
                                  "source_roles": {source.source_id: "tender"}})
    exported = next(item for item in indexed.values() if item["locator"] == table["locator"])
    assert table["text"] == exported["text"] == "\n".join("\t".join(row) for row in original_table.rows)
    assert table["text_basis"] == exported["text_basis"] == "table_rows_tsv_v1"
    assert table["text_sha256"] == hashlib.sha256(exported["text"].encode()).hexdigest()
    assert original_table.text == ""  # Neither source view mutates N1's object.


@pytest.mark.parametrize("pages", [[], [0], [-1], [1, 1], [True], [1.0], [2], "1", None])
def test_invalid_or_ambiguous_page_scope_is_rejected(tmp_path, pages):
    path = native_pdf(tmp_path)
    with pytest.raises(NativeLayoutError, match="NATIVE_LAYOUT_PAGE_SCOPE_INVALID"):
        read_native_format_layout(path, sha(path), pages)


@pytest.mark.parametrize("expected", ["0" * 64, "bad", None])
def test_source_hash_mismatch_or_invalid_hash_is_rejected(tmp_path, expected):
    path = native_pdf(tmp_path)
    with pytest.raises(NativeLayoutError, match="NATIVE_LAYOUT_SOURCE_HASH_(?:MISMATCH|INVALID)"):
        read_native_format_layout(path, expected, [1])


def test_global_rotation_is_not_normalized_or_silently_accepted(tmp_path):
    path = native_pdf(tmp_path)
    rotated = tmp_path / "rotated.pdf"
    writer = PdfWriter();page = PdfReader(path).pages[0];page.rotate(90);writer.add_page(page)
    with rotated.open("wb") as handle:writer.write(handle)
    with pytest.raises(NativeLayoutError, match="NATIVE_LAYOUT_UNSUPPORTED_ROTATION"):
        read_native_format_layout(rotated, sha(rotated), [1])


@pytest.mark.parametrize("kind", ["image", "curve", "rotated_text"])
def test_unsupported_objects_or_transformed_text_fail_closed(tmp_path, kind):
    path = native_pdf(tmp_path, **{kind: True})
    with pytest.raises(NativeLayoutError, match="NATIVE_LAYOUT_UNSUPPORTED_(?:NONNATIVE_OBJECT|TEXT_TRANSFORM)"):
        read_native_format_layout(path, sha(path), [1])


def test_same_source_and_scope_are_deterministic(tmp_path):
    path = native_pdf(tmp_path)
    assert read_native_format_layout(path, sha(path), [1]) == read_native_format_layout(path, sha(path), [1])
