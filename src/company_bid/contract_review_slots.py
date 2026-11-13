"""Finite source-only contract fields for later N3 review.

This module does not consume N2 answers, decide response types, or fill values.
An absent value in extracted text is not proof of a physical blank in a PDF.
The returned catalogue is deliberately incomplete and is not a reading waiver.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

from .extraction import _index, _pdf_footer_skips
from .storage import digest


RULE = "contract_review_slots_v1"
SUPPORTED_FORMS = (
    "other_terms", "schedule", "quality", "party_a_material_list",
    "party_a_work_conditions", "party_a_cooperation", "improvement_deadline",
    "free_service_scope", "return_materials_deadline", "fee_amounts_and_rate",
    "payment_choices_and_schedule", "quality_deposit_choice", "bank_details",
    "industry_tax_basis", "invoice_details", "invoice_scope_choice", "sme_choice",
    "acceptance_deliverables", "acceptance_deadlines", "acceptance_method", "acceptance_time_place",
    "ip_choice_with_literal_zero_references",
)
_CHAPTER = re.compile(r"第[零一二三四五六七八九十百\d]+章[^\r\n]*")
_CONTRACT = re.compile(r"第[零一二三四五六七八九十百\d]+章\s*合同(?:条款|格式|条款及格式)")
_ARTICLE = re.compile(r"第([一二三四五六七八九十百\d]+)条\s*([^\r\n.．…]+)")
_CLAUSE = re.compile(r"([1-9]\d?)\.([1-9]\d?)(?![\d.])\s*(.*)")
_ATTACHMENT = re.compile(r"附件([一二三四五六七八九十\d]+)[：:]\s*(\S.*)")
_SUB = re.compile(r"[（(]([1-9]\d?)[）)]\s*(.*)")
_EMPTY = r"[_＿]*"  # Whitespace is removed for lexical matching, never physical inference.
_CHECKED = re.compile(r"[☑☒■✓✔√]|\[[xX√✓]\]")


def _compact(text):
    return re.sub(r"\s+", "", text)


def _number(text):
    if text.isdecimal():
        return int(text)
    digits = {v: i for i, v in enumerate("零一二三四五六七八九")}
    if text in digits:
        return digits[text]
    if text == "十":
        return 10
    if text.startswith("十") and text[1:] in digits:
        return 10 + digits[text[1:]]
    if len(text) in (2, 3) and text[0] in digits and text[1] == "十":
        if len(text) == 3 and text[2] not in digits:
            return None
        return digits[text[0]] * 10 + (digits[text[2]] if len(text) == 3 else 0)
    return None


def _member(block):
    return {**{k: block[k] for k in ("block_id", "source_id", "unit_id", "page", "locator", "ordinal", "unit_ordinal")},
            "text": block["text"], "text_sha256": hashlib.sha256(block["text"].encode()).hexdigest()}


def _line(block, units):
    unit = units[block["unit_id"]]
    coverage = unit.get("coverage", {})
    match = re.fullmatch(r"pdf/page\[(\d+)\]/line\[(\d+)\]", block.get("locator", ""))
    if (not match or block.get("kind") != "paragraph" or block.get("origin") != "native" or
            block.get("read_disposition") or coverage.get("status") != "read" or
            coverage.get("origin", "native") != "native" or
            any(coverage.get(k) for k in ("unread_objects", "unmapped_glyphs", "error_code", "ignored_objects")) or
            type(coverage.get("image_count")) is not int or coverage["image_count"] != 0 or
            any(b.get("kind") == "image" for b in unit["blocks"]) or
            type(block.get("page")) is not int or int(match[1]) != block["page"] or
            unit.get("page") != block["page"] or unit.get("locator") != f"pdf/page[{block['page']}]" or
            any(char == "\ufffd" or unicodedata.category(char) in {"Co", "Cc", "Cf", "Cs", "Cn"}
                for char in block["text"])):
        return None
    return int(match[2])


def _continuous(span, units):
    if not span or any(_line(b, units) is None for b in span):
        return False
    for left, right in zip(span, span[1:]):
        if right["ordinal"] != left["ordinal"] + 1:
            return False
        if left["unit_id"] == right["unit_id"]:
            if (right["unit_ordinal"] != left["unit_ordinal"] + 1 or
                    _line(right, units) != _line(left, units) + 1):
                return False
        elif (right["page"] != left["page"] + 1 or right["unit_ordinal"] != 0 or
                _line(right, units) != 0 or
                left["unit_ordinal"] != len(units[left["unit_id"]]["blocks"]) - 1):
            return False
    return True


def _attachment_start(body, index, units):
    block = body[index]
    if not _ATTACHMENT.fullmatch(block["text"]):
        return False
    if _line(block, units) == 0:
        return True
    # An inline annex title followed immediately by its own article is also
    # bounded. A sequence of attachment references is not such a heading.
    return bool(index + 1 < len(body) and
                not (index and _ATTACHMENT.fullmatch(body[index - 1]["text"])) and
                _ARTICLE.fullmatch(body[index + 1]["text"]) and
                block["unit_id"] == body[index + 1]["unit_id"] and
                _continuous(body[index:index + 2], units))


def _attachment_reference_list(body, index, units):
    if not _ATTACHMENT.fullmatch(body[index]["text"]):
        return False
    peers = [i for i in (index - 1, index + 1) if 0 <= i < len(body) and
             _ATTACHMENT.fullmatch(body[i]["text"]) and
             body[i]["unit_id"] == body[index]["unit_id"]]
    return any(_continuous(body[min(index, i):max(index, i) + 1], units) for i in peers)


def _signature_boundaries(body, units, footer_proofs):
    """An explicit no-body marker plus a complete native signature page only."""
    result = {}
    for index, marker in enumerate(body):
        if marker["text"] not in {"（以下无正文）", "(以下无正文)"}:
            continue
        page = marker["page"]
        next_index = next((i for i in range(index + 1, len(body)) if body[i]["page"] != page), None)
        if next_index is None or body[next_index]["page"] != page + 1 or _line(body[next_index], units) != 0:
            continue
        intervening = body[index + 1:next_index]
        if any(b["block_id"] not in footer_proofs for b in intervening):
            continue
        signature = [b for b in body[next_index:] if b["unit_id"] == body[next_index]["unit_id"]]
        content = [b for b in signature if b["block_id"] not in footer_proofs]
        if len(content) != 9 or not re.fullmatch(r"[（(]本页无正文，为《[^《》\r\n]{0,120}》签章页[）)]", content[0]["text"]):
            continue
        labels = ["甲方：", "（盖章）", "法定代表人（负责人）或委托代理人：", "日期：",
                  "乙方：", "（盖章）", "法定代表人（负责人）或委托代理人：", "日期："]
        if [_compact(b["text"]) for b in content[1:]] != labels or not _continuous([marker, *intervening, *signature], units):
            continue
        result[index] = {"kind": "explicit_contract_end_and_native_signature_page",
                         "members": [_member(b) for b in [marker, *content]],
                         "coverage": [{"unit_id": key, "coverage_sha256": digest(units[key]["coverage"])}
                                      for key in dict.fromkeys(b["unit_id"] for b in [marker, *signature])],
                         "separated_page_footers": [{**_member(b), "footer_proof": footer_proofs[b["block_id"]]}
                                                    for b in [*intervening, *signature] if b["block_id"] in footer_proofs]}
    return result


def _document_end_boundary(body, units, footer_proofs, document):
    """Only an expected final native page ending in a proved page footer."""
    if not body or body[-1]["block_id"] not in footer_proofs:
        return None
    footer = body[-1]
    final_unit = document["units"][-1]
    final_page = [b for b in body if b["unit_id"] == final_unit["unit_id"]]
    if (footer["unit_id"] != final_unit["unit_id"] or footer["page"] != document["expected_units"] or
            not final_page or _line(final_page[0], units) != 0 or not _continuous(final_page, units)):
        return None
    return {"kind": "expected_last_native_page_with_proven_footer", "expected_units": document["expected_units"],
            "last_unit_id": final_unit["unit_id"], "last_page_members": [_member(b) for b in final_page],
            "coverage_sha256": digest(final_unit["coverage"]), "footer_proof": footer_proofs[footer["block_id"]]}


def _fields(text):
    """Match closed lexical slots; arbitrary spaces and empty punctuation do not qualify."""
    t = _compact(text)
    sub = _SUB.fullmatch(t)
    core = sub[2] if sub else t
    fields, kinds, issues = [], [], []

    def add(kind, label, owner=None):
        kinds.append(kind)
        fields.append({"field": label, "owner_party": owner, "reading_status": "read",
                       "value_status": "value_not_supplied_in_read_text", "value": None,
                       "values_inferred": False, "applicability": "not_determined"})

    if re.fullmatch(r"其他[：:]" + _EMPTY, core):
        add("other_terms", "其他条款")
    material = re.fullmatch(r"甲方应向乙方提供技术资料、数据、材料或样品[,，]甲方对其提供资料的准确性和完整性负责[：:]"
                            + "".join(r"\(" + letter + r"\)" + _EMPTY + r"[；;]" for letter in "abcd"), core)
    if material:
        for letter in "abcd":
            add("party_a_material_list", f"甲方提供的资料、数据、材料或样品（{letter}）", "甲方")
    for pattern, kind, label in (
        (r"甲方应向乙方提供工作条件[：:]" + _EMPTY, "party_a_work_conditions", "甲方提供的工作条件"),
        (r"甲方应向乙方提供的其[他它]协作[：:]" + _EMPTY, "party_a_cooperation", "甲方提供的其他协作"),
    ):
        if re.fullmatch(pattern, core):
            add(kind, label, "甲方")
    if re.fullmatch(r"甲方有权对乙方的技术服务工作提出改进意见，乙方应当在" + _EMPTY + r"日内对甲方的意见进行调整、修改。", core):
        add("improvement_deadline", "乙方调整、修改意见的天数", "乙方")
    if re.fullmatch(r"本合同项下技术服务项目结束后，乙方应当为甲方就相关报告内容的理解和实施过程中遇到的相关问题提供免费服务，具体包括[：:]" + _EMPTY + r"。", core):
        add("free_service_scope", "项目结束后免费服务的具体内容", "乙方")
    if re.fullmatch(r"技术服务工作成果验收完成后" + _EMPTY + r"个工作日内，乙方应归还甲方交予的全部资料、数据等，不得擅自留存复制品，或者乙方应按照甲方允许的方式销毁全部资料、数据。", core):
        add("return_materials_deadline", "乙方归还或销毁资料的工作日数", "乙方")
    if re.fullmatch(r"乙方(?:应当|应)按照下列进度要求向甲方提供技术服务[：:]本技术服务项目的履行期限为[：:]"
                    + _EMPTY + r"至" + _EMPTY + r"。具体进度要求为[：:]" + _EMPTY + r"。", core):
        add("schedule", "履行期限起止日期")
        add("schedule", "具体进度要求")
    if re.fullmatch(r"乙方(?:将|应当|应)按照下列质量要求向甲方提供技术服务[：:]" + _EMPTY + r"。", core):
        add("quality", "技术服务质量要求")

    # These bounded tokens can coexist with fixed figures and conditions. Only
    # their empty fields are reported; every surrounding word stays in the proof.
    if core.startswith("本合同项下技术服务费用"):
        for m in re.finditer(r"(不含税总额|含税总额)为[：:]" + _EMPTY + r"元人民币，[（(]大写[：:]人民币" + _EMPTY + r"[）)]", core):
            add("fee_amounts_and_rate", m[1] + "及大写金额")
        if re.search(r"税率" + _EMPTY + r"%[。；]", core):
            add("fee_amounts_and_rate", "技术服务费用税率")
    if core.startswith("技术服务费用由甲方按照以下第"):
        payment = re.match(r"技术服务费用由甲方按照以下第" + _EMPTY + r"条约定，付款方式为[：:]", core)
        deposit = re.match(r"技术服务费用由甲方按照以下第" + _EMPTY + r"条约定①不适用质量保证金。②适用质量保证金。", core)
        if payment and not _CHECKED.search(core) and re.search(r"□银行转账；□银行汇票；□其他" + _EMPTY + r"，", core):
            add("payment_choices_and_schedule", "付款条款选项")
            add("payment_choices_and_schedule", "付款方式选项（银行转账/银行汇票/其他）")
            for pattern, label in (
                (r"通过验收后" + _EMPTY + r"日内", "一次性支付期限（日）"),
                (r"本合同生效后" + _EMPTY + r"日内", "首期支付期限（日）"),
                (r"支付报酬总价" + _EMPTY + r"%", "首期支付比例"),
                (r"项目验收合格后" + _EMPTY + r"日内", "验收后支付期限（日）"),
                (r"支付至报酬总价的" + _EMPTY + r"%", "验收后累计支付比例"),
            ):
                if re.search(pattern, core):
                    add("payment_choices_and_schedule", label)
        if deposit and not _CHECKED.search(core):
            add("quality_deposit_choice", "质量保证金适用选项（①/②）")
    for heading, labels, kind, owner in (
        ("乙方开户银行名称、地址、帐号和税号（如适用）为：", ("开户银行", "地址", "银行联行行号", "帐号", "税号"), "bank_details", "乙方"),
        ("开具发票所需甲方信息：", ("纳税人名称", "税务登记号", "开户行名称", "开户行账号", "税务登记地址", "财务电话"), "invoice_details", "甲方"),
    ):
        if re.fullmatch(re.escape(heading) + "".join(re.escape(label) + r"[：:]" + _EMPTY for label in labels), core):
            for label in labels:
                add(kind, label, owner)
    if core.startswith("在履行本合同期间，每一方应") and re.search(r"本合同执行" + _EMPTY + r"行业增值税税率", core):
        add("industry_tax_basis", "适用增值税税率的行业")
    if not _CHECKED.search(core):
        if core.startswith("乙方应在甲方向乙方支付每期费用之前") and "针对□全部；□当期服务费用向甲方" in core:
            add("invoice_scope_choice", "增值税专用发票开具范围（全部/当期）")
        if core.startswith("乙方保证其在本合同签署之日□属于；□不属于依据"):
            add("sme_choice", "乙方是否属于中小企业", "乙方")
    if re.fullmatch(r"技术服务成果形式[：:]" + _EMPTY + r"[,，]份数为" + _EMPTY + r"份，介质为" + _EMPTY + r"。", core):
        for label in ("技术服务成果形式", "技术服务成果份数", "技术服务成果介质"):
            add("acceptance_deliverables", label)
    if core.startswith("技术服务工作成果的验收程序："):
        for pattern, label in (
            (r"各项技术服务工作成果自完成之日起" + _EMPTY + r"日内，", "首次提请验收期限（日）"),
            (r"并自验收不合格之日起" + _EMPTY + r"日内重新提请验收。", "不合格后重新提请验收期限（日）"),
            (r"任一项技术服务工作成果连续" + _EMPTY + r"次验收不合格的，", "触发未完成及付款后果的连续不合格次数"),
        ):
            if re.search(pattern, core):
                add("acceptance_deadlines", label)
    if re.fullmatch(r"技术服务工作成果的验收方法[：:]" + _EMPTY + r"。", core):
        add("acceptance_method", "技术服务工作成果的验收方法")
    if re.fullmatch(r"验收时间和地点[：:]" + _EMPTY + r"。", core):
        add("acceptance_time_place", "验收时间和地点")
    # A closed conjunction, not a general permission for unresolved references.
    # No target for the literal zero references and no ownership is supplied.
    ip_parts = re.split(r"(?=\([1-4]\))", core)
    if (len(ip_parts) == 5 and core.startswith("受限于第0条和第0条规定的效力，") and
            ip_parts[0].count("第0条") == 3 and "第0条所述技术服务工作成果以外的新技术成果" in ip_parts[0] and
            re.search(r"这些新技术成果及其知识产权的归属采用以下第" + _EMPTY + r"种方式[：:]$", ip_parts[0]) and
            ip_parts[1].startswith("(1)归甲方所有，未经甲方同意乙方不得") and
            ip_parts[2].startswith("(2)归甲方所有，乙方□有权无偿使用；□可以在双方协商确定向甲方支付的使用价格后有偿使用") and
            ip_parts[3].startswith("(3)归乙方所有，甲方有权无偿使用。未经乙方同意，甲方不得") and
            ip_parts[4].startswith("(4)双方共有，任何一方都有权自己使用；但任何一方") and
            re.search(r"甲方占\d+%，乙方占\d+%的比例进行分配。$", ip_parts[4]) and
            not _CHECKED.search(core)):
        add("ip_choice_with_literal_zero_references", "新技术成果及知识产权归属方式（第1～4种）")
        add("ip_choice_with_literal_zero_references", "第2种方式中乙方有偿或无偿使用的选项")
        issues.append({"kind": "literal_zero_article_reference", "literal_reference": "第0条",
                       "occurrence_count": 3, "reading_status": "read",
                       "value_status": "reference_requires_clarification", "resolved_reference": None,
                       "owner_party": None, "values_inferred": False})
    return list(dict.fromkeys(kinds)), fields, issues


def contract_review_slot_catalog(parsed: dict, manifest: dict) -> dict:
    """Return independently read contract pending fields and explicit limits.

    Invalid source identities retain the existing N1 indexing error. Unread,
    incomplete, unsupported or ambiguous spans never become pending values.
    Caller must retain all original N2 obligations and review uncovered entries.
    """
    blocks, units, documents = _index(parsed, manifest)
    result = {"rule": RULE, "exhaustive": False, "coverage_status": "limited_lexical_rules",
              "physical_blank_status": "not_determined", "satisfaction": "not_determined",
              "supported_forms": list(SUPPORTED_FORMS), "items": [], "uncovered_clauses": [],
              "unavailable_sources": [], "unresolved_scopes": [], "limitations": [
                  "Unmatched clauses are not evidence of complete fields or satisfaction.",
                  "No physical whitespace, selected option, company material gap, or value is inferred.",
                  "Only native PDF clauses with complete, consecutive reading and a next boundary are supported.",
                  "Fields before numbered articles, nested decimal clauses, and final unbounded clauses are not covered."]}
    for document in documents:
        source_id = document["source_id"]
        if (document.get("format") != "pdf" or not re.fullmatch(r"[a-f0-9]{64}", document["sha256"]) or
                document["sha256"] == "0" * 64 or
                parsed.get("coverage", {}).get("required_complete") is not True or
                type(document.get("expected_units")) is not int or document["expected_units"] != len(document["units"]) or
                [u.get("page") for u in document["units"]] != list(range(1, len(document["units"]) + 1)) or
                any(u.get("coverage", {}).get("status") != "read" or
                    any(u.get("coverage", {}).get(k) for k in ("unread_objects", "unmapped_glyphs", "error_code"))
                    for u in document["units"])):
            result["unavailable_sources"].append({"source_id": source_id, "status": "not_eligible_for_slot_inference"})
            continue
        body = [b for b in blocks.values() if b["source_id"] == source_id]
        footer_proofs = {key: proof for key, proof in _pdf_footer_skips(blocks).items()
                         if proof["source_id"] == source_id and _line(blocks[key], units) is not None}
        footer_ids = set(footer_proofs)
        positions = {b["block_id"]: i for i, b in enumerate(body)}
        attachment_starts = {i for i in range(len(body)) if _attachment_start(body, i, units)}
        signature_boundaries = _signature_boundaries(body, units, footer_proofs)
        document_end = _document_end_boundary(body, units, footer_proofs, document)
        chapter = scope = article = None
        starts = []
        for i, block in enumerate(body):
            text = block["text"]
            if _CHAPTER.fullmatch(text):
                chapter, article = block, None
                scope = {"kind": "main_contract", "title": text, "heading": block} if _CONTRACT.fullmatch(text) else None
            attachment = _ATTACHMENT.fullmatch(text)
            if chapter and _CONTRACT.fullmatch(chapter["text"]) and attachment and i in attachment_starts:
                scope = {"kind": "contract_attachment", "attachment_number": attachment[1], "title": text, "heading": block}
                article = None
            elif scope and attachment and not _attachment_reference_list(body, i, units):
                # An unbounded possible annex cannot inherit main-contract scope.
                scope = article = None
                result["unresolved_scopes"].append({"source_id": source_id, "heading": _member(block),
                                                     "reason": "attachment_scope_not_bounded"})
            heading = _ARTICLE.fullmatch(text)
            if scope and heading:
                article = block
            clause = _CLAUSE.fullmatch(text)
            if (scope and article and clause and _ARTICLE.fullmatch(article["text"]) and
                    _number(_ARTICLE.fullmatch(article["text"])[1]) == int(clause[1])):
                starts.append((i, chapter, dict(scope), article, clause))

        for start, ancestor, scope, article, clause in starts:
            end = next((i for i in range(start + 1, len(body))
                        if _CLAUSE.fullmatch(body[i]["text"]) or _ARTICLE.fullmatch(body[i]["text"]) or
                        _CHAPTER.fullmatch(body[i]["text"]) or
                        i in attachment_starts or
                        (scope["kind"] == "main_contract" and i in signature_boundaries) or
                        (_ATTACHMENT.fullmatch(body[i]["text"]) and not _attachment_reference_list(body, i, units))), None)
            end_is_document_footer = bool(end is None and document_end and scope["kind"] == "contract_attachment" and
                start == len(body) - 2 and body[start]["unit_id"] == body[-1]["unit_id"] and
                re.fullmatch(r"其他[：:]" + _EMPTY, _compact(clause[3])))
            if end_is_document_footer:
                end = len(body) - 1
            raw = body[start:end] if end is not None else body[start:]
            selected = [b for b in raw if b["block_id"] not in footer_ids]
            identity = {"source_id": source_id, "scope": {k: v for k, v in scope.items() if k != "heading"},
                        "clause_number": clause[1] + "." + clause[2], "block_ids": [b["block_id"] for b in selected]}
            boundary = body[end] if end is not None else None
            sibling = _CLAUSE.fullmatch(boundary["text"]) if boundary else None
            next_article = _ARTICLE.fullmatch(boundary["text"]) if boundary else None
            boundary_ok = bool(boundary and ((sibling and sibling[1] == clause[1] and int(sibling[2]) == int(clause[2]) + 1) or
                (next_article and _number(next_article[1]) == int(clause[1]) + 1) or
                end in attachment_starts or (scope["kind"] == "main_contract" and end in signature_boundaries) or
                end_is_document_footer))
            context = [ancestor, scope["heading"], article]
            article_index = positions[article["block_id"]]
            first_clause = next((i for i in range(article_index + 1, start + 1) if _CLAUSE.fullmatch(body[i]["text"])), start)
            preamble_raw = body[article_index + 1:first_clause]
            preamble = [b for b in preamble_raw if b["block_id"] not in footer_ids]
            if (not boundary_ok or any(_line(b, units) is None for b in context) or
                    not _continuous([article, *preamble_raw, body[first_clause]], units) or
                    not _continuous([*raw, boundary], units)):
                result["uncovered_clauses"].append({**identity, "reason": "source_or_boundary_not_proven"})
                continue
            parts = [(selected, None, [], boundary)]
            if (re.fullmatch(r"[甲乙]方权利与义务", _compact(clause[3])) or
                    _compact(clause[3]) == "双方确定，按以下标准和方式对乙方提交的技术服务工作成果进行验收："):
                indices = [i for i, b in enumerate(selected) if _SUB.fullmatch(b["text"])]
                parts = []
                numbers = [int(_SUB.fullmatch(selected[i]["text"])[1]) for i in indices]
                valid_indices = indices if numbers == list(range(1, len(indices) + 1)) else []
                for position, index in enumerate(valid_indices):
                    stop = indices[position + 1] if position + 1 < len(indices) else len(selected)
                    number = int(_SUB.fullmatch(selected[index]["text"])[1])
                    sub_boundary = selected[stop] if stop < len(selected) else boundary
                    parts.append((selected[index:stop], str(number), selected[:indices[0]], sub_boundary))
            matched = False
            for members, sub_number, parents, sub_boundary in parts:
                if not members:
                    continue
                # Remove only the numbered clause marker for matching. Preserve it in evidence.
                text = "\n".join(b["text"] for b in members)
                lexical = text if sub_number else _CLAUSE.fullmatch(members[0]["text"])[3] + "\n" + "\n".join(b["text"] for b in members[1:])
                kinds, fields, issues = _fields(lexical)
                if not fields:
                    continue
                matched = True
                proof = {"rule": RULE, "source_id": source_id, "source_sha256": document["sha256"],
                         "parsed_sha256": digest(parsed), "source_reading_sha256": digest(document),
                         "scope_heading": _member(scope["heading"]), "contract_ancestor": _member(ancestor),
                         "article_heading": _member(article), "article_preamble": [_member(b) for b in preamble],
                         "parent_context": [_member(b) for b in parents],
                         "members": [_member(b) for b in members], "complete_clause_members": [_member(b) for b in selected],
                         "next_boundary": _member(boundary), "subclause_next_boundary": _member(sub_boundary),
                         "contract_end_boundary": signature_boundaries.get(end) if scope["kind"] == "main_contract" else None,
                         "document_end_boundary": document_end if end_is_document_footer else None,
                         "separated_page_footers": [{**_member(b), "footer_proof": footer_proofs[b["block_id"]]}
                                                    for b in [*preamble_raw, *raw] if b["block_id"] in footer_ids],
                         "coverage": [{"unit_id": key, "coverage_sha256": digest(units[key]["coverage"])}
                                      for key in dict.fromkeys(b["unit_id"] for b in [*context, *preamble_raw, *raw, boundary])]}
                owners = {field["owner_party"] for field in fields}
                item = {**identity, "block_ids": [b["block_id"] for b in members], "subclause_number": sub_number,
                        "source_span_kind": "complete_numbered_subclause" if sub_number else "complete_numbered_clause",
                        "text": text, "pending_fields": fields, "slot_forms": kinds,
                        "blocking_issues": issues,
                        "reference_status": "requires_clarification" if issues else "not_checked_exhaustively",
                        "owner_party": next(iter(owners)) if len(owners) == 1 else None,
                        "reading_status": "read", "value_status": "value_not_supplied_in_read_text",
                        "response_status": "pending_contract_agreement", "satisfaction": "not_determined",
                        "physical_blank_status": "not_determined", "values_inferred": False,
                        "source_proof": proof, "source_proof_sha256": digest(proof)}
                item["pending_id"] = "CONTRACT-PENDING-" + digest({k: item[k] for k in ("scope", "source_proof_sha256", "pending_fields")})[:20]
                result["items"].append(item)
            # A matched field is never evidence that all other fields were found.
            result["uncovered_clauses"].append({**identity, "reason": "other_fields_not_exhaustively_checked" if matched else "not_matched_by_finite_rules"})
    return result
