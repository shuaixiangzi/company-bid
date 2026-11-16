"""Separate response content from source review while preserving every obligation."""
from __future__ import annotations

import copy
import re

from .storage import digest


def _identity(prefix, value):
    return prefix + digest(value)[:20]


def _plain(text):
    return re.sub(r"\s+", "", text)


def _proof_display_labels(attachment_ids, actual, candidate=None):
    """Name only the exact attached objects, never other same-RID material."""
    origins = []
    for aid in attachment_ids:
        item = actual[aid]
        title = item.get("title", "").strip()
        if not title:
            continue
        key = (item.get("bundle_id") or aid, title)
        origin = next((row for row in origins if (row["bundle_id"], row["title"]) == key), None)
        if origin is None:
            origin = {"title": title, "bundle_id": key[0], "attachment_ids": [], "source_refs": [],
                      "label_source": "attachment.title"}
            origins.append(origin)
        origin["attachment_ids"].append(aid)
        origin["source_refs"] = list(dict.fromkeys([*origin["source_refs"], *item.get("source_refs", [])]))
    if not origins and candidate:
        field = "title" if candidate.get("title") else "basis.maintained_title"
        title = candidate.get("title") or candidate.get("basis", {}).get("maintained_title", "")
        if title.strip():
            origins.append({"title": title.strip(), "bundle_id": candidate.get("group_id"),
                "attachment_ids": list(attachment_ids), "source_refs": list(candidate.get("source_refs", [])),
                "label_source": "assembly_candidate." + field})
    return list(dict.fromkeys(row["title"] for row in origins)), origins


def _proof_label_prefix(labels):
    return "已附证明：" + "；".join("“" + value + "”" for value in labels) + "。" if labels else ""


def _relocate_material_preamble(main, subgroups, format_sections, blocks, known, add_refs):
    """Keep a proven 'other materials' introduction with its own subheading."""
    target = subgroups.get("其他")
    if target is None:
        return []
    result = []
    for section in main:
        chapter = format_sections.get(section["section_id"], {})
        ids = chapter.get("content_block_ids", [])
        anchors = [i for i, bid in enumerate(ids) if blocks[bid]["text"] == target["title"]]
        if len(anchors) != 1:
            continue
        tail = [blocks[bid] for bid in ids[anchors[0] + 1:]]
        if (not tail or not tail[0]["text"].startswith("其中单位负责人")
                or not tail[-1]["text"].endswith(("格式如下：", "格式如下:"))):
            continue
        anchor = blocks[ids[anchors[0]]]
        scope = [anchor, *tail]
        positions = [re.fullmatch(r"pdf/page\[(\d+)\]/line\[(\d+)\]", b["locator"]) for b in scope]
        if (not all(positions) or len({b["source_id"] for b in scope}) != 1
                or any(b.get("origin") != "native" or b.get("read_disposition") or b["kind"] != "paragraph" for b in scope)):
            continue
        page, start = map(int, positions[0].groups())
        if any((int(p[1]), int(p[2])) != (page, start + i) for i, p in enumerate(positions)):
            continue
        following = [f for f in format_sections.values() if f["title"] == "单位负责人为同一人或存在控股、管理关系的单位情况说明"
                     and f.get("content_block_ids")]
        following = [f for f in following if (b := blocks[f["content_block_ids"][0]])["source_id"] == anchor["source_id"]
                     and b["locator"].startswith(f"pdf/page[{page+1}]/") and b["text"] == f["title"]]
        if len(following) != 1:
            continue
        moved = []
        for source in tail:
            matches = [item for item in section["fixed_content"] if item["kind"] == "paragraph" and item["text"] == source["text"]]
            if len(matches) != 1:
                raise ValueError("MATERIAL_PREAMBLE_SOURCE_OWNER_AMBIGUOUS")
            moved.append(matches[0])
        if target["fixed_content"] or section["fixed_content"][-len(moved):] != moved:
            raise ValueError("MATERIAL_PREAMBLE_ORDER_CONFLICT")
        target["fixed_content"] = moved
        section["fixed_content"] = section["fixed_content"][:-len(moved)]
        target["source_refs"] = list(dict.fromkeys([*target["source_refs"], *(ref for item in moved for ref in item["source_refs"])]))
        refs = [rid for rid in section["requirement_refs"] if all(b["text"] in known[rid]["text"] for b in tail)]
        add_refs(target, refs)
        result.append({"from_section_id": section["section_id"], "to_section_id": target["section_id"],
            "source_heading_block_id": anchor["block_id"], "source_block_ids": [b["block_id"] for b in tail],
            "following_format_id": following[0]["format_id"], "requirement_refs": refs,
            "before_after_content_sha256": digest(moved), "rule": "contiguous_other_materials_introduction_v1"})
    return result


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

    preamble_relocations = _relocate_material_preamble(main, subgroups, format_sections, blocks, known, add_refs)

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
            labels, label_origins = _proof_display_labels(placed, adopted, candidate)
            title = "已附证明的适用条件仍待核对"
            reason = _proof_label_prefix(labels) + "按已维护来源装配完整证明包；没有据此认定资格合格、获得评分或确定本次人员派任。"
            action = "；".join(candidate.get("pending_checks", [])) or "核对原件、日期、等级及本次适用范围。"
            existing = next((item for item in issues if item["requirement_refs"] == [rid]
                and item["title"] == "证明材料适用性待核对" and item["severity"] == "warning"), None)
            if existing:
                existing.update(section_id=target["section_id"], title=title, reason=reason, action=action)
                iid = existing["issue_id"]
            else:
                iid = marker(target, [rid], title, reason, action, "warning")
            assembly.append({**candidate, "response_section_id": target["section_id"], "issue_id": iid,
                             "display_labels": labels, "display_label_provenance": label_origins,
                             "qualification_result": "not_determined"})

    lookup_reclassifications = []
    assembled_by_requirement = {}
    for row in assembly:
        assembled_by_requirement.setdefault(row["requirement_id"], []).extend(row["attachment_ids"])
    for item in issues:
        refs = item["requirement_refs"]
        if (item["title"] != "所需证明附件未能唯一匹配" or not refs
                or not all(assembled_by_requirement.get(ref) for ref in refs)):
            continue
        ids = list(dict.fromkeys(aid for ref in refs for aid in assembled_by_requirement[ref]))
        if any(aid not in adopted for aid in ids):
            continue
        labels, label_origins = _proof_display_labels(ids, adopted)
        previous = {key: item[key] for key in ("title", "reason", "action", "severity")}
        item.update(title="已附证明的适用条件仍待核对", severity="warning",
            reason=_proof_label_prefix(labels) + "后续资料匹配已按来源装配证明页；此前通用检索未能唯一匹配不再代表当前没有附件。资格、有效期及本次适用性仍未确定。",
            action="对照正文实际证明包核对招标条件、原件、有效期及人员安排；有缺项时线下补齐，不能仅因已附证明而认定满足。")
        lookup_reclassifications.append({"issue_id": item["issue_id"], "requirement_refs": list(refs),
            "previous": previous, "current": {key: item[key] for key in previous}, "attachment_ids": ids,
            "display_labels": labels, "display_label_provenance": label_origins,
            "qualification_result": "not_determined"})

    appendix_placements = []
    performance = subgroups.get("业绩")
    if performance and performance["attachment_refs"]:
        # A source caption explicitly orders the table before its evidence.
        # Preserve the parent heading and all original requirement identities;
        # move only the complete ordered proof references to that table's tail.
        candidates = []
        for section in main:
            chapter = format_sections.get(section["section_id"], {})
            title = chapter.get("title", "")
            ids = chapter.get("content_block_ids", [])
            if (re.fullmatch(r"(?:响应人|投标人)业绩统计表[（(]后附业绩材料[）)]", _plain(title))
                    and ids and blocks.get(ids[0], {}).get("text") == title
                    and section["title"] == title and not section["writing_tasks"]
                    and any(item["kind"] == "table_ref" for item in section["fixed_content"])):
                candidates.append(section)
        placement = {"from_section_id": performance["section_id"],
                     "source_instruction": "table_before_appended_performance_proofs",
                     "status": "not_relocated", "candidate_section_ids": [s["section_id"] for s in candidates]}
        if len(candidates) == 1:
            target = candidates[0]
            ordered = list(performance["attachment_refs"])
            if target["attachment_refs"] or any(adopted[aid]["section_id"] != performance["section_id"] for aid in ordered):
                raise ValueError("PERFORMANCE_APPENDIX_OWNER_CONFLICT")
            for aid in ordered:
                adopted[aid]["section_id"] = target["section_id"]
                add_refs(target, adopted[aid]["requirement_refs"])
            target["attachment_refs"] = ordered
            performance["attachment_refs"] = []
            for row in assembly:
                if row["response_section_id"] == performance["section_id"]:
                    row["original_response_section_id"] = performance["section_id"]
                    row["response_section_id"] = target["section_id"]
            placement.update(status="relocated_after_prescribed_table", to_section_id=target["section_id"],
                attachment_ids=ordered, source_block_id=format_sections[target["section_id"]]["content_block_ids"][0],
                qualification_result="not_determined")
        appendix_placements.append(placement)

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
            "native_preamble_relocations": preamble_relocations,
            "proof_assembly": assembly, "section_order": [s["section_id"] for s in sections],
            "appendix_placements": appendix_placements,
            "lookup_issue_reclassifications": lookup_reclassifications,
            "source_sections_preserved": all(key in {s["section_id"] for s in sections} for key in before),
            "satisfaction_status": "not_determined"}
