from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace

import pytest
from docx import Document

from company_bid.models import DocumentPackage, PreparedBid
from company_bid.project_answers import ProjectAnswerError, apply_project_answers
from company_bid.rendering import render_package
from company_bid.source_format_contracts import NativeParagraphLayout, format_digest
from company_bid.source_format_rendering import _native_paragraph


SOCIAL = "负责人须为响应人自有员工，提供实际缴费证明。"
CLAUSE = "本人 （姓名）系（响应人名称）的法定代表人，现委托 （姓名）"
SIGNATURE = "委托代理人： （签字）"
PAYMENT_SOURCE = "F-PAYMENT-" + "a" * 64


def source(identity, kind, *, hash_char="1"):
    return {"source_id": identity, "title": identity, "path": identity + ".png", "sha256": hash_char * 64,
            "locator": "原件", "kind": kind, "provenance": "manual_input" if kind == "manual_input" else "original"}


def question(key, reqs, issues=(), **values):
    return {"key": key, "status": "unresolved", "requirement_ids": reqs, "issue_ids": list(issues),
            "evidence_refs": [], "conditions": {}, **values}


def requirement(rid, text, *, category="qualification", mode="attachment"):
    return {"requirement_id": rid, "category": category, "text": text, "source_refs": ["T"],
            "response_mode": mode}


def issue(iid, sid, rid, text, *, reason="原件待核"):
    return {"issue_id": iid, "section_id": sid, "requirement_refs": [rid], "title": "待核", "reason": reason,
            "action": "另附经核对的材料。", "source_requirement": text}


def attachment(aid, sid, title, reqs, proof):
    return {"attachment_id": aid, "section_id": sid, "title": title, "path": proof + ".png", "sha256": "1" * 64,
            "source_refs": [proof], "requirement_refs": reqs, "description": "冻结原图"}


@pytest.fixture
def sample():
    plan = {
        "preparation_note": "隔离验证样本", "project": {"project_id": "P", "name": "测试项目", "source_refs": ["T"]},
        "company": {"company_id": "C", "name": "测试公司", "source_refs": ["T"]},
        "facts": [], "review_actions": [], "synthetic": False, "tables": [{
            "table_id": "TAB", "section_id": "QUOTE", "title": "3 报价表", "columns": ["C1", "C2"],
            "rows": [[{"text": "项目负责人"}, {"issue_refs": ["M-002"]}],
                     [{"text": "项目负责人签字"}, {"text": "＿＿"}]],
            "source_refs": ["T"], "requirement_refs": ["RQ"], "header_from_columns": False,
        }],
        "requirements": [requirement("RS", SOCIAL), requirement("RQ", "项目负责人\t", mode="table", category="commercial"),
                         requirement("RA", CLAUSE + "\n" + SIGNATURE, mode="fixed_text"),
                         requirement("RC", "业绩应按项目提交材料。")],
        "sources": [source("T", "tender"), source("D", "tender"), source("SNAP", "manual_input"),
                    source("PER", "attachment"), source("PERF", "attachment"), source("MIX", "attachment"),
                    source(PAYMENT_SOURCE, "attachment", hash_char="a")],
        "sections": [{"section_id": "S", "title": "人员材料", "kind": "facts", "requirement_refs": ["RS", "RC"],
                     "attachment_refs": ["OLD", "PERFORMANCE", "MIXED"]},
                    {"section_id": "QUOTE", "title": "3 报价表", "kind": "commercial", "requirement_refs": ["RQ"],
                     "table_refs": ["TAB"]},
                    {"section_id": "FORM", "title": "1 法定代表人授权委托书", "kind": "facts", "requirement_refs": ["RA"],
                     "fixed_content": [{"kind": "paragraph", "text": text, "source_refs": ["T"]} for text in (CLAUSE, SIGNATURE)]},
                    {"section_id": "PERFORMANCE", "title": "项目业绩", "kind": "attachments", "requirement_refs": ["RC"]}],
        "issues": [issue("M-001", "S", "RS", SOCIAL), issue("M-002", "QUOTE", "RQ", "项目负责人\t",
                         reason="原表空白格无唯一、允许使用的明确事实值。"),
                   issue("M-003", "FORM", "RA", CLAUSE + "\n" + SIGNATURE)],
        "attachments": [attachment("OLD", "S", "旧负责人", ["RS"], "PER"),
                        attachment("PERFORMANCE", "S", "项目业绩原件", ["RC"], "PERF"),
                        attachment("MIXED", "S", "项目负责人历史记录", ["RS", "RC"], "MIX")],
    }
    questions = [question("manager_name", ["RS", "RQ"], ["M-002"],
                          evidence_refs=[{"kind": "attachment", "attachment_id": "PERFORMANCE", "source_ref": "PERF"}]),
                 question("employer", ["RS"]), question("deadline", ["RS"]),
                 question("payment_evidence", ["RS"], ["M-001"], conditions={"window_days": 120, "minimum_payments": 1},
                          evidence_refs=[{"kind": "reviewed_registration", "attachment_id": "OLD", "source_ref": "PER"}]),
                 question("signer_role", ["RA"], ["M-003"], conditions={"allowed_roles": ["legal_representative", "agent"]}),
                 question("agent_name", ["RA"], ["M-003"])]
    return PreparedBid.model_validate(plan).model_dump(mode="json"), questions


def payment(name="新负责人", employer="测试公司", day="2026-10-01", ref=PAYMENT_SOURCE):
    return {"person_name": name, "employer": employer, "paid_on": day, "source_ref": ref}


def known_deadline(questions):
    row = next(row for row in questions if row["key"] == "deadline")
    row.update(status="known", value={"submitted_before": "2026-10-31T12:00:00+08:00", "source_ref": "D"},
               value_source_refs=["D"])


def test_skip_and_partial_person_confirmation_preserve_frozen_plan(sample):
    plan, questions = sample
    before = deepcopy(plan)
    assert apply_project_answers(plan, questions, {}, "SNAP") == before
    changed = apply_project_answers(plan, questions, {"manager_name": "新负责人"}, "SNAP")
    assert plan == before
    assert changed["tables"][0]["rows"][0][1]["text"] == "新负责人"
    assert changed["tables"][0]["rows"][1] == plan["tables"][0]["rows"][1]
    assert [row["issue_id"] for row in changed["issues"]] == ["M-001", "M-003"]
    assert "实际缴费凭证仍待核对" in changed["issues"][0]["reason"]
    assert "本项目填报的拟派负责人为新负责人。" in changed["issues"][0]["reason"]
    assert "填报的用工主体为" not in changed["issues"][0]["reason"]
    assert "确认前的材料" in changed["issues"][0]["reason"]
    assert [item["attachment_id"] for item in changed["attachments"]] == ["PERFORMANCE", "MIXED"]
    assert changed["attachments"][0] == plan["attachments"][1]
    assert changed["attachments"][1]["requirement_refs"] == ["RC"]
    assert changed["attachments"][1]["section_id"] == "PERFORMANCE"
    assert changed["sources"] == before["sources"]
    PreparedBid.model_validate(changed)


@pytest.mark.parametrize("remaining_proof", [False, True])
def test_withdrawn_personnel_proof_reconciles_current_labels(sample, remaining_proof):
    plan, questions = sample
    ref, section_id = "RQF", "S"
    text = "拟派项目负责人需提交证书。"
    plan["requirements"].append(requirement(ref, text))
    section = next(section for section in plan["sections"] if section["section_id"] == section_id)
    section["requirement_refs"].append(ref)
    next(row for row in questions if row["key"] == "manager_name")["requirement_ids"].append(ref)
    for identity, name in [("OLD-CERT", "旧负责人"), *([("CURRENT-CERT", "新负责人")] if remaining_proof else [])]:
        plan["attachments"].append(attachment(identity, section_id, name, [ref], "PER"))
        section["attachment_refs"].append(identity)
    attached_title = "已附证明的适用条件仍待核对"
    former_action = "结合正文中的完整证明包逐项核对本条条件；转移附件位置不代表资格通过。"
    plan["issues"].extend([
        {**issue("M-010", section_id, ref, text, reason="已附证明：旧负责人。"), "title": attached_title},
        {**issue("M-011", section_id, ref, text, reason="原证明页已转入正文。"),
         "title": "证明材料落点及适用性待核对", "action": former_action},
    ])
    changed = apply_project_answers(plan, questions, {"manager_name": "新负责人"}, "SNAP")
    assert [row["attachment_id"] for row in changed["attachments"] if ref in row["requirement_refs"]] == (
        ["CURRENT-CERT"] if remaining_proof else [])
    attached, placement = changed["issues"][-2:]
    assert attached["title"] == (attached_title if remaining_proof else "本次拟派人员证明待核对及补充")
    assert ("仍有已装配证明" if remaining_proof else "当前未装配相关证明") in attached["reason"].split("\n")[0]
    assert "确认前的材料检索与读取记录：\n已附证明：旧负责人。" in attached["reason"]
    assert placement["title"] == plan["issues"][-1]["title"]
    assert former_action not in placement["action"] and "原办理动作：" + former_action in placement["reason"]
    assert ("正文保留证明" if remaining_proof else "确认后装配") in placement["action"]


def test_matching_payment_keeps_independent_document_and_waits_for_field_review(sample):
    plan, questions = sample
    known_deadline(questions)
    for key, value in (("manager_name", "新负责人"), ("employer", "测试公司")):
        next(row for row in questions if row["key"] == key).update(status="known", value=value, value_source_refs=["SNAP"])
    result = apply_project_answers(plan, questions, {"payment_evidence": payment()}, "SNAP",
                                   uploaded_payment_source_id=PAYMENT_SOURCE)
    proof = next(row for row in result["attachments"] if row["attachment_id"].startswith("PROJECT-PAYMENT"))
    assert proof["requirement_refs"] == ["RS"]
    assert set(proof["source_refs"]) == {PAYMENT_SOURCE, "SNAP"}
    assert "正式截止日 30 日" in result["issues"][0]["reason"]
    assert "实际缴费和资格仍待核" in result["issues"][0]["reason"]
    assert "填报的用工主体为测试公司，人员关系待核" in result["issues"][0]["reason"]
    assert "M-001" in {issue["issue_id"] for issue in result["issues"]}
    PreparedBid.model_validate(result)


@pytest.mark.parametrize(("person", "day", "source_ref", "reason"), [
    ("旧负责人", "2026-10-01", PAYMENT_SOURCE, "与本项目选择不一致"),
    ("新负责人", "2026-01-01", PAYMENT_SOURCE, "不在截止日前 120 日"),
    ("新负责人", "2026-10-31", PAYMENT_SOURCE, "与正式截止日相同"),
    ("新负责人", "2026-10-01", "PER", "参保登记页"),
])
def test_payment_conflict_is_pending_and_unattached(sample, person, day, source_ref, reason):
    plan, questions = sample
    known_deadline(questions)
    changed = apply_project_answers(plan, questions, {"manager_name": "新负责人", "employer": "测试公司",
                        "payment_evidence": payment(name=person, day=day, ref=source_ref)}, "SNAP",
                                    uploaded_payment_source_id=PAYMENT_SOURCE)
    assert all(not row["attachment_id"].startswith("PROJECT-PAYMENT") for row in changed["attachments"])
    assert reason in changed["issues"][0]["reason"]


def test_unrelated_source_and_conflicting_deadline_are_rejected(sample):
    plan, questions = sample
    with pytest.raises(ProjectAnswerError, match="PAYMENT_SOURCE_OUT_OF_SCOPE"):
        apply_project_answers(plan, questions, {"payment_evidence": payment(ref="PERF")}, "SNAP")
    known_deadline(questions)
    with pytest.raises(ProjectAnswerError, match="DEADLINE_SOURCE_CONFLICT"):
        apply_project_answers(plan, questions, {"deadline": {"submitted_before": "2026-10-30T12:00:00+08:00",
                              "source_ref": "D"}}, "SNAP")


def test_signer_branches_protect_signature_and_limit_agent_to_actual_name_slot(sample):
    plan, questions = sample
    direct = apply_project_answers(plan, questions, {"signer_role": "legal_representative"}, "SNAP")
    assert direct["sections"][2] == plan["sections"][2]
    assert "拟由法定代表人（单位负责人）签署" in direct["issues"][2]["reason"]
    assert direct["issues"][2]["action"] == plan["issues"][2]["action"]
    with pytest.raises(ProjectAnswerError, match="AGENT_ROLE_REQUIRED"):
        apply_project_answers(plan, questions, {"signer_role": "legal_representative", "agent_name": "代理甲"}, "SNAP")

    agent_question = next(row for row in questions if row["key"] == "signer_role")
    agent_question.update(status="known", value="agent", value_source_refs=["T"])
    agent = apply_project_answers(plan, questions, {"agent_name": "代理甲"}, "SNAP")
    authorization = next(sec for sec in agent["sections"] if sec["section_id"] == "FORM")
    assert authorization["fixed_content"][0]["text"] == CLAUSE.replace("现委托 ", "现委托代理甲")
    assert authorization["fixed_content"][1]["text"] == SIGNATURE
    assert "拟由代理人签署。拟任代理人：代理甲。" in agent["issues"][2]["reason"]
    assert agent["issues"][2]["action"] == plan["issues"][2]["action"]
    assert {"SNAP", "T"} == set(authorization["fixed_content"][0]["source_refs"])
    PreparedBid.model_validate(agent)


def test_native_agent_word_run_requires_bound_fact_and_retains_form_marks():
    i = CLAUSE.rindex(" ")
    rule = {"start": i, "end": i + 1, "basis": "native_vector_rule", "fixed_index": 0,
            "width_pt": 42.0, "locator": "pdf/page[1]/rule[0]", "bbox": [40.0, 55.0, 82.0, 55.0]}
    element = NativeParagraphLayout(fixed_indices=[0], source_block_ids=["B"], source_locators=["pdf/page[1]/line[0]"],
        source_texts=[CLAUSE], output_texts_sha256=format_digest([CLAUSE]), bbox=[5.0, 40.0, 83.0, 55.0],
        font_family="SimSun", font_size_pt=12.0, bold=False, alignment="left", left_pt=5.0, right_pt=0.0,
        first_line_indent_pt=0.0, line_spacing_pt=15.0, underlines=[rule], grouping_rule="one_native_line")
    payload = {"label": "本项目拟任授权代理人", "value": "代理甲", "source_refs": ["SNAP"]}
    loaded = SimpleNamespace(**payload, fact_id="PROJECT-ANSWER-agent_name-" + format_digest(payload)[:24])
    block = SimpleNamespace(text=CLAUSE, fact_refs=[loaded.fact_id], source_refs=["T", "SNAP"])
    section = SimpleNamespace(title="1 法定代表人授权委托书", blocks=[block],
                               native_format=SimpleNamespace(bindings=[], elements=[element], page_width_pt=100.0))
    package = SimpleNamespace(facts=[loaded], sources=[SimpleNamespace(source_id="SNAP", kind="manual_input",
                                                                       provenance="manual_input")])
    paragraph = _native_paragraph(Document(), section, element, 0, package=package)
    assert paragraph.text.replace("\N{NO-BREAK SPACE}", "") == CLAUSE.replace("现委托 ", "现委托代理甲")
    assert any(run.text == "代理甲" and run.underline for run in paragraph.runs)
    block.source_refs.remove("SNAP")
    paragraph = _native_paragraph(Document(), section, element, 0, package=package)
    assert "代理甲" not in paragraph.text and paragraph.text.count("（姓名）") == 2


def test_signer_scope_alone_does_not_authorize_writing_an_agent_name(sample):
    plan, questions = sample
    proxy = next(row for row in questions if row["key"] == "agent_name")
    proxy["requirement_ids"] = ["RS"]
    updated = apply_project_answers(plan, questions, {"signer_role": "agent", "agent_name": "代理甲"}, "SNAP")
    original_form = next(section for section in plan["sections"] if section["section_id"] == "FORM")
    updated_form = next(section for section in updated["sections"] if section["section_id"] == "FORM")
    assert updated_form == original_form
    fact = next(row for row in updated["facts"] if row["label"] == "本项目拟任授权代理人")
    assert any(fact["fact_id"] in section["fact_refs"] for section in updated["sections"])


@pytest.mark.parametrize("target", ["table", "section"])
def test_current_manager_does_not_fill_same_label_in_a_historical_table(sample, target):
    plan, questions = sample
    table = plan["tables"][0]
    contextual_item = table if target == "table" else next(
        section for section in plan["sections"] if section["section_id"] == table["section_id"])
    contextual_item["title"] = "历史项目人员登记表"
    changed = apply_project_answers(plan, questions, {"manager_name": "新负责人"}, "SNAP")
    assert changed["tables"][0] == table
    assert next(issue for issue in changed["issues"] if issue["issue_id"] == "M-002") in plan["issues"]


def test_ambiguous_assignment_and_conflicting_known_choice_are_rejected(sample):
    plan, questions = sample
    manager = next(row for row in questions if row["key"] == "manager_name")
    manager["conditions"]["ambiguous_assignment_scope"] = True
    with pytest.raises(ProjectAnswerError, match="PERSON_SCOPE_AMBIGUOUS"):
        apply_project_answers(plan, questions, {"manager_name": "测试甲"}, "SNAP")
    manager.update(conditions={}, status="known", value="测试乙", value_source_refs=["SNAP"])
    with pytest.raises(ProjectAnswerError, match="FROZEN_VALUE_CONFLICT"):
        apply_project_answers(plan, questions, {"manager_name": "测试甲"}, "SNAP")


@pytest.mark.parametrize(("tail", "closed"), [("。", True), ("并附其资格证书。", False)])
def test_only_wholly_answered_name_requirement_loses_generic_marker(sample, tail, closed):
    plan, questions = sample
    text = "应在人员表中填写拟派项目负责人姓名" + tail
    req = next(req for req in plan["requirements"] if req["requirement_id"] == "RQ")
    req.update(text=text, response_mode="factual_response", category="qualification")
    plan["tables"][0]["title"] = "拟派项目人员表"
    plan["issues"].append({**issue("M-004", "QUOTE", "RQ", text,
                                  reason="该要求不是可自由起草的技术方法，且无明确可填字段。"),
                           "title": "事实响应资料待完善"})
    result = apply_project_answers(plan, questions, {"manager_name": "新负责人"}, "SNAP")
    assert result["tables"][0]["rows"][0][1]["text"] == "新负责人"
    assert result["tables"][0]["rows"][1][1]["text"] == "＿＿"
    assert ("M-004" not in {item["issue_id"] for item in result["issues"]}) == closed


@pytest.mark.parametrize(("employer", "split", "tail", "closed"), [
    ("测试公司", False, "。", True),
    (None, False, "。", False),
    ("测试公司", True, "。", False),
    ("测试公司", False, "并附用工证明。", False),
])
def test_all_named_fields_must_be_filled_in_the_same_personnel_table(sample, employer, split, tail, closed):
    plan, questions = sample
    text = "应在人员配置表中填写拟派项目负责人姓名及用工主体" + tail
    req = next(req for req in plan["requirements"] if req["requirement_id"] == "RQ")
    req.update(text=text, response_mode="factual_response", category="qualification")
    next(question for question in questions if question["key"] == "employer")["requirement_ids"].append("RQ")
    table = plan["tables"][0]
    table["title"] = "人员配置表"
    employer_row = [{"text": "用工主体", "fact_refs": [], "issue_refs": []},
                    {"text": "", "fact_refs": [], "issue_refs": []}]
    if split:
        plan["tables"].append({**table, "table_id": "SECOND", "rows": [employer_row]})
        next(section for section in plan["sections"] if section["section_id"] == "QUOTE")["table_refs"].append("SECOND")
    else:
        table["rows"].append(employer_row)
    plan["issues"].append({**issue("M-006", "QUOTE", "RQ", text,
                                  reason="该要求不是可自由起草的技术方法，且无明确可填字段。"),
                           "title": "事实响应资料待完善"})
    answers = {"manager_name": "新负责人"}
    if employer:
        answers["employer"] = employer
    result = apply_project_answers(plan, questions, answers, "SNAP")
    assert result["tables"][0]["rows"][0][1]["text"] == "新负责人"
    actual_employer = next(row[1]["text"] for table in result["tables"] for row in table["rows"] if row[0]["text"] == "用工主体")
    assert actual_employer == (employer or "")
    assert ("M-006" not in {item["issue_id"] for item in result["issues"]}) == closed


@pytest.mark.parametrize(("role", "verb", "expected"), [
    ("agent", "提供", "按招标文件提供授权委托书"),
    ("agent", "提交", "按招标文件提供授权委托书"),
    ("agent", "提\n供", "按招标文件提供授权委托书"),
    ("legal_representative", "提供", "另附经核对的材料。"),
])
def test_role_specific_proof_updates_actionable_word_checklist(sample, tmp_path, role, verb, expected):
    plan, questions = sample
    text = f"投标文件应由法定代表人或委托代理人签字；委托代理人签字时须{verb}授权委托书。"
    other = "同一章节的身份证明粘贴和格式核对规则。"
    plan["requirements"] = [requirement("RA", text, mode="factual_response"),
                            requirement("RB", other, mode="fixed_text")]
    plan["tables"] = []
    plan["attachments"] = []
    plan["sources"] = [row for row in plan["sources"] if row["source_id"] in {"T", "SNAP"}]
    for row in plan["sources"]:
        content = (text if row["source_id"] == "T" else role).encode()
        path = tmp_path / (row["source_id"] + ".txt")
        path.write_bytes(content)
        row.update(path=str(path), sha256=sha256(content).hexdigest())
    section = next(section for section in plan["sections"] if section["section_id"] == "FORM")
    section["requirement_refs"].append("RB")
    plan["sections"] = [section]
    plan["issues"] = [issue("M-007", "FORM", "RA", text),
                      {**issue("M-008", "FORM", "RB", text + "；" + other), "requirement_refs": ["RA", "RB"]}]
    questions = [question("signer_role", ["RA", "RB"], conditions={"allowed_roles": ["legal_representative", "agent"]}),
                 question("agent_name", ["RA", "RB"])]
    result = apply_project_answers(plan, questions, {"signer_role": role}, "SNAP")
    assert expected in result["issues"][0]["action"]
    assert result["issues"][1] == plan["issues"][1]
    package_data = {key: val for key, val in result.items() if key not in {"tables", "sections", "input_kind"}}
    fact_by_id = {row["fact_id"]: row for row in result["facts"]}
    sections = []
    for section in result["sections"]:
        refs = section["requirement_refs"]
        blocks = [{"kind": "paragraph", "text": block["text"], "requirement_refs": refs}
                  for block in section.get("fixed_content", [])]
        blocks += [{"kind": "paragraph", "text": f"{fact_by_id[fid]['label']}：{fact_by_id[fid]['value']}",
                    "requirement_refs": refs} for fid in section.get("fact_refs", [])]
        blocks += [{"kind": "issue", "issue_id": row["issue_id"], "requirement_refs": row["requirement_refs"]}
                   for row in result["issues"] if row["section_id"] == section["section_id"]]
        sections.append({"section_id": section["section_id"], "title": section["title"], "requirement_refs": refs,
                         "blocks": blocks})
    package = DocumentPackage.model_validate({**package_data, "title": "隔离测试标书", "sections": sections})
    render_package(package, tmp_path)
    output = next(tmp_path.glob("*待补*.docx"))
    text = [para.text for para in Document(output).paragraphs]
    actionable = text[next(i for i, para in enumerate(text) if para.startswith("M-007 ")):
                      next(i for i, para in enumerate(text) if para.startswith("M-008 "))]
    actions = [para for para in actionable if para.startswith("补充动作：")]
    assert len(actions) == 1 and expected in actions[0]
    if role == "legal_representative":
        assert "补充动作：另附经核对的材料。" == actions[0]
        assert any("拟由法定代表人（单位负责人）签署" in para for para in actionable)


def test_negative_or_shared_actor_proof_cannot_replace_generic_action(sample):
    plan, _ = sample
    plan["requirements"] = [requirement("RA", "投标文件由代理人签字时无需提供授权委托书。", mode="factual_response"),
                            requirement("RB", "投标文件由法定代表人或代理人签字，须提供授权委托书。", mode="factual_response")]
    section = next(section for section in plan["sections"] if section["section_id"] == "FORM")
    section["requirement_refs"].append("RB")
    plan["sections"] = [section]
    plan["issues"] = [issue(f"M-00{index}", "FORM", row["requirement_id"], row["text"])
                      for index, row in enumerate(plan["requirements"], 7)]
    updated = apply_project_answers(plan, [question("signer_role", ["RA", "RB"])], {"signer_role": "agent"}, "SNAP")
    assert updated["issues"] == plan["issues"]


@pytest.mark.parametrize(("role", "needed"), [
    ("agent", "按招标文件提供授权委托书"),
    ("legal_representative", "补充实际签署的法定代表人（单位负责人）身份证明"),
])
def test_role_proof_applies_to_its_own_procedure_instead_of_a_cover_rule(sample, role, needed):
    plan, _ = sample
    proof = ("由响应人的法定代表人（单位负责人）签字或加盖电子印章的，应附法定代表人（单\n"
             "位负责人）身份证明，由代理人签字或加盖电子印章的，应附由法定代表人（单位负责人）\n签署的授权委托书。")
    cover = "响应文件封面须法定代表人或授权代表签署并加盖单位公章，并按时递交。"
    plan["requirements"].extend([requirement("RP", proof, mode="procedure"),
                                 requirement("RF", cover, mode="procedure")])
    scope = [question("signer_role", ["RA", "RP", "RF"])]
    plan["review_actions"] = [
        {"action_id": action, "kind": "procedure", "status": "pending_manual", "requirement_refs": [ref],
         "source_refs": ["T"], "source_requirement": text, "instruction": "按原条文线下办理。"}
        for action, ref, text in (("proof", "RP", proof), ("cover", "RF", cover), ("cover-quote", "RP", cover))
    ]
    updated = apply_project_answers(plan, scope, {"signer_role": role}, "SNAP")
    assert needed in updated["review_actions"][0]["instruction"]
    assert updated["review_actions"][1:] == plan["review_actions"][1:]
    assert all(row["status"] == "pending_manual" for row in updated["review_actions"])


def test_signing_issue_preserves_actions_outside_its_own_proof_branch(sample):
    plan, _ = sample
    agent_proof = "投标文件由代理人签字时须提供授权委托书。"
    direct_proof = "投标文件由法定代表人签字时须提供身份证明。"
    cover = "投标文件封面由法定代表人或代理人签字，须加盖公章并递交。"
    plan["requirements"].extend([requirement("RP", agent_proof), requirement("RD", direct_proof),
                                 requirement("RF", cover)])
    plan["issues"] = [
        {**issue("M-010", "FORM", "RP", agent_proof), "action": "另核对其他未完成的事项。"},
        {**issue("M-011", "FORM", "RF", cover), "action": "核对公章和递交时限。"},
        {**issue("M-012", "FORM", "RD", direct_proof), "action": "直接签署时核对身份证明。"},
        {**issue("M-013", "FORM", "RP", agent_proof + cover), "requirement_refs": ["RP", "RF"],
         "action": "核对本页盖章并递交。"},
        {**issue("M-014", "FORM", "RP", cover), "action": "按封面原要求递交。"},
    ]
    scope = [question("signer_role", ["RA", "RP", "RD", "RF"], [row["issue_id"] for row in plan["issues"]])]
    updated = apply_project_answers(plan, scope, {"signer_role": "agent"}, "SNAP")
    action, *other = [row["action"] for row in updated["issues"]]
    assert "按招标文件提供授权委托书" in action and "另核对其他未完成的事项。" in action
    assert other == [row["action"] for row in plan["issues"][1:]]
    assert all("拟由代理人签署" in row["reason"] for row in updated["issues"])
