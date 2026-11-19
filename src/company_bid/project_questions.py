"""Derive project decisions from a validated, frozen N3 PreparedBid."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
import re
import unicodedata


_LABELS = {
    "manager_name": "本项目拟派负责人",
    "employer": "本次人员的用工主体",
    "deadline": "响应文件正式递交截止时间",
    "payment_evidence": "本次人员的社保实缴凭据",
    "signer_role": "响应文件签署身份",
    "agent_name": "本次授权代理人姓名",
}
_FIELD_LABELS = {
    "manager_name": {"项目负责人", "项目负责人姓名", "拟派项目负责人", "拟派项目负责人姓名",
                     "本项目负责人", "本项目拟派负责人", "项目经理", "项目经理姓名", "拟派项目经理"},
    "employer": {"用工主体", "雇用单位", "用人单位", "缴费单位", "参保单位"},
    "signer_role": {"签署身份", "响应文件签署身份", "投标文件签署身份"},
    "agent_name": {"代理人姓名", "授权代理人姓名", "授权代表姓名", "委托代理人姓名", "被授权人姓名"},
}
_ROLE_VALUES = {"legal_representative": "legal_representative", "法定代表人": "legal_representative",
                "法人代表": "legal_representative", "单位负责人": "legal_representative",
                "agent": "agent", "代理人": "agent", "委托代理人": "agent", "授权代表": "agent"}
_ROLES = ("legal_representative", "agent")
_MANAGER = r"项目(?:负责人|经理)"
_BIDDER = r"(?:响应人|投标人|供应商|报价人|应答人)"
_BID_FILE = r"(?:响应|投标|报价|应答|比选|竞争性谈判)(?:文件|函|书)"
_LEGAL = r"(?:法定代表人|法人代表|单位负责人)(?:\((?:单位)?负责人\))?"
_AGENT = r"(?:其)?(?:(?:授权的?)?(?:委托)?代理人|(?:授权的?|委托|受托)代表|授权委托人|被授权人)"
_SIGN = r"(?:签字|签名|签署|签章|签字盖章|盖章|加盖(?:个人)?电子(?:印章|签章))"
_SIGN_OR = rf"(?:亲自)?(?:{_SIGN})?(?:或者|或|、)(?:由)?"
_SIGN_EITHER = re.compile(rf"(?:{_LEGAL}{_SIGN_OR}{_AGENT}|{_AGENT}{_SIGN_OR}{_LEGAL})")
_SIGN_AFTER_ROLE = re.compile(rf"^[:(]*(?:本人|亲自)?{_SIGN}")
_SIGN_BEFORE_ROLE = re.compile(rf"{_SIGN}(?:人|者|身份)?(?:应|须)?(?:由|为|是)?$")
_BID_SIGN_SCOPE = re.compile(rf"{_BID_FILE}|{_BIDDER}(?!代表)|(?:身份|授权)(?:委托书|证明)")
_SIGN_BRANCH = re.compile(rf"由(?:{_BIDDER}的?)?({_LEGAL}|{_AGENT})(?:亲自)?"
                          rf"{_SIGN}(?:(?:或者|或){_SIGN})?.{{0,12}}?(?:的|时)$")
_SIGN_DIRECTIVE = re.compile(rf"(?:必须|仅可|只能|只允许|须|应|需|统一)(?:当)?(?:由)?"
                             rf"(?:{_BIDDER}的?)?({_LEGAL}|{_AGENT})(?:本人|亲自)?{_SIGN}"
                             rf"|({_LEGAL}|{_AGENT})(?:本人|亲自)?(?:必须|须|应|需)(?:当)?{_SIGN}"
                             rf"|{_BID_FILE}(?:封面)?由(?:{_BIDDER}的?)?({_LEGAL}|{_AGENT})(?:本人|亲自)?{_SIGN}")
_SIGN_PERMISSION = re.compile(rf"(?:也可|亦可|或可|还可|可以|可)由({_LEGAL}|{_AGENT})(?:本人|亲自)?{_SIGN}")
_SIGN_ONLY_LEGAL = re.compile(r"(?:不得|禁止|不允许)(?:由|委托)(?:任何|其)?(?:授权|委托)?(?:代理人|代表)")
_FORM_SIGNING = re.compile(r"^(?:\d+(?:\.\d+)*[、.\s]*)?(?:公司基本情况|报价表|投标报价表|响应报价表|"
                           r"投标函|响应函|质量保证承诺书|售后服务承诺|法定代表人身份证明)")
_AGENT_FORM = re.compile(r"^(?:\d+(?:\.\d+)*[、.\s]*)?(?:法定代表人|法人代表)?授权(?:委托)?书")
_MANAGER_FIELD = re.compile(rf"(?m)^\s*(?:(?:本项目)?拟(?:派|投入|任))?{_MANAGER}(?:姓名)?\s*"
                            r"(?=[:：]|\t|\n|$)")
_MANAGER_APPOINTMENT = re.compile(rf"(?:拟(?:派|投入|任|定)(?:的|一名)?|确定|选定|填写|填报|配备|任命|明确)"
                                  rf"(?:本项目)?({_MANAGER})|({_MANAGER})(?:姓名|人选|必须|应|须|需|持有|具备|具有)")
_MANAGER_PAIR = re.compile(rf"{_MANAGER}(?:及|和|与|、|以及)(?:本(?:项目)?的?)?{_MANAGER}")
_MANAGER_USE = re.compile(rf"(?:拟(?:派|投入|任|定)(?:的|一名)?|确定|选定|填写|填报|配备|任命|明确|更换)"
                          rf"(?:本项目)?{_MANAGER}|{_MANAGER}(?:姓名|人选)|"
                          rf"{_MANAGER}(?:在本项目|在响应文件|自(?:19|20)\d\d年|必须|应当|需|应|须|"
                          r"持有|具备|具有|不得随意更换|不得擅自更换|不少于|至少|担任|为|是)")
_EMPLOYMENT = re.compile(r"自有员工|本单位(?:在职)?(?:职工|员工)|本企业(?:在职)?(?:人员|员工|职工)|"
                          r"(?:用工主体|用人单位|缴费单位|雇用单位|参保单位)|(?:本单位|本企业)缴纳")
_EMPLOYMENT_FIELD = re.compile(r"(?:提供|确认|注明|填写|列明|列出|载明|明确|填报)(?:本次|有关)?"
                               r"(?:用工主体|用人单位|缴费单位|雇用单位|参保单位)")
_SOCIAL = r"(?:社会保险|社保|养老保险)"
_PAY = re.compile(rf"(?:实缴|实际(?:缴费|缴纳)|已(?:缴费|缴纳)|缴纳(?:过)?(?:\d+|[一二三四五六七八九十]+)次|"
                  rf"(?:缴费|缴纳)记录|{_SOCIAL}(?:缴费|缴纳)|缴纳{_SOCIAL})")
_PROOF = re.compile(r"证明|凭据|凭证|记录|明细|票据")
_DEADLINE_LABEL = (rf"(?:{_BID_FILE}(?:的)?(?:递交|提交|送达|上传)|(?:递交|提交|送达|上传){_BID_FILE}(?:的)?|"
                   r"(?:递交|提交|投标|响应|报价|应答|截标))(?:的)?截止(?:时间|时点|日期|日)?")
_DEADLINE = re.compile(_DEADLINE_LABEL)
_DEADLINE_MARKER = r":|为|是|定于|调整为|变更为|延至|延长至|\t+"
_DEADLINE_INSTANT = (r"(?P<value>(?:19|20)\d{2}(?:年\d{1,2}月\d{1,2}日|[-/.]\d{1,2}[-/.]\d{1,2}(?=T|\s))"
                     r"(?:\(北京时间\)|北京时间|T)?\s*\d{1,2}(?::\d{2}(?::\d{2})?|时\d{1,2}分(?:\d{1,2}秒)?))"
                     r"(?!\d|:)(?!\s*(?:至|到|或|\(?(?:[-+]\d|UTC|GMT|PST|EST|Z\b|当地时间)))")
_FORMAL_DEADLINE = re.compile(rf"{_DEADLINE_LABEL}(?:和地点|及地点)?(?:{_DEADLINE_MARKER})"
                              rf"(?:\s|\(北京时间\)|北京时间)*{_DEADLINE_INSTANT}")
_DEADLINE_ACTOR = re.compile(rf"^(?:\d+(?:\.\d+)*[、.\s]*)?(?:{_BIDDER})?(?:应|须|需|必须)(?:当)?(?:在|于)"
                             rf"{_DEADLINE_INSTANT}(?:之)?前(?:完成)?(?:递交|提交|送达|上传){_BID_FILE}")
_DEADLINE_DOCUMENT = re.compile(rf"^(?:\d+(?:\.\d+)*[、.\s]*)?{_BID_FILE}(?:应|须|需|必须)(?:当)?(?:在|于)"
                                rf"{_DEADLINE_INSTANT}(?:之)?前(?:完成)?(?:递交|提交|送达|上传)")
_FULL_SOURCE_DEADLINE = re.compile(rf"^(?:\d+(?:\.\d+)*[、.\s]*)?(?:本项目(?:的)?)?(?:现将)?{_DEADLINE_LABEL}"
                                    rf"(?:和地点|及地点)?(?:$|{_DEADLINE_MARKER}|详见)")
_RELATIVE_SUBMISSION = re.compile(rf"(?:应|须|需|必须)(?:在|于).{{0,45}}{_DEADLINE_LABEL}前"
                                  rf"(?:完成)?(?:递交|提交|送达|上传){_BID_FILE}")
_NON_SUBMISSION_DEADLINE = re.compile(r"(?:报名|发放|领取|获取|下载|答疑|报名费|澄清|异议|保证金|"
                                      r"开标|评审|投标有效期|响应有效期)(?:的)?(?:时间|日期|截止|受理|提交)?(?:为|:|\t)")
_NEGATIVE = re.compile(r"(?:不要求|无需|无须|不必|不需要?|免于|免予|不应|不得|禁止|不允许|"
                       r"不接受|不采用|不适用|不含|不包括)(?:再|另行|重复)?(?:填写|填报|安排|配备|委派|拟派|"
                       r"选派|设立|设置|提供|提交|出具|附上?|核对|由|选择)?")
_SCOPE_EXCLUSION = re.compile(r"^(?:第[一二三四五六七八九十\d]+(?:章|条)[、. ]*|\d+(?:\.\d+)*[、. ]*|\(\d+\)[、. ]*)*"
                              r"(?:例如|示例|举例|样例|仅供参考|参考格式|历史(?:项目|业绩|合同|标书)|"
                              r"(?:类似|同类|以往|原有|既往|往期|已完(?:成|工)|以前)(?:合同|项目|业绩)|"
                              r"(?:响应人|投标人|供应商)?业绩(?:统计|汇总|一览)表|(?:本|该)合同(?:履行|签署|生效)|"
                              r"(?:合同|协议)(?:模板|格式|样本|通用条款|专用条款)|异议函|"
                              r"(?:如果|若|如|待)(?:中标|成交|中选|签约|签订合同)|中标(?:人|后)|成交(?:人|后)|"
                              r"甲方|乙方|承包人|发包人|采购人|招标人)")
_CONTRACT_SECTION = re.compile(r"^(?:第[一二三四五六七八九十\d]+章\s*)?合同(?:协议书|条款|模板|格式)(?:[：:]|$)")
_CONDITIONAL = re.compile(r"^(?:如果|若|如)(?!由|实由)")
_ROLE_SUBJECT = re.compile(rf"{_LEGAL}|{_AGENT}")
_MANAGER_SUBJECT = re.compile(_MANAGER)
_SOCIAL_SUBJECT = re.compile(_SOCIAL)
_PAYMENT_PERSON = re.compile(rf"{_MANAGER}|拟(?:派|投入|任)人员|本项目人员")
_PAYMENT_WINDOW = re.compile(rf"{_DEADLINE_LABEL}(?:之)?前([1-9]\d{{0,3}})(?:个)?(?:自然|日历)?[日天](?:以内|内)")
_PAYMENT_MINIMUM = re.compile(r"至少(?:须)?(?:缴纳|缴费)(?:过)?([1-9]\d*|[一二两三四五六七八九十])次")
_PAYMENT_NUMBER = {char: value for value, char in enumerate("一二三四五六七八九十", 1)} | {"两": 2}
_MANAGER_LOT = re.compile(r"第?[一二三四五六七八九十\d]+(?:标段|包件)")
_CORPORATE_TABLE = re.compile(r"^(?:\d+[.、 ]*)?(?:公司|企业|投标人|响应人)基本情况")
_AGENT_PROOF_USE = re.compile(r"(?:必须|应|须|需)(?:附|提交|提供|出具)(?:由[^,]{1,40}签署的)?(?:法定代表人)?授权委托书")
_QUOTED = re.compile(r'“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|"[^"\n]*"')
_CST = timezone(timedelta(hours=8))


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip()


def _clauses(text: str) -> list[str]:
    # Join visual line wrapping. Keep TSV column breaks so formal labels can
    # be identified without reading a neighboring field as their value.
    text = _QUOTED.sub("", text)
    text = re.sub(r"[^\S\t]+", lambda gap: " " if gap.start() and text[gap.start() - 1].isdigit()
                  and text[gap.end():gap.end() + 1].isdigit() else "", text)
    return [part.strip() for part in re.split(r"[。;！？!?]", text) if part.strip()]


def _applicable_parts(clause: str) -> list[str]:
    parts = re.split(r",|(?:但是|但)(?=(?:须|应|需|必须|仍))", clause)
    return [part for part in parts if not _SCOPE_EXCLUSION.search(part.strip())]


def _negative_for(part: str, subject: re.Pattern) -> bool:
    """A local denial of a choice is different from a constraint on a chosen person."""
    for negative in _NEGATIVE.finditer(part):
        before = part[max(0, negative.start() - 24):negative.start()]
        if subject.search(part[negative.end():negative.end() + 24]) or (
                subject.search(before) and re.search(r"(?:姓名|人选|证明|凭据|凭证|记录|明细|票据)$", before)):
            return True
    return False


def _deadline_value(raw: str) -> str | None:
    fields = [int(field) for field in re.findall(r"\d+", raw)]
    try:
        return datetime(*fields, tzinfo=_CST).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return None


def _source_location(source: dict, kind: str) -> dict:
    return {"kind": kind, "source_ref": source["source_id"], "locator": source["locator"], "title": source["title"]}


def _held_value(key: str, facts: list[dict], sources: dict, project_source_ids: set[str]) -> tuple[str | None, list[str]]:
    values = defaultdict(list)
    for fact in facts:
        refs = fact["source_refs"]
        if (_normalize(fact["label"]) not in _FIELD_LABELS[key] or not refs or not set(refs) <= project_source_ids or
                any(sources[ref]["kind"] != "manual_input" or
                    sources[ref].get("provenance") not in {"manual_input", "user_authorized_fact", "synthetic"} for ref in refs)):
            continue
        value = fact["value"].strip()
        if key == "signer_role":
            value = _ROLE_VALUES.get(value)
        if value and "\n" not in value and "\r" not in value:
            values[value].extend(refs)
    if len(values) == 1:
        value, refs = next(iter(values.items()))
        return value, list(dict.fromkeys(refs))
    return None, []


def _support_issues(key: str, issues: list[dict], tables: list[dict], requirement_ids: list[str]) -> list[dict]:
    patterns = {
        "manager_name": r"拟派(?:项目)?负责人|项目(?:负责人|经理)(?:姓名|人选)",
        "employer": r"用工主体|用人单位|缴费单位|参保单位|自有员工",
        "deadline": r"正式递交截止|正式投标截止",
        "payment_evidence": r"参保登记|社保(?:证明|缴费|实缴)|社会保险(?:证明|缴费|实缴)|实缴|实际缴费",
        "signer_role": r"实际签署人|签署身份|签署方式|本人签署|直接签署",
        "agent_name": r"实际签署人|签署身份|代理人姓名|被授权人姓名|授权(?:人选|委托书)",
    }
    ids = set(requirement_ids)
    table_issues = set()
    if key == "manager_name":
        for table in tables:
            if ids.isdisjoint(table["requirement_refs"]):
                continue
            for cells in table["rows"]:
                if cells and _normalize(cells[0]["text"]).rstrip(":") in _FIELD_LABELS[key]:
                    table_issues.update(ref for cell in cells[1:] for ref in cell["issue_refs"])
    return [issue for issue in issues if not ids.isdisjoint(issue["requirement_refs"]) and (
        issue["issue_id"] in table_issues or re.search(patterns[key],
            " ".join([issue["title"], issue["reason"].split("\n", 1)[0], issue["action"]])))]


def _related_proof(key: str, plan: dict, requirement_ids: list[str], issues: list[dict], sources: dict) -> list[dict]:
    if key not in {"manager_name", "employer", "payment_evidence", "signer_role", "agent_name"}:
        return []
    attachments_by_source = defaultdict(list)
    result = {}
    for attachment in plan["attachments"]:
        for ref in attachment["source_refs"]:
            source = sources[ref]
            if source["kind"] not in {"company_material", "attachment"}:
                continue
            attachments_by_source[ref].append(attachment["attachment_id"])
            if set(requirement_ids).intersection(attachment["requirement_refs"]):
                identity = (ref, attachment["attachment_id"])
                result[identity] = {**_source_location(source, "attachment"), "title": attachment["title"],
                                    "attachment_id": attachment["attachment_id"]}
    if key not in {"manager_name", "employer", "payment_evidence"}:
        return list(result.values())
    # PreparedBid retains the independent page review in the issue notice.
    # Expose it only when the notice resolves to one frozen, scoped original.
    for issue in issues:
        note = re.search(r"^原图复核候选（(?:Word 原件中的 )?([^）]+)）。本页为职工参保登记明细。([^\n]*)$",
                         issue["reason"], re.M)
        if not note:
            continue
        locator, reading = note.groups()
        candidates = [s for s in sources.values() if s["kind"] in {"company_material", "attachment"} and (
            s["locator"] == locator or s["locator"].startswith("docx/") and
            PurePosixPath(s["locator"]).name == locator)]
        if len(candidates) != 1:
            continue
        source = candidates[0]
        transcription = ("ocr_failed_pending_review" if "读取或校验失败" in reading else
                         "ocr_no_text_unresolved" if "未取得文字" in reading else "not_confirmed")
        attachment_ids = attachments_by_source[source["source_id"]] or [None]
        for attachment_id in attachment_ids:
            evidence = {**_source_location(source, "reviewed_registration"), "title": "职工参保登记明细",
                        "transcription_status": transcription}
            if attachment_id:
                evidence["attachment_id"] = attachment_id
            result[(source["source_id"], attachment_id)] = evidence
    return list(result.values())


def _personnel_needs(part: str) -> set[str]:
    needs = set()
    manager = _MANAGER_USE.search(part) and not _negative_for(part, _MANAGER_SUBJECT)
    payment = (_SOCIAL_SUBJECT.search(part) and _PAY.search(part) and _PROOF.search(part) and
               _PAYMENT_PERSON.search(part) and not _negative_for(part, _SOCIAL_SUBJECT))
    if manager:
        needs.add("manager_name")
    if (manager or payment) and _EMPLOYMENT.search(part):
        needs.add("employer")
    if payment:
        needs.add("payment_evidence")
        if _DEADLINE.search(part):
            needs.add("deadline")
    return needs


def _signing_routes(clause: str, parts: list[str], text: str) -> tuple[list[set[str]], set[str]]:
    routes = []
    branches = set()
    expressed = set()
    permissions = set()
    if not (_BID_SIGN_SCOPE.search(clause) or _FORM_SIGNING.search(text)):
        return routes, branches
    for part in parts:
        if re.search(r"(?:异议|投诉|质疑)函|(?:本|该)合同(?:签|生效)", part):
            continue
        if _SIGN_ONLY_LEGAL.search(part) and re.search(_SIGN, clause):
            routes.append({"legal_representative"})
        if _negative_for(part, _ROLE_SUBJECT):
            continue
        if pair := _SIGN_EITHER.search(part):
            if (re.search(_SIGN, pair[0]) or _SIGN_AFTER_ROLE.search(part[pair.end():]) or
                    _SIGN_BEFORE_ROLE.search(part[:pair.start()])):
                routes.append(set(_ROLES))
            continue
        if branch := _SIGN_BRANCH.search(part):
            if _PROOF.search(clause) or "授权委托书" in clause:
                branches.add("legal_representative" if re.fullmatch(_LEGAL, branch[1]) else "agent")
            continue
        if permission := _SIGN_PERMISSION.search(part):
            permissions.add("legal_representative" if re.fullmatch(_LEGAL, permission[1]) else "agent")
        if (single := _SIGN_DIRECTIVE.search(part)) and (re.search(_BID_FILE, part) or _FORM_SIGNING.search(text)):
            actor = next(value for value in single.groups() if value)
            expressed.add("legal_representative" if re.fullmatch(_LEGAL, actor) else "agent")
    if expressed or permissions == set(_ROLES):
        routes.append(expressed | permissions)
    return routes, branches


def build_project_questions(prepared: dict, *, project_source_ids: set[str] | None = None) -> list[dict]:
    """Project questions never convert a candidate or a selection into proof.

    Pass the serialized PreparedBid under the N3 checkpoint's ``prepared`` key.
    ``source_refs`` bind the triggers to tender originals and ``value_source_refs``
    identify the distinct sources for a known value. Evidence locators are display
    references, and no file contents or private filesystem paths are exposed.
    Only the caller's verified N0 project-supplement sources can supply existing
    project selections. Without that scope, tender rules still apply.
    """
    project_refs = project_source_ids or set()
    sources = {source["source_id"]: source for source in prepared["sources"]}
    requirements = {}
    relevant = defaultdict(list)
    deadline_values = defaultdict(list)
    payment_conditions = defaultdict(set)
    allowed_signers = []
    required_role_sources = defaultdict(list)
    signer_branches = defaultdict(list)
    agent_forms = []
    manager_roles = set()
    manager_lots = set()
    contract_requirements = {ref for section in prepared["sections"] if _CONTRACT_SECTION.search(section["title"])
                             for ref in section["requirement_refs"]}
    for requirement in prepared["requirements"]:
        rid = requirement["requirement_id"]
        refs = [ref for ref in requirement["source_refs"] if sources[ref]["kind"] == "tender" and
                sources[ref].get("provenance") in {"original", "synthetic"}]
        text = _normalize(requirement["text"])
        if not refs or rid in contract_requirements or _SCOPE_EXCLUSION.search(text):
            continue
        requirements[rid] = refs
        if _AGENT_FORM.search(text):
            agent_forms.append(rid)
            continue
        if (requirement["response_mode"] in {"table", "fixed_text"} and not _CORPORATE_TABLE.search(text)
                and (field := _MANAGER_FIELD.search(text))):
            relevant["manager_name"].append(rid)
            manager_roles.update(_MANAGER_SUBJECT.findall(field[0]))
        for clause in _clauses(text):
            if _SCOPE_EXCLUSION.search(clause) or _CONDITIONAL.search(clause):
                continue
            parts = _applicable_parts(clause)
            clause_needs = set()
            for part in parts:
                if _CONDITIONAL.search(part) or _SIGN_BRANCH.search(part):
                    continue
                needs = _personnel_needs(part)
                clause_needs.update(needs)
                if "manager_name" in needs:
                    manager_roles.update(first or second for first, second in _MANAGER_APPOINTMENT.findall(part))
                    if _MANAGER_PAIR.search(part):
                        manager_roles.update(_MANAGER_SUBJECT.findall(part))
                    manager_lots.update(_MANAGER_LOT.findall(part))
                if "payment_evidence" in needs:
                    payment_conditions["window_days"].update(map(int, _PAYMENT_WINDOW.findall(part)))
                    payment_conditions["minimum_payments"].update(int(count) if count.isdigit() else _PAYMENT_NUMBER[count]
                                                                 for count in _PAYMENT_MINIMUM.findall(part))
                explicit_date = _DEADLINE_ACTOR.search(part) or _DEADLINE_DOCUMENT.search(part)
                if ((explicit_date or _FULL_SOURCE_DEADLINE.search(part) or _RELATIVE_SUBMISSION.search(part))
                        and not _NON_SUBMISSION_DEADLINE.search(part)):
                    clause_needs.add("deadline")
                    match = explicit_date or _FORMAL_DEADLINE.search(part)
                    if match and (value := _deadline_value(match["value"])):
                        deadline_values[value].extend(refs)
            if clause_needs & {"manager_name", "payment_evidence"} and _EMPLOYMENT_FIELD.search(clause):
                clause_needs.add("employer")
            for key in clause_needs:
                relevant[key].append(rid)
            routes, branches = _signing_routes(clause, parts, text)
            if routes:
                relevant["signer_role"].append(rid)
                allowed_signers.extend(routes)
            for route in routes:
                if len(route) == 1:
                    required_role_sources[next(iter(route))].extend(refs)
            for branch in branches:
                signer_branches[branch].append(rid)
            if _AGENT_PROOF_USE.search(clause):
                agent_forms.append(rid)

    if set(signer_branches) == set(_ROLES):
        allowed_signers.append(set(_ROLES))
    if allowed_signers:
        for role in _ROLES:
            relevant["signer_role"].extend(signer_branches[role])
    permitted = set.intersection(*allowed_signers) if allowed_signers else set()
    selected_signer, signer_refs = _held_value("signer_role", prepared["facts"], sources, project_refs)
    if len(permitted) == 1:
        selected_signer = next(iter(permitted))
        signer_refs = list(dict.fromkeys(required_role_sources[selected_signer]))
    elif selected_signer not in permitted:
        selected_signer, signer_refs = None, []
    if "agent" in permitted and selected_signer != "legal_representative":
        relevant["agent_name"].extend([*relevant["signer_role"], *agent_forms])

    ambiguous_assignment = len(manager_roles) > 1 or len(manager_lots) > 1
    answers = []
    for key, label in _LABELS.items():
        ids = list(dict.fromkeys(relevant[key]))
        if not ids:
            continue
        refs = list(dict.fromkeys(ref for rid in ids for ref in requirements[rid]))
        issues = _support_issues(key, prepared["issues"], prepared["tables"], ids)
        value, value_refs = _held_value(key, prepared["facts"], sources, project_refs) if key in _FIELD_LABELS else (None, [])
        conditions = {}
        depends_on = None
        if key == "deadline":
            if len(deadline_values) == 1:
                date, date_refs = next(iter(deadline_values.items()))
                value_refs = list(dict.fromkeys(date_refs))
                value = {"submitted_before": date, "source_ref": value_refs[0]}
            elif len(deadline_values) > 1:
                conditions["source_conflict"] = True
        elif key == "payment_evidence":
            conditions = {name: next(iter(values)) for name, values in payment_conditions.items() if len(values) == 1}
            if any(len(values) > 1 for values in payment_conditions.values()):
                conditions["source_conflict"] = True
        elif key == "signer_role":
            value, value_refs = selected_signer, signer_refs
            conditions = {"allowed_roles": [role for role in _ROLES if role in permitted]}
        elif key == "agent_name" and selected_signer is None:
            depends_on = {"key": "signer_role", "value": "agent"}
            value, value_refs = None, []
        if ambiguous_assignment and key in {"manager_name", "employer", "payment_evidence"}:
            conditions["ambiguous_assignment_scope"] = True
            value, value_refs = None, []
        evidence = [_source_location(sources[ref], "tender") for ref in refs]
        evidence.extend(_source_location(sources[ref], "project_input") for ref in value_refs if ref not in refs)
        evidence.extend(_related_proof(key, prepared, ids, issues, sources))
        answers.append({"key": key, "label": label, "status": "conditional" if depends_on else "known" if value is not None else "unresolved",
                        "requirement_ids": ids, "issue_ids": [issue["issue_id"] for issue in issues], "source_refs": refs,
                        "evidence_refs": evidence, "value": value, "value_source_refs": value_refs,
                        "conditions": conditions, "depends_on": depends_on})
    return answers
