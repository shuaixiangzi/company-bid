"""N3 source-page scopes and actual section ownership for editable formats."""
from __future__ import annotations

import copy
import re

from .native_layout import NATIVE_FORMAT_LAYOUT_VERSION, NativeLayoutError, read_native_format_layout
from .source_format_contracts import NativeFormatLayoutV2, format_digest
from .source_format import _sha, _font, _centered, _paragraph, _can_join, _fill_line, _line_underlines, _cell_insets, _cell_underlines


VERSION = "source-response-format-v2"


def _quote_ref(ref, block, source_map):
    source = source_map.get(ref)
    parent = source_map.get(block["source_id"])
    return bool(source and parent and source["source_id"] != block["source_id"] and
        source["sha256"] == parent["sha256"] and source["path"] == parent["path"] and
        source["locator"].split(";",1)[0] == block["locator"] and
        f"quote_sha256={_sha(block['text'])}" in source["locator"].split("; "))


def _fixed_blocks(section, allowed, blocks, source_map, tables):
    result=[]
    for item in section["fixed_content"]:
        refs=item["source_refs"]
        if item["kind"] == "table_ref":
            table=next((t for t in tables if t["table_id"]==item["table_ref"]),None)
            if table is None or table["section_id"] != section["section_id"]:
                raise ValueError("NATIVE_SCOPE_TABLE_OWNER_INVALID")
            refs=table["source_refs"]
        matches=[blocks[key] for key in allowed if
            blocks[key]["kind"] == ("paragraph" if item["kind"]=="paragraph" else "table") and
            any(_quote_ref(ref,blocks[key],source_map) for ref in refs)]
        if len(matches) != 1:
            raise ValueError("NATIVE_SCOPE_FIXED_SOURCE_NOT_UNIQUE")
        result.append(matches[0])
    return result


def _member(block, role):
    return {"block_id":block["block_id"],"locator":block["locator"],"ordinal":block["ordinal"],
        "kind":block["kind"],"physical_page":block["page"],"text_sha256":_sha(block["text"]),
        "bbox":block["bbox"],"role":role}


def _bounds(items):
    return [min(x["bbox"][0] for x in items),min(x["bbox"][1] for x in items),
            max(x["bbox"][2] for x in items),max(x["bbox"][3] for x in items)]


def recover_one(ctx, section, chapter, blocks, source, tables, facts, manifest, scoped_source):
    selected=[blocks[key] for key in chapter["content_block_ids"]]
    if not selected or selected[0]["kind"] != "paragraph" or selected[0]["text"] != section["title"]:
        return None,"unsupported_heading_scope"
    pages=sorted({b.get("page") for b in selected if b.get("page") is not None})
    if not pages or any(b.get("page") is None for b in selected):
        return None,"unsupported_missing_physical_page"
    if pages != list(range(pages[0],pages[-1]+1)):
        raise ValueError("NATIVE_SCOPE_PAGE_GAP")
    if selected != sorted(selected,key=lambda b:b["ordinal"]):
        raise ValueError("NATIVE_SCOPE_SOURCE_ORDER_CHANGED")
    if any(b.get("origin") != "native" or b.get("read_disposition") or b["source_id"] != source["source_id"] or
           b["kind"] not in {"paragraph","table"} for b in selected):
        return None,"unsupported_non_native_or_omitted_block"
    full=sorted([b for b in blocks.values() if b["source_id"] == source["source_id"] and b.get("page") in pages],key=lambda b:b["ordinal"])
    unit_input={"source_id":source["source_id"],"source_sha256":source["sha256"],"physical_pages":pages,
        "frozen_blocks":selected,"frozen_page_blocks":full,"version":VERSION,
        "native_reader_version":NATIVE_FORMAT_LAYOUT_VERSION}
    def read():
        value=read_native_format_layout(source["path"],source["sha256"],pages,heading_locator=selected[0]["locator"])
        ctx.capture("n3-native-source-layout",value)
        return value
    native=ctx.run_unit("native-format-"+format_digest(unit_input)[:20],unit_input,read)
    page_map={p["page_number"]:p for p in native["pages"]}
    measured={b["locator"]:b for p in native["pages"] for b in [*p["lines"],*p["tables"]]}
    if set(measured) != {b["locator"] for b in full}:
        raise ValueError("NATIVE_SCOPE_WHOLE_PAGE_READING_MISMATCH")
    for block in full:
        item=measured.get(block["locator"])
        if (item is None or block["text"] != item["text"] or len(block["bbox"]) != 4 or
                any(abs(a-b)>.01 for a,b in zip(block["bbox"],item["bbox"]))):
            raise ValueError("NATIVE_FORMAT_FROZEN_BLOCK_MISMATCH")
        if block["kind"] == "table" and (block["rows"] != item["rows"] or block.get("merges",[]) != item["merges"] or
                block.get("column_widths",[]) != item["column_widths_pt"]):
            raise ValueError("NATIVE_FORMAT_FROZEN_GRID_MISMATCH")
    selected_ids={b["block_id"] for b in selected}
    page_scopes=[]
    fixed=copy.deepcopy(section["fixed_content"])
    if len(selected)-1 != len(fixed):
        return None,"unsupported_transformed_or_instruction_only_body"
    for number in pages:
        page=page_map[number]
        originals=[b for b in full if b["page"] == number]
        members=[_member(b,"selected" if b["block_id"] in selected_ids else
            "page_furniture" if measured[b["locator"]].get("page_furniture_candidate") else "excluded") for b in originals]
        visible=[m for m in members if m["role"] != "page_furniture"]
        chosen=[i for i,m in enumerate(visible) if m["role"] == "selected"]
        if not chosen or chosen != list(range(chosen[0],chosen[-1]+1)):
            raise ValueError("NATIVE_SCOPE_SELECTED_BLOCKS_NOT_CONTIGUOUS")
        partial=any(m["role"] == "excluded" for m in visible)
        if len(pages)>1 and partial:
            return None,"unsupported_partial_cross_page_scope"
        page_scopes.append({"physical_page":number,"page_width_pt":page["width_pt"],"page_height_pt":page["height_pt"],
            "scope_kind":"partial_page" if partial else "whole_page","scope_bbox":_bounds([b for b in selected if b["page"]==number]),
            "members":members,"fixed_indices":[i for i,b in enumerate(selected[1:]) if b["page"]==number],
            "native_page_sha256":format_digest(page),"frozen_page_sha256":format_digest(originals)})
    elements,bindings,pending=[],[],[]
    def flush():
        if pending:
            number=pending[0][1]["page"];page=page_map[number]
            element=_paragraph([x[0] for x in pending],[x[1]["text"] for x in pending],
                [x[1]["block_id"] for x in pending],[measured[x[1]["locator"]] for x in pending],
                fixed,page["horizontal_rules"],page["width_pt"])
            elements.append({**element,"physical_page":number});pending.clear()
    for index,(block,item) in enumerate(zip(selected[1:],fixed)):
        page=page_map[block["page"]]
        ref=scoped_source({"source_id":source["source_id"],"block_id":block["block_id"],"block_ids":[block["block_id"]],
            "locator":block["locator"],"quote":block["text"]})
        item["source_refs"]=list(dict.fromkeys([*item["source_refs"],ref]))
        if block["kind"] == "paragraph":
            if item["kind"] != "paragraph" or item["text"] != block["text"]:
                return None,"unsupported_transformed_fixed_paragraph"
            line=measured[block["locator"]]
            if pending and (pending[-1][1]["page"] != block["page"] or not _can_join(measured[pending[-1][1]["locator"]],line)):
                flush()
            bindings.extend(_fill_line(item,block["text"],block["block_id"],index,facts,manifest,
                source_rules=_line_underlines(line,page["horizontal_rules"],index)))
            pending.append((index,block))
        else:
            flush()
            table=next((t for t in tables if t["table_id"]==item.get("table_ref")),None)
            if item["kind"] != "table_ref" or table is None or table["section_id"] != section["section_id"]:
                return None,"unsupported_table_owner"
            table["source_refs"]=list(dict.fromkeys([*table["source_refs"],ref]))
            raw=measured[block["locator"]]
            insets=[c["alignment_evidence"]["left_gap_pt"] for c in raw["cells"] if c["runs"] and
                    1<=c["alignment_evidence"]["left_gap_pt"]<=10]
            cells=[]
            for cell in raw["cells"]:
                font=_font(cell["runs"] or next((c["runs"] for c in raw["cells"] if c["runs"]),[]))
                cells.append({"row":cell["row"],"column":cell["column"],**font,
                    "alignment":"center" if cell["alignment_evidence"]["horizontal"]=="center" else "left",
                    "vertical_alignment":cell["alignment_evidence"]["vertical"] if cell["runs"] else "center",
                    **_cell_insets(cell,min(insets,default=0.)),"underlines":_cell_underlines(cell,page["horizontal_rules"])})
            elements.append({"kind":"table","physical_page":block["page"],"fixed_index":index,
                "source_block_id":block["block_id"],"source_locator":block["locator"],"table_id":table["table_id"],
                "bbox":raw["bbox"],"column_widths_pt":raw["column_widths_pt"],"row_heights_pt":raw["row_heights_pt"],"cells":cells,
                "source_rows":block["rows"],"source_merges":block.get("merges",[]),
                "source_grid_sha256":format_digest(block["rows"]),"output_grid_sha256":format_digest(table["rows"])})
    flush()
    heading=selected[0];head=measured[heading["locator"]];page=page_map[heading["page"]]
    result={"schema_version":VERSION,"source_id":source["source_id"],"source_sha256":source["sha256"],
        "physical_page":heading["page"],"page_width_pt":page["width_pt"],"page_height_pt":page["height_pt"],
        "heading_text":heading["text"],"heading_bbox":heading["bbox"],
        "heading_alignment":"center" if _centered(head) else "right" if head["bbox"][0]>page["width_pt"]/2 else "left",
        "heading_source_ref":scoped_source({"source_id":source["source_id"],"block_id":heading["block_id"],
            "block_ids":[heading["block_id"]],"locator":heading["locator"],"quote":heading["text"]}),
        "heading_source_block_id":heading["block_id"],"heading_source_locator":heading["locator"],
        **{"heading_"+k:v for k,v in _font(head["runs"]).items()},"elements":elements,"bindings":bindings,
        "native_read_sha256":format_digest(native),"frozen_blocks_sha256":format_digest(selected),"page_scopes":page_scopes,
        "form_role":"prescribed_cover" if chapter.get("source_role")=="prescribed_cover" else "response"}
    result["proof_sha256"]=format_digest(result)
    spec=NativeFormatLayoutV2.model_validate(result)
    section["fixed_content"],section["native_format"]=fixed,spec.model_dump(mode="json")
    return spec,None


def recover_all(ctx, sections, formats, blocks, source_map, tables, facts, manifest, scoped_source):
    from .extraction import _id, PlanningError
    results=[];delegations=[]
    allowed={key for f in formats for key in f["content_block_ids"]}
    own={};heads={};chapters={};claimed={}
    for chapter in formats:
        sid=_id("SEC-",chapter["format_id"]);section=next((s for s in sections if s["section_id"]==sid),None)
        row={"format_id":chapter["format_id"],"section_id":sid,"title":chapter["title"],"status":"not_recovered","satisfaction_status":"not_determined"}
        results.append(row)
        if section is None:
            row["reason"]="planned_section_missing";continue
        source=source_map.get(chapter["citation"]["source_id"])
        if source is None:
            row["reason"]="source_missing";continue
        if not section["fixed_content"] and chapter.get("source_role") != "prescribed_cover":
            body=[blocks[key] for key in chapter["content_block_ids"]][1:]
            if body and all(b["kind"]=="paragraph" and re.fullmatch(r"[（(](?:内容)?格式自拟(?:[，,][^（）()\n]+)?[）)]",b["text"].strip()) for b in body):
                row.update(status="self_drafted_no_prescribed_body",reason="source_explicitly_delegates_body_drafting",body_review_status="separate_required",
                    source_instruction_blocks=[{"block_id":b["block_id"],"locator":b["locator"],"text_sha256":_sha(b["text"])} for b in body])
                continue
        heading=blocks[chapter["citation"]["block_id"]]
        heads[sid]=heading;chapters[sid]=chapter
        try:fixed=_fixed_blocks(section,allowed,blocks,source_map,tables)
        except ValueError as exc:raise PlanningError(str(exc)) from exc
        own[sid]=[heading,*fixed]
    # Source structural headings already delegated by response organization
    # retain their own owner and may have a fixed preamble following the title.
    main_ids=set(own)
    format_head_ids={f["citation"]["block_id"] for f in formats}
    for key in sorted(allowed,key=lambda k:blocks[k]["ordinal"]):
        block=blocks[key]
        if block["kind"] != "paragraph" or key in format_head_ids:continue
        # Planning can pass sections before SectionPlan supplies its defaults.
        candidates = [s for s in sections
                      if s["section_id"] not in main_ids and s["title"] == block["text"]
                      and s.get("document_role", "response") == "response"
                      and s.get("render_body", True)
                      and any(_quote_ref(ref, block, source_map) for ref in s["source_refs"])]
        if len(candidates)>1:raise PlanningError("NATIVE_SCOPE_HEADING_OWNER_AMBIGUOUS")
        if len(candidates)==1:
            section=candidates[0];sid=section["section_id"]
            own[sid]=[block,*_fixed_blocks(section,allowed,blocks,source_map,tables)]
            heads[sid]=block
            chapters[sid]={"title":block["text"],"content_block_ids":[b["block_id"] for b in own[sid]],
                           "citation":{"source_id":block["source_id"]}}
            delegations.append({"section_id":sid,"heading_block_id":block["block_id"],"source_block_ids":[b["block_id"] for b in own[sid]]})
    for sid,selected in own.items():
        for block in selected:
            if block["block_id"] in claimed and claimed[block["block_id"]]!=sid:raise PlanningError("NATIVE_SCOPE_MULTIPLE_BLOCK_OWNERS")
            claimed[block["block_id"]]=sid
    for sid,selected in own.items():
        section=next(s for s in sections if s["section_id"]==sid);chapter=chapters[sid]
        row=next((r for r in results if r["section_id"]==sid),None)
        if row is None:row=next(r for r in delegations if r["section_id"]==sid)
        source=source_map[chapter["citation"]["source_id"]]
        if not all(re.fullmatch(r"pdf/page\[\d+\]/(?:line|table)\[\d+\]",b["locator"]) for b in selected):
            row.update(status="not_recovered",reason="unsupported_non_pdf_native_scope");continue
        chosen={**chapter,"content_block_ids":[b["block_id"] for b in selected]}
        try:spec,reason=recover_one(ctx,section,chosen,blocks,source,tables,facts,manifest,scoped_source)
        except NativeLayoutError as exc:
            if "UNSUPPORTED" not in exc.code and exc.code!="NATIVE_LAYOUT_UNRESOLVED_GLYPH":raise PlanningError(exc.code) from exc
            row.update(status="not_recovered",reason=exc.code);continue
        except ValueError as exc:raise PlanningError("NATIVE_SOURCE_FORMAT_INVALID") from exc
        if spec is None:row.update(status="not_recovered",reason=reason)
        else:row.update(status="source_layout_recovered",physical_pages=[p.physical_page for p in spec.page_scopes],
                        proof_sha256=spec.proof_sha256,binding_count=len(spec.bindings),form_role=spec.form_role)
    # Every source member has a concrete actual owner, including headings that
    # were separated from the raw candidate span before layout recovery.
    coverage=[]
    for chapter,row in zip(formats,results):
        if row["status"]=="self_drafted_no_prescribed_body":continue
        missing=[key for key in chapter["content_block_ids"] if key not in claimed]
        if missing:
            ctx.capture("n3-native-scope-unaccounted",{"format_id":chapter["format_id"],"block_ids":missing})
            raise PlanningError("NATIVE_SCOPE_UNACCOUNTED_SOURCE_BLOCK",{"format_id":chapter["format_id"],"block_ids":missing})
        coverage.append({"format_id":chapter["format_id"],"owners":[{"block_id":key,"section_id":claimed[key]} for key in chapter["content_block_ids"]]})
    report={"schema_version":VERSION,"formats":results,"delegated_sections":delegations,"source_ownership":coverage,
        "all_format_paths_accounted":all(r["status"] in {"source_layout_recovered","self_drafted_no_prescribed_body"} for r in results),
        "all_prescribed_layouts_recovered":all(r["status"]=="source_layout_recovered" for r in results if r["status"]!="self_drafted_no_prescribed_body"),
        "unrecovered_is_implementation_gap_not_company_material_absence":True}
    ctx.capture("n3-native-format-plan",report)
    return report
