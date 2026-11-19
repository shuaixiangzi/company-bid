from pathlib import Path
from types import SimpleNamespace

from docx import Document
from docx.enum.text import WD_TAB_LEADER
import pdfplumber
from pypdf import PdfWriter
from pypdf.generic import NameObject
import pytest
import reportlab
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from company_bid.native_layout import read_native_format_layout
from company_bid.planning import _fact_index, _fixed_native_table_notes
from company_bid.reading import ReadingConfig, SourceDocument, _read_pdf_page
from company_bid.source_format_contracts import NativeUnderline
from company_bid.source_format_rendering import _native_paragraph
from test_reading import pdf, source, text


def test_mixed_font_baseline_preserves_line_formula_cell_and_original_boxes(tmp_path):
    borders = "30 70 240 45 re S\n150 70 m 150 115 l S\n"
    formula = "BT /F1 11 Tf 40 170 Td (A) Tj /F2 11 Tf (/) Tj /F1 11 Tf (B) Tj ET\n"
    total = formula.replace("40 170", "40 94")
    adjacent = "BT /F2 11 Tf 40 150 Td (/) Tj ET\n"
    reference = text("B", 160, 94)
    path = pdf(tmp_path / "formula.pdf", [borders + "1 0 0 rg\n" + formula + "0 0 0 rg\n" + adjacent +
                                         total + reference], separator_font=True)
    document = SourceDocument.model_validate(source(path))
    with pdfplumber.open(path) as opened:
        page = opened.pages[0]
        slash = page.chars[1]
        raw_box = [slash[key] for key in ("x0", "top", "x1", "bottom")]
        read = _read_pdf_page(document, page, 1, ReadingConfig(), None)
    measured = read_native_format_layout(path, document.sha256, [1])["pages"][0]
    assert [block.text for block in read.blocks if block.kind == "paragraph"] == ["A/B", "/"]
    assert [line["text"] for line in measured["lines"]] == ["A/B", "/"]
    physical_line = measured["lines"][0]
    assert [run["text"] for run in physical_line["runs"]] == ["A", "/", "B"]
    assert physical_line["runs"][1]["chars"][0]["bbox"] == raw_box
    assert {run["ink_color_rgb"] for run in physical_line["runs"]} == {"FF0000"}
    table, = measured["tables"]
    assert table["rows"] == next(block.rows for block in read.blocks if block.kind == "table") == [["A/B", "B"]]


@pytest.mark.parametrize(("mode", "width", "bold"), [(2, .4, True), (0, .4, False), (2, .05, False)])
def test_prescribed_heading_needs_proven_visible_stroke_to_render_as_bold(tmp_path, mode, width, bold):
    font_path = Path(reportlab.__file__).parent / "fonts" / "Vera.ttf"
    pdfmetrics.registerFont(TTFont("TestEmbeddedTitle", str(font_path)))
    input_path = tmp_path / "with_subset_name.pdf"
    page = canvas.Canvas(str(input_path), pagesize=(300, 220))
    page.setFillColorRGB(0, 0, 0)
    page.setStrokeColorRGB(0, 0, 0)
    page.setLineWidth(width)
    title = page.beginText(80, 170)
    title.setFont("TestEmbeddedTitle", 14)
    title.setTextRenderMode(mode)
    title.textOut("Bid")
    page.drawText(title)
    page.save()
    # Keep both PDF adapters' embedded font name equal for this proof fixture.
    writer = PdfWriter(clone_from=input_path)
    fonts = writer.pages[0]["/Resources"]["/Font"]
    embedded, = [font.get_object() for font in fonts.values() if "/FontDescriptor" in font.get_object()]
    family = NameObject("/BitstreamVeraSans-Roman")
    embedded[NameObject("/BaseFont")] = family
    embedded["/FontDescriptor"][NameObject("/FontName")] = family
    path = tmp_path / "heading.pdf"
    writer.write(path)
    measured = read_native_format_layout(path, source(path)["sha256"], [1],
                                         heading_locator="pdf/page[1]/line[0]")
    line, = measured["pages"][0]["lines"]
    assert all(run["bold"] is bold for run in line["runs"])
    assert ("native_heading_stroke" in line) is bold


@pytest.mark.parametrize("change", [None, "skipped_number", "new_anchor", "other_page", "different_indent"])
def test_table_notes_need_an_explicit_contiguous_native_sequence(change):
    def block(key, value, top, *, kind="paragraph"):
        return {"block_id": key, "text": value, "source_id": "S", "page": 1, "kind": kind,
                "origin": "native", "bbox": [40., top, 250., top + 11.]}

    blocks = [block("title", "样表", 60), block("grid", "价格", 72, kind="table"),
              block("note", "注：1.本表逐项核对金额。", 88),
              block("next", "2.金额有误时调整标记，另按以下规则", 103),
              block("continuation", "继续核对相应项目。", 118)]
    anchors = {"title": {"status": "prescribed", "title": "样表"}}
    if change == "skipped_number":
        blocks[3]["text"] = blocks[3]["text"].replace("2.", "3.")
    elif change == "new_anchor":
        anchors["next"] = {"status": "prescribed", "title": "另表"}
    elif change == "other_page":
        blocks[3]["page"] = 2
    elif change == "different_indent":
        blocks[3]["bbox"][0] += 12
    proof = _fixed_native_table_notes(blocks, anchors)
    if change:
        assert not proof
    else:
        assert proof["next"]["continuation_block_id"] == "continuation"
        assert proof["next"]["table_block_id"] == "grid"


def test_date_vector_tabs_keep_three_unfilled_fields_and_red_source_ink():
    document = Document()
    heading = "年 月 日"
    underlines = [NativeUnderline(fixed_index=0, start=start, end=end, width_pt=x1-x0,
                                 locator=f"pdf/page[1]/rule[{index}]", bbox=[x0, 300., x1, 300.])
                  for index, (start, end, x0, x1) in enumerate(
                      [(0, 0, 60., 100.), (1, 2, 112., 140.), (3, 4, 152., 180.)])]
    element = SimpleNamespace(font_family="SimSun", font_size_pt=12., bold=False, alignment="right",
                              bbox=[100., 288., 192., 300.], left_pt=100., right_pt=108.,
                              first_line_indent_pt=0., line_spacing_pt=15., fixed_indices=[0],
                              source_texts=[heading], underlines=underlines, ink_color_rgb="FF0000")
    section = SimpleNamespace(native_format=SimpleNamespace(elements=[element], bindings=[]),
                              blocks=[SimpleNamespace(text=heading)])
    paragraph = _native_paragraph(document, section, element, 0.)
    assert paragraph.text == "\t年\t月\t日"
    assert paragraph.paragraph_format.left_indent.pt == 60.
    assert [(tab.position.pt, tab.leader) for tab in paragraph.paragraph_format.tab_stops] == [
        (100., WD_TAB_LEADER.LINES), (140., WD_TAB_LEADER.LINES), (180., WD_TAB_LEADER.LINES)]
    assert {str(run.font.color.rgb) for run in paragraph.runs if run.text.strip()} == {"FF0000"}


def test_scoped_tender_display_identity_only_fills_with_explicit_company_fact():
    example = {"project": {"name": "项目", "source_refs": ["T"]},
               "company": {"name": "未载入公司资料", "source_refs": ["S"]}}
    assert [fact["label"] for fact in _fact_index(example, limited_tender_scope=True)] == ["项目名称"]
    assert [fact["label"] for fact in _fact_index(example)] == ["项目名称", "投标人名称"]
    explicit = {"fact_id": "F", "label": "公司名称", "value": "已有明确输入", "source_refs": ["C"]}
    assert explicit in _fact_index({**example, "facts": [explicit]}, limited_tender_scope=True)
