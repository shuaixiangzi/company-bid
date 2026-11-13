"""N3-only source controls; they never declare bidder compliance or alter N2.

The recognizers deliberately accept one closed source form. Unsupported or
mixed wording retains its original response contract. Every accepted control
binds whole native source lines before a writing task may be suppressed.
"""
from __future__ import annotations

import copy
import hashlib
import re

from .extraction import PlanningError, _hash


COMPONENTS = (
    ("response_letter", "响应函", (("响应函",),)),
    ("authorization", "授权委托书（如有）", (("授权委托书", "法人代表授权书"),)),
    ("joint_bid", "联合体协议书（如有）", (("联合体协议书",),)),
    ("deviation_tables", "商务和技术偏差表", (("商务条款偏离表", "商务偏差表"),
                                                     ("技术规格偏离表", "技术偏差表"))),
    ("quotation_tables", "报价表", (("报价表",), ("分项报价明细表",))),
    ("qualification", "资格审查资料", (("资格审查资料", "资格审查材料"),)),
    ("response_plan", "响应方案", (("响应方案", "技术服务方案"),)),
    ("other_materials", "响应人须知前附表要求的其他资料", ()),
)

_ENUMERATION = re.compile(
    r"(?:3\.1 响应文件的组成)?3\.1\.1 响应文件应包括下列内容："
    + "".join("（" + str(i) + "）" + re.escape(label) + ("。" if i == 8 else "；")
              for i, (_, label, _) in enumerate(COMPONENTS, 1))
    + r"(?:响应人在评审过程中作出的符合采购文件要求的澄清、说明和补正，构成响应文件的组成部分。)?"
)
_GLOBAL = re.compile(
    r"1\.10\.1 技术标准和要求和合同条款及格式中的关键条款均以“\*”符号标记。响应文件"
    r"应当对技术标准和要求和合同条款及格式中的关键条款作出满足性或更有利于采购人的响应，"
    r"否则，响应人的响应文件将被视为无效。"
)
_EXCEPTION = re.compile(
    r"3\.1\.2 响应人的法定代表人（单位负责人）亲自签署响应文件、亲自参加询比的，响应文件"
    r"不包括第3\.1\.1（2）目所指的授权委托书。第一章“询比采购公告”规定不接受联合体的，"
    r"或响应人没有组成联合体的，响应文件不包括第3\.1\.1（3）目所指的联合体协议书。响应人"
    r"须知前附表未要求响应人递交响应保证金的，响应文件不包括第3\.1\.1（4）目所指的响应保证金。"
)


def _proof(requirement, citation, blocks, units, documents, native_span):
    ids = citation.get("block_ids") or [citation["block_id"]]
    members = [blocks[key] for key in ids]
    document = next((doc for doc in documents if doc["source_id"] == citation["source_id"]), None)
    text = "\n".join(value["text"] for value in members)
    if (document is None or document.get("format") != "pdf" or not native_span(members)
            or any(value["source_id"] != document["source_id"]
                   or units[value["unit_id"]].get("coverage", {}).get("status") != "read" for value in members)
            or citation["quote"] != requirement["text"] or text != requirement["text"]
            or citation.get("start") != 0 or citation.get("end") != len(text)
            or citation.get("clause_id") or citation.get("table_row_id")):
        raise PlanningError("PLAN_CONTROL_SOURCE_UNPROVEN")
    payload = {"rule": "whole_native_plan_control_v1", "source_id": document["source_id"],
               "source_sha256": document["sha256"], "requirement_id": requirement["requirement_id"],
               "requirement_text_sha256": hashlib.sha256(text.encode()).hexdigest(),
               "members": [{key: value[key] for key in ("block_id", "locator", "ordinal", "unit_id", "page")}
                           | {"text_sha256": hashlib.sha256(value["text"].encode()).hexdigest()} for value in members]}
    return {**payload, "proof_sha256": _hash(payload)}


def _component_slices(requirement, proof, blocks):
    text = requirement["text"]
    spans, position = [], 0
    for member in proof["members"]:
        block = blocks[member["block_id"]]
        spans.append((position, position + len(block["text"]), block))
        position += len(block["text"]) + 1
    components = []
    for number, (key, label, _) in enumerate(COMPONENTS, 1):
        match = re.search("（" + str(number) + "）.*?" + ("。" if number == 8 else "；"), text, re.S)
        assert match is not None  # closed full enumeration was checked first
        parts = [{"block_id": block["block_id"], "locator": block["locator"],
                  "start": max(match.start(), start) - start,
                  "end": min(match.end(), end) - start,
                  "text_sha256": hashlib.sha256(block["text"].encode()).hexdigest()}
                 for start, end, block in spans if start < match.end() and end > match.start()]
        first = blocks[parts[0]["block_id"]]
        citation = {"source_id": proof["source_id"], "block_id": first["block_id"],
                    "locator": first["locator"], "quote": match.group()}
        if len(parts) > 1:
            citation["block_ids"] = [part["block_id"] for part in parts]
        components.append({"number": number, "component": key, "source_name": label,
                           "source_text": match.group(), "source_parts": parts, "citation": citation})
    return components


def collect_plan_controls(requirements, blocks, units, documents, native_span):
    """Reconstruct closed controls from current source, with no N2 mutation."""
    groups, global_controls, exceptions = {}, [], []
    for requirement in requirements:
        compact = requirement["text"].replace("\n", "")
        kind = ("components" if _ENUMERATION.fullmatch(compact) else
                "global_critical" if _GLOBAL.fullmatch(compact) else
                "number_exception" if _EXCEPTION.fullmatch(compact) else None)
        if kind is None:
            continue
        # A source control never releases unread or prescribed format gates.
        proofs = [_proof(requirement, citation, blocks, units, documents, native_span)
                  for citation in requirement["citations"]]
        if kind == "components":
            for proof in proofs:
                components = _component_slices(requirement, proof, blocks)
                identity = _hash([proof["source_id"], proof["source_sha256"],
                                  [component["source_parts"] for component in components]])
                group = groups.setdefault(identity, {"group_id": "CMP-" + identity[:24],
                    "source_id": proof["source_id"], "source_sha256": proof["source_sha256"],
                    "requirement_refs": [], "proofs": [], "components": components})
                if requirement["requirement_id"] not in group["requirement_refs"]:
                    group["requirement_refs"].append(requirement["requirement_id"])
                if proof not in group["proofs"]:
                    group["proofs"].append(proof)
        elif kind == "global_critical":
            if (requirement["category"] == "technical" and requirement["response_mode"] == "writing"
                    and requirement["format_status"] == "not_prescribed" and not requirement.get("format_title")
                    and not any(requirement.get(key) for key in
                                ("required_fact_labels", "attachment_keywords", "reference_keywords"))):
                global_controls.append({"requirement_id": requirement["requirement_id"], "proofs": proofs,
                    "reason": "closed_global_rule_is_a_pending_control_not_a_proposed_method",
                    "n2_candidate_mutation": False, "satisfaction_status": "not_determined"})
        else:
            exceptions.append({"requirement_id": requirement["requirement_id"], "proofs": proofs})
    conflicts = []
    for group in groups.values():
        for exception in exceptions:
            if any(proof["source_id"] == group["source_id"] for proof in exception["proofs"]):
                payload = {"rule": "component_number_name_conflict_v1", "group_id": group["group_id"],
                    "requirement_refs": [*group["requirement_refs"], exception["requirement_id"]],
                    "component_number": 4, "listed_name": COMPONENTS[3][1], "exception_name": "响应保证金",
                    "component_source_parts": group["components"][3]["source_parts"],
                    "exception_proofs": exception["proofs"], "disposition": "preserve_formats_pending_source_review"}
                conflicts.append({**payload, "proof_sha256": _hash(payload)})
    return {"component_groups": list(groups.values()), "global_controls": global_controls,
            "number_name_conflicts": conflicts}


def component_targets(component, group, formats, sections, tables, issues, title_key, blocks):
    """Return existing assembly candidates, never fulfillment from a title/grid."""
    roles = COMPONENTS[component["number"] - 1][2]
    targets = []
    by_table = {table["table_id"]: table for table in tables}
    for role_index, aliases in enumerate(roles):
        for chapter in formats:
            anchor = blocks[chapter["citation"]["block_id"]]
            if (anchor["source_id"] != group["source_id"]
                    or title_key(chapter["title"]) not in aliases):
                continue
            # The caller supplies a deterministic format->section ID mapping;
            # title matching above only finds a candidate family for review.
            section_id = chapter.get("planned_section_id")
            section = next((item for item in sections if item["section_id"] == section_id), None)
            if section is None:
                raise PlanningError("COMPONENT_FORMAT_PLACEMENT_MISSING")
            if any(table_id not in by_table or by_table[table_id]["section_id"] != section_id
                   for table_id in section["table_refs"]):
                raise PlanningError("COMPONENT_FORMAT_PLACEMENT_MISSING")
            targets.append({"role_index": role_index, "format_id": chapter["format_id"],
                "source_title": chapter["title"], "source_anchor": {key: anchor[key] for key in
                    ("source_id", "block_id", "locator", "ordinal", "page")},
                "source_blocks": [{"block_id": key, "text_sha256": hashlib.sha256(blocks[key]["text"].encode()).hexdigest()}
                                  for key in chapter["content_block_ids"]],
                "section_id": section_id, "table_refs": list(section["table_refs"]),
                "writing_unit_ids": [task["unit_id"] for task in section["writing_tasks"]],
                "attachment_refs": list(section["attachment_refs"]),
                "existing_issue_ids": [item["issue_id"] for item in issues if item["section_id"] == section_id],
                "has_nonempty_fixed_paragraph": any(item["kind"] == "paragraph" and item["text"].strip()
                                                    for item in section["fixed_content"]),
                "response_status": "pending_source_review", "satisfaction_status": "not_determined"})
    return copy.deepcopy(targets)
