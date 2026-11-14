"""N3 recovery of measured, source-bound response forms and identity slots.

Independently implements the protected-template/explicit-fact contracts reviewed
in docs/08 against legacy 53f20a3a. No legacy code or runtime is imported.
"""
from __future__ import annotations

import copy
import hashlib
import re

from .native_layout import NativeLayoutError, read_native_format_layout
from .source_format_contracts import NativeFormatLayout, format_digest, negative_fill_instruction


SOURCE_FORMAT_VERSION = "source-response-format-v1"


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _font(runs):
    if not runs:
        return {"font_family": "宋体", "font_size_pt": 12., "bold": False}
    first = runs[0]
    return {key: first[key] for key in ("font_family", "font_size_pt", "bold")}


def _centered(line):
    evidence = line["alignment_evidence"]
    if evidence["horizontal"] != "center":
        return False
    width = evidence["container_bbox"][2] - evidence["container_bbox"][0]
    # Symmetric edges of a long wrapped body line are not title alignment.
    return _font(line["runs"])["bold"] or line["bbox"][2] - line["bbox"][0] < width * .6


def _line_underlines(line, rules, index):
    rules = [rule for rule in rules if rule["kind"] == "fillable_rule" and
             -2 <= rule["top"] - line["bbox"][3] <= 3]
    glyph_slots = list(re.finditer(r"[_＿]{2,}", line["text"]))
    if not rules and not glyph_slots:
        return []
    # N1's physical-line view normalizes positioned native whitespace. Only
    # visible glyphs anchor a rule; the measured blank itself is the interval.
    chars = [c for run in line["runs"] for c in run["chars"] if c["text"].strip()]
    chars.sort(key=lambda c: c["bbox"][0])
    indexed, cursor = [], 0
    for char in chars:
        position = line["text"].find(char["text"], cursor)
        if position < 0:
            raise ValueError("NATIVE_FORMAT_CHARACTER_ALIGNMENT_FAILED")
        indexed.append((position, position + len(char["text"]), char["bbox"]))
        cursor = position + len(char["text"])
    result = []
    for rule in rules:
        left = [end for start, end, box in indexed if box[2] <= rule["x0"] + 1]
        right = [start for start, end, box in indexed if box[0] >= rule["x1"] - 1]
        start, end = (max(left) if left else 0), (min(right) if right else len(line["text"]))
        if start > end:
            raise ValueError("NATIVE_FORMAT_RULE_RANGE_INVALID")
        # A vector stroke may underline printed text. It changes typography,
        # never authorizes a fill: _fill_line independently limits identity slots.
        result.append({"fixed_index": index, "start": start, "end": end,
                       "width_pt": rule["length_pt"], "locator": rule["locator"],
                       "basis":"native_vector_rule", "bbox":rule["bbox"]})
    for slot in glyph_slots:
        boxes = [box for start,end,box in indexed if slot.start() <= start and end <= slot.end()]
        if len(boxes) != slot.end() - slot.start():
            raise ValueError("NATIVE_UNDERSCORE_GLYPH_ALIGNMENT_FAILED")
        bbox=[min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes)]
        result.append({"fixed_index":index,"start":slot.start(),"end":slot.end(),"width_pt":bbox[2]-bbox[0],
            "locator":f"{line['locator']}; chars[{slot.start()}:{slot.end()}]","basis":"native_underscore_glyphs","bbox":bbox})
    return result


def _identity_fact(facts, label, manifest):
    aliases = ({"公司名称", "投标人名称", "响应人名称", "供应商名称"} if label == "company_name" else {"项目名称"})
    expected = manifest["company" if label == "company_name" else "project"]["name"]
    matches = [f for f in facts if f["label"] in aliases and f["value"] == expected]
    if not matches or len({f["value"] for f in matches}) != 1:
        return None
    return matches[0]


def _fill_line(item, original, block_id, index, facts, manifest, *, source_rules=()):
    """Only two explicit identity slots; names, IDs and signatures are excluded."""
    if negative_fill_instruction(original):
        return []
    matches = {}
    for match in re.finditer(r"(?P<gap>[ \t_＿]+)(?=[（(](?:响应人|投标人|供应商)(?:名称|全称)[）)])", original):
        matches[(match.start(), match.end(), "company_name")] = None
    for match in re.finditer(r"[（(]项目名称[）)]", original):
        matches[(match.start(), match.start(), "project_name")] = None
    match = re.fullmatch(r"(?:响应人|投标人|供应商)(?:名称)?[：:](?P<gap>[ \t_＿]*)(?=[（(].*盖.*章[）)]).+", original)
    if match:
        matches[(match.start("gap"), match.end("gap"), "company_name")] = None
    for rule in source_rules:
        if rule["fixed_index"] != index:
            continue
        start,end=rule["start"],rule["end"]
        if (re.fullmatch(r"项目名称[：:]", original[:start]) and start < end and
                not original[start:end].strip(" \t_＿") and re.match(r"\s*(?:项目编号[：:]|$)",original[end:])):
            matches[(start,end,"project_name")] = rule
        placeholder=re.match(r"[（(](?:响应人|投标人|供应商)[）)]", original)
        if (placeholder and re.match(r"\s+已(?:认真)?阅读", original[placeholder.end():]) and
                start == 0 and end >= placeholder.end() and rule["basis"] == "native_vector_rule"):
            matches[(0,0,"company_name")] = rule
    bindings, text = [], original
    # Right-to-left edits preserve offsets and form an auditable exact chain.
    for start, end, label in sorted(matches, reverse=True):
        fact = _identity_fact(facts, label, manifest)
        if fact is None:
            continue
        before = text
        text = before[:start] + fact["value"] + before[end:]
        bindings.append({"fixed_index": index, "source_block_id": block_id, "start": start, "end": end,
            "original": before[start:end], "replacement": fact["value"], "label": label,
            "fact_id": fact["fact_id"], "fact_source_refs": fact["source_refs"],
            "before_text": before, "after_text": text, "before_sha256": _sha(before), "after_sha256": _sha(text),
            "protected_text_sha256": _sha(before[:start] + "<slot>" + before[end:]),
            "source_rule":matches[(start,end,label)]})
        item["fact_refs"] = list(dict.fromkeys([*item.get("fact_refs", []), fact["fact_id"]]))
        item["source_refs"] = list(dict.fromkeys([*item["source_refs"], *fact["source_refs"]]))
    item["text"] = text
    return bindings


def _paragraph(indices, originals, source_blocks, lines, fixed, rules, width):
    first, last = lines[0], lines[-1]
    left = min(line["bbox"][0] for line in lines)
    right_edge = max(line["bbox"][2] for line in lines)
    font = _font(first["runs"])
    alignment = "center" if _centered(first) else (
        "right" if first["bbox"][0] > width / 2 else "left")
    pitch = (lines[1]["bbox"][1] - first["bbox"][1]) if len(lines) > 1 else font["font_size_pt"] * 1.25
    return {"kind": "paragraph", "fixed_indices": indices, "source_block_ids": source_blocks,
        "source_texts": originals, "output_texts_sha256": format_digest([fixed[i]["text"] for i in indices]),
        "source_locators": [line["locator"] for line in lines],
        "bbox": [left, first["bbox"][1], right_edge, last["bbox"][3]], **font, "alignment": alignment,
        "left_pt": left, "right_pt": max(0., width - right_edge),
        "first_line_indent_pt": first["bbox"][0] - left, "line_spacing_pt": max(font["font_size_pt"] * 1.15, pitch),
        "underlines": [rule for line, index in zip(lines, indices) for rule in _line_underlines(line, rules, index)],
        "grouping_rule": "continuous_native_sentence" if len(indices) > 1 else "one_native_line"}


def _can_join(previous, following):
    # A complete continuous sentence at the source's line pitch. Never join a
    # centered/bold heading, a signature label, a new item, or a larger gap.
    a, b = _font(previous["runs"]), _font(following["runs"])
    def standalone(text):
        return (bool(re.match(r"(?:响应人|投标人|供应商|法定代表人|项目负责人|委托代理人|委托日期|日期|日\s*期|说明)[^：:]{0,20}[：:]", text)) or
                bool(re.search(r"签字|盖(?:单位)?章", text)))
    return (a == b and not a["bold"] and not standalone(previous["text"]) and not standalone(following["text"]) and
        not _centered(previous) and not _centered(following) and
        not re.search(r"[。；;：:！!？?]$", previous["text"]) and
        not re.match(r"(?:[0-9一二三四五六七八九十]+[、.)）]|(?:响应人|投标人|法定代表人|委托代理人|委托日期)[：:])", following["text"]) and
        0 < following["bbox"][1] - previous["bbox"][1] <= a["font_size_pt"] * 2.1)


def _cell_underlines(cell, rules):
    """Map only a measured blank vector rule to the exact source-cell string."""
    box, text = cell["bbox"], cell["text"]
    rules = [rule for rule in rules if rule["kind"] == "fillable_rule" and
             box[0] <= rule["x0"] < rule["x1"] <= box[2] and box[1] < rule["top"] < box[3]]
    if not rules:
        return []
    chars = [c for run in cell["runs"] for c in run["chars"] if c["text"].strip()]
    bands = []
    for char in sorted(chars, key=lambda c: ((c["bbox"][1] + c["bbox"][3]) / 2, c["bbox"][0])):
        middle = (char["bbox"][1] + char["bbox"][3]) / 2
        if bands and abs(middle - bands[-1][0]) <= 2:
            bands[-1][1].append(char)
        else:
            bands.append((middle, [char]))
    positions = [(i, c) for i, c in enumerate(text) if c.strip()]
    visible = [(band, char) for band, (_, members) in enumerate(bands) for char in sorted(members, key=lambda c:c["bbox"][0])]
    if any(len(char["text"]) != 1 for _,char in visible) or ''.join(char["text"] for _, char in visible) != ''.join(c for _,c in positions):
        raise ValueError("NATIVE_CELL_RULE_CHARACTER_ALIGNMENT_FAILED")
    mapped = [[] for _ in bands]
    for (band, char), (index, _) in zip(visible, positions):
        mapped[band].append((index, index + len(char["text"]), char["bbox"]))
    result = []
    for rule in rules:
        candidates = [line for line in mapped if line and -2 <= rule["top"] - max(c[2][3] for c in line) <= 3]
        if len(candidates) != 1:
            raise ValueError("NATIVE_CELL_RULE_BASELINE_AMBIGUOUS")
        line = candidates[0]
        left = [end for start,end,b in line if b[2] <= rule["x0"] + 1]
        right = [start for start,end,b in line if b[0] >= rule["x1"] - 1]
        start, end = max(left, default=line[0][0]), min(right, default=line[-1][1])
        if start > end or text[start:end].strip(" \t_＿"):
            raise ValueError("NATIVE_CELL_RULE_NOT_AN_EXACT_BLANK")
        result.append({"start":start,"end":end,"width_pt":rule["length_pt"],"locator":rule["locator"]})
    return result


def _cell_insets(cell, fallback):
    evidence = cell["alignment_evidence"]
    if not cell["runs"]:
        return {"left_inset_pt":fallback,"right_inset_pt":fallback,"top_inset_pt":0.,"bottom_inset_pt":0.,
                "inset_basis":"same_table_minimum_horizontal_inset" if fallback else "no_measured_inset"}
    horizontal, vertical = evidence["horizontal"], evidence["vertical"]
    return {"left_inset_pt":0. if horizontal == "center" else max(0., evidence["left_gap_pt"]),
            "right_inset_pt":0. if horizontal == "center" else fallback,
            "top_inset_pt":max(0., evidence["top_gap_pt"]) if vertical == "top" else 0.,
            "bottom_inset_pt":max(0., evidence["bottom_gap_pt"]) if vertical == "bottom" else 0.,
            "inset_basis":"measured_glyph_bounds_with_same_table_right_inset" if horizontal != "center" and fallback else "measured_glyph_bounds"}


def _recover_one(ctx, section, chapter, blocks, source, tables, facts, manifest, scoped_source):
    selected = [blocks[key] for key in chapter["content_block_ids"]]
    pages = {b.get("page") for b in selected}
    if len(pages) != 1 or None in pages:
        return None, "unsupported_multiple_physical_pages"
    if any(b.get("origin") != "native" or b.get("read_disposition") or b["kind"] not in {"paragraph", "table"} for b in selected):
        return None, "unsupported_non_native_or_omitted_block"
    page_number = next(iter(pages))
    unit_input = {"source_id": source["source_id"], "source_sha256": source["sha256"],
                  "physical_page": page_number, "frozen_blocks": selected, "version": SOURCE_FORMAT_VERSION}
    def read():
        value = read_native_format_layout(source["path"], source["sha256"], [page_number])
        ctx.capture("n3-native-source-layout", value)
        return value
    native = ctx.run_unit("native-format-" + format_digest(unit_input)[:20], unit_input, read)
    page = native["pages"][0]
    measured = {b["locator"]: b for b in [*page["lines"], *page["tables"]]}
    for block in selected:
        item = measured.get(block["locator"])
        if item is None or block["text"] != item["text"] or any(abs(a-b) > .01 for a,b in zip(block["bbox"],item["bbox"])):
            raise ValueError("NATIVE_FORMAT_FROZEN_BLOCK_MISMATCH")
        if block["kind"] == "table" and (block["rows"] != item["rows"] or block.get("merges", []) != item["merges"] or
                block.get("column_widths", []) != item["column_widths_pt"]):
            raise ValueError("NATIVE_FORMAT_FROZEN_GRID_MISMATCH")
    selected_locators = {b["locator"] for b in selected}
    if any(b["locator"] not in selected_locators for b in [*page["lines"], *page["tables"]]
           if not b.get("page_furniture_candidate")):
        return None, "unsupported_partial_native_page"
    heading = selected[0]
    if heading["kind"] != "paragraph" or heading["text"] != chapter["title"]:
        return None, "unsupported_heading_scope"
    content = selected[1:]
    fixed = copy.deepcopy(section["fixed_content"])
    if len(content) != len(fixed):
        return None, "unsupported_transformed_or_instruction_only_body"
    elements, bindings, pending_lines = [], [], []
    def flush():
        if pending_lines:
            elements.append(_paragraph([x[0] for x in pending_lines], [x[1]["text"] for x in pending_lines],
                [x[1]["block_id"] for x in pending_lines], [measured[x[1]["locator"]] for x in pending_lines],
                fixed, page["horizontal_rules"], page["width_pt"]))
            pending_lines.clear()
    for index, (block, item) in enumerate(zip(content, fixed)):
        # Existing generic references need not name a singleton block. Native
        # proof adds an explicit member scope without changing global citation
        # IDs or the unrelated writing inputs.
        native_ref = scoped_source({"source_id": source["source_id"], "block_id": block["block_id"],
            "block_ids": [block["block_id"]], "locator": block["locator"], "quote": block["text"]})
        item["source_refs"] = list(dict.fromkeys([*item["source_refs"], native_ref]))
        if block["kind"] == "paragraph":
            if item["kind"] != "paragraph" or item["text"] != block["text"]:
                return None, "unsupported_transformed_fixed_paragraph"
            line = measured[block["locator"]]
            if pending_lines and not _can_join(measured[pending_lines[-1][1]["locator"]], line):
                flush()
            bindings.extend(_fill_line(item, block["text"], block["block_id"], index, facts, manifest,
                                      source_rules=_line_underlines(line, page["horizontal_rules"], index)))
            pending_lines.append((index, block))
        else:
            flush()
            table = next((t for t in tables if t["table_id"] == item.get("table_ref")), None)
            if item["kind"] != "table_ref" or table is None or table["section_id"] != section["section_id"]:
                return None, "unsupported_table_owner"
            table["source_refs"] = list(dict.fromkeys([*table["source_refs"], native_ref]))
            raw = measured[block["locator"]]
            cells = []
            small_insets = [c["alignment_evidence"]["left_gap_pt"] for c in raw["cells"] if c["runs"] and
                            1 <= c["alignment_evidence"]["left_gap_pt"] <= 10]
            fallback = min(small_insets, default=0.)
            for cell in raw["cells"]:
                font = _font(cell["runs"])
                if not cell["runs"]:
                    font = _font(next((c["runs"] for c in raw["cells"] if c["runs"]), []))
                cells.append({"row": cell["row"], "column": cell["column"], **font,
                    "alignment": "center" if cell["alignment_evidence"]["horizontal"] == "center" else "left",
                    "vertical_alignment": cell["alignment_evidence"]["vertical"] if cell["runs"] else "center",
                    **_cell_insets(cell, fallback), "underlines":_cell_underlines(cell, page["horizontal_rules"])})
            elements.append({"kind": "table", "fixed_index": index, "source_block_id": block["block_id"],
                "source_locator": block["locator"],
                "table_id": table["table_id"], "bbox": raw["bbox"], "column_widths_pt": raw["column_widths_pt"],
                "row_heights_pt": raw["row_heights_pt"], "cells": cells,
                "source_rows": block["rows"], "source_merges": block.get("merges", []),
                "source_grid_sha256": format_digest(block["rows"]), "output_grid_sha256": format_digest(table["rows"])})
    flush()
    head = measured[heading["locator"]]
    result = {"schema_version": SOURCE_FORMAT_VERSION, "source_id": source["source_id"], "source_sha256": source["sha256"],
        "physical_page": page_number, "page_width_pt": page["width_pt"], "page_height_pt": page["height_pt"],
        "heading_text": heading["text"], "heading_bbox": heading["bbox"],
        "heading_alignment": "center" if _centered(head) else ("right" if head["bbox"][0] > page["width_pt"]/2 else "left"),
        "heading_source_ref": scoped_source({"source_id": source["source_id"], "block_id": heading["block_id"],
            "block_ids": [heading["block_id"]], "locator": heading["locator"], "quote": heading["text"]}),
        "heading_source_block_id": heading["block_id"], "heading_source_locator": heading["locator"],
        **{"heading_" + k: v for k,v in _font(head["runs"]).items()}, "elements": elements, "bindings": bindings,
        "native_read_sha256": format_digest(native), "frozen_blocks_sha256": format_digest(selected)}
    result["proof_sha256"] = format_digest(result)
    spec = NativeFormatLayout.model_validate(result)
    section["fixed_content"], section["native_format"] = fixed, spec.model_dump(mode="json")
    return spec, None


def recover_source_formats(ctx, sections, formats, blocks, source_map, tables, facts, manifest, scoped_source):
    """N3-only opt-in: explicit per-format recovery status, no hidden fallback."""
    from .extraction import _id, PlanningError
    results = []
    for chapter in formats:
        sid = _id("SEC-", chapter["format_id"])
        section = next((s for s in sections if s["section_id"] == sid), None)
        row = {"format_id": chapter["format_id"], "section_id": sid, "title": chapter["title"],
               "status": "not_recovered", "satisfaction_status": "not_determined"}
        results.append(row)
        source = source_map.get(chapter["citation"]["source_id"])
        if section is None or not section["fixed_content"] or section["writing_tasks"] or chapter.get("source_role") == "prescribed_cover":
            row["reason"] = "cover_existing_path_or_no_complete_fixed_body"
            continue
        if source is None or not all(re.fullmatch(r"pdf/page\[\d+\]/(?:line|table)\[\d+\]", blocks[b]["locator"])
                                     for b in chapter["content_block_ids"]):
            row["reason"] = "unsupported_non_pdf_native_scope"
            continue
        try:
            spec, reason = _recover_one(ctx, section, chapter, blocks, source, tables, facts, manifest, scoped_source)
        except NativeLayoutError as exc:
            if "UNSUPPORTED" not in exc.code and exc.code != "NATIVE_LAYOUT_UNRESOLVED_GLYPH":
                raise PlanningError(exc.code) from exc
            row["reason"] = exc.code
            continue
        except ValueError as exc:
            raise PlanningError("NATIVE_SOURCE_FORMAT_INVALID") from exc
        if spec is None:
            row["reason"] = reason
        else:
            row.update(status="source_layout_recovered", physical_page=spec.physical_page,
                       proof_sha256=spec.proof_sha256, binding_count=len(spec.bindings))
    report = {"schema_version": SOURCE_FORMAT_VERSION, "formats": results,
              "complete": all(row["status"] == "source_layout_recovered" for row in results),
              "unrecovered_is_implementation_gap_not_company_material_absence": True}
    ctx.capture("n3-native-format-plan", report)
    return report
