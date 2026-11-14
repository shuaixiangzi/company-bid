"""Separate response content from source review while preserving every obligation."""
from __future__ import annotations

import copy
import re

from .storage import digest


def _identity(prefix, value):
    return prefix + digest(value)[:20]


def _plain(text):
    return re.sub(r"\s+", "", text)


def organize_response(sections, requirements, details, formats, blocks, issues, attachments,
                      manifest, material_report):
    """Change presentation, never eligibility or the original requirement text.

    A review-only obligation always receives an explicit response-side pending
    marker. Original sections, sources and individual issues stay in the review.
    """
    known = {r["requirement_id"]: r for r in requirements}
    # Keep the format section's original application identity.
    from .extraction import _id
    format_sections = {_id("SEC-", f["format_id"]): f for f in formats}
    before = {s["section_id"]: copy.deepcopy(s) for s in sections}
    main, review, changes, new_sections = [], [], [], []

    def marker(section, refs, title, reason, action, severity="missing"):
        item = {"issue_id": f"M-{len(issues) + 1:03d}", "section_id": section["section_id"],
            "requirement_refs": list(dict.fromkeys(refs)), "title": title, "reason": reason,
            "action": action, "severity": severity,
            "source_requirement": "\n".join(known[r]["text"] for r in dict.fromkeys(refs))}
        issues.append(item)
        return item["issue_id"]

    def add_refs(section, refs):
        section["requirement_refs"] = list(dict.fromkeys([*section["requirement_refs"], *refs]))
        section["source_refs"] = list(dict.fromkeys([*section["source_refs"],
            *(ref for rid in refs for ref in known[rid]["source_refs"])]))
        if not section["writing_tasks"]:
            section["nonwriting_requirement_refs"] = list(section["requirement_refs"])

    def create(key, title, refs, level=1):
        value = {"section_id": _identity("SEC-", ["response-organization-v1", key]),
            "title": title, "document_role": "response", "kind": "facts", "requirement_refs": [],
            "source_refs": [], "fact_refs": [], "table_refs": [], "attachment_refs": [], "writing_tasks": [],
            "fixed_content": [], "nonwriting_requirement_refs": [], "level": level,
            "start_on_new_page": False, "render_body": True}
        add_refs(value, refs);new_sections.append(value)
        return value

    for section in sections:
        old_role = section.get("document_role", "response")
        if section["section_id"] in format_sections or section["writing_tasks"]:
            section["document_role"] = "response"
            chapter = format_sections.get(section["section_id"])
            if chapter and re.match(r"^\d+\s+\S", chapter["title"]):
                section["level"] = 1
            main.append(section)
        elif section["render_body"]:
            section["document_role"] = "review"
            review.append(section)
        else:
            # Non-visible procedure records remain available to the checklist.
            section["document_role"] = "review"
            review.append(section)
        if section["document_role"] != old_role:
            changes.append({"section_id": section["section_id"], "from": old_role, "to": section["document_role"],
                            "requirement_refs": section["requirement_refs"]})

    families = [s for s in main if format_sections.get(s["section_id"], {}).get("source_derivation", {}).get("kind")
                == "native_material_family_prefix"]
    family = families[0] if len(families) == 1 else None
    subgroups, relocations = {}, []
    if family:
        chapter = format_sections[family["section_id"]]
        for bid in chapter["source_derivation"]["sibling_block_ids"]:
            block = blocks[bid]
            match = re.fullmatch(r"\s*(\d+\.\d+)[、,.．]?\s*(资质|业绩|财务要求|信誉要求|人员要求|其他)材料[：:]?\s*", block["text"])
            if not match:
                continue
            topic = {"财务要求": "财务", "信誉要求": "信誉", "人员要求": "人员"}.get(match[2], match[2])
            refs = [rid for rid, item in details.items() if _plain(item["text"]) == _plain(block["text"])]
            section = create([family["section_id"], bid], block["text"].strip(), refs or family["requirement_refs"], 2)
            subgroups[topic] = section
        # Reassign the native material subheadings that an earlier flat format
        # continuation placed after the performance table. Text itself is exact.
        heading_texts = {_plain(s["title"]): s for s in subgroups.values()}
        for section in main:
            retained = []
            for item in section["fixed_content"]:
                target = heading_texts.get(_plain(item.get("text", ""))) if item["kind"] == "paragraph" else None
                if target is None:
                    retained.append(item)
                else:
                    target["source_refs"] = list(dict.fromkeys([*target["source_refs"], *item["source_refs"]]))
                    relocations.append({"from_section_id": section["section_id"], "to_section_id": target["section_id"],
                                        "source_refs": item["source_refs"], "text_sha256": digest(item["text"])})
            section["fixed_content"] = retained

    def topic_for(text):
        if re.search(r"业绩|历史项目|项目合同|中标通知|验收文件", text):return "业绩"
        if re.search(r"人员|项目负责人|项目经理|身份证|社会保险|职称|CP[234]", text):return "人员"
        if re.search(r"财务|审计|资产负债|资信", text):return "财务"
        if re.search(r"信誉|信用|诉讼|仲裁|行贿|黑名单|事故", text):return "信誉"
        if re.search(r"资质|营业执照|法人资格|质量管理体系认证|证书", text):return "资质"
        return "其他"

    summary_groups = {}
    response_ids = {s["section_id"] for s in main}
    for section in review:
        if not section["render_body"]:
            continue
        for rid in section["requirement_refs"]:
            if known[rid]["response_mode"] == "procedure":
                continue
            detail = details.get(rid, known[rid])
            # Contract source is never moved into a company qualification slot.
            contract = bool(re.search(r"合同条款|原文与待核事项|合同条款原文", section["title"] + "\n" +
                "\n".join(i.get("text", "")[:60] for i in section["fixed_content"])))
            qualification = known[rid]["category"] == "qualification" and not contract
            target = subgroups.get(topic_for(detail["text"])) if qualification else None
            if target is None:
                key = "合同条款响应待核事项" if contract else "补充响应与待核事项"
                if key not in summary_groups:
                    summary_groups[key] = create(key, key, [rid])
                target = summary_groups[key]
            add_refs(target, [rid])
            response_ids.add(target["section_id"])

    # One response marker per destination; detailed original issues remain
    # separate in the review document and in the immutable plan.
    review_refs = {rid for s in review if s["render_body"] for rid in s["requirement_refs"]
                   if known[rid]["response_mode"] != "procedure"}
    pending_links = []
    for target in [*subgroups.values(), *summary_groups.values()]:
        refs = [rid for rid in target["requirement_refs"] if rid in review_refs]
        if refs:
            iid = marker(target, refs, target["title"].rstrip("：:") + "待核对及补充",
                "本栏目仍有待补资料、待确认事实或适用条件；详细原文、逐项缺项及来源保存在配套核对清单。",
                "核对本栏目实际资料与全部招标条件，补齐对应缺项后线下完成响应；不得将已有候选材料视为资格通过。")
            pending_links.append({"section_id": target["section_id"], "requirement_refs": refs, "issue_id": iid})

    # Move complete proof packages into the response, retaining every actual
    # originating requirement and an explicit applicability warning.
    supplied = {a["attachment_id"]: a for a in manifest.get("attachments", [])}
    adopted = {a["attachment_id"]: a for a in attachments}
    assembly = []
    for candidate in material_report.get("assembly_candidates", []):
        rid = candidate["requirement_id"]
        if rid not in known:
            continue
        text = details.get(rid, known[rid])["text"]
        target = subgroups.get(topic_for(text)) or family
        if target is None:
            targets = [s for s in main if rid in s["requirement_refs"]]
            if len(targets) != 1:
                continue
            target = targets[0]
        add_refs(target, [rid])
        response_ids.add(target["section_id"])
        placed = []
        for aid in candidate["attachment_ids"]:
            original = supplied[aid]
            if aid in adopted and adopted[aid]["section_id"] in response_ids:
                owner = next(s for s in [*main, *new_sections] if s["section_id"] == adopted[aid]["section_id"])
                add_refs(owner, [rid]);adopted[aid]["requirement_refs"] = list(dict.fromkeys([*adopted[aid]["requirement_refs"], rid]))
                placed.append(aid)
                continue
            for old in [*sections, *new_sections]:
                old["attachment_refs"] = [ref for ref in old["attachment_refs"] if ref != aid]
            old_refs = adopted[aid]["requirement_refs"] if aid in adopted else []
            add_refs(target, old_refs)
            value = {**copy.deepcopy(original), "section_id": target["section_id"],
                     "requirement_refs": list(dict.fromkeys([*old_refs, rid]))}
            if aid in adopted:
                attachments.remove(adopted[aid])
            attachments.append(value);adopted[aid] = value
            target["attachment_refs"] = list(dict.fromkeys([*target["attachment_refs"], aid]));placed.append(aid)
        if placed:
            iid = marker(target, [rid], "已附证明的适用条件仍待核对",
                "按已维护来源装配完整证明包；没有据此认定资格合格、获得评分或确定本次人员派任。",
                "；".join(candidate.get("pending_checks", [])) or "核对原件、日期、等级及本次适用范围。", "warning")
            assembly.append({**candidate, "response_section_id": target["section_id"], "issue_id": iid,
                             "qualification_result": "not_determined"})

    for section in review:
        if (section["render_body"] and not any((section["fixed_content"], section["fact_refs"],
                section["table_refs"], section["attachment_refs"], section["writing_tasks"])) and
                not any(i["section_id"] == section["section_id"] for i in issues)):
            marker(section, section["requirement_refs"], "证明材料落点及适用性待核对",
                "原证明页已转入标书对应资料栏目，原要求及来源在此保留供核对。",
                "结合正文中的完整证明包逐项核对本条条件；转移附件位置不代表资格通过。", "warning")

    # Keep original fixed table titles and order inside their native parent.
    arranged = []
    family_children = [s for s in main if family and s is not family and
                       s["level"] > 1 and formats and
                       any(word in s["title"] for word in ("业绩统计表", "单位负责人为同一人"))]
    for section in main:
        if section in family_children:
            continue
        arranged.append(section)
        if section is family:
            for topic, subgroup in subgroups.items():
                if not subgroup["attachment_refs"] and not any(i["section_id"] == subgroup["section_id"] for i in issues):
                    marker(subgroup, subgroup["requirement_refs"], subgroup["title"].rstrip("：:") + "待补",
                           "材料栏目已读取，尚无完整可采用资料。", "补充本栏目实际资料并核对适用条件。")
                arranged.append(subgroup)
                for child in family_children:
                    if (topic == "业绩" and "业绩统计表" in child["title"]) or (topic == "其他" and "单位负责人为同一人" in child["title"]):
                        child["level"] = 3;arranged.append(child)
            arranged.extend(s for s in family_children if s not in arranged)
    arranged.extend(summary_groups.values())
    sections[:] = [*arranged, *review]
    return {"rule": "explicit_response_review_organization_v1", "role_changes": changes,
            "native_heading_relocations": relocations, "response_pending_links": pending_links,
            "proof_assembly": assembly, "section_order": [s["section_id"] for s in sections],
            "source_sections_preserved": all(key in {s["section_id"] for s in sections} for key in before),
            "satisfaction_status": "not_determined"}
