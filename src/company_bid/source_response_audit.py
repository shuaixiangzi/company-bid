"""Private, source-first omission ledger, not a semantic compliance verdict.

Every frozen tender text cell/paragraph remains in the denominator, including
uncited text. Exact quotation coverage and actual response destinations are
separate. A linked paragraph, a draft or a pending action never proves that all
obligations in a source passage have been understood or fulfilled.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict

from .extraction import _index, _pdf_footer_skips, _raw_grid
from .storage import digest
from .table_continuations import build_table_continuations, build_table_row_catalog


AUDIT_VERSION = "source-response-audit-v1"


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _one(pattern, text):
    matches = re.findall(pattern, text)
    if len(matches) != 1:
        raise ValueError("SOURCE_AUDIT_SCOPE_INVALID")
    return matches[0]


def _pieces(block):
    if block["kind"] != "table":
        return [(None, None, 0, len(block["text"]), block["text"])]
    rows, _, _ = _raw_grid(block)
    result, offset = [], 0
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            result.append((r, c, offset, offset + len(text), text))
            offset += len(text) + int(c < len(row) - 1)
        offset += int(r < len(rows) - 1)
    return result


def _scope(source, quote, blocks, documents, row_catalog, footers):
    """Rebind the frozen N3 scope to actual N1 text, with no fuzzy matches."""
    locator = source["locator"]
    first_locator = locator.split(";", 1)[0]
    candidates = [b for b in blocks.values() if b["locator"] == first_locator and
        any(d["source_id"] == b["source_id"] and d["sha256"] == source["sha256"] for d in documents)]
    if len(candidates) != 1:
        raise ValueError("SOURCE_AUDIT_LOCATION_INVALID")
    first = candidates[0]
    identity = re.findall(r"(?:^|;\s*)blocks=([^;]+)(?=;|$)", locator)
    ids = identity[0].split(",") if len(identity) == 1 else [first["block_id"]]
    if len(identity) > 1 or not ids or ids[0] != first["block_id"] or len(set(ids)) != len(ids):
        raise ValueError("SOURCE_AUDIT_MEMBERS_INVALID")
    if any(key not in blocks or blocks[key]["source_id"] != first["source_id"] for key in ids):
        raise ValueError("SOURCE_AUDIT_MEMBERS_INVALID")
    members = [blocks[key] for key in ids]
    if ids != sorted(ids, key=lambda key: blocks[key]["ordinal"]):
        raise ValueError("SOURCE_AUDIT_MEMBERS_INVALID")
    if identity:
        end_locator = _one(r"(?:^|;\s*)through=([^;]+)(?=;|$)", locator)
        if end_locator != members[-1]["locator"]:
            raise ValueError("SOURCE_AUDIT_MEMBERS_INVALID")
    if len(members) > 1:
        between = [b for b in blocks.values() if members[0]["ordinal"] <= b["ordinal"] <= members[-1]["ordinal"]]
        if any(b["block_id"] not in ids and b["block_id"] not in footers for b in between):
            raise ValueError("SOURCE_AUDIT_MEMBER_GAP")
    quote_hash = _one(r"(?:^|;\s*)quote_sha256=([a-f0-9]{64})(?=;|$)", locator)
    if quote_hash != _sha(quote):
        raise ValueError("SOURCE_AUDIT_QUOTE_HASH_INVALID")
    basis = _one(r"(?:^|;\s*)basis=([^;]+)(?=;|$)", locator)
    row_view = "selected_row_view_chars[" in locator
    tag = "selected_row_view_chars" if row_view else "chars"
    a, b = map(int, _one(r"(?:^|;\s*)" + tag + r"\[(\d+):(\d+)\](?=;|$)", locator))
    if row_view:
        matches = [row for row in row_catalog if row["source_id"] == first["source_id"] and
                   row["block_ids"] == ids and row["quote"] == quote]
        if basis != "frozen_table_rows_tsv_v1" or len(matches) != 1 or a != 0 or b != len(quote):
            raise ValueError("SOURCE_AUDIT_ROW_SCOPE_INVALID")
        cells = matches[0]["cell_refs"]
        expected = ",".join((cell["block_id"] + ":" if cell.get("block_id") else "") +
                            f"r{cell['row']}c{cell['column']}" for cell in cells)
        if _one(r"(?:^|;\s*)cells=([^;]+)(?=;|$)", locator) != expected:
            raise ValueError("SOURCE_AUDIT_ROW_SCOPE_INVALID")
        result = []
        for cell in cells:
            key = cell.get("block_id", first["block_id"])
            for row, column, start, end, text in _pieces(blocks[key]):
                if (row, column) == (cell["row"], cell["column"]) and text:
                    result.append((key, row, column, 0, len(text)))
        return result
    if basis not in {"native_block_text_v1", "table_rows_tsv_v1", "consecutive_blocks_lf_v1",
                     "consecutive_mixed_blocks_lf_v1", "consecutive_native_tables_tsv_v1", "frozen_clause_span_v1"}:
        raise ValueError("SOURCE_AUDIT_BASIS_UNSUPPORTED")
    text = "\n".join(member["text"] for member in members)
    if not 0 <= a < b <= len(text) or text[a:b] != quote:
        raise ValueError("SOURCE_AUDIT_QUOTE_RANGE_INVALID")
    result, cursor = [], 0
    for member in members:
        for row, column, start, end, cell in _pieces(member):
            left, right = max(start + cursor, a), min(end + cursor, b)
            if left < right:
                result.append((member["block_id"], row, column, left - cursor - start, right - cursor - start))
        cursor += len(member["text"]) + 1
    return result


def _destinations(package, rid):
    result = []
    for section in package["sections"]:
        for index, block in enumerate(section.get("blocks", [])):
            if rid not in block.get("requirement_refs", []):
                continue
            kind = block["kind"]
            # A title alone is not a substantive response destination.
            if kind == "heading":
                continue
            result.append({"kind": kind, "section_id": section["section_id"], "block_index": index,
                           "document_role": section.get("document_role", "response"),
                           "rendered": section.get("render_body", True), "content_sha256": digest(block)})
    for collection, key, kind in (("issues", "issue_id", "pending_issue"),
                                 ("review_actions", "action_id", "pending_action")):
        result.extend({"kind": kind, "id": item[key], "content_sha256": digest(item)}
                      for item in package.get(collection, []) if rid in item["requirement_refs"])
    return result


def build_source_response_audit(parsed: dict, manifest: dict, package: dict) -> dict:
    """Build from authenticated N1 and validated N4; caller captures privately.

    The function deliberately reports unmapped source text, not 'missing
    requirements': titles, instructions and repeated text require human source
    review before any requirement is added. Blank source cells remain distinct
    from unread/ignored objects. Inputs are never mutated.
    """
    blocks, units, documents = _index(parsed, manifest)
    tender_ids = {d["source_id"] for d in documents}
    blocks = {key: value for key, value in blocks.items() if value["source_id"] in tender_ids}
    reading_units, reading_documents = [], []
    for document in documents:
        actual_count = len(document["units"])
        expected_count = document.get("expected_units")
        reading_documents.append({"source_id": document["source_id"], "source_sha256": document["sha256"],
            "expected_units": expected_count, "actual_units": actual_count,
            "denominator_status": "not_recorded" if expected_count is None else
                "matched" if type(expected_count) is int and expected_count == actual_count else "mismatch"})
        for unit in document["units"]:
            coverage = unit.get("coverage", {})
            status = coverage.get("status", "not_recorded")
            errors = {key: coverage[key] for key in ("unread_objects", "unmapped_glyphs", "error_code") if coverage.get(key)}
            disposition = ("reading_incomplete" if status not in {"read", "empty"} or errors else
                           "source_empty_page" if status == "empty" and not unit.get("blocks") else
                           "read_without_blocks_requires_review" if not unit.get("blocks") else "read")
            reading_units.append({"source_id": document["source_id"], "source_sha256": document["sha256"],
                "unit_id": unit["unit_id"], "locator": unit["locator"], "page": unit.get("page"),
                "block_count": len(unit.get("blocks", [])), "reading_status": status,
                "disposition": disposition, "errors": errors})
    footers = _pdf_footer_skips(blocks)
    row_catalog = []
    for document in documents:
        members = [b for b in blocks.values() if b["source_id"] == document["source_id"]]
        continuations = build_table_continuations(document, members, footers)
        row_catalog.extend(build_table_row_catalog(document, members, continuations))
    sources = {s["source_id"]: s for s in package["sources"]}
    if len(sources) != len(package["sources"]):
        raise ValueError("SOURCE_AUDIT_DUPLICATE_SOURCE")
    mapped, context_mapped = defaultdict(list), defaultdict(list)
    requirement_rows = []
    for requirement in package["requirements"]:
        rid = requirement["requirement_id"]
        refs = [(ref, requirement["text"], "requirement") for ref in requirement["source_refs"]]
        refs += [(ref, context["text"], "context") for context in requirement.get("source_contexts", [])
                 for ref in context["source_refs"]]
        for ref, quote, role in refs:
            source = sources.get(ref)
            if source is None or source["kind"] != "tender":
                raise ValueError("SOURCE_AUDIT_SOURCE_INVALID")
            for key, row, column, start, end in _scope(source, quote, blocks, documents, row_catalog, footers):
                target = mapped if role == "requirement" else context_mapped
                target[(key, row, column)].append({"requirement_id": rid, "source_ref": ref,
                                                  "start": start, "end": end})
        targets = _destinations(package, rid)
        heading_sections = [section["section_id"] for section in package["sections"]
                            if section.get("render_body", True) and section.get("title") == requirement["text"]
                            and rid in section.get("requirement_refs", [])]
        requirement_rows.append({"requirement_id": rid, "quote_sha256": _sha(requirement["text"]),
            "source_refs": requirement["source_refs"], "destinations": targets, "exact_heading_section_ids": heading_sections,
            "destination_status": "linked_for_review" if targets else
                "heading_only_requires_scope_review" if heading_sections else "no_substantive_destination",
            "semantic_coverage": "not_determined", "satisfaction_status": "not_determined"})
    result = []
    for block in blocks.values():
        source_sha = next(d["sha256"] for d in documents if d["source_id"] == block["source_id"])
        reading = units[block["unit_id"]].get("coverage", {})
        for row, column, start, end, text in _pieces(block):
            identity = (block["block_id"], row, column)
            links, contexts = mapped[identity], context_mapped[identity]
            covered = {i for link in links for i in range(link["start"], link["end"])}
            missing = [i for i, char in enumerate(text) if not char.isspace() and i not in covered]
            ranges = []
            for i in missing:
                if ranges and i == ranges[-1][1]:
                    ranges[-1][1] += 1
                else:
                    ranges.append([i, i + 1])
            if reading.get("status") not in {"read", "empty"} or any(
                    reading.get(key) for key in ("unread_objects", "unmapped_glyphs", "error_code")):
                status = "reading_incomplete"
            elif block.get("read_disposition"):
                status = "reader_ignored_requires_review"
            elif block["kind"] == "image":
                status = "non_text_requires_review"
            elif block["block_id"] in footers:
                status = "proven_page_furniture"
            elif not text.strip():
                status = "source_blank"
            elif not missing:
                status = "fully_quoted"
            elif links:
                status = "partly_quoted"
            else:
                status = "not_quoted"
            item = {"source_id": block["source_id"], "source_sha256": source_sha,
                    "block_id": block["block_id"], "locator": block["locator"], "page": block.get("page"),
                    "reading_status": reading.get("status", "not_recorded"),
                    "row": row, "column": column, "text": text, "text_sha256": _sha(text),
                    "quotation_status": status, "requirement_links": links, "context_links": contexts,
                    "unquoted_ranges": [{"start": a, "end": b, "text": text[a:b]} for a, b in ranges],
                    "semantic_review": "not_checked"}
            if block["block_id"] in footers:
                item["footer_proof"] = footers[block["block_id"]]
            if block.get("read_disposition"):
                item["read_disposition"] = block["read_disposition"]
            result.append(item)
    report = {"version": AUDIT_VERSION, "parsed_sha256": digest(parsed), "package_sha256": digest(package),
              "status": "structural_audit_only", "semantic_completeness": "not_determined",
              "reading_unit_count": len(reading_units), "reading_counts": dict(Counter(r["disposition"] for r in reading_units)),
              "reading_denominator_status": "matched" if all(r["denominator_status"] == "matched" for r in reading_documents)
                  else "requires_review",
              "reading_units": reading_units, "reading_documents": reading_documents,
              "source_unit_count": len(result), "quotation_counts": dict(Counter(r["quotation_status"] for r in result)),
              "requirement_count": len(requirement_rows),
              "without_destination_count": sum(r["destination_status"] == "no_substantive_destination" for r in requirement_rows),
              "source_units": result, "requirements": requirement_rows}
    return {**report, "audit_sha256": digest(report)}
