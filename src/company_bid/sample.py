"""Generate unmistakably synthetic M1 fixtures at runtime outside Git."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .models import (
    Attachment,
    Company,
    Fact,
    Issue,
    PreparedBid,
    Project,
    Requirement,
    SectionPlan,
    SourceRef,
    TableCell,
    TableSpec,
    file_sha256,
)


def _proof_image(path: Path):
    image = Image.new("RGB", (1500, 960), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("arial.ttf", 56)
        small = ImageFont.truetype("arial.ttf", 35)
    except OSError:
        font = ImageFont.load_default(size=56)
        small = ImageFont.load_default(size=35)
    draw.rectangle((30, 30, 1470, 930), outline="#b00000", width=12)
    draw.text((85, 90), "SYNTHETIC TEST ONLY", font=font, fill="#b00000")
    draw.text((85, 180), "NOT A CERTIFICATE", font=font, fill="#b00000")
    draw.text((85, 310), "Fictional inspection process record", font=small, fill="black")
    for index, text in enumerate([
        "1  Work permit and isolation check",
        "2  Numbered inspection points",
        "3  Abnormal value repeat measurement",
        "4  Records and issue list handover",
    ]):
        draw.text((110, 420 + index * 95), text, font=small, fill="#333333")
    draw.text((85, 850), "No real company, qualification, signature or seal", font=small, fill="#b00000")
    with path.open("xb") as stream:
        image.save(stream, format="PNG")


def create_synthetic_sample(directory: Path) -> PreparedBid:
    """Create fresh synthetic source files and return manually prepared input.

    Existing files are never overwritten. The picture proves image assembly,
    and does not pretend to prove a company's actual qualification.
    """
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    tender = directory / "synthetic-tender.txt"
    company = directory / "synthetic-company-input.txt"
    proof = directory / "synthetic-process-proof.png"
    if any(path.exists() for path in [tender, company, proof]):
        raise FileExistsError("synthetic fixture already exists; use a fresh directory")
    tender_text = "\n".join([
        "合成测试招标要求 不对应真实项目 不得用于投标",
        "项目名称：虚构工业设备巡检服务项目。",
        "R-001：按投标人基本情况表填写名称与资料属性，保持字段顺序。",
        "R-002：针对拟巡检设备编制实施方法，说明作业隔离与许可、测点编号、异常复测、记录和成果交付。",
        "R-003：提供合成流程演练记录图，用于验证证明图片实际装入 Word；此图不作为资格证明。",
        "R-004：填写服务报价表，列明服务总价和报价依据；报价不得由系统估算。",
        "固定表格一：字段｜填报内容；字段顺序为投标人名称、资料属性。",
        "固定表格二：报价项目｜金额及说明；字段为服务总价、报价依据。",
    ]) + "\n"
    company_text = "\n".join([
        "合成测试人工输入 不对应实际公司事实",
        "公司名称：虚构示例巡检技术有限公司（合成测试）。",
        "资料属性：仅用于软件验证的虚构主体，不构成实际资格或履约能力声明。",
        "公司证明图片为合成流程演练记录，不是证照，不包含真实印章。",
        "未提供服务报价。",
    ]) + "\n"
    with tender.open("x", encoding="utf-8") as stream:
        stream.write(tender_text)
    with company.open("x", encoding="utf-8") as stream:
        stream.write(company_text)
    _proof_image(proof)
    sources = [
        SourceRef(source_id="S-TENDER", title="合成巡检项目要求", path=tender, sha256=file_sha256(tender), locator="第2至8行", kind="tender", provenance="synthetic", synthetic=True),
        SourceRef(source_id="S-COMPANY", title="合成公司人工输入", path=company, sha256=file_sha256(company), locator="第2至5行", kind="manual_input", provenance="synthetic", synthetic=True),
        SourceRef(source_id="S-PROOF", title="合成流程演练记录图片", path=proof, sha256=file_sha256(proof), locator="整幅图像", kind="attachment", provenance="synthetic", synthetic=True),
    ]
    facts = [
        Fact(fact_id="F-NAME", label="投标人名称", value="虚构示例巡检技术有限公司（合成测试）", source_refs=["S-COMPANY"]),
        Fact(fact_id="F-TYPE", label="资料属性", value="仅用于软件验证的虚构主体，不构成实际资格或履约能力声明", source_refs=["S-COMPANY"]),
    ]
    requirements = [
        Requirement(requirement_id="R-001", text="按投标人基本情况表填写名称与资料属性，保持字段顺序。", category="fixed_table", source_refs=["S-TENDER"]),
        Requirement(requirement_id="R-002", text="说明巡检作业隔离与许可、测点编号、异常复测、记录和成果交付。", category="technical", source_refs=["S-TENDER"]),
        Requirement(requirement_id="R-003", text="实际装入合成流程演练记录图片，且不作为资格证明。", category="procedure", source_refs=["S-TENDER"]),
        Requirement(requirement_id="R-004", text="服务报价表填写服务总价和报价依据，不由系统估算。", category="commercial", source_refs=["S-TENDER"]),
    ]
    tables = [
        TableSpec(table_id="T-BASIC", section_id="S1", title="投标人基本情况表", columns=["字段", "填报内容"], rows=[
            [TableCell(text="投标人名称"), TableCell(text=facts[0].value, fact_refs=["F-NAME"])],
            [TableCell(text="资料属性"), TableCell(text=facts[1].value, fact_refs=["F-TYPE"])],
        ], source_refs=["S-TENDER", "S-COMPANY"], requirement_refs=["R-001"], column_widths=[1, 2.6]),
        TableSpec(table_id="T-PRICE", section_id="S3", title="服务报价表", columns=["报价项目", "金额及说明"], rows=[
            [TableCell(text="服务总价"), TableCell(issue_refs=["M-001"])],
            [TableCell(text="报价依据"), TableCell(text="人工填写并核对价格、税费和服务范围。")],
        ], source_refs=["S-TENDER", "S-COMPANY"], requirement_refs=["R-004"], column_widths=[1, 2.6]),
    ]
    return PreparedBid(
        synthetic=True,
        preparation_note="本输入由人工准确整理的合成样本构成，不是自动解析结果。公司、项目和证明图片均为虚构，仅用于 M1 软件验证。报价缺项必须线下补充。",
        project=Project(project_id="P-SYNTHETIC", name="虚构工业设备巡检服务项目", source_refs=["S-TENDER"]),
        company=Company(company_id="C-SYNTHETIC", name=facts[0].value, source_refs=["S-COMPANY"]),
        sources=sources,
        facts=facts,
        requirements=requirements,
        sections=[
            SectionPlan(section_id="S1", title="一 投标人基本情况", kind="facts", requirement_refs=["R-001"], fact_refs=["F-NAME", "F-TYPE"], table_refs=["T-BASIC"]),
            SectionPlan(section_id="S2", title="二 巡检拟实施方案", kind="technical", requirement_refs=["R-002"]),
            SectionPlan(section_id="S3", title="三 服务报价", kind="commercial", requirement_refs=["R-004"], table_refs=["T-PRICE"]),
            SectionPlan(section_id="S4", title="四 合成流程演练记录附件", kind="attachments", requirement_refs=["R-003"], attachment_refs=["A-PROOF"]),
        ],
        tables=tables,
        attachments=[Attachment(attachment_id="A-PROOF", section_id="S4", title="合成流程演练记录图", path=proof, sha256=file_sha256(proof), source_refs=["S-PROOF"], requirement_refs=["R-003"], synthetic=True, description="显著标注 SYNTHETIC TEST ONLY 和 NOT A CERTIFICATE 的合成流程图。仅验证附件装配，不证明真实公司资质。")],
        issues=[Issue(issue_id="M-001", title="服务总价", section_id="S3", requirement_refs=["R-004"], reason="合成人工输入未提供服务总价，系统不估算价格。", action="在服务报价表填写经授权的总价，核对税费、计价口径和服务范围后线下签章。", source_requirement="合成要求 R-004 规定服务总价和报价依据必须填写。")],
    )
