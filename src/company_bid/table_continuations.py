"""Conservative whole-table continuation and exact-row source proofs.

Continuation quotes contain both complete frozen grids; row-catalog quotes
contain only their proved complete source rows. Neither activates unchecked
options, rewrites cells, or adds unrelated rows to a continuing clause.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from decimal import Decimal


_HEADER = ["条款号", "条款名称", "编列内容"]
_RULE = "native_pdf_table_continuation_v1"
_CLAUSE = re.compile(r"[1-9]\d*(?:\.\d+)*(?:[（(]\d+[）)])?")
_SCORING_RULE = "native_pdf_scoring_table_continuation_v1"
_SCORING_HEADERS = {
    ("序号", "评审项目", "评审标准", "标准分"),
    ("序号", "评价项目", "评审标准", "分数"),
}


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _grid(block: dict, source_id: str, boundary: str):
    rows, widths, merges = block.get("rows"), block.get("column_widths"), block.get("merges")
    page, ordinal = block.get("page"), block.get("ordinal")
    if (block.get("source_id") != source_id or block.get("kind") != "table" or
            block.get("origin") != "native" or block.get("read_disposition") or
            type(page) is not int or page < 1 or type(ordinal) is not int or ordinal < 0 or
            not isinstance(block.get("block_id"), str) or not block["block_id"] or
            not re.fullmatch(rf"pdf/page\[{page}\]/table\[\d+\]", str(block.get("locator", ""))) or
            not isinstance(rows, list) or len(rows) < 2 or
            any(not isinstance(row, list) or len(row) != 3 or any(not isinstance(cell, str) for cell in row) for row in rows) or
            rows[0] != _HEADER or not isinstance(widths, list) or len(widths) != 3 or
            any(type(width) not in (int, float) or not math.isfinite(width) or width <= 0 for width in widths) or
            not isinstance(merges, list) or block.get("text_basis") != "table_rows_tsv_v1" or
            not isinstance(block.get("header_rows", []), list) or
            any(type(row) is not int or not 0 <= row < len(rows) for row in block.get("header_rows", []))):
        return None
    text = "\n".join("\t".join(row) for row in rows)
    if block.get("text") != text:
        return None
    boundary_row = len(rows) - 1 if boundary == "left" else 1
    occupied, covered = set(), set()
    for merge in merges:
        if not isinstance(merge, dict):
            return None
        r, c, rs, cs = (merge.get("row"), merge.get("column"), merge.get("row_span", 1), merge.get("col_span", 1))
        if (any(type(value) is not int for value in (r, c, rs, cs)) or min(r, c) < 0 or
                min(rs, cs) < 1 or rs * cs == 1 or r + rs > len(rows) or c + cs > 3 or
                0 in range(r, r + rs) or boundary_row in range(r, r + rs)):
            return None
        for row in range(r, r + rs):
            for column in range(c, c + cs):
                cell = (row, column)
                if cell in occupied or (cell != (r, c) and rows[row][column]):
                    return None
                occupied.add(cell)
                if cell != (r, c):
                    covered.add(cell)
    for index, row in enumerate(rows[1:], 1):
        if boundary == "right" and index == 1:
            if row[0] != "" or row[1] != "" or not row[2].strip():
                return None
        elif any(not row[column].strip() and (index, column) not in covered for column in (0, 1)):
            return None
    if boundary == "left" and (not _CLAUSE.fullmatch(rows[-1][0]) or not rows[-1][2].strip()):
        return None
    return {"block_id": block["block_id"], "locator": block["locator"], "page": page, "ordinal": ordinal,
            "origin": "native", "text_basis": block["text_basis"], "text_sha256": _text_hash(text),
            "grid_sha256": _hash({"rows": rows, "merges": merges, "column_widths": widths,
                                  "header_rows": block.get("header_rows", [])}),
            "header": list(_HEADER), "column_widths": list(widths), "boundary_row": boundary_row,
            "boundary_cells_sha256": _hash(rows[boundary_row])}


def _footer(block: dict, evidence: dict, source_id: str, left_page: int):
    """Consume a pre-proved footer; never infer a skip from numeric text alone."""
    if not isinstance(evidence, dict):
        return None
    stable = ("source_id", "block_id", "locator", "kind", "page", "status", "printed_page", "rule", "reason", "evidence_pages")
    if any(key not in evidence for key in stable):
        return None
    pages, printed = evidence["evidence_pages"], evidence["printed_page"]
    if (block.get("source_id") != source_id or block.get("kind") != "paragraph" or
            block.get("origin") != "native" or block.get("read_disposition") or
            block.get("page") != left_page or
            any(evidence[key] != block.get(key) for key in ("source_id", "block_id", "locator", "kind", "page")) or
            evidence["status"] != "skipped_page_footer" or evidence["rule"] != "pdf-page-footer-v1" or
            evidence["reason"] != "bottommost_numeric_line_in_consistent_page_sequence" or
            type(printed) is not int or not isinstance(pages, list) or len(pages) < 3 or
            any(type(page) is not int or page < 1 for page in pages) or pages != sorted(set(pages)) or left_page not in pages):
        return None
    if not isinstance(block.get("text"), str):
        return None
    text = block["text"].strip()
    number = re.fullmatch(r"[-－—]?\s*(\d{1,4})\s*[-－—]?", text)
    if not number or int(number[1]) != printed:
        return None
    proof = {key: evidence[key] for key in stable}
    return {"block_id": block["block_id"], "locator": block["locator"], "ordinal": block["ordinal"],
            "text_sha256": _text_hash(block["text"]), "footer_proof": proof, "footer_proof_sha256": _hash(proof)}


def _scoring_header(row: list):
    view = tuple(re.sub(r"[ \t\r\n]", "", cell) for cell in row)
    return list(view) if view in _SCORING_HEADERS else None


def _scoring_grid(block: dict, source_id: str, *, require_header: bool):
    rows, widths, merges = block.get("rows"), block.get("column_widths"), block.get("merges")
    page, ordinal = block.get("page"), block.get("ordinal")
    if (block.get("source_id") != source_id or block.get("kind") != "table" or block.get("origin") != "native" or
            block.get("read_disposition") or type(page) is not int or page < 1 or
            type(ordinal) is not int or ordinal < 0 or not isinstance(block.get("block_id"), str) or not block["block_id"] or
            not re.fullmatch(rf"pdf/page\[{page}\]/table\[\d+\]", str(block.get("locator", ""))) or
            not isinstance(rows, list) or not rows or
            any(not isinstance(row, list) or len(row) != 4 or any(not isinstance(cell, str) for cell in row) for row in rows) or
            not isinstance(widths, list) or len(widths) != 4 or
            any(type(width) not in (int, float) or not math.isfinite(width) or width <= 0 for width in widths) or
            not isinstance(merges, list) or block.get("text_basis") != "table_rows_tsv_v1" or
            not isinstance(block.get("header_rows", []), list) or
            any(type(row) is not int or not 0 <= row < len(rows) for row in block.get("header_rows", []))):
        return None
    header = _scoring_header(rows[0])
    if require_header and (header is None or len(rows) < 2):
        return None
    text = "\n".join("\t".join(row) for row in rows)
    if block.get("text") != text:
        return None
    occupied, merged_rows = set(), set()
    for merge in merges:
        if not isinstance(merge, dict):
            return None
        r, c, rs, cs = merge.get("row"), merge.get("column"), merge.get("row_span", 1), merge.get("col_span", 1)
        if (any(type(value) is not int for value in (r, c, rs, cs)) or min(r, c) < 0 or min(rs, cs) < 1 or
                rs * cs == 1 or r + rs > len(rows) or c + cs > 4 or header is not None and r == 0):
            return None
        for row in range(r, r + rs):
            merged_rows.add(row)
            for column in range(c, c + cs):
                cell = (row, column)
                if cell in occupied or cell != (r, c) and rows[row][column]:
                    return None
                occupied.add(cell)
    metadata = {"block_id": block["block_id"], "locator": block["locator"], "page": page, "ordinal": ordinal,
                "origin": "native", "text_basis": block["text_basis"], "text_sha256": _text_hash(text),
                "grid_sha256": _hash({"rows": rows, "merges": merges, "column_widths": widths,
                                      "header_rows": block.get("header_rows", [])}),
                "row_count": len(rows), "column_widths": list(widths),
                "header": list(rows[0]) if header else None, "header_view": header,
                "header_row": 0 if header else None, "table_family": "native_pdf_scoring_four_columns"}
    return metadata, merged_rows


def _score_number(value: str):
    return Decimal(value.strip()) if re.fullmatch(r"\d{1,9}(?:\.\d{1,6})?", value.strip()) else None


def _score_data_row(row: list) -> bool:
    return bool(re.fullmatch(r"[1-9]\d*", row[0]) and row[1].strip() and row[2].strip() and _score_number(row[3]) is not None)


def _score_opening(text: str):
    """A strict unmatched Chinese parenthesis, not mathematical interval repair."""
    if not text.strip() or re.search(r"[。；;.!?！？：:]\s*$", text):
        return None
    stack = []
    for offset, character in enumerate(text):
        if character in "（(":
            stack.append((character, offset))
        elif character in "）)":
            if not stack or stack[-1][0] != ("（" if character == "）" else "("):
                return None
            stack.pop()
    return stack[0][1] if len(stack) == 1 and stack[0][0] == "（" else None


def _score_bracket_proof(left: str, right: str):
    opening = _score_opening(left)
    if opening is None:
        return None
    stack, closing = ["（"], None
    for offset, character in enumerate(right):
        if character in "（(":
            stack.append(character)
        elif character in "）)":
            if not stack or stack[-1] != ("（" if character == "）" else "("):
                return None
            stack.pop()
            if not stack and closing is None:
                closing = offset
    if stack or closing is None or re.search(r"[。；;.!?！？]", right[:closing]):
        return None
    return {"rule": "unclosed_chinese_parenthesis_closed_before_sentence_boundary_v1",
            "left_open_offset": opening, "right_close_offset": closing,
            "left_cell_sha256": _text_hash(left), "right_cell_sha256": _text_hash(right)}


def _score_width_proof(left: list, right: list):
    a, b = math.fsum(left), math.fsum(right)
    first = [math.fsum(left[:index]) / a for index in range(1, 4)]
    second = [math.fsum(right[:index]) / b for index in range(1, 4)]
    deviations = [abs(x - y) for x, y in zip(first, second)]
    relative = abs(a - b) / a
    if max(deviations) > .01 or relative > .02:
        return None
    return {"rule": "normalized_internal_column_boundaries_v1", "left_total_width": a, "right_total_width": b,
            "left_normalized_boundaries": first, "right_normalized_boundaries": second,
            "boundary_deviations": deviations, "maximum_boundary_deviation": max(deviations),
            "relative_total_width_difference": relative, "total_difference_denominator": "left_total_width",
            "maximum_allowed_boundary_deviation": .01, "maximum_allowed_relative_total_difference": .02}


def _score_tail_shape(block: dict) -> bool:
    rows = block.get("rows")
    return bool(block.get("kind") == "table" and block.get("origin") == "native" and
                isinstance(rows, list) and rows and isinstance(rows[0], list) and len(rows[0]) == 4 and
                all(isinstance(cell, str) for cell in rows[0]) and rows[0][0] == rows[0][1] == rows[0][3] == "" and rows[0][2].strip())


def _score_tail_has_context(block: dict, previous: dict | None) -> bool:
    """Require scoring source context beyond a sparse instruction-row shape."""
    rows = block["rows"]
    # A closing continuation followed by the original numeric total remains
    # suspicious even when supplied without its preceding scoring table.
    if (len(rows) >= 2 and isinstance(rows[-1], list) and len(rows[-1]) == 4 and
            all(isinstance(cell, str) for cell in rows[-1]) and rows[-1][:3] == ["合计", "", ""] and
            _score_number(rows[-1][3]) is not None and _score_bracket_proof("（", rows[0][2]) is not None):
        return True
    if previous is None or previous.get("origin") != "native":
        return False
    prior_rows, page, prior_page = previous.get("rows"), block.get("page"), previous.get("page")
    if (not isinstance(prior_rows, list) or not prior_rows or not isinstance(prior_rows[0], list) or
            len(prior_rows[0]) != 4 or any(not isinstance(cell, str) for cell in prior_rows[0]) or
            _scoring_header(prior_rows[0]) is None or type(page) is not int or type(prior_page) is not int or
            page not in (prior_page, prior_page + 1)):
        return False
    parent = _scoring_grid(block, block.get("source_id"), require_header=False)
    # An independently merged instruction row explains its empty covered
    # cells. Malformed geometry provides no exemption; an open preceding
    # scoring row is separately guarded even when this table is independent.
    return not (parent is not None and 0 in parent[1])


def _scoring_continuations(document: dict, body: list, safe_skips: dict) -> list:
    results = []
    for index, left in enumerate(body):
        parent = _scoring_grid(left, document["source_id"], require_header=True)
        if parent is None or not all(_score_data_row(row) for row in left["rows"][1:]):
            continue
        left_row = len(left["rows"]) - 1
        if left_row in parent[1] or _score_opening(left["rows"][-1][2]) is None:
            continue
        following, skipped = index + 1, []
        while following < len(body) and body[following].get("kind") != "table":
            footer = _footer(body[following], safe_skips.get(body[following]["block_id"]), document["source_id"], left["page"])
            if footer is None:
                break
            skipped.append(footer)
            following += 1
        if following >= len(body):
            continue
        right = body[following]
        tail = _scoring_grid(right, document["source_id"], require_header=False)
        if (tail is None or not _score_tail_shape(right) or len(right["rows"]) != 2 or 0 in tail[1] or
                right["page"] != left["page"] + 1 or
                [block["ordinal"] for block in body[index:following + 1]] != list(range(left["ordinal"], right["ordinal"] + 1))):
            continue
        total = right["rows"][1]
        total_merge = [{"row": 1, "column": 0, "row_span": 1, "col_span": 3}]
        canonical_merges = [{"row": merge["row"], "column": merge["column"], "row_span": merge.get("row_span", 1),
                             "col_span": merge.get("col_span", 1)} for merge in right["merges"]]
        if total[:3] != ["合计", "", ""] or canonical_merges != total_merge or _score_number(total[3]) is None:
            continue
        scores = [_score_number(row[3]) for row in left["rows"][1:]]
        if [row[0] for row in left["rows"][1:]] != [str(value) for value in range(1, len(left["rows"]))] or sum(scores) != _score_number(total[3]):
            continue
        widths = _score_width_proof(left["column_widths"], right["column_widths"])
        brackets = _score_bracket_proof(left["rows"][-1][2], right["rows"][0][2])
        if widths is None or brackets is None:
            continue
        tables = [{**parent[0], "boundary_row": left_row, "boundary_cells_sha256": _hash(left["rows"][left_row])},
                  {**tail[0], "boundary_row": 0, "boundary_cells_sha256": _hash(right["rows"][0])}]
        quote = left["text"] + "\n" + right["text"]
        proof = {"rule": _SCORING_RULE, "source_id": document["source_id"], "source_sha256": document["sha256"],
                 "block_ids": [left["block_id"], right["block_id"]], "quote": quote, "quote_sha256": _text_hash(quote),
                 "tables": tables, "skipped_blocks": skipped, "column_alignment": widths, "parenthesis_closure": brackets,
                 "score_total_check": {"left_score_cells": [{"row": row, "column": 3, "raw_value": values[3]} for row, values in enumerate(left["rows"][1:], 1)],
                                       "right_total_cell": {"row": 1, "column": 3, "raw_value": total[3]}, "sum_matches_source_total": True},
                 "continuation": {"left_block_id": left["block_id"], "left_row": left_row, "right_block_id": right["block_id"], "right_row": 0,
                                  "clause_number": left["rows"][-1][0], "clause_name": left["rows"][-1][1],
                                  "scope": "boundary_rows_only; quote_preserves_both_complete_grids"}}
        results.append({**proof, "proof_sha256": _hash(proof)})
    return results


def build_table_continuations(document: dict, source_blocks: list[dict], safe_skips: dict) -> list[dict]:
    """Return reproducible two-table proofs, or no proof for unknown structure.

    ``source_blocks`` may be a bounded window, but every intervening ordinal
    must be present. ``safe_skips`` comes from the frozen reader footer proof;
    it is not a model-provided instruction. No block coordinates are needed.
    """
    source_id, source_hash = document.get("source_id"), document.get("sha256")
    if (document.get("format") != "pdf" or not isinstance(source_id, str) or not source_id or
            not isinstance(source_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", source_hash) or
            not isinstance(source_blocks, list) or not isinstance(safe_skips, dict) or
            any(not isinstance(block, dict) for block in source_blocks)):
        return []
    body = [block for block in source_blocks if block.get("source_id") == source_id]
    if (any(type(block.get("ordinal")) is not int or block["ordinal"] < 0 or
            not isinstance(block.get("block_id"), str) or not block["block_id"] for block in body) or
            len({block["block_id"] for block in body}) != len(body) or
            len({block["ordinal"] for block in body}) != len(body)):
        return []
    body.sort(key=lambda block: block["ordinal"])
    results = []
    for index, left in enumerate(body):
        left_grid = _grid(left, source_id, "left")
        if left_grid is None:
            continue
        following = index + 1
        skipped = []
        while following < len(body) and body[following].get("kind") != "table":
            footer = _footer(body[following], safe_skips.get(body[following]["block_id"]), source_id, left["page"])
            if footer is None:
                break
            skipped.append(footer)
            following += 1
        if following >= len(body):
            continue
        right = body[following]
        right_grid = _grid(right, source_id, "right")
        if (right_grid is None or right["page"] != left["page"] + 1 or
                left_grid["column_widths"] != right_grid["column_widths"] or
                [block["ordinal"] for block in body[index:following + 1]] != list(range(left["ordinal"], right["ordinal"] + 1))):
            continue
        quote = left["text"] + "\n" + right["text"]
        proof = {"rule": _RULE, "source_id": source_id, "source_sha256": source_hash,
                 "block_ids": [left["block_id"], right["block_id"]], "quote": quote, "quote_sha256": _text_hash(quote),
                 "tables": [left_grid, right_grid], "skipped_blocks": skipped,
                 "continuation": {"left_block_id": left["block_id"], "left_row": left_grid["boundary_row"],
                                  "right_block_id": right["block_id"], "right_row": 1,
                                  "clause_number": left["rows"][-1][0], "clause_name": left["rows"][-1][1],
                                  "scope": "boundary_rows_only; quote_preserves_both_complete_grids"}}
        results.append({**proof, "proof_sha256": _hash(proof)})
    results.extend(_scoring_continuations(document, body, safe_skips))
    results.sort(key=lambda proof: proof["tables"][0]["ordinal"])
    return results


def _row_parent(block: dict, source_id: str):
    """Validate a whole grid while keeping unrelated valid merges harmless."""
    rows, widths, merges = block.get("rows"), block.get("column_widths"), block.get("merges")
    page, ordinal = block.get("page"), block.get("ordinal")
    if (block.get("source_id") != source_id or block.get("kind") != "table" or
            block.get("origin") != "native" or block.get("read_disposition") or
            type(page) is not int or page < 1 or type(ordinal) is not int or ordinal < 0 or
            not isinstance(block.get("block_id"), str) or not block["block_id"] or
            not re.fullmatch(rf"pdf/page\[{page}\]/table\[\d+\]", str(block.get("locator", ""))) or
            not isinstance(rows, list) or len(rows) < 2 or
            any(not isinstance(row, list) or len(row) != 3 or any(not isinstance(cell, str) for cell in row) for row in rows) or
            rows[0] != _HEADER or not isinstance(widths, list) or len(widths) != 3 or
            any(type(width) not in (int, float) or not math.isfinite(width) or width <= 0 for width in widths) or
            not isinstance(merges, list) or block.get("text_basis") != "table_rows_tsv_v1" or
            not isinstance(block.get("header_rows", []), list) or
            any(type(row) is not int or not 0 <= row < len(rows) for row in block.get("header_rows", []))):
        return None
    text = "\n".join("\t".join(row) for row in rows)
    if block.get("text") != text:
        return None
    occupied, merged_rows = set(), set()
    for merge in merges:
        if not isinstance(merge, dict):
            return None
        r, c, rs, cs = (merge.get("row"), merge.get("column"), merge.get("row_span", 1), merge.get("col_span", 1))
        if (any(type(value) is not int for value in (r, c, rs, cs)) or min(r, c) < 0 or
                min(rs, cs) < 1 or rs * cs == 1 or r + rs > len(rows) or c + cs > 3 or r == 0):
            return None
        for row in range(r, r + rs):
            merged_rows.add(row)
            for column in range(c, c + cs):
                cell = (row, column)
                if cell in occupied or (cell != (r, c) and rows[row][column]):
                    return None
                occupied.add(cell)
    metadata = {"block_id": block["block_id"], "locator": block["locator"], "page": page, "ordinal": ordinal,
                "origin": "native", "text_basis": block["text_basis"], "text_sha256": _text_hash(text),
                "grid_sha256": _hash({"rows": rows, "merges": merges, "column_widths": widths,
                                      "header_rows": block.get("header_rows", [])}),
                "row_count": len(rows), "column_widths": list(widths), "header": list(_HEADER)}
    return metadata, merged_rows


def _row_part(block: dict, row: int) -> dict:
    start = sum(len("\t".join(value)) + 1 for value in block["rows"][:row])
    quote = "\t".join(block["rows"][row])
    cells, cursor = [], start
    for column, value in enumerate(block["rows"][row]):
        cells.append({"block_id": block["block_id"], "row": row, "column": column,
                      "start": cursor, "end": cursor + len(value)})
        cursor += len(value) + 1
    return {"block_id": block["block_id"], "locator": block["locator"], "row": row,
            "start": start, "end": start + len(quote), "quote": quote,
            "quote_sha256": _text_hash(quote), "cell_refs": cells}


def _unknown_row_tails(rows: list) -> set[int]:
    """Do not expose a row while later empty-key conditions remain unbound.

    Blank or merged separators do not prove a new clause. A later explicit
    numeric key and label is the only supported reset; no rows are combined.
    A leading continuation row can itself have an unsupported further tail.
    """
    anchor, reserved = None, set()
    for row, values in enumerate(rows[1:], 1):
        if _CLAUSE.fullmatch(values[0]) and values[1].strip():
            anchor = row
        elif not values[0].strip() and not values[1].strip() and values[2].strip():
            if anchor is not None:
                reserved.add(anchor)
            elif row == 1:
                anchor = row
    return reserved


def _build_front_matter_row_catalog(document: dict, source_blocks: list[dict], continuations: list[dict]) -> list[dict]:
    """Freeze exact three-column source rows, joining only proved continuation rows.

    Offsets in ``parts`` and ``cell_refs`` are relative to each original parent
    TSV. They are application-computed evidence, never model-selected slices.
    Unknown continuation structure still requires the caller's input guard.
    """
    source_id, source_hash = document.get("source_id"), document.get("sha256")
    if (document.get("format") != "pdf" or not isinstance(source_id, str) or not source_id or
            not isinstance(source_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", source_hash) or
            not isinstance(source_blocks, list) or not isinstance(continuations, list) or
            any(not isinstance(block, dict) for block in source_blocks)):
        return []
    body = [block for block in source_blocks if block.get("source_id") == source_id]
    if (any(type(block.get("ordinal")) is not int or block["ordinal"] < 0 or
            not isinstance(block.get("block_id"), str) or not block["block_id"] for block in body) or
            len({block["block_id"] for block in body}) != len(body) or
            len({block["ordinal"] for block in body}) != len(body)):
        return []
    body.sort(key=lambda block: block["ordinal"])
    blocks = {block["block_id"]: block for block in body}
    parents = {block["block_id"]: parent for block in body if (parent := _row_parent(block, source_id)) is not None}
    unknown_tails = {key: _unknown_row_tails(blocks[key]["rows"]) for key in parents}
    safe_skips, reserved = {}, set()
    for proof in continuations:
        if not isinstance(proof, dict):
            continue
        ids = proof.get("block_ids")
        if isinstance(ids, list) and len(ids) == 2 and all(isinstance(key, str) for key in ids):
            if ids[0] in parents:
                reserved.add((ids[0], len(blocks[ids[0]]["rows"]) - 1))
            reserved.add((ids[1], 1))
        for skip in proof.get("skipped_blocks", []) if isinstance(proof.get("skipped_blocks", []), list) else []:
            footer = skip.get("footer_proof") if isinstance(skip, dict) else None
            if isinstance(footer, dict) and isinstance(footer.get("block_id"), str):
                safe_skips[footer["block_id"]] = footer
    # Rebuild from frozen metadata so a stale or fabricated row/parent proof
    # cannot turn a half-clause into a selectable catalog entry.
    rebuilt = build_table_continuations(document, body, safe_skips)
    joined = {}
    for proof in rebuilt:
        if proof not in continuations:
            continue
        left_id, right_id = proof["block_ids"]
        if left_id not in parents or right_id not in parents:
            continue
        left_row, right_row = proof["continuation"]["left_row"], proof["continuation"]["right_row"]
        if (left_row in parents[left_id][1] or right_row in parents[right_id][1] or
                left_row in unknown_tails[left_id] or right_row in unknown_tails[right_id]):
            continue
        joined[(left_id, left_row)] = (right_id, right_row, proof["proof_sha256"])
    # A leading empty-key row is never an independent clause. Conservatively
    # reserve the preceding page's last row even when its join is unproved;
    # the caller must report that unsupported boundary rather than select half.
    tables = [block for block in body if block["block_id"] in parents]
    for left, right in zip(tables, tables[1:]):
        if (right["page"] == left["page"] + 1 and right["rows"][1][:2] == ["", ""] and
                right["rows"][1][2].strip()):
            reserved.add((left["block_id"], len(left["rows"]) - 1))
            reserved.add((right["block_id"], 1))
    results = []
    for block in tables:
        key = block["block_id"]
        metadata, merged_rows = parents[key]
        for row, values in enumerate(block["rows"][1:], 1):
            if (row in merged_rows or row in unknown_tails[key] or
                    not _CLAUSE.fullmatch(values[0]) or not values[1].strip()):
                continue
            pair = joined.get((key, row))
            if (key, row) in reserved and pair is None:
                continue
            parts, parent_tables = [_row_part(block, row)], [metadata]
            if pair:
                right_id, right_row, continuation_hash = pair
                parts.append(_row_part(blocks[right_id], right_row))
                parent_tables.append(parents[right_id][0])
            quote = "\n".join(part["quote"] for part in parts)
            ids = [table["block_id"] for table in parent_tables]
            proof = {"rule": "native_pdf_front_matter_row_v1", "source_id": source_id, "source_sha256": source_hash,
                     "block_id": key, "locator": block["locator"], "block_ids": ids,
                     "quote": quote, "quote_sha256": _text_hash(quote),
                     "parent_quote_sha256": _text_hash("\n".join(blocks[identity]["text"] for identity in ids)),
                     "tables": parent_tables, "parts": parts,
                     "cell_refs": [cell for part in parts for cell in part["cell_refs"]]}
            if pair:
                proof["continuation_proof_sha256"] = continuation_hash
            proof_hash = _hash(proof)
            results.append({"table_row_id": "TROW-" + proof_hash[:24], **proof, "proof_sha256": proof_hash})
    return results


def _scoring_body(document: dict, source_blocks: list[dict]):
    if (document.get("format") != "pdf" or not isinstance(document.get("source_id"), str) or not document["source_id"] or
            not isinstance(document.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", document["sha256"]) or
            not isinstance(source_blocks, list) or any(not isinstance(block, dict) for block in source_blocks)):
        return None
    body = [block for block in source_blocks if block.get("source_id") == document["source_id"]]
    if (any(type(block.get("ordinal")) is not int or block["ordinal"] < 0 or
            not isinstance(block.get("block_id"), str) or not block["block_id"] for block in body) or
            len({block["block_id"] for block in body}) != len(body) or
            len({block["ordinal"] for block in body}) != len(body)):
        return None
    return sorted(body, key=lambda block: block["ordinal"])


def _rebuild_given_continuations(document: dict, body: list, continuations: list):
    safe_skips = {}
    for proof in continuations:
        if not isinstance(proof, dict):
            continue
        skipped = proof.get("skipped_blocks", [])
        for skip in skipped if isinstance(skipped, list) else []:
            footer = skip.get("footer_proof") if isinstance(skip, dict) else None
            if isinstance(footer, dict) and isinstance(footer.get("block_id"), str):
                safe_skips[footer["block_id"]] = footer
    return [proof for proof in build_table_continuations(document, body, safe_skips) if proof in continuations]


def _build_scoring_row_catalog(document: dict, source_blocks: list[dict], continuations: list[dict]):
    body = _scoring_body(document, source_blocks)
    if body is None or not isinstance(continuations, list):
        return []
    blocks = {block["block_id"]: block for block in body}
    parents = {block["block_id"]: parent for block in body
               if (parent := _scoring_grid(block, document["source_id"], require_header=True)) is not None}
    proofs = [proof for proof in _rebuild_given_continuations(document, body, continuations) if proof["rule"] == _SCORING_RULE]
    joined = {}
    for proof in proofs:
        left_id, right_id = proof["block_ids"]
        parent = _scoring_grid(blocks[right_id], document["source_id"], require_header=False)
        if left_id not in parents or parent is None:
            continue
        parents[right_id] = parent
        joined[(left_id, proof["continuation"]["left_row"])] = (right_id, 0, proof["proof_sha256"])
    reserved = set()
    tables = [block for block in body if block.get("kind") == "table"]
    for left, right in zip(tables, tables[1:]):
        if (left["block_id"] in parents and right.get("page") == left.get("page", -1) + 1 and
                _score_tail_shape(right) and _score_tail_has_context(right, left)):
            reserved.add((left["block_id"], len(left["rows"]) - 1))
    results = []
    for block in tables:
        key = block["block_id"]
        if key not in parents or parents[key][0]["header_row"] is None:
            continue
        metadata, merged_rows = parents[key]
        unknown = _unknown_row_tails(block["rows"])
        for row, values in enumerate(block["rows"][1:], 1):
            if row in merged_rows or row in unknown or not _score_data_row(values):
                continue
            pair = joined.get((key, row))
            if pair is None and ((key, row) in reserved or _score_opening(values[2]) is not None):
                continue
            parts, parent_tables = [_row_part(block, row)], [metadata]
            if pair:
                right_id, right_row, continuation_hash = pair
                parts.append(_row_part(blocks[right_id], right_row))
                parent_tables.append(parents[right_id][0])
            ids = [table["block_id"] for table in parent_tables]
            quote = "\n".join(part["quote"] for part in parts)
            proof = {"rule": "native_pdf_scoring_row_v1", "source_id": document["source_id"], "source_sha256": document["sha256"],
                     "block_id": key, "locator": block["locator"], "block_ids": ids, "quote": quote, "quote_sha256": _text_hash(quote),
                     "parent_quote_sha256": _text_hash("\n".join(blocks[identity]["text"] for identity in ids)),
                     "tables": parent_tables, "parts": parts, "cell_refs": [cell for part in parts for cell in part["cell_refs"]]}
            if pair:
                proof["continuation_proof_sha256"] = continuation_hash
            proof_hash = _hash(proof)
            results.append({"table_row_id": "TROW-" + proof_hash[:24], **proof, "proof_sha256": proof_hash})
    return results


def build_table_row_catalog(document: dict, source_blocks: list[dict], continuations: list[dict]) -> list[dict]:
    """Original front-matter rows plus explicitly headered/proved scoring rows."""
    rows = _build_front_matter_row_catalog(document, source_blocks, continuations)
    rows.extend(_build_scoring_row_catalog(document, source_blocks, continuations))
    rows.sort(key=lambda row: (row["tables"][0]["ordinal"], row["parts"][0]["row"]))
    return rows


def scoring_table_continuation_issues(document: dict, source_blocks: list[dict], continuations: list[dict]) -> list[dict]:
    """Return suspicious native scoring tails lacking a reproducible full proof.

    Callers must pass full proofs from ``build_table_continuations``, not the
    compact model catalog. A nonempty result is input uncertainty to reject or
    investigate before a call; it is not evidence that a table is complete.
    Sparse instruction rows require nearby scoring-source or genuine closing
    continuation evidence. An all-empty first row (for example a later total)
    is not a tail.
    """
    body = _scoring_body(document, source_blocks)
    if body is None or not isinstance(continuations, list):
        return []
    proofs = [proof for proof in _rebuild_given_continuations(document, body, continuations) if proof["rule"] == _SCORING_RULE]
    left_ids = {proof["block_ids"][0] for proof in proofs}
    right_ids = {proof["block_ids"][1] for proof in proofs}
    issues = []
    previous = None
    for block in body:
        if block.get("kind") != "table":
            continue
        prior, previous = previous, block
        if block.get("origin") != "native":
            continue
        rows = block.get("rows")
        side = None
        if (_score_tail_shape(block) and _score_tail_has_context(block, prior) and
                block["block_id"] not in right_ids):
            side, row, reason = "right", 0, "headerless_standard_text_without_proven_scoring_continuation"
        elif (isinstance(rows, list) and len(rows) >= 2 and
              all(isinstance(values, list) and len(values) == 4 and all(isinstance(cell, str) for cell in values) for values in rows) and
              _scoring_header(rows[0]) and _score_data_row(rows[-1]) and _score_opening(rows[-1][2]) is not None and
              block["block_id"] not in left_ids):
            side, row, reason = "left", len(rows) - 1, "open_parenthesis_without_proven_scoring_continuation"
        if side:
            issues.append({"source_id": document["source_id"], "source_sha256": document["sha256"],
                           "block_id": block["block_id"], "locator": block.get("locator"), "page": block.get("page"),
                           "row": row, "side": side, "reason": reason})
    return issues
