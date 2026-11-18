"""Readable task list with one complete source appendix and a private index."""
from __future__ import annotations

import hashlib
import re
from collections import OrderedDict

from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt

from .storage import digest


_WRAPPERS = {
    "响应要求（招标原文）：", "响应表格要求（招标原文）：", "响应编写规则（招标原文，待核对）：",
    "关键条款全局约束（招标原文）：", "合同来源范围提示（招标原文）：", "合同条款原文（履约办理及来源待核对）：",
    "合同待约定原文（已读文本未给值）：", "合同来源范围及父条件（原文）：",
    "合同条款原文（履约及适用性待核对）：", "响应组成要求（招标原文）：", "关键条款全局规则（招标原文）：",
}

_ACTION_DISPLAY_REPLACEMENTS = (
    ("historical_proof_manual_review_wording_v1",
     "本次agent视觉查看不是用户核验；父文档页范围仍为未核实的登记关系。",
     "历史证明的本次适用性仍待核对；与人工标书的页码对应关系尚未核实。"),
)


def _display_action(action):
    """Translate only registered process wording; keep the source in the index."""
    changes=[]
    for rule,original,display in _ACTION_DISPLAY_REPLACEMENTS:
        count=action.count(original)
        if count:
            action=action.replace(original,display)
            changes.append({"field":"action","rule":rule,"original_text":original,
                            "display_text":display,"occurrences":count})
    return action,changes


_REASON_DISPLAY_REPLACEMENTS = (
    ("dictionary_stage_unsearched_wording_v1",
     "当前表达尚未完成可支持的材料检索，不能据此认定公司没有材料。",
     "词典阶段：当前表达未形成可用检索结果；后续目录补查见逐要求记录，不能据此认定公司没有材料。"),
)


def _display_reason(reason):
    """Relabel only the exact generated preamble; never edit source quotes."""
    changes = []
    for rule, original, display in _REASON_DISPLAY_REPLACEMENTS:
        if reason.startswith(original):
            reason = display + reason[len(original):]
            changes.append({"field": "reason", "rule": rule, "original_text": original,
                            "display_text": display, "occurrences": 1})
    return reason, changes


_NUMERIC_CONFLICT = "MATERIAL_OCR_NUMERIC_REFERENCE_MISMATCH"
_LINKED_OCR_SUCCESS = "所关联 OCR 状态保持执行成功（不代表字段准确）。"


def material_review_report(ctx):
    """Read the completed N3 unit used by this run, even after an N6-only fork."""
    n3 = next((node for node in ctx._state["nodes"] if node["key"] == "N3"), None)
    if n3 is None or n3["status"] != "succeeded":
        return None
    assembly = n3["units"].get("plan-assemble")
    if assembly is None or assembly["status"] != "succeeded":
        return None
    report = ctx.executor._checkpoint_data(assembly).get("report")
    if not report:
        return None
    run_id = assembly["output_ref"]["path"].split("/")[1]
    failed = {}
    for unit_id, unit in n3["units"].items():
        if not unit_id.startswith("material-ocr-validation-") or unit["status"] != "failed":
            continue
        attempt = unit["attempts"][-1]
        if attempt["status"] == "failed" and attempt.get("error_code") == _NUMERIC_CONFLICT:
            failed[unit_id] = {"run_id": run_id, "unit_id": unit_id, "attempt_id": attempt["attempt_id"],
                               "status": "failed", "error_code": attempt["error_code"]}
    return {"changes": report.get("material_recall_issue_changes", []),
            "proof_groups": report.get("material_matching", {}).get("proof_groups", []),
            "validation_failures": failed}


def _numeric_conflict_evidence(unit, failed, sources):
    from .material_source_review import MaterialSourceReviewFailure, validate_material_source_review

    disposition = unit["disposition"]
    if (disposition["error_code"] != _NUMERIC_CONFLICT or
            disposition.get("schema_version") != "material-ocr-disposition.v1" or
            disposition["status"] != "reading_failed_pending_review" or
            disposition.get("source_review_authenticated") is not True or
            disposition["machine_text_adopted"] is not False or
            disposition["qualification_result"] != "not_determined" or
            disposition.get("source_review") != unit["source_review"] or
            digest(disposition) != unit["disposition_sha256"]):
        return None
    try:
        review = validate_material_source_review(unit["source_review"])
    except MaterialSourceReviewFailure:
        return None
    identity, ocr = disposition["identity"], review["ocr"]
    identity_hash = digest(identity)
    current = failed.get("material-ocr-validation-" + identity_hash[:24])
    source = sources.get(unit["source_id"])
    if (current is None or current["run_id"] != disposition.get("validation_run_id") or
            current["status"] != "failed" or current["error_code"] != disposition["error_code"] or
            identity_hash != disposition["identity_sha256"] or
            any(identity.get(key) != value for key, value in review["identity"].items()) or
            any(identity.get(key) != unit[key] for key in ("source_id", "source_sha256", "target", "format")) or
            identity["image_sha256"] != unit["ocr_image_sha256"] or
            source is None or source.kind != "company_material" or source.sha256 != identity["source_sha256"] or
            not set(unit["group_ids"]) <= {link["group_id"] for link in identity["group_bindings"]}):
        return None
    provenance = disposition["provenance"]
    if (review["field"]["disposition"] != "machine_error" or ocr["status"] != "succeeded" or
            ocr["run_id"] != disposition["origin_run_id"] or ocr["unit_id"] != disposition["origin_unit_id"] or
            any(ocr[key] != provenance.get(origin) for key, origin in (
                ("run_id", "origin_run_id"), ("node_key", "origin_node_key"),
                ("unit_id", "origin_unit_id"), ("response_sha256", "raw_response_sha256"))) or
            ocr["response_sha256"] != provenance.get("raw_response_snapshot", {}).get("sha256")):
        return None
    return {"source_identity": {**review["identity"], "target": unit["target"], "format": unit["format"]},
            "source_review_sha256": review["record_sha256"], "disposition_sha256": unit["disposition_sha256"],
            "historical_ocr": ocr, "current_validation": {**current, "disposition": disposition["status"],
                                                           "machine_text_adopted": False}}


def _display_source_review_status(issue, change, groups, failed, sources):
    """Relabel one generated field note only where the same proof failed current validation."""
    reason = issue["reason"]
    if (not failed or change is None or change["after"] != issue or
            change["rule"] != "append_scoped_stage_context_preserve_missing_conditions_and_provenance"):
        return reason, []
    prefix = change["before"]["reason"] + "\n"
    if not reason.startswith(prefix):
        return reason, []

    contexts = change["requirement_contexts"]
    rids = [context["requirement_id"] for context in contexts]
    if (rids != change["requirement_refs"] or len(rids) != len(set(rids)) or
            not set(rids) <= set(issue["requirement_refs"])):
        return reason, []

    from .material_recall import _ocr_failure_notes

    lines = reason[len(prefix):].splitlines(keepends=True)
    offsets = [len(prefix)]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    headers = [index for index, line in enumerate(lines) if line.startswith("【") and "】词典阶段：" in line]
    changes = []
    for context in contexts:
        rid = context["requirement_id"]
        short = " ".join(context["original_requirement"].split())
        short = short[:36] + ("…" if len(short) > 36 else "")
        label = rid + ("｜" + short if short else "")
        matches = [index for index in headers if lines[index].startswith(f"【{label}】词典阶段：")]
        if len(matches) != 1:
            continue
        start = matches[0] + 1
        end = next((index for index in headers if index >= start), len(lines))
        for unit in context["ocr_failure_review"]["units"]:
            if unit["source_review"] is None or not unit["group_ids"] or not set(unit["group_ids"]) <= groups.keys():
                continue
            evidence = _numeric_conflict_evidence(unit, failed, sources)
            if evidence is None:
                continue
            earlier, review_line = _ocr_failure_notes({"failed_unit_count": 0, "units": [unit]}, groups)
            matching_pairs = [index for index in range(start, end - 1)
                              if lines[index].rstrip("\r\n") == earlier
                              and lines[index + 1].rstrip("\r\n") == review_line]
            if len(matching_pairs) != 1 or review_line.count(_LINKED_OCR_SUCCESS) != 1:
                continue
            left = offsets[matching_pairs[0] + 1] + review_line.index(_LINKED_OCR_SUCCESS)
            right = left + len(_LINKED_OCR_SUCCESS)
            display = ("历史 OCR 执行成功（当时返回转录）。该页当前原图字段校验失败"
                       f"（{evidence['current_validation']['error_code']}），失败转录未采用，仍待核对。")
            changes.append({"field": "reason", "rule": "material_ocr_historical_and_current_status_v1",
                            "original_text": _LINKED_OCR_SUCCESS, "display_text": display, "occurrences": 1,
                            "original_start": left, "original_end": right, "requirement_id": rid,
                            "group_ids": unit["group_ids"], **evidence})
    changes.sort(key=lambda item: item["original_start"])
    if len({item["original_start"] for item in changes}) != len(changes):
        return reason, []
    display_reason = reason
    for item in reversed(changes):
        display_reason = display_reason[:item["original_start"]] + item["display_text"] + display_reason[item["original_end"]:]
    return display_reason, changes


def _display_material_requirement_labels(reason, issue, catalog):
    """Replace exact generated line headers with existing source appendix IDs."""
    lines = reason.splitlines(keepends=True)
    changes, quote_ids = [], []
    for rid in dict.fromkeys(issue.requirement_refs):
        requirement = catalog["requirements"][rid]
        short = " ".join(requirement.text.split())
        short = short[:36] + ("…" if len(short) > 36 else "")
        original = f"【{rid}｜{short}】词典阶段："
        qid = catalog["requirement_quote_ids"][rid]
        display = f"【{qid}｜{short}】词典阶段："
        count = 0
        for number, line in enumerate(lines):
            if line.startswith(original):
                lines[number] = display + line[len(original):]
                count += 1
        if count:
            quote_ids.append(qid)
            changes.append({"field": "reason", "rule": "material_requirement_header_source_id_v1",
                            "original_text": original, "display_text": display, "occurrences": count,
                            "requirement_id": rid, "quote_id": qid,
                            "source_refs": list(requirement.source_refs)})
    return "".join(lines), changes, list(dict.fromkeys(quote_ids))


def _body_quote(text):
    first, separator, rest = text.partition("\n")
    return rest if separator and rest and first in _WRAPPERS else text


def _excerpt_heading(title, requirement_refs, catalog):
    """Mark a demonstrably truncated source prefix, without completing it."""
    short = " ".join(title.split())
    if short and not short.endswith("…") and any(
        " ".join(catalog["requirements"][rid].text.split()).startswith(short)
        and len(" ".join(catalog["requirements"][rid].text.split())) > len(short)
        for rid in requirement_refs
    ):
        return title + "…（摘要，完整要求见原文附录）"
    return title


def _note_references(note_ids):
    """Compact only consecutive known IDs, preserving order and exact members."""
    numbers = [int(note_id.removeprefix("核对说明")) for note_id in dict.fromkeys(note_ids)]
    spans = []
    for number in numbers:
        if spans and number == spans[-1][-1] + 1:
            spans[-1].append(number)
        else:
            spans.append([number])
    return "、".join(f"核对说明{span[0]:03d}" + (f"～{span[-1]:03d}" if len(span)>1 else "")
                    for span in spans)


def shared_reason_catalog(rows, catalog):
    """Deduplicate exact complete generated lines; keep each occurrence in place.

    This is a reversible display projection, not a new material conclusion.
    Candidate notes must follow an authenticated, exact requirement header.
    Similar notes, embedded quotations and unrecognized scopes stay inline.
    """
    candidates = OrderedDict()
    prepared = {}
    for row in rows:
        issue = row["original_issue"]
        headers = [change for change in row["display_transformations"]
                   if change["rule"] == "material_requirement_header_source_id_v1"]
        original_lines = issue["reason"].splitlines()
        parts = []
        active = []
        offset = 0
        for line_number, raw_line in enumerate(row["display_reason"].splitlines(keepends=True)):
            text = raw_line.rstrip("\r\n")
            ending = raw_line[len(text):]
            if text.startswith("【") and "】词典阶段：" in text:
                active = []
            # Different original RIDs can intentionally share one readable
            # quote ID and heading. Bind this occurrence to its original line,
            # never to the set of IDs with the same displayed heading.
            original_line = original_lines[line_number] if line_number < len(original_lines) else ""
            matches = [change["requirement_id"] for change in headers
                       if original_line.startswith(change["original_text"])
                       and text.startswith(change["display_text"])]
            if matches:
                active = list(dict.fromkeys(matches)) if len(set(matches)) == 1 else []
            scope = None
            refs = list(active)
            if text.startswith("本条候选证明") and active:
                scope = "requirement_candidates"
            elif (active and text.startswith("候选证明“") and
                  text.endswith("；读取失败状态保留，需核对原图或补齐可读证明。") and
                  re.search(r"（MATERIAL_OCR_[A-Z0-9_]+）",text)):
                scope = "requirement_candidates"
            elif text.startswith("原件读取："):
                scope = "selected_material_reading_summary"
                refs = list(issue["requirement_refs"])
                active = []
            elif text.startswith("目录补查仅使用名称"):
                scope = "catalog_scope_limit"
                refs = list(issue["requirement_refs"])
                active = []
            source_refs = list(dict.fromkeys(
                source for rid in refs for source in catalog["requirements"][rid].source_refs))
            part = {"kind": "inline", "text": text, "line_ending": ending,
                    "start": offset, "end": offset + len(text)}
            if scope:
                part.update(scope=scope, requirement_refs=refs, source_refs=source_refs)
                candidates.setdefault((scope, text), []).append(
                    {"issue_id": row["issue_id"], "start": offset, "end": offset + len(text),
                     "requirement_refs": refs, "source_refs": source_refs})
            parts.append(part)
            offset += len(raw_line)
        assert offset == len(row["display_reason"])
        prepared[row["issue_id"]] = parts
    notes = []
    ids = {}
    for (scope, text), occurrences in candidates.items():
        if len({item["issue_id"] for item in occurrences}) < 2:
            continue
        note_id = f"核对说明{len(notes)+1:03d}"
        ids[(scope, text)] = note_id
        notes.append({"display_id": note_id, "scope": scope, "text": text,
                      "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                      "occurrences": occurrences})
    for row in rows:
        parts = prepared[row["issue_id"]]
        for part in parts:
            key = (part.get("scope"), part["text"])
            if key in ids:
                part["kind"] = "shared_note"
                part["note_id"] = ids[key]
        # Preserve exact text and offsets independently of the shortened prose.
        assert "".join(p["text"] + p["line_ending"] for p in parts) == row["display_reason"]
        display = []
        for part in parts:
            if part["kind"] == "shared_note":
                same_scope = (display and display[-1]["kind"] == "shared_notes"
                              and display[-1]["scope"] == part["scope"]
                              and display[-1]["requirement_refs"] == part["requirement_refs"])
                if not same_scope:
                    display.append({"kind": "shared_notes", "scope": part["scope"],
                                    "requirement_refs": part["requirement_refs"], "note_ids": [], "parts": []})
                display[-1]["note_ids"].append(part["note_id"])
                display[-1]["parts"].append(part)
            else:
                display.append({"kind": "inline", "text": part["text"], "parts": [part]})
        for segment in display:
            if segment["kind"] != "shared_notes":
                continue
            label = {"requirement_candidates": "本条证明读取仍待核对",
                     "selected_material_reading_summary": "整体读取范围与局限",
                     "catalog_scope_limit": "目录补查适用范围"}[segment["scope"]]
            segment["text"] = label + "：见" + _note_references(segment["note_ids"]) + "。"
        row["reason_segments"] = parts
        row["rendered_reason_segments"] = display
        row["rendered_reason"] = "\n".join(segment["text"] for segment in display)
        row["shared_note_ids"] = list(dict.fromkeys(p["note_id"] for p in parts if p["kind"] == "shared_note"))
    return notes


def _location(source):
    """Only reader-facing page/paragraph/table positions, never artifact paths."""
    locator=source.locator
    title=source.title or {"tender":"招标文件","company_material":"公司材料","reference_bid":"人工参考"}.get(source.kind,"原件")
    if re.search(r"[a-f0-9]{20,}|\.local[/\\]|[A-Za-z]:[/\\]",title,re.IGNORECASE):
        title={"tender":"招标文件","company_material":"公司材料","reference_bid":"人工参考"}.get(source.kind,"原件")
    pages=list(dict.fromkeys(int(x) for x in re.findall(r"pdf/page\[(\d+)\]",locator)))
    if pages:
        position="第"+"至".join(str(x) for x in pages)+"页"
        table=re.match(r"pdf/page\[\d+\]/table\[(\d+)\]",locator)
        if table:position+=f"，表{int(table[1])+1}"
        else:
            line=re.match(r"pdf/page\[\d+\]/line\[(\d+)\]",locator)
            if line:position+=f"，原文行{int(line[1])+1}"
        cells=re.search(r"(?:^|;)\s*cells=(.*?)(?:;|$)",locator)
        if cells:
            rows=list(dict.fromkeys(int(x)+1 for x in re.findall(r"(?:^|[:,])r(\d+)c",cells[1])))
            if rows:position+="，第"+"、".join(map(str,rows))+"行"
        return title+" "+position
    paragraph=re.search(r"paragraph\[(\d+)\]",locator)
    if paragraph:return f"{title} 第{int(paragraph[1])+1}段"
    table=re.search(r"table\[(\d+)\]",locator)
    if table:return f"{title} 表{int(table[1])+1}"
    return title


def task_list_catalog(package):
    sources={s.source_id:s for s in package.sources}
    requirements={r.requirement_id:r for r in package.requirements}
    quotes=OrderedDict()
    def add(text,refs):
        if text.strip():
            row=quotes.setdefault(text,{"source_refs":[]})
            row["source_refs"]=list(dict.fromkeys([*row["source_refs"],*refs]))
    def req_sources(refs):
        return list(dict.fromkeys(s for rid in refs for s in requirements[rid].source_refs))
    for r in package.requirements:add(r.text,r.source_refs)
    for issue in package.issues:add(issue.source_requirement,req_sources(issue.requirement_refs))
    for action in package.review_actions:
        add(action.source_requirement,action.source_refs)
        for context in action.source_scope_contexts:add(context.text,[context.source_ref])
    for section in package.sections:
        if section.document_role != "review" or not section.render_body:continue
        for block in section.blocks:
            if block.kind == "paragraph":add(_body_quote(block.text),block.source_refs or req_sources(block.requirement_refs))
            elif block.kind == "list":
                for text in block.items:add(text,block.source_refs or req_sources(block.requirement_refs))
    # Containment is useful only within the same precise source scope. A short
    # repeated sentence in a different clause must keep its own original entry.
    parents=[];owner={}
    def scopes(text):
        return {ref for ref in quotes[text]["source_refs"] if len(re.findall(
            r"(?:^|;\s*)quote_sha256=[0-9a-f]{64}(?=;|$)",sources[ref].locator)) == 1}
    for text in sorted(quotes,key=lambda t:-len(t)):
        parent=next((p for p in parents if p.count(text)==1 and p.find(text)==p.rfind(text) and scopes(text)&scopes(p)),None)
        if parent is None:parents.append(text);parent=text
        owner[text]=parent
    order={text:i for i,text in enumerate(quotes)}
    parents.sort(key=lambda p:min(order[text] for text in quotes if owner[text]==p))
    ids={text:f"原文{i:03d}" for i,text in enumerate(parents,1)}
    entries=[]
    for parent in parents:
        children=[text for text in quotes if owner[text]==parent]
        refs=list(dict.fromkeys(ref for text in children for ref in quotes[text]["source_refs"]))
        entries.append({"display_id":ids[parent],"text":parent,"source_refs":refs,
            "locations":list(dict.fromkeys(_location(sources[ref]) for ref in refs)),
            "contained_quotes":[{"text":text,"start":parent.index(text),"end":parent.index(text)+len(text),"source_refs":quotes[text]["source_refs"]} for text in children]})
    return {"entries":entries,"quote_ids":{text:ids[owner[text]] for text in quotes},"requirements":requirements,
            "sources":sources,"requirement_quote_ids":{r.requirement_id:ids[owner[r.text]] for r in package.requirements}}


def render_task_list(package, *, material_review_report=None, glyph_maps=None):
    from .rendering import _base_document, _font, _indent, _page_furniture, _render_table, _render_section_content, RenderingError
    from .review_source_context import material_stage_display
    from .source_format_rendering import recover_review_source_tables, render_review_source_table
    doc=_base_document(package,"待补与办理清单")
    _page_furniture(doc.sections[0],package.style,numbered=True,start=1)
    catalog=task_list_catalog(package)
    sources=catalog["sources"];sections={s.section_id:s for s in package.sections}
    index={"mode":"task_list","schema_version":"review-document-index-v1","issues":[],"actions":[],"review_sections":[],
        "source_quotes":catalog["entries"],"requirement_quote_ids":catalog["requirement_quote_ids"],
        "source_index":[source.model_dump(mode="json") for source in package.sources]}
    report = material_review_report or {}
    review_changes = report.get("changes", [])
    material_changes = {change["issue_id"]: change for change in review_changes}
    material_groups = {group["group_id"]: group for group in report.get("proof_groups", [])}
    failed_validations = report.get("validation_failures", {})
    for issue in package.issues:
        refs=list(dict.fromkeys(ref for rid in issue.requirement_refs for ref in catalog["requirements"][rid].source_refs))
        locs=list(dict.fromkeys(_location(sources[ref]) for ref in refs))
        qid=catalog["quote_ids"][issue.source_requirement]
        status="待核对" if issue.severity=="warning" else "待补"
        original_issue = issue.model_dump(mode="json")
        display_action,display_changes=_display_action(issue.action)
        display_reason,source_changes=_display_source_review_status(
            original_issue, material_changes.get(issue.issue_id), material_groups, failed_validations, sources)
        display_reason,reason_changes=_display_reason(display_reason)
        display_reason,label_changes,material_qids=_display_material_requirement_labels(display_reason,issue,catalog)
        display_reason,stage_changes,stage_qids=material_stage_display(display_reason,issue,catalog,review_changes)
        index["issues"].append({"display_id":issue.issue_id,"issue_id":issue.issue_id,"section_id":issue.section_id,
            "requirement_refs":issue.requirement_refs,"quote_ids":list(dict.fromkeys([qid,*material_qids,*stage_qids])),
            "source_refs":refs,"locations":locs,"severity":issue.severity,"status":status,
            "original_issue":original_issue,"display_reason":display_reason,"display_action":display_action,
            "display_transformations":display_changes+reason_changes+label_changes+stage_changes+source_changes})
    index["shared_reason_notes"]=shared_reason_catalog(index["issues"],catalog)
    issue_rows={row["issue_id"]:row for row in index["issues"]}
    index["section_heading_display"]=[]
    index["source_quote_layout"]=[]
    def paragraph(text="",*,bold=False,keep_next=False):
        p=doc.add_paragraph()
        p.alignment=WD_ALIGN_PARAGRAPH.LEFT
        pf=p.paragraph_format;pf.space_before=pf.space_after=Pt(0)
        pf.line_spacing=package.style.body_line_spacing;pf.keep_with_next=keep_next
        pf.widow_control=True;pf.keep_together=len(text)<=240
        _indent(p._p,0 if bold else package.style.body_first_line_chars,package.style.body_pt)
        _font(p.add_run(text),name=package.style.font_family,size=package.style.body_pt,bold=bold)
        return p
    def locations(refs):return list(dict.fromkeys(_location(sources[ref]) for ref in refs))
    def original(text):return catalog["quote_ids"][text]
    doc.add_paragraph("待补与办理清单","Title")
    paragraph("项目："+package.project.name)
    paragraph("按编号核对待补资料、待确认事实和办理事项。原文集中列于后面的原文附录；已附材料不代表资格或评分已通过。")
    if index["shared_reason_notes"]:
        paragraph("重复的读取说明集中列在“核对说明”中，事项中的说明编号保留其原对应要求；请结合所在要求核对，不据此认定事实或资格。")
    if package.synthetic:paragraph("合成测试样本，仅用于系统验证。",bold=True)
    doc.add_paragraph("待补事项","Heading 1")
    if not package.issues:paragraph("本次未列出待补事项；办理和来源核对仍见下文。")
    groups=OrderedDict()
    for issue in package.issues:groups.setdefault(issue.section_id,[]).append(issue)
    for sid,issues in groups.items():
        section=sections[sid]
        display_title=_excerpt_heading(section.title,section.requirement_refs,catalog)
        doc.add_paragraph(display_title,"Heading 2")
        index["section_heading_display"].append({"section_id":sid,"original_title":section.title,"display_title":display_title})
        if section.document_role=="review":paragraph("位置：配套核对内容；不作为已完成的投标响应。")
        for issue in issues:
            row=issue_rows[issue.issue_id]
            paragraph(f"{issue.issue_id} {issue.title}（{row['status']}）",bold=True,keep_next=True)
            for number,segment in enumerate(row["rendered_reason_segments"]):
                paragraph(("原因：" if number==0 else "")+segment["text"])
            paragraph("动作："+row["display_action"])
            paragraph(f"依据：{original(issue.source_requirement)}；"+"；".join(row["locations"]))
    doc.add_paragraph("办理与适用条件核对","Heading 1")
    paragraph("以下事项保留原文规定，尚未据此认定已办理、适用或满足。")
    for number,action in enumerate(package.review_actions,1):
        display=f"办理{number:03d}";kind="商业约束待核对" if action.kind=="commercial_constraint" else "程序事项待办理"
        status="尚未核验" if action.status=="not_checked" else "待线下办理"
        qids=list(dict.fromkeys([original(action.source_requirement),*(original(c.text) for c in action.source_scope_contexts)]))
        refs=list(dict.fromkeys([*action.source_refs,*(c.source_ref for c in action.source_scope_contexts)]));locs=locations(refs)
        section_ids=[sid for sid in sections if set(action.requirement_refs)&set(sections[sid].requirement_refs)]
        positions=list(dict.fromkeys(sections[sid].title for sid in section_ids
            if sections[sid].document_role=="response" and sections[sid].render_body)) or ["配套办理事项"]
        paragraph(f"{display} {kind}（{status}）",bold=True,keep_next=True)
        paragraph("动作："+action.instruction)
        if action.source_scope_status!="not_applicable":paragraph("适用范围尚待确认，不据此判定正文、报价或资格。")
        paragraph("对应章节："+"；".join(positions))
        paragraph("依据："+"、".join(qids)+"；"+"；".join(locs))
        index["actions"].append({"display_id":display,"action_id":action.action_id,"requirement_refs":action.requirement_refs,
            "quote_ids":qids,"source_refs":refs,"locations":locs,"section_titles":positions,"section_ids":section_ids})
    review=[s for s in package.sections if s.document_role=="review" and s.render_body]
    if review:
        doc.add_paragraph("其他核对内容","Heading 1")
        paragraph("以下来源原文集中见附录，保留相关核对内容；不作为已完成的投标响应。")
        issues={i.issue_id:i for i in package.issues};seen=set()
        for sec in review:
            display_title=_excerpt_heading(sec.title,sec.requirement_refs,catalog)
            doc.add_paragraph(display_title,"Heading 2");qids=[];retained=[]
            index["section_heading_display"].append({"section_id":sec.section_id,"original_title":sec.title,"display_title":display_title})
            for block in sec.blocks:
                if block.kind=="paragraph":qids.append(original(_body_quote(block.text)))
                elif block.kind=="list":qids.extend(original(text) for text in block.items)
                elif block.kind=="issue":retained.append(block.issue_id)
                elif block.kind=="heading":paragraph(block.text,bold=True)
                elif block.kind=="table":_render_table(doc,block.table,issues,package.style)
                elif block.kind=="attachment":
                    _render_section_content(doc,package,sec.model_copy(update={"blocks":[block],"native_format":None}),seen_attachment_groups=seen)
            qids=list(dict.fromkeys(qids))
            if qids:paragraph("核对原文："+"、".join(qids))
            if retained:paragraph("相关待补："+"、".join(retained))
            index["review_sections"].append({"section_id":sec.section_id,"title":sec.title,"requirement_refs":sec.requirement_refs,"quote_ids":qids,"issue_ids":retained,
                "table_ids":[b.table.table_id for b in sec.blocks if b.kind=="table"],
                "attachment_ids":[b.attachment_id for b in sec.blocks if b.kind=="attachment"]})
    if index["shared_reason_notes"]:
        doc.add_page_break();doc.add_paragraph("核对说明","Heading 1")
        paragraph("以下完整说明按文字完全相同集中保留。每次引用的位置及对应要求不变；相同说明不表示不同要求或证明已通过核验。")
        for note in index["shared_reason_notes"]:
            paragraph(note["display_id"],bold=True,keep_next=True)
            paragraph(note["text"])
    doc.add_page_break();doc.add_paragraph("原文附录","Heading 1")
    paragraph("每段原文仅集中保留一次；事项中的原文编号可在本文件中直接搜索。")
    table_cache={}
    table_options = {} if glyph_maps is None else {"glyph_maps": glyph_maps}
    for entry in catalog["entries"]:
        paragraph(entry["display_id"],bold=True,keep_next=True)
        paragraph("；".join(entry["locations"]),keep_next=True)
        layout=recover_review_source_tables(entry["text"],entry["source_refs"],sources,table_cache,**table_options)
        if layout["status"]=="failed":
            raise RenderingError("REVIEW_SOURCE_TABLE_SOURCE_VALIDATION_FAILED")
        index["source_quote_layout"].append({"display_id":entry["display_id"],**layout})
        if layout["status"]=="unsupported":
            paragraph("表格布局待核：本段保留已读原文，尚未可靠恢复列及合并关系；金额、分值或条件对应须回原件核对。",bold=True)
        for fragment in layout["fragments"]:
            if fragment["kind"]=="table":
                render_review_source_table(doc,fragment,package.style)
            elif fragment["text"].strip():
                if fragment.get("layout_status")=="unsupported":
                    paragraph("以下片段的表格布局待核：原文完整保留，列及合并关系尚未可靠恢复，请回原件核对。",bold=True)
                paragraph(fragment["text"])
    return doc,index
