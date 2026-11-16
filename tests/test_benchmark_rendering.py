import pytest
from docx import Document
from docx.oxml.ns import qn
from PIL import Image

from company_bid.models import DocumentPackage, HeadingBlock, ParagraphBlock, file_sha256
from company_bid.rendering import render_package
from company_bid.sample import create_synthetic_sample
from test_rendering import package_for


def package(tmp_path):
    return package_for(create_synthetic_sample(tmp_path / "input"))


def test_a4_cover_toc_and_body_have_separate_page_sections(tmp_path):
    result = render_package(package(tmp_path), tmp_path / "out")
    doc = Document(result["bid_docx"]["path"])
    assert len(doc.sections) == 3
    for section in doc.sections:
        assert section.page_width.cm == pytest.approx(21, abs=0.01)
        assert section.page_height.cm == pytest.approx(29.7, abs=0.01)
        assert section.left_margin.cm == pytest.approx(2.5, abs=0.01)
        assert section.header_distance.cm == pytest.approx(1.5, abs=0.01)
        assert section.footer_distance.cm == pytest.approx(1.75, abs=0.01)
    title = next(p for p in doc.paragraphs if p.style.name == "Title")
    assert title.text == "投 标 文 件"
    assert title.runs[0].font.size.pt == 48
    assert doc.sections[0]._sectPr.find(qn("w:pgNumType")).get(qn("w:start")) == "1"
    for section in doc.sections[1:]:
        assert section._sectPr.find(qn("w:pgNumType")).get(qn("w:start")) is None
    assert all("PAGE" in section.footer._element.xml for section in doc.sections)
    assert not doc.styles["Title"]._element.xpath("./w:pPr/w:pBdr")


def test_style_profile_controls_geometry_typography_and_cjk_defaults(tmp_path):
    data = package(tmp_path).model_dump()
    data["style"].update(page_size="Letter", margin_cm=2.0, font_family="仿宋", body_pt=13,
                         body_line_spacing=1.75, body_first_line_chars=2,
                         heading_pt=(18, 16, 14), table_pt=11,
                         table_border_color="222222", cover_title_pt=40)
    result = render_package(DocumentPackage.model_validate(data), tmp_path / "out")
    doc = Document(result["bid_docx"]["path"])
    assert doc.sections[0].page_width.inches == pytest.approx(8.5)
    assert doc.sections[0].left_margin.cm == pytest.approx(2, abs=0.01)
    normal = doc.styles["Normal"]
    assert normal.font.size.pt == 13
    assert normal.paragraph_format.line_spacing == 1.75
    indent = normal._element.find(qn("w:pPr")).find(qn("w:ind"))
    assert indent.get(qn("w:firstLineChars")) == "200"
    assert indent.get(qn("w:firstLine")) == "520"
    for name, size in [("Normal", 26), ("Heading 1", 36), ("Heading 2", 32)]:
        r_pr = doc.styles[name]._element.get_or_add_rPr()
        fonts = r_pr.get_or_add_rFonts()
        assert all(fonts.get(qn(f"w:{tag}")) == "仿宋" for tag in ["ascii", "hAnsi", "eastAsia", "cs"])
        assert not any(fonts.get(qn(f"w:{tag}")) for tag in ["asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme"])
        assert r_pr.find(qn("w:szCs")).get(qn("w:val")) == str(size)


def test_company_default_typography_applies_to_headings_body_and_tables(tmp_path):
    data = package(tmp_path).model_dump()
    for role in ("fact", "neutral"):
        data["sections"][1]["blocks"].append(
            ParagraphBlock(text=f"{role} 正文样式检查", content_role=role,
                           fact_refs=[data["facts"][0]["fact_id"]] if role == "fact" else []).model_dump())
    result = render_package(DocumentPackage.model_validate(data), tmp_path / "out")
    doc = Document(result["bid_docx"]["path"])
    for name, size, chars in (("Heading 1", 16, "0"), ("Heading 2", 15, "0"),
                              ("Heading 3", 14, "0"), ("Normal", 12, "200")):
        style = doc.styles[name]
        assert style.font.size.pt == size
        assert style._element.get_or_add_rPr().get_or_add_rFonts().get(qn("w:eastAsia")) == "宋体"
        pf = style.paragraph_format
        assert pf.space_before.pt == pf.space_after.pt == 0
        assert pf.line_spacing == 1.5
        indent = style._element.xpath("./w:pPr/w:ind")[0]
        assert indent.get(qn("w:firstLineChars")) == chars
        assert indent.get(qn("w:left")) == indent.get(qn("w:right")) == "0"
    body = [p for p in doc.paragraphs if p.text.startswith(("拟在现场", "（1）", "fact 正文", "neutral 正文"))]
    assert len(body) == 4
    for paragraph in body:
        assert paragraph.style.name == "Normal"
        assert not paragraph._p.xpath("./w:pPr/w:ind")  # Inherit the two-character body indent.
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    pf = paragraph.paragraph_format
                    assert pf.space_before.pt == pf.space_after.pt == 0
                    assert pf.line_spacing == 1.5
                    indent = paragraph._p.xpath("./w:pPr/w:ind")[0]
                    assert indent.get(qn("w:firstLineChars")) == "0"
                    assert indent.get(qn("w:left")) == indent.get(qn("w:right")) == "0"
                    assert all(run.font.size.pt == 10.5 and run.font.name == "宋体" for run in paragraph.runs)


def test_explicit_heading_levels_and_page_breaks_follow_plan(tmp_path):
    data = package(tmp_path).model_dump()
    data["sections"][1].update(level=2, start_on_new_page=True)
    data["sections"][1]["blocks"].insert(0, HeadingBlock(text="2.1.1 现场许可", level=3).model_dump())
    result = render_package(DocumentPackage.model_validate(data), tmp_path / "out")
    doc = Document(result["bid_docx"]["path"])
    target = next(p for p in doc.paragraphs if p.text == data["sections"][1]["title"])
    assert target.style.name == "Heading 2"
    assert target._p.getprevious().xpath('.//w:br[@w:type="page"]')
    assert next(p for p in doc.paragraphs if p.text == "2.1.1 现场许可").style.name == "Heading 3"
    assert all("2.1.1 2.1.1" not in p.text for p in doc.paragraphs)


def test_table_grid_cell_widths_and_borders_match_available_page_area(tmp_path):
    result = render_package(package(tmp_path), tmp_path / "out")
    doc = Document(result["bid_docx"]["path"])
    table = doc.tables[0]
    grid = [int(c.get(qn("w:w"))) for c in table._tbl.tblGrid]
    area = round((doc.sections[-1].page_width - doc.sections[-1].left_margin - doc.sections[-1].right_margin) / 635)
    assert sum(grid) == area
    assert grid[1] / grid[0] == pytest.approx(2.6, rel=0.01)
    assert int(table._tbl.tblPr.find(qn("w:tblW")).get(qn("w:w"))) == area
    assert table._tbl.tblPr.find(qn("w:tblLayout")).get(qn("w:type")) == "fixed"
    assert all(edge.get(qn("w:color")) == "000000" for edge in table._tbl.tblPr.find(qn("w:tblBorders")))
    assert all(int(c._tc.tcPr.tcW.get(qn("w:w"))) == width for c, width in zip(table.rows[0].cells, grid))
    assert table.rows[0]._tr.xpath("./w:trPr/w:tblHeader")
    assert all(paragraph.paragraph_format.keep_with_next for cell in table.rows[0].cells for paragraph in cell.paragraphs)
    for row in table.rows:
        assert row._tr.xpath("./w:trPr/w:cantSplit")
        assert not row._tr.xpath('./w:trPr/w:trHeight[@w:hRule="exact"]')
        for cell in row.cells:
            assert cell.paragraphs[0]._p.xpath('./w:pPr/w:ind[@w:firstLineChars="0"]')


def test_portrait_attachment_keeps_aspect_ratio_inside_body_box(tmp_path):
    data = package(tmp_path).model_dump()
    image = tmp_path / "portrait.png"
    Image.new("RGB", (600, 1800), "white").save(image)
    digest = file_sha256(image)
    data["attachments"][0].update(path=image, sha256=digest)
    data["sources"][2].update(path=image, sha256=digest)
    result = render_package(DocumentPackage.model_validate(data), tmp_path / "out")
    doc = Document(result["bid_docx"]["path"])
    picture = doc.inline_shapes[0]
    assert picture.width / picture.height == pytest.approx(1 / 3, rel=0.001)
    assert picture.width.cm <= 16.01
    assert picture.height.cm <= 19.71


def test_existing_markers_are_canonical_once_and_repeated_cells_use_short_id(tmp_path):
    data = package(tmp_path).model_dump()
    rows = data["sections"][2]["blocks"][0]["table"]["rows"]
    rows[0][1]["text"] = "【待补 M-001：服务总价】"
    rows[1][1].update(text="【待补 M-001】", issue_refs=["M-001"])
    result = render_package(DocumentPackage.model_validate(data), tmp_path / "out")
    table = Document(result["bid_docx"]["path"]).tables[1]
    assert table.cell(1, 1).text == "【待补 M-001】"
    assert table.cell(2, 1).text == "【待补 M-001】"


def test_trace_and_preparation_information_are_kept_out_of_bid_body(tmp_path):
    prepared = create_synthetic_sample(tmp_path / "input")
    result = render_package(package_for(prepared), tmp_path / "out")
    bid = Document(result["bid_docx"]["path"])
    trace = Document(result["issues_docx"]["path"])
    body_text = "\n".join(p.text for p in bid.paragraphs)
    trace_text = "\n".join(p.text for p in trace.paragraphs)
    assert prepared.preparation_note not in body_text
    assert "SHA-256" not in body_text
    assert "响应要求 R-" not in body_text
    assert all(source.sha256 in trace_text and source.source_id in trace_text and source.locator in trace_text for source in prepared.sources)
    assert all(fact.fact_id in trace_text and fact.source_refs[0] in trace_text for fact in prepared.facts)
    assert "制作范围与来源追溯" in trace_text


def test_second_document_failure_never_publishes_a_partial_result(tmp_path, monkeypatch):
    import company_bid.rendering as rendering

    def fail(_package):
        raise OSError("test assembly failure")

    monkeypatch.setattr(rendering, "_render_issues", fail)
    with pytest.raises(OSError, match="test assembly failure"):
        render_package(package(tmp_path), tmp_path / "out")
    assert not list((tmp_path / "out").glob("*.docx"))
