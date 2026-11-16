"""Readable task list with one complete source appendix and a private index."""
from __future__ import annotations

import re
from collections import OrderedDict

from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt


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


def render_task_list(package):
    from .rendering import _base_document, _font, _page_furniture, _render_table, _render_section_content
    doc=_base_document(package,"待补与办理清单")
    _page_furniture(doc.sections[0],package.style,numbered=True,start=1)
    catalog=task_list_catalog(package)
    sources=catalog["sources"];sections={s.section_id:s for s in package.sections}
    index={"mode":"task_list","schema_version":"review-document-index-v1","issues":[],"actions":[],"review_sections":[],
        "source_quotes":catalog["entries"],"requirement_quote_ids":catalog["requirement_quote_ids"],
        "source_index":[source.model_dump(mode="json") for source in package.sources]}
    def paragraph(text="",*,bold=False,keep_next=False):
        p=doc.add_paragraph()
        p.alignment=WD_ALIGN_PARAGRAPH.LEFT
        pf=p.paragraph_format;pf.first_line_indent=Pt(0);pf.space_before=Pt(0);pf.space_after=Pt(3)
        pf.line_spacing=1.2;pf.keep_with_next=keep_next
        _font(p.add_run(text),name=package.style.font_family,size=12,bold=bold)
        return p
    def locations(refs):return list(dict.fromkeys(_location(sources[ref]) for ref in refs))
    def original(text):return catalog["quote_ids"][text]
    doc.add_paragraph("待补与办理清单","Title")
    paragraph("项目："+package.project.name)
    paragraph("按编号核对待补资料、待确认事实和办理事项。原文集中列于后面的原文附录；已附材料不代表资格或评分已通过。")
    if package.synthetic:paragraph("合成测试样本，仅用于系统验证。",bold=True)
    doc.add_paragraph("待补事项","Heading 1")
    if not package.issues:paragraph("本次未列出待补事项；办理和来源核对仍见下文。")
    groups=OrderedDict()
    for issue in package.issues:groups.setdefault(issue.section_id,[]).append(issue)
    for sid,issues in groups.items():
        section=sections[sid]
        doc.add_paragraph(section.title,"Heading 2")
        if section.document_role=="review":paragraph("位置：配套核对内容；不作为已完成的投标响应。")
        for issue in issues:
            refs=list(dict.fromkeys(ref for rid in issue.requirement_refs for ref in catalog["requirements"][rid].source_refs))
            locs=locations(refs);qid=original(issue.source_requirement)
            status="待核对" if issue.severity=="warning" else "待补"
            display_action,display_changes=_display_action(issue.action)
            display_reason,reason_changes=_display_reason(issue.reason)
            display_reason,label_changes,material_qids=_display_material_requirement_labels(display_reason,issue,catalog)
            display_changes += reason_changes + label_changes
            paragraph(f"{issue.issue_id} {issue.title}（{status}）",bold=True,keep_next=True)
            paragraph(f"原因：{display_reason}\n动作：{display_action}")
            paragraph(f"依据：{qid}；"+"；".join(locs))
            index["issues"].append({"display_id":issue.issue_id,"issue_id":issue.issue_id,"section_id":sid,
                "requirement_refs":issue.requirement_refs,"quote_ids":list(dict.fromkeys([qid,*material_qids])),"source_refs":refs,"locations":locs,"severity":issue.severity,"status":status,
                "original_issue":issue.model_dump(mode="json"),"display_reason":display_reason,"display_action":display_action,"display_transformations":display_changes})
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
            doc.add_paragraph(sec.title,"Heading 2");qids=[];retained=[]
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
    doc.add_page_break();doc.add_paragraph("原文附录","Heading 1")
    paragraph("每段原文仅集中保留一次；事项中的原文编号可在本文件中直接搜索。")
    for entry in catalog["entries"]:
        paragraph(entry["display_id"],bold=True,keep_next=True)
        paragraph("；".join(entry["locations"]),keep_next=True)
        paragraph(entry["text"])
    return doc,index
