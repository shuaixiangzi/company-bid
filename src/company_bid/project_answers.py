"""Apply a frozen, project-specific confirmation to the document plan."""
from __future__ import annotations

import re
from copy import deepcopy
from datetime import date, datetime

from .slots import whole_cell_slot
from .source_format_contracts import format_digest, negative_fill_instruction


class ProjectAnswerError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


_LABELS = {
    "manager_name": "本项目拟派负责人",
    "employer": "本项目填报的用工主体",
    "signer_role": "本项目拟采用的签署方式",
    "agent_name": "本项目拟任授权代理人",
    "deadline": "正式递交截止时点",
    "payment_evidence": "社保原件核对",
}
_MANUAL_PROVENANCE = {"manual_input", "user_authorized_fact"}
_AUTHORIZATION = re.compile(r"(?:\d+[.、\s]*)?(?:法定?人?代表人?|单位负责人)?授权(?:委托)?书")
_AGENT_SLOT = re.compile(r"现委托(?P<blank>[ \t_＿]+)[（(]姓名[）)]\s*$")
_PERSONNEL_FIELDS_ONLY = re.compile(r"应在(?P<table>(?:拟派项目)?人员(?:配置)?表)中填写拟派项目负责人姓名(?P<employer>及用工主体)?[。.]?")
_HISTORICAL_PERSONNEL = re.compile(r"(?:历史|既往|以往|已完成|已完工)(?:项目|合同|业绩)|业绩(?:统计|汇总|一览)表")
_SOCIAL_FIELDS = {"manager_name", "employer", "deadline", "payment_evidence"}
_SIGNING_FIELDS = {"signer_role", "agent_name"}
_ROLE_VALUES = {"legal_representative": "法定代表人（单位负责人）签署", "agent": "代理人签署"}
_SIGNING_PROOF = re.compile(r"(?:应当|应|须|需|必须)?(?:提供|提交|随附|附上|附)(?P<subject>[^。；;]{0,36}?)(?P<proof>身份证明|授权委托书)")
_SIGNING_ACTION = r"签字|签署|签章|盖章|加盖(?:电子)?印章"
_SIGNING_ACTOR = {"legal_representative": r"法定代表人|单位负责人", "agent": r"代理人"}
_SIGNING_PROOF_SPLIT = re.compile(r"[。；;！？\n]|[，,](?=(?:由(?:(?:响应人|投标人)的?)?)?(?:法定代表人|单位负责人|"
                                  rf"(?:其)?(?:委托|授权)?代理人).{{0,16}}(?:{_SIGNING_ACTION}))")
_NO_SIGNING_PROOF = re.compile(r"无需|无须|不需|不必|不得|不应|不能|免于|不用|不要")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CST_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?\+08:00")


def _one_line(value, limit):
    return (isinstance(value, str) and 0 < len(value) <= limit and value == value.strip()
            and not any(not char.isprintable() for char in value))


def _shape(value, fields):
    return (isinstance(value, dict) and set(value) == set(fields)
            and all(_one_line(value[key], limit) for key, limit in fields.items()))


def _day(value):
    if not _ISO_DATE.fullmatch(value):
        raise ProjectAnswerError("PROJECT_ANSWER_PAYMENT_DATE_INVALID")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ProjectAnswerError("PROJECT_ANSWER_PAYMENT_DATE_INVALID") from None


def _deadline(value):
    if not _CST_TIMESTAMP.fullmatch(value):
        raise ProjectAnswerError("PROJECT_ANSWER_DEADLINE_INVALID")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise ProjectAnswerError("PROJECT_ANSWER_DEADLINE_INVALID") from None


def _known(question):
    return question.get("value") if question.get("status") == "known" else None


def validate_project_answer_values(questions: list[dict], answers: dict) -> None:
    """Validate the finite answer schema before consuming or freezing its facts."""
    rows = {question["key"]: question for question in questions}
    allowed = set(rows)
    if not isinstance(answers, dict) or not set(answers) <= (allowed & set(_LABELS)):
        raise ProjectAnswerError("PROJECT_ANSWER_FIELD_OUT_OF_SCOPE")
    if set(answers) & {"manager_name", "employer", "payment_evidence"} and any(
            question["key"] == "manager_name" and question.get("conditions", {}).get("ambiguous_assignment_scope")
            for question in questions):
        raise ProjectAnswerError("PROJECT_ANSWER_PERSON_SCOPE_AMBIGUOUS")
    for key, limit in (("manager_name", 48), ("employer", 120), ("agent_name", 48)):
        if key in answers and not _one_line(answers[key], limit):
            raise ProjectAnswerError("PROJECT_ANSWER_TEXT_INVALID")
        if key in answers and (held := _known(rows[key])) is not None and held != answers[key]:
            raise ProjectAnswerError("PROJECT_ANSWER_FROZEN_VALUE_CONFLICT")
    if "signer_role" in answers and (not isinstance(answers["signer_role"], str)
                                      or answers["signer_role"] not in _ROLE_VALUES):
        raise ProjectAnswerError("PROJECT_ANSWER_SIGNER_ROLE_INVALID")
    role_question = rows.get("signer_role", {})
    known_role = _known(role_question)
    role = answers.get("signer_role", known_role)
    if "signer_role" in answers and (known_role and role != known_role or
            "allowed_roles" in role_question.get("conditions", {}) and
            role not in role_question["conditions"]["allowed_roles"]):
        raise ProjectAnswerError("PROJECT_ANSWER_SIGNER_ROLE_CONFLICT")
    if "agent_name" in answers and role != "agent":
        raise ProjectAnswerError("PROJECT_ANSWER_AGENT_ROLE_REQUIRED")
    if "deadline" in answers:
        if not _shape(answers["deadline"], {"submitted_before": 25, "source_ref": 128}):
            raise ProjectAnswerError("PROJECT_ANSWER_DEADLINE_INVALID")
        _deadline(answers["deadline"]["submitted_before"])
    if "payment_evidence" in answers:
        if not _shape(answers["payment_evidence"], {"person_name": 48, "employer": 120, "paid_on": 10, "source_ref": 128}):
            raise ProjectAnswerError("PROJECT_ANSWER_PAYMENT_INVALID")
        _day(answers["payment_evidence"]["paid_on"])


def _fact(plan, key, value, source_ref, *, label=None, evidence_refs=()):
    refs = [source_ref, *evidence_refs]
    payload = {"label": label or _LABELS[key], "value": value, "source_refs": list(dict.fromkeys(refs))}
    identity = "PROJECT-ANSWER-" + key + "-" + format_digest(payload)[:24]
    plan["facts"].append({"fact_id": identity, **payload})
    return identity


def _targets(questions, keys, field):
    return {ref for key in keys if key in questions for ref in questions[key].get(field, [])}


def _add_response_section(plan, scope, *, title, ref, fact_ids, right_after=None):
    if not scope or not fact_ids:
        return None
    by_id = {req["requirement_id"]: req for req in plan["requirements"]}
    refs = [rid for rid in by_id if rid in scope and by_id[rid]["response_mode"] != "procedure"]
    if not refs:
        return None
    candidates = [(position, section) for position, section in enumerate(plan["sections"])
                  if section["document_role"] == "response" and section["render_body"]
                  and section["section_id"] != plan.get("prescribed_cover_section_id")
                  and set(section["requirement_refs"]) & set(refs)]
    if not candidates:
        return None
    position, target = max(candidates, key=lambda item: (item[1]["section_id"] == right_after,
        len(set(item[1]["requirement_refs"]) & set(refs)), -len(item[1]["requirement_refs"])))
    section = {"section_id": "PROJECT-ANSWER-SECTION-" + format_digest([ref, title, refs])[:24],
               "title": title, "kind": "facts", "document_role": "response", "requirement_refs": refs,
               "level": min(max(2, target["level"]), 3), "render_body": True,
               "source_refs": [ref], "fact_refs": fact_ids, "attachment_refs": []}
    plan["sections"].insert(position + 1, section)
    return section


def _fill_current_person_cells(plan, fields, questions):
    sections = {section["section_id"]: section for section in plan["sections"]}
    removed_markers = set()
    complete_field_requirements = {}
    requirements = {}
    for req in plan["requirements"]:
        match = _PERSONNEL_FIELDS_ONLY.fullmatch(re.sub(r"\s+", "", req["text"]))
        if req["response_mode"] == "factual_response" and match:
            needed = {"manager_name", "employer"} if match["employer"] else {"manager_name"}
            requirements[req["requirement_id"]] = (match["table"].removeprefix("拟派项目"), needed, req["text"])
    for table in plan["tables"]:
        section = sections[table["section_id"]]
        historic = any(_HISTORICAL_PERSONNEL.search(re.sub(r"\s+", "", title))
                       for title in (section["title"], table["title"]))
        if section["document_role"] != "response" or historic:
            continue
        native = section.get("native_format") or {}
        native_table = next((value for value in native.get("elements", []) if value.get("table_id") == table["table_id"]), None)
        rows = native_table["source_rows"] if native_table else [[cell["text"] for cell in row] for row in table["rows"]]
        filled_fields = set()
        for row, original in enumerate(rows):
            # Only an exact two-cell field. Exclude combined personnel tables,
            # historical performance lists, signature cells and merged targets.
            if len(original) != 2 or not whole_cell_slot(original[1]):
                continue
            label = re.sub(r"\s+", "", original[0]).rstrip("：:")
            key = {"项目负责人": "manager_name", "拟派项目负责人": "manager_name",
                   "用工主体": "employer", "用工单位": "employer"}.get(label)
            if key not in fields or not set(questions[key]["requirement_ids"]) & set(table["requirement_refs"]):
                continue
            if any(merge["row"] <= row < merge["row"] + merge["row_span"] and
                   merge["column"] <= 1 < merge["column"] + merge["col_span"] for merge in table["merges"]):
                continue
            if native_table and any(cell["row"] == row and cell["column"] == 1 and cell["underlines"]
                                    for cell in native_table["cells"]):
                continue
            actual = table["rows"][row][1]
            if actual["text"] != original[1] or actual["fact_refs"]:
                continue
            value, fact_id = fields[key]
            removed_markers.update(actual["issue_refs"])
            actual.update(text=value, fact_refs=[fact_id], issue_refs=[])
            filled_fields.add(key)
            if native_table:
                native_table["output_grid_sha256"] = format_digest(table["rows"])
                native["proof_sha256"] = format_digest({key: val for key, val in native.items() if key != "proof_sha256"})
        title = re.sub(r"\s+", "", table["title"]).removeprefix("拟派项目")
        for ref in set(table["requirement_refs"]) & requirements.keys():
            specified_title, needed, source_text = requirements[ref]
            if specified_title == title and needed <= filled_fields:
                complete_field_requirements[(table["section_id"], ref)] = source_text
    remaining_markers = {ref for table in plan["tables"] for row in table["rows"] for cell in row for ref in cell["issue_refs"]}
    filled_only = removed_markers - remaining_markers
    return_issues = []
    for issue in plan["issues"]:
        if issue["issue_id"] in filled_only and issue["reason"] == "原表空白格无唯一、允许使用的明确事实值。":
            continue
        refs = issue["requirement_refs"]
        if (len(refs) == 1 and issue["issue_id"] not in remaining_markers and issue["title"] == "事实响应资料待完善"
                and issue["reason"] == "该要求不是可自由起草的技术方法，且无明确可填字段。"
                and complete_field_requirements.get((issue["section_id"], refs[0])) == issue["source_requirement"]):
            continue
        return_issues.append(issue)
    plan["issues"] = return_issues


def authorization_blank(original):
    """Return the measured non-signature name position in the source clause."""
    if not original.startswith("本人") or negative_fill_instruction(original):
        return None
    candidates = list(_AGENT_SLOT.finditer(original))
    return candidates[0].span("blank") if len(candidates) == 1 else None


def _bind_agent_name(plan, value, fact_id, source_ref, scope):
    sources = {source["source_id"]: source for source in plan["sources"]}
    candidates = []
    for section in plan["sections"]:
        if (section["document_role"] != "response" or not _AUTHORIZATION.fullmatch(section["title"].strip())
                or not set(section["requirement_refs"]) & scope):
            continue
        native = section.get("native_format")
        if native:
            for element in native["elements"]:
                if element["kind"] != "paragraph":
                    continue
                for index, original in zip(element["fixed_indices"], element["source_texts"]):
                    span = authorization_blank(original)
                    rules = [rule for rule in element["underlines"] if rule["fixed_index"] == index
                             and (rule["start"], rule["end"]) == span and rule["bbox"] is not None
                             and rule["basis"] == "native_vector_rule"]
                    if len(rules) == 1:
                        candidates.append((section, section["fixed_content"][index], None))
        else:
            for block in section["fixed_content"]:
                if block["kind"] == "paragraph" and (span := authorization_blank(block["text"])) and any(
                        sources.get(ref, {}).get("kind") == "tender" for ref in block["source_refs"]):
                    candidates.append((section, block, span))
    if len(candidates) != 1:
        return None
    section, block, span = candidates[0]
    block["fact_refs"].append(fact_id)
    block["source_refs"].append(source_ref)
    if span:
        start, end = span
        block["text"] = block["text"][:start] + value + block["text"][end:]
    return section["section_id"]


def native_agent_value(package, section, index, original):
    """Resolve the one answer-bound native field for Word's editable runs."""
    if not _AUTHORIZATION.fullmatch(section.title.strip()) or not authorization_blank(original):
        return None
    block = section.blocks[index]
    sources = {source.source_id: source for source in package.sources}
    matches = []
    for fact in package.facts:
        if (fact.fact_id in block.fact_refs and fact.label == _LABELS["agent_name"]
                and fact.fact_id.startswith("PROJECT-ANSWER-agent_name-") and len(fact.source_refs) == 1):
            ref = fact.source_refs[0]
            source = sources.get(ref)
            payload = {"label": fact.label, "value": fact.value, "source_refs": fact.source_refs}
            if (ref in block.source_refs and source is not None and source.kind == "manual_input"
                    and source.provenance in _MANUAL_PROVENANCE and _one_line(fact.value, 48)
                    and fact.fact_id == "PROJECT-ANSWER-agent_name-" + format_digest(payload)[:24]):
                matches.append(fact.value)
    return matches[0] if len(matches) == 1 else None


def _frozen_personnel_candidates(plan, questions, scope):
    registered = {proof.get("attachment_id") for key in ("manager_name", "payment_evidence")
                  for proof in questions.get(key, {}).get("evidence_refs", [])
                  if proof.get("kind") == "reviewed_registration"}
    old_person = _known(questions.get("manager_name", {}))
    rows = []
    for row in plan["attachments"]:
        related = bool(set(row["requirement_refs"]) & scope)
        title_is_personnel = bool(re.search(r"项目负责人|项目经理|人员证明|参保|社保|社会保险|AMPP|\bCP[234]\b", row["title"], re.I))
        if row["attachment_id"] in registered or related and (
                set(row["requirement_refs"]) <= scope or title_is_personnel or row["title"] == old_person
                or (row.get("bundle_id") or "").startswith("REF-PERSON-")):
            rows.append(row)
    return {row["attachment_id"]: row for row in rows}


def _withdraw_personnel(plan, candidates, selected_name, personnel_scope, source_ref):
    withdrawn = [row for row in candidates.values() if row["title"] != selected_name]
    bundles = {row["bundle_id"] for row in withdrawn if row.get("bundle_id")}
    ids = {row["attachment_id"] for row in withdrawn}
    rows = [row for row in plan["attachments"] if row["attachment_id"] in ids or row.get("bundle_id") in bundles]
    if not rows:
        return []
    ids.update(row["attachment_id"] for row in rows)
    for section in plan["sections"]:
        section["attachment_refs"] = [ref for ref in section["attachment_refs"] if ref not in ids]
    preserved = {row["attachment_id"]: _independent_attachment(plan, row, personnel_scope, source_ref) for row in rows}
    for row in preserved.values():
        if row is not None and row.get("bundle_id") and any(
                preserved[old["attachment_id"]] is None for old in rows if old.get("bundle_id") == row["bundle_id"]):
            row.update(bundle_id=None, page_index=1, page_count=1)
    plan["attachments"] = [preserved.get(row["attachment_id"], row) for row in plan["attachments"]
                           if preserved.get(row["attachment_id"], row) is not None]
    return rows


def _independent_attachment(plan, row, personnel_scope, source_ref):
    refs = [ref for ref in row["requirement_refs"] if ref not in personnel_scope]
    if not refs:
        return None
    origin = next(section for section in plan["sections"] if section["section_id"] == row["section_id"])
    targets = [section for section in plan["sections"] if section["render_body"]
               and not set(section["requirement_refs"]) & personnel_scope
               and section["document_role"] == origin["document_role"]
               and set(refs) <= set(section["requirement_refs"])
               and section["section_id"] != plan.get("prescribed_cover_section_id")]
    if targets:
        target = min(targets, key=lambda section: (bool(section.get("native_format")), len(section["requirement_refs"])))
    else:
        section_id = "PROJECT-ANSWER-ATTACHMENT-" + format_digest([source_ref, row["section_id"], refs])[:24]
        target = {"section_id": section_id, "title": "其他要求对应的证明材料", "kind": "attachments",
                  "level": origin["level"], "render_body": True, "document_role": origin["document_role"],
                  "requirement_refs": refs, "attachment_refs": [], "source_refs": [source_ref]}
        plan["sections"].insert(plan["sections"].index(origin) + 1, target)
    target["attachment_refs"].append(row["attachment_id"])
    return {**row, "section_id": target["section_id"], "requirement_refs": refs}


def _payment_source_evidence(source, proof_refs, sources, frozen_attachments, uploaded):
    if not uploaded:
        return [row for row in proof_refs if row["source_ref"] == source["source_id"]], False
    proof_hash = source["sha256"]
    matching_sources = {ref for ref, frozen in sources.items()
                        if ref != source["source_id"] and frozen["sha256"] == proof_hash}
    matching_attachments = {row["attachment_id"] for row in frozen_attachments if row["sha256"] == proof_hash}
    if not matching_sources and not matching_attachments:
        return [], True
    # A frozen page keeps its original disposition even if uploaded under a new filename.
    return [row for row in proof_refs if row["source_ref"] in matching_sources or
            row.get("attachment_id") in matching_attachments], False


def _payment_note(plan, questions, answers, sources, candidates, effective_deadline, *,
                  frozen_attachments, uploaded_payment_source_id):
    payment = answers.get("payment_evidence")
    if not payment:
        return "实际缴费凭证仍待核对。", None
    source = sources.get(payment["source_ref"])
    if source is None or source["kind"] not in {"attachment", "company_material"}:
        raise ProjectAnswerError("PROJECT_ANSWER_PAYMENT_SOURCE_INVALID")
    proof_refs = questions["payment_evidence"].get("evidence_refs", [])
    uploaded = (source["source_id"] == uploaded_payment_source_id and source["kind"] == "attachment"
                and source["provenance"] == "original" and source["source_id"] == "F-PAYMENT-" + source["sha256"])
    proof, uploaded = _payment_source_evidence(source, proof_refs, sources, frozen_attachments, uploaded)
    if any(row["kind"] == "reviewed_registration" for row in proof):
        return "所选原图是参保登记页，实际缴费凭证仍待补。", None
    payment_refs = set(questions["payment_evidence"]["requirement_ids"])
    if not uploaded:
        scoped = [row for row in candidates.values() if any(
                  item.get("attachment_id") == row["attachment_id"] and item["source_ref"] in row["source_refs"]
                  for item in proof)]
        if not scoped:
            raise ProjectAnswerError("PROJECT_ANSWER_PAYMENT_SOURCE_OUT_OF_SCOPE")
        payment_attachment_ids = {row["attachment_id"] for row in scoped
                                  if re.search(r"实缴|缴费", row["title"]) and not re.search(r"登记", row["title"])}
        if not payment_attachment_ids:
            return "所选历史材料的实缴用途待核，仍需提供可核对的实际缴费凭证。", None
        if not any(row["attachment_id"] in payment_attachment_ids and
                   payment_refs.intersection(row["requirement_refs"]) for row in plan["attachments"]):
            return "所选历史凭证与本次人员安排的归属待核，未作为本次实缴附件采用。", None
    if not answers.get("manager_name") or not answers.get("employer"):
        return "已登记需核对的缴费材料，拟派人员、用工主体和凭证归属仍待确认。", None
    if payment["person_name"] != answers["manager_name"] or payment["employer"] != answers["employer"]:
        return "录入的缴费凭证人员或用工主体与本项目选择不一致，须重新核对归属。", None
    own_employment_required = any(req["requirement_id"] in payment_refs and re.search(
        r"自有员工|本单位(?:在职)?员工|投标人(?:的)?正式员工|响应人(?:的)?正式员工", re.sub(r"\s+", "", req["text"]))
        for req in plan["requirements"])
    if own_employment_required and answers["employer"] != plan["company"]["name"]:
        return "本项目填报的用工主体与自有员工条件存在待核差异，须核对人员关系及实缴凭证。", None
    conditions = questions["payment_evidence"].get("conditions", {})
    window = conditions.get("window_days")
    if type(window) is not int or not 1 <= window <= 3650:
        return "已记录所选材料及填报的缴费日期，招标规定的实缴区间仍待核。", source if uploaded else None
    if not effective_deadline:
        return "已记录所选材料及填报的缴费日期，正式递交截止及实缴窗口仍待核。", source if uploaded else None
    distance = (_deadline(effective_deadline["submitted_before"]).date() - _day(payment["paid_on"])).days
    if distance == 0:
        return "录入缴费日期与正式截止日相同，缴费时点及招标期限的适用口径仍待核。", None
    if not 0 < distance <= window:
        return f"录入缴费日期不在截止日前 {window} 日区间内，须补核适用的实缴凭证。", None
    return (f"录入缴费日期距已定位的正式截止日 {distance} 日。"
            "原件对应人员、用工主体、实际缴费和资格仍待核。", source if uploaded else None)


def _qualify_deadline(question, supplied, sources):
    def original_tender(ref):
        source = sources.get(ref, {})
        return source.get("kind") == "tender" and source.get("provenance") in {"original", "synthetic"}

    known = _known(question)
    if supplied and not original_tender(supplied["source_ref"]):
        raise ProjectAnswerError("PROJECT_ANSWER_DEADLINE_SOURCE_INVALID")
    if not known:
        if supplied and supplied["source_ref"] not in question.get("source_refs", []):
            raise ProjectAnswerError("PROJECT_ANSWER_DEADLINE_SOURCE_OUT_OF_SCOPE")
        return supplied, False
    value_sources = question.get("value_source_refs", [])
    if known["source_ref"] not in value_sources or not original_tender(known["source_ref"]):
        raise ProjectAnswerError("PROJECT_ANSWER_DEADLINE_SOURCE_INVALID")
    if supplied is None:
        return known, True
    if (supplied["source_ref"] not in value_sources or
            _deadline(supplied["submitted_before"]) != _deadline(known["submitted_before"])):
        raise ProjectAnswerError("PROJECT_ANSWER_DEADLINE_SOURCE_CONFLICT")
    return {**known, "source_ref": supplied["source_ref"]}, True


def _add_pending_payment(plan, section, scope, source, proof, signing_ref):
    if section is None or source is None:
        return
    reqs = [ref for ref in section["requirement_refs"] if ref in scope]
    if not reqs:
        return
    identity = "PROJECT-PAYMENT-" + format_digest([signing_ref, source["source_id"], reqs])[:24]
    attachment = {"attachment_id": identity, "section_id": section["section_id"],
                  "title": "本次提交的社保凭证图片（实缴待核）", "path": source["path"],
                  "sha256": source["sha256"], "source_refs": [source["source_id"], signing_ref],
                  "requirement_refs": reqs, "synthetic": plan["synthetic"],
                  "description": f"经办人填报的缴费日期为 {proof['paid_on']}。请对照原件核实人员、单位及实缴记录，资格仍待核。"}
    plan["attachments"].append(attachment)
    section["attachment_refs"].append(identity)


def apply_project_answers(plan: dict, questions: list[dict], answers: dict, source_ref: str, *,
                          uploaded_payment_source_id: str | None = None) -> dict:
    """Project confirmed decisions into N4's plan without altering frozen inputs."""
    validate_project_answer_values(questions, answers)
    if not answers:
        return plan
    result = deepcopy(plan)
    sources = {source["source_id"]: source for source in result["sources"]}
    source = sources.get(source_ref)
    if source is None or source["kind"] != "manual_input" or source["provenance"] not in _MANUAL_PROVENANCE:
        raise ProjectAnswerError("PROJECT_ANSWER_SNAPSHOT_SOURCE_INVALID")
    indexed = {question["key"]: question for question in questions}
    if set(answers) & _SOCIAL_FIELDS:
        _apply_personnel(result, indexed, answers, sources, source_ref, uploaded_payment_source_id)
    if set(answers) & _SIGNING_FIELDS:
        _apply_signing(result, indexed, answers, source_ref)
    return result


def _apply_personnel(plan, questions, answers, sources, ref, uploaded_payment_source_id):
    fields = {key: (answers[key], _fact(plan, key, answers[key], ref))
              for key in ("manager_name", "employer") if key in answers}
    _fill_current_person_cells(plan, fields, questions)
    facts = [identity for _, identity in fields.values()]
    scope = _targets(questions, ("payment_evidence",), "requirement_ids")
    manager_scope = _targets(questions, ("manager_name",), "requirement_ids")
    personal_scope = scope | {req["requirement_id"] for req in plan["requirements"]
                             if req["requirement_id"] in manager_scope and req["category"] == "qualification"
                             and req["response_mode"] in {"attachment", "factual_response"}}
    candidates = _frozen_personnel_candidates(plan, questions, personal_scope)
    frozen_attachments = plan["attachments"]
    group_scope = personal_scope | {req["requirement_id"] for req in plan["requirements"]
        if re.search(r"人员要求材料|项目组人员配置情况", re.sub(r"\s+", "", req["text"][:90]))}
    withdrawn = _withdraw_personnel(plan, candidates, answers["manager_name"], group_scope, ref) if "manager_name" in answers else []
    submitted, verified = _qualify_deadline(questions.get("deadline", {}), answers.get("deadline"), sources)
    if submitted and "deadline" in answers:
        label = _LABELS["deadline"] if verified else "人工提供的递交截止时点（待核）"
        facts.append(_fact(plan, "deadline", submitted["submitted_before"], ref, label=label,
                           evidence_refs=[submitted["source_ref"]]))
    effective = {key: value for key in ("manager_name", "employer")
                 if (value := _known(questions.get(key, {}))) is not None}
    effective.update(answers)
    evidence_note, proof = _payment_note(plan, questions, effective, sources, candidates, submitted if verified else None,
                                         frozen_attachments=frozen_attachments,
                                         uploaded_payment_source_id=uploaded_payment_source_id)
    withdrawal_note = "已撤出归属需重新确认的历史人员证明；历史检索和读取记录仍可查。" if withdrawn else ""
    pending = "".join(("人工提供的递交截止时点仍待正式原件核对。" if submitted and not verified else "",
                       evidence_note, withdrawal_note))
    include_note = bool(answers.get("payment_evidence") or withdrawn)
    if include_note:
        facts.append(_fact(plan, "payment_evidence", pending, ref,
                           evidence_refs=[proof["source_id"]] if proof else []))
    section = _add_response_section(plan, scope or manager_scope, title="拟派负责人及用工资料核对",
                                    ref=ref, fact_ids=facts)
    if proof:
        _add_pending_payment(plan, section, scope, proof, answers["payment_evidence"], ref)
    withdrawal_scope = {rid for row in withdrawn for rid in row["requirement_refs"]} & group_scope
    assembled_requirements = {rid for row in plan["attachments"] for rid in row["requirement_refs"]}
    for issue in plan["issues"]:
        refs = set(issue["requirement_refs"])
        if refs and refs <= scope:
            issue["title"] = "拟派负责人社保资料待核"
            issue["action"] = "核对本次拟派人员、用工主体、正式递交截止及窗口内的实缴原件。"
            manager, employer = effective.get("manager_name"), effective.get("employer")
            note = ((f"本项目填报的拟派负责人为{manager}。" if manager else "")
                    + (f"填报的用工主体为{employer}，人员关系待核。" if employer else "")
                    + pending)
        elif refs & withdrawal_scope:
            has_assembled = bool(refs & assembled_requirements)
            note = ("已撤出归属需重新确认的部分人员证明；本项仍有已装配证明，适用条件待核。" if has_assembled else
                    "已撤出归属需重新确认的历史人员证明；本项当前未装配相关证明，待按本次拟派安排核对。")
            if not has_assembled and issue["title"] == "已附证明的适用条件仍待核对":
                issue["title"] = "本次拟派人员证明待核对及补充"
            if issue["action"] == "结合正文中的完整证明包逐项核对本条条件；转移附件位置不代表资格通过。":
                issue["reason"] += "\n原办理动作：" + issue["action"]
                issue["action"] = ("逐项核对正文保留证明的适用条件，并按本次拟派安排补齐相应证明。" if has_assembled else
                                   "核对本次拟派人员及本条证明要求，确认后装配相应证明。")
            else:
                issue["action"] = "按本次拟派安排重新核对证明归属及其他原要求。" + issue["action"]
        else:
            continue
        issue["reason"] = note + "\n确认前的材料检索与读取记录：\n" + issue["reason"]


def _signing_proof_roles(text):
    """Recognize only an affirmative proof action scoped to a signing actor."""
    roles = set()
    for clause in _SIGNING_PROOF_SPLIT.split(re.sub(r"\s+", "", text)):
        for match in _SIGNING_PROOF.finditer(clause):
            actor_prefix = clause[:match.start()]
            if _NO_SIGNING_PROOF.search(clause[:match.start("subject")].rsplit("，", 1)[-1]):
                continue
            role = "agent" if match["proof"] == "授权委托书" else "legal_representative"
            if role == "legal_representative" and "代理人" in match["subject"]:
                continue
            other_role = "legal_representative" if role == "agent" else "agent"
            actor = re.search(rf"(?:{_SIGNING_ACTOR[role]}).{{0,28}}(?:{_SIGNING_ACTION})", actor_prefix)
            other = re.search(rf"(?:{_SIGNING_ACTOR[other_role]}).{{0,28}}(?:{_SIGNING_ACTION})", actor_prefix)
            if actor and not other:
                roles.add(role)
    return roles


def _apply_signing(plan, questions, answers, ref):
    question = questions.get("signer_role", {})
    role = answers.get("signer_role", _known(question))
    role_refs = question.get("value_source_refs", []) if "signer_role" not in answers else []
    facts = [_fact(plan, "signer_role", _ROLE_VALUES[role], ref, evidence_refs=role_refs)]
    bound_section = None
    name = answers.get("agent_name")
    scope = _targets(questions, _SIGNING_FIELDS, "requirement_ids")
    proof_requirements = {req["requirement_id"]: _signing_proof_roles(req["text"])
                          for req in plan["requirements"] if req["requirement_id"] in scope}
    proof_rids = {rid for rid, roles in proof_requirements.items() if roles}
    proof_required = any(role in roles for roles in proof_requirements.values())
    if role == "agent":
        if name:
            fact_id = _fact(plan, "agent_name", name, ref)
            name_scope = _targets(questions, ("agent_name",), "requirement_ids")
            bound_section = _bind_agent_name(plan, name, fact_id, ref, name_scope)
            if bound_section is None:
                facts.append(fact_id)
        action = ("按招标文件提供授权委托书，核对代理人、授权范围、期限、日期和签章。" if proof_required else
                  "按招标文件核对授权委托书及身份证明要求，确认代理权限并完成签署。")
        pending = "本项目拟由代理人签署。" + (f"拟任代理人：{name}。" if name else "代理人姓名待补。")
    else:
        action = ("补充实际签署的法定代表人（单位负责人）身份证明，核对签署人、证件和签章。" if proof_required else
                  "由法定代表人（单位负责人）完成签署，依招标文件核对是否另需附其身份证明。")
        pending = "本项目拟由法定代表人（单位负责人）签署。"
    facts.append(_fact(plan, "signer_role", action, ref, label="签署证明待补" if proof_required else "签署与证明办理"))
    _add_response_section(plan, scope, title="签署方式及证明核对", ref=ref, fact_ids=facts, right_after=bound_section)
    issue_ids = _targets(questions, _SIGNING_FIELDS, "issue_ids")
    for issue in plan["issues"]:
        refs = set(issue["requirement_refs"])
        if not refs or not refs <= scope:
            continue
        issue_roles = _signing_proof_roles(issue["source_requirement"]) if refs <= proof_rids else set()
        if issue["issue_id"] not in issue_ids and not issue_roles:
            continue
        local_proof = role in issue_roles and all(role in proof_requirements[rid] for rid in refs)
        unresolved = "签署仍待办理。"
        if local_proof:
            unresolved = "签署及本签署方式对应的证明仍待办理。"
            if issue["title"] == "按实际签署人补齐身份证明或授权":
                issue["action"] = action
            else:
                issue["action"] = action + " " + issue["action"]
        issue["reason"] = pending + unresolved + "\n" + issue["reason"]
    for operation in plan["review_actions"]:
        refs = operation["requirement_refs"]
        if (refs and all(role in proof_requirements.get(rid, ()) for rid in refs)
                and role in _signing_proof_roles(operation["source_requirement"])):
            operation["instruction"] = pending + action + " " + operation["instruction"]
