from copy import deepcopy
from hashlib import sha256

import pytest

from company_bid.models import PreparedBid
from company_bid.project_answers import ProjectAnswerError, apply_project_answers
from company_bid.project_questions import (
    PERSONNEL_ROLES,
    QUALIFICATION_PERSONNEL,
    build_project_questions,
)
from company_bid.project_review import (
    ProjectReviewExecutor,
    project_review_input,
    review_form,
    review_summary,
    submit_answers,
)
from company_bid.runtime import NodeSpec


MANAGER, TECHNICAL, SITE = list(PERSONNEL_ROLES)[1:]
MONTH_RULE = (
    "投标截止日前近 6 个月内任意一个月本单位缴纳社保的证明材料"
    "（保险证明需体现：工伤、失业、养老并加盖社保公章）"
)
QUALIFICATION = (
    "8.1 项目经理（负责人）资格要求：1 人，"
    "①自 2022 年 9 月 1 日起作为项目经理（负责人）参与同类项目业绩，须提供证明材料：合同主要页；\n"
    "②提供本单位劳动合同；\n"
    "③需提供" + MONTH_RULE + "\n"
    "（如是退休返聘人员，需提供退休证明和返聘劳务合同）；"
)
SCORING = (
    "项目组织\n机构\n（7 分）\t"
    "1. 项目负责人具有 AMPP（原 NACE）阴极保护认证体系中 CP1 级证书或中国阴极保护协会 CPL1 级证书得 2 分；"
    "具有 AMPP（原 NACE）阴极保护认证体系中 CP2 级证书或中国阴极保护协会 CPL2 级及以上证书得 3 分，满分 3 分；\n"
    "2. 配备 1 名具有中级及以上职称证书的技术负责人得 1 分，"
    "每多配备 1 名符合上述要求的技术负责人加 1 分，满分 2 分；\n"
    "3. 需配备现场工作人员，每提供 1 人得 1 分，满分 2 分。"
    "需在响应文件技术服务项目组成员表中列明。\n"
    "（以上人员须提供" + MONTH_RULE + "）；"
    "注：以上相关人员需提供证件扫描件加盖公章，不提供的不得分；\t7"
)
DEADLINE = "递交响应文件截止时间和地点\t详见采购公告"
PLATFORM_DEADLINE = (
    "5.1 响应文件递交截止时间：详见合成电子招标平台（网址：htt\n"
    "ps://example.test/home/）公告；"
)
FORM = "其他表格\n项目负责人\t\n备注\t"


def _source(ref, locator, *, kind="tender", provenance="original"):
    return {"source_id": ref, "title": ref, "path": ref + ".pdf", "sha256": "1" * 64,
            "locator": locator, "kind": kind, "provenance": provenance}


def _quote(ref, text, locator):
    return _source(ref, f"{locator}; quote_sha256={sha256(text.encode()).hexdigest()}")


def sample(qualification=QUALIFICATION, scoring=SCORING, deadline=DEADLINE):
    requirements = [
        ("QUAL", qualification, "qualification", "attachment", "pdf/page[3]/line[7]"),
        ("SCORE", scoring, "technical", "writing", "pdf/page[8]/table[2]"),
        ("DATE", deadline, "commercial", "fixed_text", "pdf/page[4]/line[5]"),
        ("FORM", FORM, "commercial", "table", "pdf/page[9]/table[0]"),
    ]
    sources = [_quote("S-" + rid, text, locator) for rid, text, _, _, locator in requirements]
    sources += [_source("SNAP", "填报记录", kind="manual_input", provenance="manual_input"),
                _source("RET", "证明候选", kind="company_material"),
                _source("OTHER", "其他资料", kind="company_material")]
    plan = {
        "input_kind": "automatic", "preparation_note": "合成问题测试",
        "project": {"project_id": "P", "name": "样本", "source_refs": ["S-QUAL"]},
        "company": {"company_id": "C", "name": "样本公司", "source_refs": ["S-QUAL"]},
        "sources": sources, "facts": [], "review_actions": [], "issues": [], "tables": [],
        "requirements": [{"requirement_id": rid, "text": text, "category": category, "response_mode": mode,
                          "source_refs": ["S-" + rid]} for rid, text, category, mode, _ in requirements],
        "sections": [{"section_id": rid, "title": rid, "kind": "facts", "requirement_refs": [rid],
                      "table_refs": ["UNRELATED"] if rid == "FORM" else [],
                      "attachment_refs": ["ATT"] if rid == "QUAL" else []}
                     for rid, *_ in requirements],
        "attachments": [{"attachment_id": "ATT", "section_id": "QUAL", "title": "资格范围证明候选",
                         "path": "RET.png", "sha256": "1" * 64, "description": "合成资料",
                         "requirement_refs": ["QUAL"], "source_refs": ["RET"]}],
        "prescribed_cover_section_id": None,
    }
    plan["tables"].append({"table_id": "UNRELATED", "section_id": "FORM", "title": "其他表格",
                           "columns": ["字段", "填写项"], "rows": [[{"text": "项目负责人"}, {"text": ""}]],
                           "source_refs": ["S-FORM"], "requirement_refs": ["FORM"]})
    return PreparedBid.model_validate(plan).model_dump(mode="json")


def _by_role(plan):
    return {item["key"]: item for item in build_project_questions(plan) if item["key"] in PERSONNEL_ROLES}


@pytest.mark.parametrize("scoring", [
    SCORING,
    "其他评分项\t资格另有退休要求\t2\n" + SCORING + "\n合计\t\t7",
])
def test_month_rules_bind_independent_roles_and_exact_source_scope(scoring):
    plan = sample(scoring=scoring)
    rows = _by_role(plan)
    assert set(rows) == set(PERSONNEL_ROLES)
    assert rows[QUALIFICATION_PERSONNEL]["requirement_ids"] == ["QUAL"]
    assert rows[QUALIFICATION_PERSONNEL]["source_refs"] == ["S-QUAL"]
    assert rows[QUALIFICATION_PERSONNEL]["conditions"]["required_people"] == 1
    assert rows[QUALIFICATION_PERSONNEL]["conditions"]["requires_similar_project_experience_proof"]
    assert rows[QUALIFICATION_PERSONNEL]["conditions"]["retirement_rehire"]["required_proofs"] == ["退休证明", "返聘劳务合同"]
    for key, row in rows.items():
        assert row["conditions"]["window_months"] == 6
        assert row["conditions"]["months_to_prove"] == 1
        assert "window_days" not in row["conditions"]
        if key != QUALIFICATION_PERSONNEL:
            assert row["requirement_ids"] == ["SCORE"]
            assert row["source_refs"] == ["S-SCORE"]
            assert "required_people" not in row["conditions"]
            assert "retirement_rehire" not in row["conditions"]
    assert rows[TECHNICAL]["conditions"]["people_for_max_points"] == 2
    assert rows[SITE]["conditions"]["people_for_max_points"] == 2
    questions = {item["key"]: item for item in build_project_questions(plan)}
    assert questions["deadline"]["status"] == "unresolved"
    assert "payment_evidence" not in questions
    assert questions["manager_name"]["requirement_ids"] == ["FORM"]


@pytest.mark.parametrize(("qualification", "scoring"), [
    (QUALIFICATION.split("③")[0], SCORING.replace("以上人员须提供", "仅现场人员须提供")),
    (QUALIFICATION.split("③")[1], SCORING.replace("符合上述要求的", "不满足条件的")),
    ("示例：" + QUALIFICATION, SCORING.replace("项目组织", "其他事项")),
    (QUALIFICATION.replace("需提供退休证明", "无需提供退休证明"), SCORING.replace("加1分", "加2分").replace("加 1 分", "加 2 分")),
    (QUALIFICATION.replace("资格要求：1", "资格要求：2"), SCORING + "\n" + SCORING),
    (QUALIFICATION.split("③")[1], SCORING.replace("符合上述要求的", "具有任意经历的")),
])
def test_partial_negated_or_ambiguous_terms_do_not_invent_roles(qualification, scoring):
    assert _by_role(sample(qualification=qualification, scoring=scoring)) == {}


def test_manual_provenance_hash_mismatch_and_wrong_layout_are_excluded():
    plan = sample()
    plan["sources"][0]["provenance"] = "manual_input"
    plan["sources"][1]["locator"] = plan["sources"][1]["locator"].replace("table[2]", "line[2]")
    assert _by_role(plan) == {}
    wrong = sample()
    for source in wrong["sources"][:2]:
        source["locator"] = source["locator"].split(";")[0] + "; quote_sha256=" + "0" * 64
    assert _by_role(wrong) == {}
    plan = sample()
    plan["input_kind"] = "manual_prepared"
    assert _by_role(plan) == {}


@pytest.mark.parametrize("first_subclause", ["须提供中级职称证书。", "无须提供任何同类项目业绩证明。"])
def test_qualification_experience_is_not_inferred_from_unrelated_or_negative_text(first_subclause):
    prefix, rest = QUALIFICATION.split("①")
    qualification = prefix + "①" + first_subclause + "②" + rest.split("②", 1)[1]
    plan = sample(qualification=qualification)
    questions = build_project_questions(plan)
    row = next(item for item in questions if item["key"] == QUALIFICATION_PERSONNEL)
    assert "requires_similar_project_experience_proof" not in row["conditions"]
    answers = {QUALIFICATION_PERSONNEL: {"people": []}}
    result = apply_project_answers(plan, questions, answers, "SNAP")
    assert "同类项目的业绩证明" not in result["issues"][0]["action"]
    assert "本单位劳动合同" in result["issues"][0]["action"]


def test_role_answers_are_isolated_to_pending_issues():
    plan = sample()
    before = deepcopy(plan)
    answers = {QUALIFICATION_PERSONNEL: {"people": [{"name": "合成甲", "employment_status": "retired_rehire",
                                                    "retirement_proof_source_ref": "RET"}]},
               MANAGER: {"people": [{"name": "合成乙"}]},
               TECHNICAL: {"people": [{"name": "合成丙", "paid_month": "2026-08"}, {"name": "合成丁"},
                                      {"name": "合成戊"}]},
               SITE: {"people": []}}
    result = apply_project_answers(plan, build_project_questions(plan), answers, "SNAP")
    assert plan == before
    for field in ("facts", "sections", "tables", "attachments", "requirements"):
        assert result[field] == before[field]
    PreparedBid.model_validate(result)
    qualification, manager, technical, site = result["issues"]
    assert qualification["requirement_refs"] == ["QUAL"] and technical["requirement_refs"] == ["SCORE"]
    assert "退休证明候选 RET 待核" in qualification["reason"] and "返聘劳务合同待核" in qualification["reason"]
    assert "同类项目的业绩证明" in qualification["action"]
    assert "合成乙" in manager["reason"] and "合成甲" not in manager["reason"]
    assert all(person in technical["reason"] for person in ("合成丙", "合成丁", "合成戊"))
    assert "正式递交截止及适用月份待核" in technical["reason"]
    assert "人选及证明尚待补充" in site["reason"]
    assert "AMPP（原 NACE）" in manager["action"] and "中国阴极保护协会" in manager["action"]
    assert "中级及以上职称证书" in technical["action"] and "2 人对应评分上限" in technical["action"]
    assert "技术服务项目组成员表" in site["action"] and "加盖公章的证件扫描件" in site["action"]
    assert all("工伤、失业、养老并加盖社保公章" in issue["action"] for issue in result["issues"])
    assert all(issue["severity"] == "warning" for issue in result["issues"])


@pytest.mark.parametrize(("role", "people", "error"), [
    (QUALIFICATION_PERSONNEL, [{"name": "甲"}, {"name": "乙"}], "QUALIFICATION_COUNT"),
    (QUALIFICATION_PERSONNEL, [{"employment_status": "employee", "retirement_proof_source_ref": "RET"}], "EMPLOYMENT_INVALID"),
    (SITE, [{"employment_status": "retired_rehire"}], "PERSONNEL_INVALID"),
    (MANAGER, [{"rehire_contract_source_ref": "RET"}], "PERSONNEL_INVALID"),
    (SITE, [{"paid_month": "2026-01-01"}], "MONTH_INVALID"),
    (SITE, [{"paid_month": "2026-13"}], "MONTH_INVALID"),
    (TECHNICAL, [{"name": "甲"}, {"name": "甲"}], "DUPLICATE_PERSON"),
    (TECHNICAL, [{"payment_source_ref": "RET"}], "PROOF_OUT_OF_SCOPE"),
    (QUALIFICATION_PERSONNEL, [{"payment_source_ref": "OTHER"}], "PROOF_OUT_OF_SCOPE"),
])
def test_invalid_or_cross_scope_answers_are_rejected(role, people, error):
    plan = sample()
    with pytest.raises(ProjectAnswerError, match=error):
        apply_project_answers(plan, build_project_questions(plan), {role: {"people": people}}, "SNAP")


def test_registered_page_stays_distinct_from_payment():
    plan = sample()
    plan["sources"][-2]["locator"] = "docx/word/media/registration.jpeg"
    plan["issues"].append({"issue_id": "M-001", "requirement_refs": ["QUAL"], "title": "参保登记",
                           "section_id": "QUAL", "reason": "原图复核候选（registration.jpeg）。本页为职工参保登记明细。",
                           "action": "核对实缴证明", "source_requirement": QUALIFICATION})
    result = build_project_questions(plan)
    with pytest.raises(ProjectAnswerError, match="REGISTRATION_NOT_PROOF"):
        apply_project_answers(plan, result, {QUALIFICATION_PERSONNEL: {"people": [
            {"name": "甲", "payment_source_ref": "RET"}]}}, "SNAP")


def test_whole_table_scope_does_not_expose_neighboring_row_as_personnel_evidence():
    plan = sample(scoring="其他评分项\t相邻内容\t2\n" + SCORING + "\n合计\t\t9")
    plan["attachments"][0]["requirement_refs"].append("SCORE")
    rows = build_project_questions(plan)
    site = next(row for row in rows if row["key"] == SITE)
    assert {item["kind"] for item in site["evidence_refs"]} == {"tender"}
    source = plan["requirements"][1]["text"]
    start, end = site["source_quote_range"]
    assert source[start:end] == SCORING
    answer = {SITE: {"people": [{"name": "甲"}]}}
    result = apply_project_answers(plan, rows, answer, "SNAP")
    assert result["issues"][-1]["source_requirement"] == SCORING
    complement = result["issues"][-1]["action"]
    assert "相邻内容" not in complement and "技术服务项目组成员表" in complement


def test_summary_month_boundaries_use_only_formally_located_deadline():
    plan = sample(deadline="投标文件递交截止时间：2026年12月20日09:30。")
    questions = build_project_questions(plan)
    answers = {SITE: {"people": [{"name": "边界甲", "paid_month": "2026-06"}, {"name": "完整乙", "paid_month": "2026-07"},
                                 {"name": "超出丙", "paid_month": "2026-05"}, {"name": "当月丁", "paid_month": "2026-12"}]}}
    summary = review_summary({"questions": questions, "answers": answers})
    assert "资格要求 1 名" in summary and "2 人对应评分上限" in summary
    assert "招标原件第 8 页第 3 张表" in summary
    assert summary.count("位于期限边界") == 2
    assert "截止月之前的完整月份范围" in summary
    assert "超出该月窗范围" in summary
    questions = build_project_questions(sample())
    answers["deadline"] = {"submitted_before": "2026-12-20T09:30+08:00", "source_ref": "S-DATE"}
    unverified = review_summary({"questions": questions, "answers": answers})
    assert "仍待正式招标原件核对" in unverified and "适用月份待核" in unverified
    assert "位于期限边界" not in unverified


def test_platform_notice_points_to_its_own_original_clause_without_setting_a_date():
    plan = sample(deadline=PLATFORM_DEADLINE)
    date_source = next(source for source in plan["sources"] if source["source_id"] == "S-DATE")
    date_source["locator"] = date_source["locator"].replace("page[4]/line[5]", "page[9]/line[1]")
    questions = build_project_questions(plan)
    row = next(item for item in questions if item["key"] == "deadline")
    assert row["conditions"]["platform_notice"] == {"name": "合成电子招标平台", "source_refs": ["S-DATE"]}
    assert row["status"] == "unresolved" and row["value"] is None and row["value_source_refs"] == []
    summary = review_summary({"questions": questions, "answers": {}})
    date_section = summary.split("## 响应文件正式递交截止时间")[1].split("\n## ")[0]
    assert "请到招标原件所指的合成电子招标平台查看本项目公告" in date_section
    assert "招标原件第 9 页" in date_section and "招标原件第 3 页" not in date_section
    assert "尚待查证" in date_section


@pytest.mark.parametrize("label", ["报价文件递交截止", "澄清截止", "响应文件澄清截止", "报名截止"])
def test_non_submission_platform_notices_do_not_guide_deadline_review(label):
    questions = build_project_questions(sample(deadline=PLATFORM_DEADLINE.replace("响应文件递交截止", label)))
    row = next(item for item in questions if item["key"] == "deadline")
    assert "platform_notice" not in row["conditions"]
    assert "合成电子招标平台" not in review_summary({"questions": questions, "answers": {}})


@pytest.mark.parametrize("mutation", [
    {"provenance": "manual_input"},
    {"provenance": "synthetic"},
    {"locator": "pdf/page[4]/line[5]; quote_sha256=" + "0" * 64},
])
def test_platform_notices_require_a_verified_original_quote(mutation):
    plan = sample(deadline=PLATFORM_DEADLINE)
    date_source = next(source for source in plan["sources"] if source["source_id"] == "S-DATE")
    date_source.update(mutation)
    questions = build_project_questions(plan)
    row = next(item for item in questions if item["key"] == "deadline")
    assert "platform_notice" not in row["conditions"]
    assert "合成电子招标平台" not in review_summary({"questions": questions, "answers": {}})


def test_pre_writing_pause_skip_and_apply_scoped_answers(tmp_path):
    plan = sample()
    initial = {"manifest": {"source_roles": {}}, "prepared": plan, "source_artifacts": []}
    nodes = [NodeSpec(f"N{i}", "test", lambda ctx, value: value, config_keys=()) for i in range(4)]
    nodes.append(NodeSpec("N4", "test", project_review_input, config_keys=("project_answer_snapshot",)))
    executor = ProjectReviewExecutor(tmp_path, nodes, {"review_before_writing": True})
    parent = executor.create_run(initial)
    assert executor.execute(parent)["cursor"] == 4
    form = review_form(executor, parent)
    form.update(reviewed_by="测试经办人", answers={SITE: {"people": [{"name": "测试人员"}]}})
    child, configuration = submit_answers(executor, parent, form)
    assert executor.resume(parent)["status"] == "succeeded"
    assert executor.output(parent, "N4")["prepared"] == plan
    child_runner = ProjectReviewExecutor(tmp_path, nodes, configuration)
    assert child_runner.resume(child)["status"] == "succeeded"
    assert review_form(child_runner, child)["answers"] == form["answers"]
    child_plan = child_runner.output(child, "N4")["prepared"]
    assert child_plan["tables"] == plan["tables"]
    assert len(child_plan["issues"]) == 1 and "测试人员" in child_plan["issues"][0]["reason"]
