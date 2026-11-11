"""Conservative source proofs for a native PDF table split across two pages.

The quote always contains both complete frozen grids. Boundary-row metadata
only explains the join; it never activates an unchecked option, joins cells,
or claims that unrelated rows belong to the continuing clause.
"""
from __future__ import annotations

import hashlib
import json
import math
import re


_HEADER = ["条款号", "条款名称", "编列内容"]
_RULE = "native_pdf_table_continuation_v1"
_CLAUSE = re.compile(r"[1-9]\d*(?:\.\d+)*(?:[（(]\d+[）)])?")


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
    return results
