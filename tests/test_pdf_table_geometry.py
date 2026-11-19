import hashlib
from pathlib import Path

import pdfplumber
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, NameObject, NumberObject
import pytest
import reportlab
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from company_bid.native_layout import NativeLayoutError, read_native_format_layout
from company_bid.pdf_table_geometry import discover_pdf_tables
from company_bid.reading import ReadingConfig, SourceDocument, _read_pdf_page, _UnitFailure
from test_reading import pdf, text


GRID = ("30 70 m 270 70 l S\n30 110 m 270 110 l S\n30 150 m 270 150 l S\n"
        "30 70 m 30 150 l S\n150 70 m 150 150 l S\n270 70 m 270 150 l S\n")


def _read(path):
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    source = SourceDocument(source_id="geometry", title="Test", role="tender", path=path, sha256=source_hash)
    with pdfplumber.open(path) as document:
        unit = _read_pdf_page(source, document.pages[0], 1, ReadingConfig(), None)
    return unit, read_native_format_layout(path, source_hash, [1])["pages"][0]


def test_highlight_fragments_do_not_split_a_genuine_cell(tmp_path):
    highlight = "q 1 1 0 rg 165 137 11 12 re f 176 137 12 12 re f 188 137 18 12 re f Q\n"
    commands = GRID + highlight + text("X123Z", 166, 141) + text("Keep this", 167, 123) + text("Name", 40, 85)
    path = pdf(tmp_path / "decorated.pdf", [commands])
    with pdfplumber.open(path) as document:
        source_page, = document.pages
        assert len(source_page.rects) == 3
        assert sum(len(table.cells) for table in source_page.find_tables()) != 4
    unit, native = _read(path)
    table, = [item for item in unit.blocks if item.kind == "table"]
    assert table.rows == [["", "X123Z\nKeep this"], ["Name", ""]]
    decision, = unit.coverage.transformations
    assert decision == native["table_discovery"]
    assert len(decision["fill_bboxes"]) == 3
    assert native["tables"][0]["rows"] == table.rows
    assert len(native["horizontal_rules"]) == 3


def test_fill_defined_grid_is_kept_without_an_independent_boundary(tmp_path):
    commands = ("q 1 1 0 rg 30 100 40 14 re f 70 86 40 14 re f "
                "0 1 1 rg 70 100 40 14 re f 30 86 40 14 re f Q\n"
                + text("AB", 40, 103) + text("CD", 80, 103) + text("EF", 40, 89) + text("GH", 80, 89))
    path = pdf(tmp_path / "filled-grid.pdf", [commands])
    with pdfplumber.open(path) as document:
        page, = document.pages
        result = discover_pdf_tables(page)
        assert not result.text_backgrounds
        table, = result.tables
        assert len(table.cells) == 4
    unit, native = _read(path)
    expected = [["AB", "CD"], ["EF", "GH"]]
    assert next(item for item in unit.blocks if item.kind == "table").rows == expected
    assert native["tables"][0]["rows"] == expected


def test_inline_highlight_without_native_grid_returns_the_whole_paragraph(tmp_path):
    highlight = "q 0 1 0 rg 45 136 10 18 re f 55 136 9 18 re f 64 136 7 18 re f Q\n"
    path = pdf(tmp_path / "inline-highlight.pdf", [highlight + text("AB1234CD", 32, 140)])
    with pdfplumber.open(path) as document:
        page, = document.pages
        assert len(page.find_tables()) == 1
    unit, native = _read(path)
    assert [(block.kind, block.text) for block in unit.blocks] == [("paragraph", "AB1234CD")]
    assert not native["tables"] and [line["text"] for line in native["lines"]] == ["AB1234CD"]
    assert len(native["table_discovery"]["fill_bboxes"]) == 3


def _formula_pdf(tmp_path, *, ambiguous):
    path = tmp_path / "formula.pdf"
    formula = "BT /F1 11 Tf 165 114 Td (A) Tj /F2 11 Tf (/) Tj /F1 11 Tf (\\() Tj /F2 11 Tf (1+) Tj"
    if not ambiguous:
        formula += " /F1 11 Tf (B\\)) Tj"
    raw = pdf(tmp_path / "raw.pdf", [GRID + formula + " ET\n" + text("Next", 165, 85)], separator_font=True)
    writer = PdfWriter(clone_from=raw)
    fonts = writer.pages[0]["/Resources"]["/Font"]
    fonts["/F2"][NameObject("/FirstChar")] = NumberObject(43)
    fonts["/F2"][NameObject("/LastChar")] = NumberObject(49)
    fonts["/F2"][NameObject("/Widths")] = ArrayObject([NumberObject(500)] * 7)
    writer.write(path)
    return path


def test_bracketed_formula_glyphs_preserve_original_coordinates_and_move_together(tmp_path):
    unit, native = _read(_formula_pdf(tmp_path, ambiguous=False))
    table = next(item for item in unit.blocks if item.kind == "table")
    assert table.rows == [["", "A/(1+B)"], ["", "Next"]]
    assert native["tables"][0]["rows"] == table.rows
    assert [item["kind"] for item in unit.coverage.transformations] == [
        "native_pdf_table_separator_ownership", "native_pdf_table_baseline_ownership",
        "native_pdf_table_baseline_ownership"]
    cell = next(item for item in native["tables"][0]["cells"] if (item["row"], item["column"]) == (0, 1))
    char_boxes = {char["text"]: char["bbox"] for run in cell["runs"] for char in run["chars"]}
    assert [char_boxes[glyph] for glyph in "/1+"] == [item["glyph_bbox"] for item in unit.coverage.transformations]


def test_missing_second_formula_anchor_remains_ambiguous(tmp_path):
    path = _formula_pdf(tmp_path, ambiguous=True)
    with pytest.raises(_UnitFailure) as exc:
        _read(path)
    assert exc.value.code == "PDF_TABLE_CELL_OWNERSHIP_AMBIGUOUS"
    with pytest.raises(NativeLayoutError) as exc:
        read_native_format_layout(path, hashlib.sha256(path.read_bytes()).hexdigest(), [1])
    assert exc.value.code == "NATIVE_LAYOUT_TABLE_CELL_OWNERSHIP_AMBIGUOUS"


def test_embedded_outline_recovers_pure_numeric_cells_across_row_and_outer_edges(tmp_path):
    font_path = Path(reportlab.__file__).parent / "fonts" / "Vera.ttf"
    pdfmetrics.registerFont(TTFont("TableTestVera", str(font_path)))
    raw = tmp_path / "raw.pdf"
    source = canvas.Canvas(str(raw), pagesize=(300, 220))
    source.grid([30, 150, 270], [70, 110, 150])
    source.setFont("TableTestVera", 11)
    source.drawString(38, 114, "12")
    source.drawString(165, 114, "2.5%")
    source.drawString(38, 74, "34")
    source.drawString(165, 74, "0.05%")
    source.drawString(38, 45, "Outside")
    source.save()
    writer = PdfWriter(clone_from=raw)
    fonts = writer.pages[0]["/Resources"]["/Font"]
    font = next(fonts[key] for key in fonts if fonts[key].get("/FontDescriptor"))
    font["/FontDescriptor"][NameObject("/Descent")] = NumberObject(-1000)
    path = tmp_path / "numeric.pdf"
    writer.write(path)
    unit, native = _read(path)
    table = next(item for item in unit.blocks if item.kind == "table")
    assert table.rows == [["12", "2.5%"], ["34", "0.05%"]]
    assert [item.text for item in unit.blocks if item.kind == "paragraph"] == ["Outside"]
    assert native["tables"][0]["rows"] == table.rows
    assert [line["text"] for line in native["lines"]] == ["Outside"]
    proofs = unit.coverage.transformations
    assert len(proofs) == 13 and sum(proof["center_cell_bbox"] is None for proof in proofs) == 7
    assert all(proof["basis"] == "embedded_font_glyph_outline" for proof in proofs)


def test_diagonal_is_preserved_as_a_drawing_rule_and_does_not_add_a_column(tmp_path):
    diagonal = "100 149.75 m 149.75 110.25 l S\n"
    path = pdf(tmp_path / "diagonal.pdf", [GRID + diagonal + text("Header", 68, 128) + text("Value", 165, 128)])
    with pdfplumber.open(path) as document:
        page, = document.pages
        assert len(page.find_tables()[0].cells) == 5
    unit, native = _read(path)
    table = next(item for item in unit.blocks if item.kind == "table")
    assert table.rows == [["Header", "Value"], ["", ""]]
    assert native["tables"][0]["rows"] == table.rows
    assert native["table_discovery"]["diagonal_rule_endpoints"] == [[[100, 70.25], [149.75, 109.75]]]
