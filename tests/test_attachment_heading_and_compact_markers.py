"""Document presentation keeps every proof page and full companion issue data."""
from copy import deepcopy
import hashlib
from pathlib import Path
from zipfile import ZipFile

from docx import Document
from lxml import etree
from PIL import Image
import pytest

from company_bid.models import DocumentPackage, file_sha256
from company_bid.rendering import render_package
from company_bid.sample import create_synthetic_sample
from test_rendering import package_for


def proof_package(tmp_path, groups, *, role="response", section_title=None, image_size=(60, 80)):
    data = package_for(create_synthetic_sample(tmp_path / "input")).model_dump(mode="json")
    original = data["attachments"][0]
    section = next(item for item in data["sections"] if item["section_id"] == original["section_id"])
    section["document_role"] = role
    if role == "review":
        next(item for item in data["requirements"] if item["requirement_id"] == "R-003")["response_mode"] = "procedure"
    if section_title:
        section["title"] = section_title
    data["attachments"], section["blocks"] = [], []
    for index, (identity, title, count, bundled) in enumerate(groups):
        for page in range(1, count + 1):
            image = tmp_path / f"proof-{index}-{page}.png"
            Image.new("RGB", image_size, (index * 50, page * 30, 90)).save(image)
            attachment_id = f"{identity}-page-{page}" if bundled else identity
            value = {**original, "attachment_id": attachment_id, "title": title,
                "bundle_id": identity if bundled else None, "page_count": count, "page_index": page,
                "path": str(image), "sha256": file_sha256(image), "description": f"Synthetic {identity} {page}/{count}"}
            data["attachments"].append(value)
            section["blocks"].append({"kind": "attachment", "attachment_id": attachment_id})
    return DocumentPackage.model_validate(data)


def rendered_page_hashes(path):
    namespaces = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main",
                  "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    with ZipFile(path) as archive:
        document = etree.fromstring(archive.read("word/document.xml"))
        relations = etree.fromstring(archive.read("word/_rels/document.xml.rels"))
        targets = {item.get("Id"): item.get("Target") for item in relations}
        return [hashlib.sha256(archive.read("word/" + targets[link])).hexdigest()
                for link in document.xpath("//a:blip/@r:embed", namespaces=namespaces)]


@pytest.mark.parametrize("role", ["response", "review"])
def test_heading_is_per_bundle_not_page_or_shared_title_and_every_image_stays(tmp_path, role):
    # Identical titles are distinct bundles. A single's ID matching a bundle
    # ID is also distinct; neither may suppress another actual proof package.
    package = proof_package(tmp_path, [("B", "同名证明包", 3, True),
        ("C", "同名证明包", 2, True), ("B", "独立单页证明", 1, False)], role=role)
    before = deepcopy(package.model_dump(mode="json"))
    output = render_package(package, tmp_path / "output")
    path = Path(output["bid_docx" if role == "response" else "issues_docx"]["path"])
    document = Document(path)
    headings = [p.text for p in document.paragraphs if p.style.name.startswith("Heading")]
    assert headings.count("同名证明包") == 2
    assert headings.count("独立单页证明") == 1
    captions = [p for p in document.paragraphs if "同名证明包（第" in p.text]
    assert [p.text for p in captions] == ["同名证明包（第2/3页）", "同名证明包（第3/3页）", "同名证明包（第2/2页）"]
    assert all(p.style.name == "Normal" for p in captions)
    assert rendered_page_hashes(path) == [a.sha256 for a in package.attachments]
    assert package.model_dump(mode="json") == before


def test_section_title_already_names_bundle_without_another_toc_heading(tmp_path):
    package = proof_package(tmp_path, [("B", "证明材料栏目", 2, True)], section_title="证明材料栏目")
    output = render_package(package, tmp_path / "output")
    document = Document(output["bid_docx"]["path"])
    assert [p.text for p in document.paragraphs if p.style.name.startswith("Heading")].count("证明材料栏目") == 1
    assert any(p.text == "证明材料栏目（第2/2页）" and p.style.name == "Normal" for p in document.paragraphs)
    assert len(document.inline_shapes) == 2


def test_continuation_image_uses_more_page_height_without_resampling_or_cropping(tmp_path):
    package = proof_package(tmp_path, [("B", "合成长图证明包", 2, True)], image_size=(60, 180))
    output = render_package(package, tmp_path / "output")
    doc = Document(output["bid_docx"]["path"])
    first, continuation = doc.inline_shapes
    assert first.height.cm == pytest.approx(19.7, abs=.01)
    assert continuation.height.cm == pytest.approx(22.7, abs=.01)
    assert first.width / first.height == pytest.approx(1 / 3, rel=.001)
    assert continuation.width / continuation.height == pytest.approx(1 / 3, rel=.001)
    assert rendered_page_hashes(output["bid_docx"]["path"]) == [a.sha256 for a in package.attachments]


def test_main_level_one_chapters_start_new_pages_and_lower_levels_remain_continuous(tmp_path):
    data = package_for(create_synthetic_sample(tmp_path / "input")).model_dump(mode="json")
    data["sections"][1]["level"] = 2
    output = render_package(DocumentPackage.model_validate(data), tmp_path / "output")
    doc = Document(output["bid_docx"]["path"])
    for section in data["sections"][1:]:
        heading = next(p for p in doc.paragraphs if p.text == section["title"])
        breaks = heading._p.getprevious().xpath('.//w:br[@w:type="page"]')
        assert bool(breaks) == (section["level"] == 1)


def test_table_marker_is_short_but_full_issue_remains_in_companion(tmp_path):
    package = package_for(create_synthetic_sample(tmp_path / "input"))
    data = package.model_dump(mode="json")
    issue = data["issues"][0]
    issue["title"] = "服务报价表第2行第2列原格式完整长标签" * 8
    table = next(b["table"] for s in data["sections"] for b in s["blocks"]
                 if b["kind"] == "table" and b["table"]["table_id"] == "T-PRICE")
    table["rows"][0][1]["text"] = "原单元格说明【待补 M-001：" + issue["title"] + "】"
    table["rows"][1][1]["issue_refs"] = ["M-001"]
    updated = DocumentPackage.model_validate(data)
    before = deepcopy(updated.model_dump(mode="json"))
    output = render_package(updated, tmp_path / "output")
    bid = Document(output["bid_docx"]["path"])
    assert bid.tables[1].cell(1, 1).text == "原单元格说明【待补 M-001】"
    assert "【待补 M-001】" in bid.tables[1].cell(2, 1).text
    assert issue["title"] not in "\n".join(cell.text for row in bid.tables[1].rows for cell in row.cells)
    companion = "\n".join(p.text for p in Document(output["issues_docx"]["path"]).paragraphs)
    for text in (issue["title"], issue["reason"], issue["action"], issue["source_requirement"]):
        assert text in companion
    assert updated.model_dump(mode="json") == before


def test_table_issue_does_not_repeat_as_a_standalone_block_but_other_issue_remains(tmp_path):
    data = package_for(create_synthetic_sample(tmp_path / "input")).model_dump(mode="json")
    section = next(item for item in data["sections"] if item["section_id"] == "S3")
    original = data["issues"][0]
    data["issues"].append({**original, "issue_id": "M-002", "title": "只有独立位置的缺项", "action": "独立事项仍须办理。"})
    section["blocks"].extend([{"kind": "issue", "issue_id": "M-001", "requirement_refs": ["R-004"]},
                              {"kind": "issue", "issue_id": "M-002", "requirement_refs": ["R-004"]}])
    package = DocumentPackage.model_validate(data)
    output = render_package(package, tmp_path / "output")
    bid = Document(output["bid_docx"]["path"])
    body = "\n".join(p.text for p in bid.paragraphs)
    assert "M-001" not in body and "【待补 M-001】" in bid.tables[1].cell(1, 1).text
    assert "【待补 M-002：只有独立位置的缺项】" in body
    assert "独立事项仍须办理。" in body
    companion = "\n".join(p.text for p in Document(output["issues_docx"]["path"]).paragraphs)
    assert "M-001 服务总价" in companion and original["action"] in companion
    assert "M-002 只有独立位置的缺项" in companion and "独立事项仍须办理。" in companion


def test_checklist_reuses_requirement_text_and_keeps_noncanonical_source_once(tmp_path):
    data = package_for(create_synthetic_sample(tmp_path / "input")).model_dump(mode="json")
    original = data["issues"][0]
    requirement = next(item for item in data["requirements"] if item["requirement_id"] == "R-004")
    original["source_requirement"] = requirement["text"]
    extra_source = "独立冻结来源补充：此文本不等同于任何要求对象，仍须完整可检索。"
    for number in (2, 3):
        identity = f"M-{number:03d}"
        data["issues"].append({**original, "issue_id": identity, "title": f"补充来源事项{number}", "source_requirement": extra_source})
        next(item for item in data["sections"] if item["section_id"] == "S3")["blocks"].append(
            {"kind": "issue", "issue_id": identity, "requirement_refs": ["R-004"]})
    # A procedure action's source is already authenticated as its requirement
    # text by the snapshot contract; the checklist must reference that same RID.
    procedure = next(item for item in data["requirements"] if item["requirement_id"] == "R-003")
    procedure["response_mode"] = "procedure"
    data["review_actions"] = [{"action_id": "ACT-TEST", "kind": "procedure", "status": "pending_manual",
        "requirement_refs": ["R-003"], "source_refs": procedure["source_refs"],
        "source_requirement": procedure["text"], "instruction": "办理这个独立程序事项并保存记录。"}]
    package = DocumentPackage.model_validate(data)
    before = deepcopy(package.model_dump(mode="json"))
    output = render_package(package, tmp_path / "output")
    text = "\n".join(p.text for p in Document(output["issues_docx"]["path"]).paragraphs)
    assert text.count(requirement["text"]) == 1
    assert text.count(procedure["text"]) == 1
    assert text.count(extra_source) == 1
    assert "SRCQ-001" in text and "响应要求”原文附录：R-004" in text
    assert "ACT-TEST" in text and "待线下办理" in text
    assert "办理这个独立程序事项并保存记录。" in text
    assert "【待补 M-001：服务总价】" not in text
    assert all(item in text for item in (original["reason"], original["action"], "来源定位：S-TENDER"))
    assert package.model_dump(mode="json") == before
