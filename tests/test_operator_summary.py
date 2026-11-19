from hashlib import sha256

from company_bid.models import DocumentPackage
from company_bid.operator_summary import build_operator_summary
from company_bid.review_document import task_list_catalog


def _package(tmp_path, *rows, tables=None, attachments=None, facts=(), extra_sources=()):
    requirements, issues, actions, sections, sources = [], [], [], [], []
    for n, (text, category, mode, title) in enumerate(rows, 1):
        rid, sid, source_id = f"R-{n}", f"SEC-{n}", f"SRC-{n}"
        path = tmp_path / f"source-{n}.txt"
        path.write_text(text, encoding="utf-8")
        digest = sha256(path.read_bytes()).hexdigest()
        sources.append(dict(source_id=source_id, title="采购原件", path=path, sha256=digest, kind="tender",
                            locator=f"pdf/page[{n}]/line[0]; quote_sha256={digest}", synthetic=True, provenance="synthetic"))
        requirements.append(dict(requirement_id=rid, text=text, category=category, response_mode=mode, source_refs=[source_id]))
        if mode == "procedure":
            actions.append(dict(action_id=f"RA-{n}", kind="procedure" if category == "procedure" else "commercial_constraint",
                                status="pending_manual" if category == "procedure" else "not_checked", requirement_refs=[rid],
                                source_refs=[source_id], source_requirement=text, instruction="保留原动作并核对。"))
            sections.append(dict(section_id=sid, title=title, document_role="review", render_body=False, requirement_refs=[rid]))
            continue
        iid = f"M-{n:03d}"
        blocks = [dict(kind="paragraph", text="响应内容待完善。", requirement_refs=[rid]),
                  dict(kind="issue", issue_id=iid, requirement_refs=[rid])]
        issues.append(dict(issue_id=iid, title=title + "待核对", section_id=sid, requirement_refs=[rid],
                           reason="此诊断内容只出现在明细。", action="依本条招标原件复核。", source_requirement=text))
        sections.append(dict(section_id=sid, title=title, requirement_refs=[rid], blocks=blocks))
    if tables:
        for section_index, table, extra_issues in tables:
            sections[section_index - 1]["blocks"].append(dict(kind="table", table=table))
            issues.extend(extra_issues)
    if attachments:
        for attachment in attachments:
            section = next(s for s in sections if s["section_id"] == attachment["section_id"])
            section["blocks"].append(dict(kind="attachment", attachment_id=attachment["attachment_id"]))
    return DocumentPackage.model_validate(dict(schema_version="1.0", synthetic=True, title="测试标书", preparation_note="测试",
            project=dict(project_id="TEST", name="测试招标", source_refs=["SRC-1"]),
            company=dict(company_id="TEST", name="测试企业", source_refs=["SRC-1"]),
            sources=[*sources, *extra_sources], sections=sections, requirements=requirements, facts=facts,
            issues=issues, review_actions=actions, attachments=attachments or []))


def _summary(package):
    catalog = task_list_catalog(package)
    before = package.model_dump(mode="json")
    result = build_operator_summary(package, catalog)
    assert package.model_dump(mode="json") == before
    assert result == build_operator_summary(package, catalog)
    assert {i.issue_id for i in package.issues} == set(result["other_issue_ids"]) | {
        ident for item in result["items"] for ident in item["issue_ids"]}
    assert {a.action_id for a in package.review_actions} == set(result["other_action_ids"]) | {
        ident for item in result["items"] for ident in item["action_ids"]}
    for item in result["items"]:
        assert set(item["primary_quote_ids"]) <= set(item["quote_ids"]) <= {quote["display_id"] for quote in catalog["entries"]}
        assert set(item["primary_source_refs"]) <= set(item["source_refs"])
        assert item["contact"]
        assert "此诊断内容" not in str(item)
    return {item["key"]: item for item in result["items"]}, result


def test_personnel_respects_explicit_project_inputs_and_tender_terms(tmp_path):
    social = "投标人应提供拟派项目负责人在投标截止时间前75日内至少缴纳两次的社会保险实缴证明，须载明用工主体。"
    source = dict(source_id="F-REVIEW-" + "a" * 64, title="确认记录", path=tmp_path / "reviewed.json", sha256="a" * 64,
                  kind="manual_input", provenance="manual_input", locator="本项目写标前补录", synthetic=True)
    old = dict(source_id="HISTORICAL", title="历史标书", path=tmp_path / "old.json", sha256="b" * 64,
               kind="company_material", provenance="derived_from_bid", locator="旧标书人员表", synthetic=True)
    package = _package(tmp_path, (social, "qualification", "attachment", "人员要求材料"),
                       ("递交投标文件截止时间：2026年12月20日09时30分。", "procedure", "procedure", "正式截止"),
                       extra_sources=(source, old), facts=(
                           dict(fact_id="PROJECT-ANSWER-manager_name-current", label="本项目拟派负责人", value="当前拟派甲", source_refs=[source["source_id"]]),
                           dict(fact_id="PROJECT-ANSWER-manager_name-history", label="本项目拟派负责人", value="历史人员乙", source_refs=[old["source_id"]]),
                       ))
    summary, _ = _summary(package)
    personnel = summary["personnel"]
    assert "当前拟派甲" in str(personnel["available"])
    assert "历史人员乙" not in str(personnel)
    assert "75 日" in " ".join(personnel["next_steps"]) and "实缴 2 次" in " ".join(personnel["next_steps"])
    assert "120" not in str(personnel)
    assert personnel["destinations"] == ["人员要求材料"]
    assert summary["submission"]["available"] == ["原文正式截止为北京时间 2026-12-20 09:30。"]
    assert summary["submission"]["action_ids"] == ["RA-2"]


def test_negative_topics_and_historical_names_stay_out_of_summary(tmp_path):
    package = _package(tmp_path,
        ("本项目无需填报项目负责人姓名，也无须提供拟派项目负责人社保实缴证明。", "qualification", "attachment", "人员要求材料"),
        ("本项目无需提供营业执照和资质证书。", "qualification", "attachment", "资质材料"),
        ("本项目无需提交类似项目业绩证明。", "qualification", "attachment", "业绩材料"),
        ("投标文件不得上传至非官方电子平台。", "procedure", "procedure", "非官方渠道禁用"),
        ("投标文件应由法定代表人本人签署。不得委托代理人签署。", "procedure", "procedure", "签署规则"),
        ("由法定代表人签署时，无需提供身份证明。", "qualification", "attachment", "法定代表人身份证明"),
        ("授权委托书样例。无需提供授权委托书。", "qualification", "attachment", "授权委托书"))
    summary, result = _summary(package)
    assert set(summary) == {"signing"}
    assert summary["signing"]["action_ids"] == ["RA-5"]
    assert "代理人" not in " ".join(summary["signing"]["next_steps"])
    assert "附其身份证明" not in " ".join(summary["signing"]["next_steps"])
    assert "法定代表人身份证明" not in summary["signing"]["destinations"]
    assert "M-007" in result["other_issue_ids"]


def test_quote_group_honors_original_table_fields_and_strict_price_comparator(tmp_path):
    table_quote = "本项目应填写投标报价表。"
    table = dict(table_id="T", section_id="SEC-1", title="投标报价表", columns=["字段", "输入"],
                 rows=[[{"text": "报价"}, {"issue_refs": ["M-010"]}], [{"text": "服务期限"}, {"issue_refs": ["M-011"]}],
                       [{"text": "备注"}, {"issue_refs": ["M-012"]}]], requirement_refs=["R-1"], source_refs=["SRC-1"])
    table_issues = [dict(issue_id=f"M-{n:03}", title=f"原表第{n - 8}行待填", section_id="SEC-1", requirement_refs=["R-1"],
                         reason="未提供", action="填写本次情况", source_requirement=table_quote) for n in range(10, 13)]
    package = _package(tmp_path, (table_quote, "commercial", "table", "投标报价表"),
                       ("本项目暂估价 17.5 万元（不含税），采购人只接受小于暂估价的报价，否则视为无效报价。", "commercial", "procedure", "报价标准"),
                       ("本项目不要求填报报价分项。", "commercial", "procedure", "分项豁免"), tables=[(1, table, table_issues)])
    summary, result = _summary(package)
    quote = summary["quotation"]
    assert quote["action_ids"] == ["RA-2"]
    assert quote["primary_source_refs"][0] == "SRC-2"
    assert quote["issue_ids"] == ["M-001", "M-010"]
    assert set(result["other_issue_ids"]) == {"M-011", "M-012"}
    assert "严格低于 17.5 万元" in " ".join(quote["next_steps"])
    assert "分项" not in " ".join(quote["next_steps"])


def test_performance_proof_is_candidate_and_qualification_path_stays_distinct(tmp_path):
    eligibility = "1 业绩要求 2024年7月1日至投标截止日，投标人至少有2项测绘业绩。须提供合同主要页或中标通知书。"
    scoring = "投标单位业绩评分：每项加2分。应提供合同主要页或验收文件。"
    proof_source = dict(source_id="OLD", kind="company_material", title="历史证明页", path=tmp_path / "old.png",
                        sha256="c" * 64, locator="docx/media/image5.png", synthetic=True, provenance="derived_from_bid")
    attachment = dict(attachment_id="A-1", section_id="SEC-1", title="旧项目材料", description="证明候选", path=proof_source["path"],
                      sha256=proof_source["sha256"], requirement_refs=["R-1"], source_refs=["OLD"], synthetic=True)
    package = _package(tmp_path, (eligibility, "qualification", "attachment", "业绩材料"),
                       (scoring, "commercial", "attachment", "业绩评分"), attachments=[attachment], extra_sources=[proof_source])
    summary, _ = _summary(package)
    performance = summary["performance"]
    assert performance["issue_ids"] == ["M-001", "M-002"]
    assert performance["evidence_source_refs"] == ["OLD"]
    assert "候选" in " ".join(performance["available"]) and "已附" not in str(performance)
    assert "至少 2 项测绘业绩" in " ".join(performance["next_steps"])
    assert "资格按合同主要页或中标通知书；评分按该条允许的合同主要页或验收文件" in " ".join(performance["next_steps"])


def test_joint_or_excluded_evidence_does_not_become_alternative(tmp_path):
    package = _package(tmp_path,
        ("业绩要求：投标人应同时提供合同主要页和中标通知书。", "qualification", "attachment", "业绩材料"),
        ("投标单位业绩评分：每项加2分。须同时提交合同主要页和验收文件。", "commercial", "attachment", "业绩评分"),
        ("投标单位业绩评分：每项加1分。验收文件不得替代合同主要页。", "commercial", "attachment", "补充评分"))
    summary, _ = _summary(package)
    assert "或" not in " ".join(summary["performance"]["next_steps"])
