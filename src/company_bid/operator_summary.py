"""Source-bound priority actions for the review checklist."""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import TYPE_CHECKING

from .preparation_rules import manual_signing_submission
from .project_answers import _ROLE_VALUES, _signing_proof_roles
from .project_questions import build_project_questions

if TYPE_CHECKING:
    from .models import DocumentPackage


_PROOF_SECTIONS = {
    "qualification": re.compile(r"(?:企业|公司|单位)?(?:资质|资格审查|财务(?:要求)?|信誉(?:要求)?|信用)(?:材料|证明|资料|审查|要求)?$"),
    "performance": re.compile(r"(?:往年|类似|项目|(?:响应人|投标人|供应商))?业绩(?:材料|证明|资料|要求|统计表|汇总表|一览表)?(?:\(后附业绩材料\))?$"),
    "personnel": re.compile(r"(?:拟派|项目组|项目)?人员(?:要求材料|材料|配置|配置表|表|证明|资料)(?:核对)?$|拟派负责人及用工资料核对"),
}
_PRICE_FORM = re.compile(r"(?:投标|响应|采购|应答)?(?:总)?报价(?:一览|汇总|明细)?表$|(?:分项|分项费用|设备|服务|货物)(?:投标)?(?:报价|价格)(?:明细|汇总)?表$|开标一览表$")
_AGENT_FORM = re.compile(r"(?:法定代表人|法人代表|单位负责人)?授权(?:委托)?书$")
_ID_FORM = re.compile(r"(?:法定代表人|法人代表|单位负责人)身份证明(?:书)?$")
_SIGN_BLANK = re.compile(r"(?:签字|签署|签名|加盖|签章|盖章|盖单位章|公章)")
_NO_SIGN = re.compile(r"(?:不要求|无需|无须|不必|不需(?:要)?|不得|禁止)(?:再|另行|纸质|手写)?(?:签字|签署|签名|签章|盖章)")
_BID_FILE = r"(?:投标|响应|报价|应答)(?:文件|书|函)"
_SUBMISSION = re.compile(rf"(?:递交|提交|送达|上传)(?:最终|电子)?{_BID_FILE}|{_BID_FILE}.{{0,16}}(?:递交|提交|送达|上传)|(?:投标|响应|递交|提交|截标)截止")
_SUBMISSION_CONTEXT = re.compile(r"(?:不予退还|提出异议|签订合同|成交通知|履约保证金|评审结果|不足三家|终止采购|可以修改或撤回)")
_NO_PROOF = re.compile(r"(?:不要求|无需|无须|不必|不需(?:要)?|免予|免于|不适用|不得|禁止)(?:再|另行|重复)?(?:提供|提交|附上?|列出|填报|填写|出具|核对)?")
_BID_EVIDENCE = re.compile(r"营业执照|法人证书|资质(?:证书|等级)|许可证|审计报告|财务报表|信用中国|信誉要求|信用信息")
_PERFORMANCE = re.compile(r"业绩(?:证明|要求|材料|资料)|(?:类似|同类|同等)项目|(?:响应|投标)单位业绩|有效业绩|业绩(?:统计|汇总|一览)表")
_PRICE_TERM = re.compile(r"(?:应|须|需|必须)(?:当)?(?:按[^。；]{1,35})?(?:在[^。；]{1,20}中)?(?:填报|填写|提交|提供|注明|报送)(?:[^。；]{0,14})(?:报价|含税价|税率)|(?:报价有效期|报价方式|最高限价).{0,40}(?:为|：|:|一致|低于)|本项目采用(?:采用)?一次报价")
_PRICE_FIELD = re.compile(r"报价|总价|单价|分项价|价格|税率|税额|标价|金额|费率|合计")
_CAP = re.compile(r"(?:本项目|本标段)暂估价(?P<amount>[0-9]+(?:\.[0-9]+)?)(?P<unit>万元|元)\((?P<tax>不含税|含税)\)[，,](?:采购人|招标人)只接受小于暂估价的报价[，,]否则视为无效报价")
_CONTACTS = {"personnel": "本项目人员安排及人事经办人", "signing": "本次签署人及用章经办人",
             "quotation": "负责本次报价的经办人", "qualification": "公司资质及证明材料经办人",
             "performance": "业绩档案经办人", "submission": "本次递交经办人"}


def _compact(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def _form_title(text):
    return re.sub(r"^(?:第[一二三四五六七八九十\d]+章|\d+(?:\.\d+)*[.、]?)", "", _compact(text)).rstrip(":")


def _unique_values(values):
    return list(dict.fromkeys(values))


def _source_texts(elements):
    for element in elements:
        if element.kind == "paragraph":
            yield from element.source_texts


class _OperatorSummary:
    def __init__(self, package, catalog):
        self.package = package
        self.catalog = catalog
        self.sources = catalog["sources"]
        self.requirements = catalog["requirements"]
        self.current = {rid: requirement for rid, requirement in self.requirements.items()
                        if all(self.is_tender(ref) for ref in requirement.source_refs)}
        self.visible = {section.section_id: section for section in package.sections
                        if section.document_role == "response" and section.render_body}
        self.issues = {issue.issue_id: issue for issue in package.issues}
        self.actions = {action.action_id: action for action in package.review_actions}
        self.items = []
        self.quote_sizes = {entry["display_id"]: len(entry["text"]) for entry in catalog["entries"]}
        raw = package.model_dump(mode="json", include={"sources", "facts", "sections", "requirements", "attachments", "issues"})
        raw["tables"] = [block["table"] for section in raw["sections"] for block in section["blocks"] if block["kind"] == "table"]
        raw["requirements"] = [item for item in raw["requirements"] if item["requirement_id"] in self.current]
        self.questions = {row["key"]: row for row in build_project_questions(raw)}

    def is_tender(self, ref):
        source = self.sources[ref]
        return source.kind == "tender" and source.provenance in {"original", "synthetic"}

    def refs(self, rids):
        return _unique_values(ref for rid, requirement in self.current.items()
                              if rid in rids for ref in requirement.source_refs)

    def issue_ids(self, rids, sections=()):
        return {issue.issue_id for issue in self.issues.values()
                if issue.section_id in sections or set(issue.requirement_refs) <= rids}

    def action_ids(self, rids):
        return {action.action_id for action in self.actions.values()
                if set(action.requirement_refs) <= rids and all(self.is_tender(ref) for ref in action.source_refs)}

    def sections(self, pattern):
        return {sid for sid, section in self.visible.items() if pattern.fullmatch(_form_title(section.title))}

    def destinations(self, sections, *, cover=False):
        labels = []
        for sid, section in self.visible.items():
            if sid not in sections:
                continue
            title = " ".join(section.title.split()).rstrip("：:")
            if len(title) > 45:
                continue
            if cover and sid == self.package.prescribed_cover_section_id:
                title += "（封面）"
            labels.append(title)
            for block in section.blocks:
                if block.kind == "table" and len(block.table.title) <= 45 and block.table.title != section.title:
                    labels.append("表《" + block.table.title + "》")
        return _unique_values(labels)

    def proof_candidates(self, rids):
        return _unique_values(ref for attachment in self.package.attachments
                              if set(attachment.requirement_refs) & rids
                              for ref in attachment.source_refs if self.sources[ref].kind in {"company_material", "attachment"}
                              and self.sources[ref].provenance in {"original", "derived_from_bid", "synthetic"})

    def add(self, key, title, rids, *, available=(), next_steps=(), sections=(), issue_ids=(),
            action_ids=(), primary_rids=(), evidence_refs=(), cover=False, fallback=None):
        # Mixed issues remain completely navigable even when several requirements
        # share the same original row. No source text moves into the digest.
        selected_issues = [issue for ident, issue in self.issues.items() if ident in issue_ids
                           and set(issue.requirement_refs) <= self.current.keys()]
        selected_actions = [action for ident, action in self.actions.items() if ident in action_ids]
        if not (selected_issues or selected_actions) or not rids or not next_steps:
            return
        qids = self.catalog["quote_ids"]
        requirement_qids = self.catalog["requirement_quote_ids"]
        matched = [rid for rid in self.current if rid in rids]
        all_rids = _unique_values([*matched, *(rid for task in [*selected_issues, *selected_actions]
                                               for rid in task.requirement_refs)])
        direct = list(primary_rids) or matched
        contexts = [context for action in selected_actions for context in action.source_scope_contexts
                    if self.is_tender(context.source_ref)]
        quotes = _unique_values([*(requirement_qids[rid] for rid in all_rids),
                                 *(qids[task.source_requirement] for task in [*selected_issues, *selected_actions]),
                                 *(qids[context.text] for context in contexts)])
        direct_quotes = _unique_values(requirement_qids[rid] for rid in direct if rid in self.current)
        readable_quotes = [qid for qid in direct_quotes if self.quote_sizes[qid] <= 650]
        self.items.append({"key": key, "title": title, "contact": _CONTACTS[key],
                           "available": list(available), "next_steps": list(next_steps),
                           "destinations": self.destinations(sections, cover=cover) or [fallback or "逐项清单中的对应事项"],
                           "issue_ids": [issue.issue_id for issue in selected_issues],
                           "action_ids": [action.action_id for action in selected_actions], "requirement_ids": all_rids,
                           "primary_quote_ids": (readable_quotes or direct_quotes)[:4],
                           "primary_source_refs": _unique_values(ref for rid in direct if rid in self.current
                                                                for ref in self.current[rid].source_refs)[:4],
                           "quote_ids": quotes,
                           "source_refs": _unique_values([*self.refs(set(all_rids)), *(ref for action in selected_actions
                                                                                   for ref in action.source_refs),
                                                          *(context.source_ref for context in contexts)]),
                           "evidence_source_refs": _unique_values(evidence_refs)})


def _confirmed_answer(summary, key):
    """Read only the authenticated N5 answer facts, never names from proofs."""
    label = {"manager_name": "本项目拟派负责人", "employer": "本项目填报的用工主体",
             "signer_role": "本项目拟采用的签署方式", "agent_name": "本项目拟任授权代理人"}[key]
    matches = []
    for fact in summary.package.facts:
        if fact.label != label or not fact.fact_id.startswith(f"PROJECT-ANSWER-{key}-"):
            continue
        refs = [summary.sources[ref] for ref in fact.source_refs]
        if not any(ref.kind == "manual_input" and ref.provenance == "manual_input" and
                   ref.locator == "本项目写标前补录" and ref.source_id == "F-REVIEW-" + ref.sha256 for ref in refs):
            continue
        compact = " ".join(fact.value.split())
        if len(compact) <= 80 and not re.search(r"[A-Za-z]:[\\/]|https?://|\.local[\\/]", compact):
            matches.append((compact, fact.source_refs))
    if len(matches) == 1:
        return matches[0]
    return None, []


def _scoped_notes(summary, rids, relevant_keys):
    """Return structured current-project questions and their outstanding items."""
    questions = [summary.questions[key] for key in relevant_keys if key in summary.questions]
    known = {rid for question in questions for rid in question["requirement_ids"]}
    issue_ids = {ident for question in questions for ident in question["issue_ids"]}
    return known | rids, issue_ids


def _personnel_summary(summary):
    keys = ("manager_name", "employer", "payment_evidence")
    rids, support = _scoped_notes(summary, set(), keys)
    if not rids:
        return
    manager = summary.questions.get("manager_name", {})
    employer = summary.questions.get("employer", {})
    payment = summary.questions.get("payment_evidence", {})
    conditions = payment.get("conditions", {})
    has_alternatives = any(summary.questions.get(key, {}).get("conditions", {}).get("ambiguous_assignment_scope") for key in keys)
    name, name_refs = _confirmed_answer(summary, "manager_name") if not has_alternatives else (None, [])
    company, company_refs = _confirmed_answer(summary, "employer") if not has_alternatives else (None, [])
    available = []
    if name and manager:
        available.append(f"本次已填拟派负责人“{name}”。")
    if company and employer:
        available.append(f"本次已填用工主体“{company}”，人员关系待核。")
    proof_ids = set(payment.get("requirement_ids", ()))
    registrations = [e["source_ref"] for e in payment.get("evidence_refs", ()) if e["kind"] == "reviewed_registration"]
    current_payment = [ref for ref in summary.proof_candidates(proof_ids)
                       if summary.sources[ref].kind == "attachment" and ref == "F-PAYMENT-" + summary.sources[ref].sha256]
    if current_payment:
        available.append("已收到本次补充的社保原件，缴费字段及适用性待核。")
    elif registrations:
        available.append("已定位参保登记页，实际缴费凭证待核对或补齐。")
    steps = []
    if has_alternatives:
        steps.append("分别确认各岗位或标段的拟派人员及所需证明。")
    elif manager:
        steps.append("按本次拟派人员核对资格及证明。" if name else "先确定本次拟派负责人，再核对其个人证明及承诺。")
    if payment:
        window, count = conditions.get("window_days"), conditions.get("minimum_payments")
        terms = []
        if not conditions.get("source_conflict"):
            if window:
                terms.append(f"正式递交截止日前 {window} 日内")
            if count:
                terms.append(f"至少实缴 {count} 次")
        steps.append("核对本人、缴费单位和实缴日期，并补齐实缴原件" + ("（" + "，".join(terms) + "）" if terms else "") + "。")
        if "deadline" in summary.questions and summary.questions["deadline"]["value"] is None:
            steps.append("以正式公告或澄清确认递交截止时点，再核算缴费窗口。")
    elif employer:
        steps.append("核对并填报拟派人员的实际用工主体。")
    sections = summary.sections(_PROOF_SECTIONS["personnel"])
    # Specific issue links can identify a named person field in a shared form.
    sections.update(summary.issues[i].section_id for i in support if i in summary.issues)
    support |= summary.issue_ids(rids, sections & summary.sections(_PROOF_SECTIONS["personnel"]))
    summary.add("personnel", "确定拟派人员并核对社保" if payment else "确定本次拟派人员", rids,
                available=available, next_steps=steps, sections=sections, issue_ids=support,
                action_ids=summary.action_ids(rids), primary_rids=[*payment.get("requirement_ids", ()), *manager.get("requirement_ids", ())],
                evidence_refs=[*name_refs, *company_refs, *registrations, *current_payment])


def _signing_summary(summary):
    route = summary.questions.get("signer_role", {})
    allowed = route.get("conditions", {}).get("allowed_roles", [])
    route_rids = set(route.get("requirement_ids", ()))
    if not route_rids:
        return
    value, refs = _confirmed_answer(summary, "signer_role")
    role = next((item for item in allowed if value == _ROLE_VALUES[item]), None) or route.get("value")
    applicable = [role] if role else allowed
    agent, agent_refs = _confirmed_answer(summary, "agent_name") if role == "agent" else (None, [])
    proofs = {rid: _signing_proof_roles(summary.current[rid].text) for rid in route_rids}
    proof_roles = {branch for value in proofs.values() for branch in value}.intersection(applicable)
    agent_sections = summary.sections(_AGENT_FORM) if "agent" in proof_roles or agent else set()
    id_sections = summary.sections(_ID_FORM) if "legal_representative" in proof_roles else set()
    sections = agent_sections | id_sections
    for sid, section in summary.visible.items():
        section_rids = route_rids.intersection(section.requirement_refs)
        if not section_rids or sid in sections:
            continue
        native = section.native_format
        if (native and any(_SIGN_BLANK.search(text) and not _NO_SIGN.search(text) for text in _source_texts(native.elements))) or any(
                _SIGN_BLANK.search(summary.current[rid].text) and not _NO_SIGN.search(summary.current[rid].text)
                and summary.current[rid].response_mode == "fixed_text" for rid in section_rids):
            sections.add(sid)
    available = []
    if role:
        available.append(f"本次拟采用{_ROLE_VALUES[role]}。" if value == _ROLE_VALUES[role] else f"原文规定由{_ROLE_VALUES[role]}。")
    if agent:
        available.append(f"本次已填拟任代理人“{agent}”，授权仍待办理。")
    if sections and not available:
        available.append("现稿保留相关签章位置。")
    steps = []
    if not role:
        steps.append("在招标文件允许的范围内确认实际签署人。")
    proof_steps = {"legal_representative": "法定代表人（单位负责人）签署时附其身份证明",
                   "agent": "代理人签署时附法定代表人（单位负责人）签署的授权委托书"}
    if proof_roles:
        steps.append("，".join(proof_steps[branch] for branch in applicable if branch in proof_roles) + "。")
    steps.append("按原表逐处核对签署人、日期和招标文件规定的签章方式，再完成签章。")
    full_rids, support = _scoped_notes(summary, route_rids, ("signer_role", "agent_name") if "agent" in applicable else ("signer_role",))
    support.update(issue.issue_id for issue in summary.issues.values()
                   if (issue.section_id in sections and re.search(r"签章字段|签署|签章|授权", issue.title))
                   or (set(issue.requirement_refs) <= full_rids and ("实际签署人" in issue.title or "身份证明或授权" in issue.title)))
    primary = [rid for rid in summary.current if rid in route_rids and (
        proofs[rid] or manual_signing_submission(summary.current[rid].text))]
    summary.add("signing", "确认签署人并办理签章", full_rids, available=available, next_steps=steps,
                sections=sections, issue_ids=support, action_ids=summary.action_ids(route_rids), primary_rids=primary,
                evidence_refs=[*refs, *agent_refs], cover=True)


def _quotation_summary(summary):
    price_tables = {sid: [block.table for block in section.blocks if block.kind == "table"
                         and _PRICE_FORM.fullmatch(_form_title(block.table.title))]
                    for sid, section in summary.visible.items()}
    price_tables = {sid: tables for sid, tables in price_tables.items() if tables}
    sections = set(price_tables)
    form_rids = {rid for tables in price_tables.values() for table in tables for rid in table.requirement_refs if rid in summary.current}
    quotes = {rid: _compact(req.text) for rid, req in summary.current.items()
              if req.category == "commercial" and req.response_mode in {"factual_response", "procedure", "table"}}
    price_rids = {rid for rid, text in quotes.items() if any(
        _CAP.search(part) or (_PRICE_TERM.search(part) and not _NO_PROOF.search(part))
        for part in re.split(r"[。;；]", text))}
    tech_refs = {rid for rid, requirement in summary.current.items() if any(
        ctx.operator == "exclude_commercial_quotation" and all(summary.is_tender(ref) for ref in ctx.source_refs)
        for ctx in requirement.source_contexts)}
    tech_rules = {rid for rid, requirement in summary.current.items() if re.fullmatch(
        r"(?:[A-Z][.、])?(?:不带|不含)商务报价的技术部分(?:包括|包含)[:：]", _compact(requirement.text))}
    rids = price_rids | form_rids | tech_refs | tech_rules
    if not rids:
        return
    available = ["现稿保留本次报价原表。"] if form_rids else []
    caps = {rid: found.groupdict() for rid, text in quotes.items() if (found := _CAP.fullmatch(text.split("。")[0]))}
    cap_values = {tuple(value.values()) for value in caps.values()}
    tax_sources = [rid for rid in price_rids if re.search(r"(?:注明|标明|列明)税率", quotes[rid])]
    tax = "，注明税率" if tax_sources else ""
    table_has_tax_pair = any(re.search(r"(?<!不)(?<!不包)含(?:增值)?税", text) and re.search(r"不(?:包含|含)(?:增值)?税", text)
                             for tables in price_tables.values() for table in tables for row in table.rows
                             for cell in row if (text := _compact(cell.text)))
    if not (price_rids or form_rids):
        steps = []
    elif any(_form_title(table.title).startswith("分项") for tables in price_tables.values() for table in tables):
        total = "含税及不含税总价" if table_has_tax_pair else "总价"
        steps = [f"填报本次{total}与分项价{tax}，按原表核对单位和合计。"]
    else:
        steps = [f"核对并填写本次报价{tax}，按招标文件核对报价口径和单位。"]
    if len(cap_values) == 1:
        amount, unit, tax_basis = next(iter(cap_values))
        steps.append(f"核对{tax_basis}报价，原文要求严格低于 {amount} {unit}。")
    elif len(cap_values) > 1:
        steps.append("原文有多处限价表述，核对各自适用范围后确定报价口径。")
    if tech_refs or tech_rules:
        steps.append("技术部分不要列入商务报价。")
    support = summary.issue_ids(price_rids | tech_refs | tech_rules) - {
        ident for tables in price_tables.values() for table in tables for row in table.rows for cell in row for ident in cell.issue_refs}
    for tables in price_tables.values():
        for table in tables:
            col_labels = table.columns if table.header_from_columns else [cell.text for cell in table.rows[0]]
            detail_form = _form_title(table.title).startswith("分项")
            for row in table.rows:
                row_is_price = len(row) > 1 and bool(_PRICE_FIELD.search(row[0].text))
                for column, cell in enumerate(row):
                    label = _compact(col_labels[column])
                    if (_PRICE_FIELD.search(label) or row_is_price and label not in {"备注", "说明"}
                            or detail_form and label in {"内容", "项目", "分项", "项目名称"}):
                        support.update(cell.issue_refs)
    support = {ident for ident in support if "签章字段" not in summary.issues[ident].title}
    primary = [*caps, *sorted(tax_sources), *sorted(tech_rules), *[rid for rid in summary.current if rid in form_rids]]
    title = "填报并复核本次报价" if price_rids or form_rids else "核对技术文件中的报价隔离"
    summary.add("quotation", title, rids, available=available, next_steps=steps,
                sections=sections, issue_ids=support, action_ids=summary.action_ids(rids), primary_rids=primary)


def _proof_summary(summary, key):
    sections = summary.sections(_PROOF_SECTIONS[key])
    form_rids = {rid for sid in sections for rid in summary.visible[sid].requirement_refs if rid in summary.current}
    subject = _PERFORMANCE if key == "performance" else _BID_EVIDENCE

    def proof_clause(requirement):
        if requirement.response_mode not in {"attachment", "factual_response", "table"}:
            return False
        scoring = key == "performance" and requirement.response_mode == "attachment" and requirement.category == "commercial"
        if requirement.category != "qualification" and requirement.requirement_id not in form_rids and not scoring:
            return False
        return any(subject.search(part) and not _NO_PROOF.search(part)
                   for part in re.split(r"[。;；]", _compact(requirement.text)))

    rids = {rid for rid, req in summary.current.items() if proof_clause(req)}
    if not rids:
        return
    all_rids = rids | form_rids
    support = summary.issue_ids(rids, sections)
    active = {summary.issues[i].section_id for i in support}
    sections = sections & active or {sid for sid in active if sid in summary.visible}
    evidence = summary.proof_candidates(all_rids)
    available = []
    steps = []
    primary = []
    if key == "qualification":
        title = "核对企业资格及资质材料"
        if evidence:
            available.append("已定位部分企业证照或资质证明候选，本次适用性待核。")
        texts = {rid: _compact(summary.current[rid].text) for rid in rids}
        certificates = {rid: text for rid, text in texts.items() if re.search(r"营业执照|法人证书|资质证书|许可证", text)
                        and not re.search(r"吊销|信用|评审项目", text)}
        if certificates:
            steps.append("逐项核对主体、证照类型、等级、有效期及原件扫描件，补齐招标所需证明。")
            primary.extend(certificates)
        audit = [rid for rid, text in texts.items() if re.search(r"须提供.{0,40}审计(?:报告|机关)|审计报告.{0,10}(?:扫描件|复印件)", text)]
        credit = [rid for rid, text in texts.items() if re.search(r"查询.{0,25}(?:信用中国|企业信用信息公示系统)", text)]
        if audit or credit:
            parts = (["按年度和格式要求补齐财务证明"] if audit else []) + (["依本标要求核对信誉查询及说明"] if credit else [])
            steps.append("，".join(parts) + "。")
            primary.extend([*audit, *credit])
        if not steps:
            steps.append("按原文核对本次资格材料的证明种类和条件，补齐未确认的原件或说明。")
    else:
        title = "选定本次申报业绩并核对证明"
        if evidence:
            available.append("已定位历史业绩证明候选，项目范围、日期及证明完整性待核。")
        eligibility = [rid for rid in summary.current if rid in rids and summary.current[rid].category == "qualification"]
        scoring = [rid for rid in summary.current if rid in rids and summary.current[rid].category != "qualification"
                   and re.search(r"(?:得|加)\d+(?:\.\d+)?分|最高(?:得|计)", _compact(summary.current[rid].text))]
        typed_minimum = {match.groups() for rid in eligibility if (match := re.search(
            r"(?P<start>\d{4}年\d{1,2}月\d{1,2}日)至(?:投标|响应)截止(?:日|时间)[,，].{0,12}?至少有(?P<count>\d+|[一二三四五六七八九十])项(?P<scope>[\u4e00-\u9fff]{2,24})业绩[。；]",
            _compact(summary.current[rid].text)))}
        if len(typed_minimum) == 1:
            start, count, scope = next(iter(typed_minimum))
            steps.append(f"先核对资格业绩（{start}至正式递交截止，至少 {count} 项{scope}业绩）。")
        else:
            steps.append("按本标原文确定申报项目，核对业务范围、时间及所需证明。")
        qualification_paths = [rid for rid in eligibility if any(
            re.search(r"合同主要页[^。]{0,75}(?:或者|或)中标通知书", clause) and not _NO_PROOF.search(clause)
            for clause in _compact(summary.current[rid].text).split("。"))]
        scoring_paths = [rid for rid in scoring if any(
            (re.search(r"合同主要页[^。]{0,75}(?:或者|或)(?:竣工\(交工\))?验收文件", clause)
             or re.search(r"证明(?:文件|材料)之一:\(1\)合同主要页[^。；;]{0,100}[；;]\(2\)(?:项目竣工\(交工\))?验收文件", clause))
            and not _NO_PROOF.search(clause) for clause in _compact(summary.current[rid].text).split("。"))]
        if qualification_paths and scoring_paths:
            steps.append("资格与评分分别核对。资格按合同主要页或中标通知书；评分按该条允许的合同主要页或验收文件。")
        elif qualification_paths:
            steps.append("按原文核对合同主要页或中标通知书及其相应日期。")
        elif scoring_paths:
            steps.append("按评分条款核对合同主要页或验收文件及其相应日期。")
        if any(block.kind == "table" and _PROOF_SECTIONS[key].fullmatch(_form_title(block.table.title))
               for sid in sections for block in summary.visible[sid].blocks):
            steps.append("把经核对的历史项目逐项填回业绩表。")
        primary.extend([*qualification_paths, *scoring_paths, *eligibility, *scoring])
    stable_primary = _unique_values(primary)
    summary.add(key, title, all_rids, available=available, next_steps=steps, sections=sections, issue_ids=support,
                action_ids=summary.action_ids(rids), primary_rids=stable_primary, evidence_refs=evidence)


def _submission_summary(summary):
    deadline = summary.questions.get("deadline", {})
    formal_rids = {rid for rid in deadline.get("requirement_ids", ()) if summary.current[rid].category == "procedure"}
    rids = {rid for rid, requirement in summary.current.items() if requirement.response_mode == "procedure"
            and requirement.category == "procedure" and (rid in formal_rids or manual_signing_submission(requirement.text)
                or (_SUBMISSION.search(_compact(requirement.text)) and not _SUBMISSION_CONTEXT.search(_compact(requirement.text))
                    and not _NO_PROOF.search(_compact(requirement.text)) and "无法上传" not in _compact(requirement.text)
                    and re.search(r"平台|(?:投标|响应|递交|提交|截标)截止", _compact(requirement.text))))}
    if not rids:
        return
    available = []
    steps = []
    when = deadline.get("value")
    if when and when["source_ref"] in summary.refs(formal_rids):
        instant = datetime.fromisoformat(when["submitted_before"])
        available.append(f"原文正式截止为北京时间 {instant:%Y-%m-%d %H:%M}。" if not instant.second else
                         f"原文正式截止为北京时间 {instant:%Y-%m-%d %H:%M:%S}。")
    elif formal_rids:
        steps.append("先核对正式采购公告或最新澄清中的递交截止时点。")
    scope_texts = [_compact(summary.current[rid].text) for rid in rids]
    if any(re.search(r"通过[^。；]{0,30}电子平台(?:递交|提交)|递交至[^。；]{0,40}(?:采购|招标|交易)平台|平台上传的文件为准", text) for text in scope_texts):
        steps.append("在招标文件指定平台递交最终签章版，核对上传记录。")
    else:
        steps.append("按本标规定的方式和地点，在正式截止前递交最终版并确认送达。")
    primary = [rid for rid in summary.current if rid in formal_rids] + [rid for rid in summary.current if rid in rids]
    summary.add("submission", "确认截止并递交最终文件", rids, available=available, next_steps=steps,
                issue_ids=summary.issue_ids(rids), action_ids=summary.action_ids(rids), primary_rids=primary,
                fallback="定稿后的投标文件及本标递交入口")


def build_operator_summary(package: DocumentPackage, catalog: dict) -> dict:
    """Project pending N5 business work into a short, reversible N6 view."""
    summary = _OperatorSummary(package, catalog)
    _personnel_summary(summary)
    _signing_summary(summary)
    _quotation_summary(summary)
    _proof_summary(summary, "qualification")
    _proof_summary(summary, "performance")
    _submission_summary(summary)
    assigned_issues = {ident for item in summary.items for ident in item["issue_ids"]}
    assigned_actions = {ident for item in summary.items for ident in item["action_ids"]}
    return {"schema_version": "operator-summary-v1", "items": summary.items,
            "other_issue_ids": [ident for ident in summary.issues if ident not in assigned_issues],
            "other_action_ids": [ident for ident in summary.actions if ident not in assigned_actions],
            "details_note": "完整要求、未纳入摘要的事项及证据，见后文逐项清单和原文附录。"}
