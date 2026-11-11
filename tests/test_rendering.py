from pathlib import Path
from zipfile import ZipFile

import pytest
from docx import Document
from pydantic import ValidationError

from company_bid.models import (
    AttachmentBlock,
    DocumentPackage,
    ListBlock,
    ParagraphBlock,
    Section,
    TableBlock,
    file_sha256,
)
from company_bid.rendering import RenderingError, render_package
from company_bid.sample import create_synthetic_sample


def package_for(prepared):
    sections = []
    for plan in prepared.sections:
        blocks = []
        if plan.kind == "technical":
            blocks = [
                ParagraphBlock(text="拟在现场核对设备边界和作业许可，完成隔离挂牌及许可确认后再实施巡检。", content_role="proposal", requirement_refs=plan.requirement_refs),
                ListBlock(items=["拟按设备位号与测点顺序编号，记录测点位置和测量条件。", "拟对异常值保持同一测点复测并比较工况，记录复测结果和处置建议。", "拟交付测点记录、异常清单和移交签收记录。"], requirement_refs=plan.requirement_refs),
            ]
        for table_id in plan.table_refs:
            blocks.append(TableBlock(table=next(table for table in prepared.tables if table.table_id == table_id)))
        for attachment_id in plan.attachment_refs:
            blocks.append(AttachmentBlock(attachment_id=attachment_id))
        sections.append(Section(section_id=plan.section_id, title=plan.title, requirement_refs=plan.requirement_refs, blocks=blocks))
    return DocumentPackage(**prepared.model_dump(exclude={"input_kind", "sections", "tables"}), title="工业设备巡检项目投标文件", sections=sections)


def test_editable_text_tables_actual_image_fields_and_two_way_missing_ids(tmp_path):
    prepared = create_synthetic_sample(tmp_path / "input")
    package = package_for(prepared)
    output = render_package(package, tmp_path / "output")
    for key in ["bid_docx", "issues_docx"]:
        path = Path(output[key]["path"])
        assert path.is_file()
        assert file_sha256(path) == output[key]["sha256"]
        assert path.stat().st_size == output[key]["size_bytes"]
    bid = Document(output["bid_docx"]["path"])
    assert len(bid.tables) == 2
    assert [cell.text for cell in bid.tables[0].rows[0].cells] == ["字段", "填报内容"]
    assert "【待补 M-001：服务总价】" in bid.tables[1].cell(1, 1).text
    text = "\n".join(p.text for p in bid.paragraphs)
    assert "拟在现场核对设备边界" in text
    assert "合成测试样本" in text
    assert prepared.sources[0].sha256 in text
    assert prepared.sources[0].locator in text
    assert "F-NAME" in text
    issues = Document(output["issues_docx"]["path"])
    issues_text = "\n".join(p.text for p in issues.paragraphs)
    assert "【待补 M-001：服务总价】" in issues_text
    assert "三 服务报价（S3）" in issues_text
    with ZipFile(output["bid_docx"]["path"]) as archive:
        media = [name for name in archive.namelist() if name.startswith("word/media/")]
        assert len(media) == 1
        assert archive.read(media[0]) == prepared.attachments[0].path.read_bytes()
        xml = archive.read("word/document.xml").decode()
        assert 'TOC \\o "1-3"' in xml
        assert "w:tblHeader" in xml
        assert "w:drawing" in xml
        assert "w:documentProtection" not in archive.read("word/settings.xml").decode()
        assert "PAGE" in archive.read("word/footer1.xml").decode()


def test_existing_result_is_not_overwritten(tmp_path):
    package = package_for(create_synthetic_sample(tmp_path / "input"))
    output = render_package(package, tmp_path / "output")
    before = Path(output["bid_docx"]["path"]).read_bytes()
    with pytest.raises(FileExistsError):
        render_package(package, tmp_path / "output")
    assert Path(output["bid_docx"]["path"]).read_bytes() == before


def test_changed_source_fails_before_any_docx_is_published(tmp_path):
    prepared = create_synthetic_sample(tmp_path / "input")
    prepared.sources[0].path.write_text("modified source", encoding="utf-8")
    with pytest.raises(RenderingError, match="source hash mismatch"):
        render_package(package_for(prepared), tmp_path / "output")
    assert not list((tmp_path / "output").glob("*.docx"))


def test_unsupported_attachment_does_not_fake_success(tmp_path):
    prepared = create_synthetic_sample(tmp_path / "input")
    bad = tmp_path / "not-supported.pdf"
    bad.write_bytes(b"%PDF-1.4\nnot a supported M1 image")
    data = package_for(prepared).model_dump()
    data["attachments"][0].update(path=bad, sha256=file_sha256(bad))
    with pytest.raises(RenderingError, match="PNG/JPEG only"):
        render_package(DocumentPackage.model_validate(data), tmp_path / "output")
    assert not list((tmp_path / "output").glob("*.docx"))


def test_disguised_attachment_is_rejected(tmp_path):
    prepared = create_synthetic_sample(tmp_path / "input")
    bad = tmp_path / "disguised.png"
    bad.write_bytes(b"not actually a PNG")
    data = package_for(prepared).model_dump()
    data["attachments"][0].update(path=bad, sha256=file_sha256(bad))
    with pytest.raises(RenderingError, match="attachment unreadable"):
        render_package(DocumentPackage.model_validate(data), tmp_path / "output")


def test_missing_body_issue_marker_is_rejected(tmp_path):
    package = package_for(create_synthetic_sample(tmp_path / "input"))
    data = package.model_dump()
    data["sections"][2]["blocks"][0]["table"]["rows"][0][1]["issue_refs"] = []
    with pytest.raises(ValidationError, match="matching body marker"):
        DocumentPackage.model_validate(data)


def test_missing_actual_attachment_block_is_rejected(tmp_path):
    data = package_for(create_synthetic_sample(tmp_path / "input")).model_dump()
    data["sections"][3]["blocks"] = [{"kind": "paragraph", "text": "附件 A-PROOF"}]
    with pytest.raises(ValidationError, match="embedded exactly once"):
        DocumentPackage.model_validate(data)


def test_fixed_table_body_merge_is_preserved_in_editable_word_table(tmp_path):
    data = package_for(create_synthetic_sample(tmp_path / "input")).model_dump()
    table = data["sections"][0]["blocks"][0]["table"]
    table["rows"].append([{"text": "合计总价"}, {"text": ""}])
    table["merges"] = [{"row": 2, "column": 0, "col_span": 2}]
    output = render_package(DocumentPackage.model_validate(data), tmp_path / "output")
    doc = Document(output["bid_docx"]["path"])
    row = doc.tables[0].rows[3]
    assert row.cells[0]._tc is row.cells[1]._tc
    assert "合计总价" in row.cells[0].text
