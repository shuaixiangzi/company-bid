from copy import deepcopy

import pytest

from company_bid.project_questions import build_project_questions


def _source(ref, kind="tender", provenance="original", locator=None):
    return {"source_id": ref, "kind": kind, "provenance": provenance, "title": ref,
            "locator": locator or f"pdf/page[1]/line[{ref[1:]}]", "path": "private-source"}


def _plan(*requirements):
    plan = {"sources": [], "requirements": [], "facts": [], "issues": [], "tables": [],
            "attachments": [], "sections": []}
    for index, requirement in enumerate(requirements, start=1):
        text, mode = requirement if isinstance(requirement, tuple) else (requirement, "attachment")
        ref = f"S{index}"
        plan["sources"].append(_source(ref))
        plan["requirements"].append({"requirement_id": f"R{index}", "text": text, "source_refs": [ref],
                                     "category": "qualification", "response_mode": mode})
    return plan


def _questions(plan, **kwargs):
    rows = build_project_questions(plan, **kwargs)
    by_key = {row["key"]: row for row in rows}
    assert len(rows) == len(by_key)
    return by_key


_MANAGER = "拟派项目负责人须填写姓名。"
_SOCIAL = "投标人应提供拟派项目负责人在投标截止时间前120日内至少缴纳一次的社会保险实缴证明，须载明用工主体。"
_DEADLINE = "投标文件递交截止时间：2026 年 12 月 20 日 09 时 30 分（北京时间）。"
_SIGN = "投标文件应由法定代表人或授权代表签字；由授权代表签字的，应提供授权委托书。"


def test_project_decisions_deduplicate_and_bind_to_tender_requirements():
    plan = _plan(_MANAGER, _SOCIAL, _DEADLINE, _SIGN, ("报价表\n项目负责人\t\n备注\t", "table"))
    original = deepcopy(plan)
    questions = _questions(plan)
    assert list(questions) == ["manager_name", "employer", "deadline", "payment_evidence", "signer_role", "agent_name"]
    assert questions["manager_name"]["requirement_ids"] == ["R1", "R2", "R5"]
    assert questions["manager_name"]["source_refs"] == ["S1", "S2", "S5"]
    assert questions["employer"]["requirement_ids"] == ["R2"]
    assert questions["deadline"]["status"] == "known"
    assert questions["deadline"]["value"] == {"submitted_before": "2026-12-20T09:30:00+08:00", "source_ref": "S3"}
    assert questions["deadline"]["value_source_refs"] == ["S3"]
    assert questions["payment_evidence"]["status"] == "unresolved"
    assert questions["payment_evidence"]["conditions"] == {"window_days": 120, "minimum_payments": 1}
    assert questions["signer_role"]["conditions"] == {"allowed_roles": ["legal_representative", "agent"]}
    assert questions["agent_name"]["status"] == "conditional"
    assert questions["agent_name"]["depends_on"] == {"key": "signer_role", "value": "agent"}
    assert all("path" not in item for row in questions.values() for item in row["evidence_refs"])
    assert plan == original


@pytest.mark.parametrize("text", [
    "本项目无需填报项目负责人姓名，也无须提供拟派项目负责人社保实缴证明。",
    "投标人不需要提供项目负责人社保实缴凭证。",
    "项目负责人社保实缴凭证不要求提供。",
    "历史项目的项目经理须填写姓名。",
    "如中标，项目经理应提交姓名并出示实缴社保凭证。",
    "其他说明示例为“拟派项目负责人须填写姓名；响应文件提交截止时间：2026年12月20日09时30分”。",
    "附：以前项目的业绩统计表，示例中项目经理须填写姓名。",
    "招标文件获取截止时间：2026-12-20 08:30；评审时间：2026年12月21日08时30分。",
    "本合同经双方法定代表人或授权代表签字后生效。",
    "投标异议函应由法定代表人或其授权代理人签字。",
    "投标文件随附法定代表人或授权代表的身份证明，由登记员签字。",
    "投标文件可由法定代表人签字。",
    "投标文件应由投标人代表签字。",
    "如果其他项目要求授权，投标文件应由法定代表人或授权代表签字。",
    "采购人须提供模拟用工主体和测试项目负责人姓名。",
    "施工协议附件仅供参考：法定代表人授权委托书。",
])
def test_inapplicable_language_does_not_create_a_question(text):
    assert _questions(_plan(text)) == {}


def test_contract_review_scope_and_unrelated_corporate_fields_do_not_select_a_current_manager():
    plan = _plan(_MANAGER, ("企业基本情况\n项目经理\t\n人员数量\t", "table"))
    plan["sections"] = [{"title": "第六章 合同条款：原文与待核事项", "requirement_refs": ["R1"]}]
    assert _questions(plan) == {}


def test_a_real_source_qualification_follows_a_distinct_negative_clause():
    plan = _plan("本项目无需提供身份证明；但须填写拟派项目负责人姓名。")
    assert set(_questions(plan)) == {"manager_name"}


@pytest.mark.parametrize("authority", [
    "招标文件发放时间：2026年12月19日12时00分至2026年12月20日12时00分；评审时间：2026-12-23 09:00。",
    "递交响应文件截止时间和地点\t详见第一章采购公告",
    "提交投标文件截止日期：2026年12月20日。",
    "递交截止时间：2026年2月31日9:00。",
    "递交截止时间：2026-12-20 09:00至2026-12-21 08:00。",
    "投标截止时间：2026-12-20 09:00 UTC。",
    "投标截止时间：2026-12-20 09:00（UTC+01:00）。",
])
def test_dates_require_an_explicit_valid_submission_time(authority):
    deadline = _questions(_plan(_SOCIAL, authority))["deadline"]
    assert deadline["value"] is None
    assert deadline["status"] == "unresolved"
    assert deadline["value_source_refs"] == []


def test_conflicting_authoritative_deadlines_remain_unresolved():
    deadline = _questions(_plan(_DEADLINE, "递交响应文件截止时间：2026-12-21 09:30。"))["deadline"]
    assert deadline["status"] == "unresolved"
    assert deadline["conditions"] == {"source_conflict": True}
    assert deadline["source_refs"] == ["S1", "S2"]


@pytest.mark.parametrize("text", [
    "现将投标截止时间变更为北京时间2026年12月20日09:30。",
    "投标人须于2026年12月20日09时30分前递交响应文件。",
    "响应文件须在2026年12月20日09:30之前提交。",
])
def test_known_deadline_can_be_given_in_an_explicit_submission_instruction(text):
    deadline = _questions(_plan(text))["deadline"]
    assert deadline["status"] == "known"
    assert deadline["value"]["submitted_before"] == "2026-12-20T09:30:00+08:00"


@pytest.mark.parametrize("text", [
    "投标文件应由法定代表人签字或由其授权代理人签署。",
    "投标文件应由法定代表人签署，也可由授权代表签字。",
])
def test_alternate_signing_verbs_preserve_the_actual_choice(text):
    sections = _questions(_plan(text))
    assert sections["signer_role"]["status"] == "unresolved"
    assert sections["signer_role"]["conditions"]["allowed_roles"] == ["legal_representative", "agent"]
    assert sections["agent_name"]["status"] == "conditional"


def test_one_permitted_signing_role_comes_from_its_own_sources():
    direct = _questions(_plan(_SIGN, "本项目投标文件须由法定代表人本人签署。"))
    assert set(direct) == {"signer_role"}
    assert direct["signer_role"]["status"] == "known"
    assert direct["signer_role"]["value"] == "legal_representative"
    assert direct["signer_role"]["value_source_refs"] == ["S2"]
    agent = _questions(_plan("投标文件由授权代表签字。", "应提供授权委托书。"))
    assert agent["signer_role"]["value"] == "agent"
    assert agent["agent_name"]["status"] == "unresolved"
    assert agent["agent_name"]["depends_on"] is None
    assert agent["agent_name"]["requirement_ids"] == ["R1", "R2"]


def test_existing_assignment_requires_current_project_scope_and_unique_value():
    plan = _plan(_MANAGER, _SOCIAL, _SIGN)
    plan["sources"].extend([_source("current", "manual_input", "user_authorized_fact", "项目确认表第一行"),
                            _source("library", "manual_input", "manual_input", "资料库人员表"),
                            _source("old", "company_material", "derived_from_bid", "旧标书人员表")])
    plan["facts"] = [{"label": "项目负责人姓名", "value": "测试甲", "source_refs": ["current"]},
                     {"label": "项目负责人姓名", "value": "测试乙", "source_refs": ["library", "old"]},
                     {"label": "签署身份", "value": "法定代表人", "source_refs": ["current"]}]
    assert _questions(plan)["manager_name"]["status"] == "unresolved"
    reviewed = _questions(plan, project_source_ids={"current"})
    assert reviewed["manager_name"]["value"] == "测试甲"
    assert reviewed["manager_name"]["value_source_refs"] == ["current"]
    assert reviewed["payment_evidence"]["status"] == "unresolved"
    assert "agent_name" not in reviewed
    plan["facts"].append({"label": "项目负责人姓名", "value": "测试丙", "source_refs": ["current"]})
    assert _questions(plan, project_source_ids={"current"})["manager_name"]["status"] == "unresolved"


def test_parallel_appointments_require_scope_resolution():
    plan = _plan("第一标段拟派项目负责人须填写姓名。", "第二标段拟派项目负责人须提供姓名。", _SOCIAL)
    rows = _questions(plan)
    assert rows["manager_name"]["conditions"]["ambiguous_assignment_scope"] is True
    assert rows["payment_evidence"]["conditions"]["ambiguous_assignment_scope"] is True
    two_roles = _questions(_plan("投标人须分别填报项目负责人及项目经理姓名。"))
    assert two_roles["manager_name"]["conditions"]["ambiguous_assignment_scope"] is True


def test_registration_candidate_keeps_original_locator_and_failed_reading_separate_from_payment():
    plan = _plan(_SOCIAL, _MANAGER)
    plan["sources"].append(_source("proof", "company_material", "derived_from_bid", "docx/word/media/image2.jpeg"))
    plan["attachments"] = [{"attachment_id": "A2", "title": "历史人员证明", "requirement_refs": ["R2"],
                            "source_refs": ["proof"]}]
    plan["issues"] = [{"issue_id": "M-1", "requirement_refs": ["R1"], "title": "已有参保登记明细，实缴依据待补",
                       "reason": "先核对拟派人员。\n原图复核候选（Word 原件中的 image2.jpeg）。本页为职工参保登记明细。"
                                 "该图的 OCR 读取或校验失败，原状态待核。", "action": "补实际缴费凭据"}]
    row = _questions(plan)["payment_evidence"]
    assert row["value"] is None
    assert row["issue_ids"] == ["M-1"]
    assert row["evidence_refs"][-1] == {"kind": "reviewed_registration", "source_ref": "proof", "attachment_id": "A2",
                                       "locator": "docx/word/media/image2.jpeg", "title": "职工参保登记明细",
                                       "transcription_status": "ocr_failed_pending_review"}
    plan["sources"].append(_source("other", "company_material", "derived_from_bid", "docx/other/image2.jpeg"))
    assert [item["kind"] for item in _questions(plan)["payment_evidence"]["evidence_refs"]] == ["tender"]
