"""Reviewed three-column scoring rows for an immutable failed-window replay."""
from __future__ import annotations

import math
import re

from .table_continuations import _footer, _hash, _row_part, _text_hash


SCORING_ROW_SOURCE_CONFIG_KEY = "scoring_row_source_resolution"
_RULE = "reviewed_native_pdf_three_column_scoring_rows_v1"
_HEADER = ["评价项目", "评价内容", "分值"]
_KEYS = {"source_id", "source_sha256", "window_id", "raw_candidate_sha256", "table_block_ids", "source_proof_sha256"}


def normalize_scoring_row_source_config(value: dict) -> dict:
    if isinstance(value, dict) and set(value) == _KEYS:
        ids = value["table_block_ids"]
        strings = [value[key] for key in _KEYS - {"table_block_ids"}]
        if (isinstance(ids, list) and len(ids) == 3 and
                all(isinstance(item, str) and 1 <= len(item) <= 120 for item in [*ids, *strings]) and len(set(ids)) == 3 and
                all(re.fullmatch(r"[a-f0-9]{64}", value[key]) for key in
                    ("source_sha256", "raw_candidate_sha256", "source_proof_sha256"))):
            return {**value, "table_block_ids": list(ids)}
    raise ValueError("SCORING_ROW_SOURCE_CONFIGURATION_INVALID")


def _box(value):
    return (isinstance(value, list) and len(value) == 4 and
            all(type(number) in (int, float) and math.isfinite(number) for number in value) and
            0 <= value[0] < value[2] and 0 <= value[1] < value[3])


def _parent(table, row_count, *, header, total):
    rows, widths = table.get("rows"), table.get("column_widths")
    expected_merges = [{"row": row_count - 1, "column": 0, "row_span": 1, "col_span": 2}] if total else []
    if (table.get("kind") != "table" or table.get("text_basis") != "table_rows_tsv_v1" or
            not isinstance(rows, list) or len(rows) != row_count or
            any(not isinstance(row, list) or len(row) != 3 or
                any(not isinstance(cell, str) for cell in row) for row in rows) or
            header and rows[0] != _HEADER or total and rows[-1][:2] != ["合计", ""] or
            table.get("merges") != expected_merges or table.get("header_rows", []) != [] or
            table.get("text") != "\n".join("\t".join(row) for row in rows) or
            not isinstance(widths, list) or len(widths) != 3 or
            any(type(width) not in (int, float) or not math.isfinite(width) or width <= 0 for width in widths)):
        return None
    x0, _, x1, _ = table["bbox"]
    if abs(math.fsum(widths) - (x1 - x0)) > .25:
        return None
    return {**{key: table[key] for key in ("block_id", "locator", "page", "ordinal", "origin", "text_basis", "bbox")},
            "text_sha256": _text_hash(table["text"]), "row_count": row_count, "column_widths": widths,
            "grid_sha256": _hash({"rows": rows, "column_widths": widths, "merges": expected_merges, "header_rows": []}),
            "column_edges_points": [x0, x0 + widths[0], x1 - widths[-1], x1]}


def _score(row, *, split=False):
    label, content, score = row
    if not content.strip() or not re.fullmatch(r"[1-9][0-9]?", score):
        return None
    label = label.replace("\n", "")
    complete_label = re.fullmatch(rf".+（{score}分）", label)
    if (split and label == "QHSE管理") or (not split and complete_label):
        return int(score)
    return None


def build_scoring_row_source_proof(config: dict, native_blocks: list, footer_evidence: dict) -> dict | None:
    """Rebuild row evidence from original grids and one demonstrably split row."""
    if not isinstance(native_blocks, list) or len(native_blocks) != 6 or any(not isinstance(block, dict) for block in native_blocks):
        return None
    commercial_heading, commercial, technical_heading, left, footer, right = native_blocks
    source_id, page = config["source_id"], left.get("page")
    if (type(page) is not int or page < 1 or [block.get("page") for block in native_blocks] != [page] * 5 + [page + 1] or
            [block.get("block_id") for block in (commercial, left, right)] != config["table_block_ids"] or
            any(block.get("source_id") != source_id or block.get("origin") != "native" or block.get("read_disposition") or
                block.get("illustration_source_use") or type(block.get("ordinal")) is not int or
                type(block.get("unit_ordinal")) is not int or not _box(block.get("bbox")) or
                not isinstance(block.get("block_id"), str) or not block["block_id"] or
                not isinstance(block.get("unit_id"), str) or not block["unit_id"] for block in native_blocks) or
            len({block["block_id"] for block in native_blocks}) != len(native_blocks) or
            len({block["unit_id"] for block in native_blocks[:-1]}) != 1 or left["unit_id"] == right["unit_id"] or
            [block["ordinal"] for block in native_blocks] != list(range(commercial_heading["ordinal"], right["ordinal"] + 1)) or
            [block["unit_ordinal"] for block in native_blocks] != list(range(commercial_heading["unit_ordinal"], footer["unit_ordinal"] + 1)) + [0] or
            commercial_heading.get("text") != "商务评价条款" or technical_heading.get("text") != "技术评价条款" or
            any(block.get("kind") != "paragraph" or not re.fullmatch(rf"pdf/page\[{page}\]/line\[\d+\]", str(block.get("locator")))
                for block in (commercial_heading, technical_heading)) or
            any(not re.fullmatch(rf"pdf/page\[{page}\]/table\[\d+\]", str(table.get("locator"))) for table in (commercial, left)) or
            right.get("locator") != f"pdf/page[{page + 1}]/table[0]"):
        return None
    if not isinstance(footer_evidence, dict) or footer.get("bbox") != footer_evidence.get("bbox"):
        return None
    skip = _footer(footer, footer_evidence, source_id, page)
    grids = [_parent(commercial, 4, header=True, total=True), _parent(left, 5, header=True, total=False),
             _parent(right, 4, header=False, total=True)]
    if skip is None or any(grid is None for grid in grids):
        return None
    deltas = [abs(a - b) for a, b in zip(grids[1]["column_edges_points"], grids[2]["column_edges_points"])]
    if (max(deltas) > 1 or not 0 < footer["bbox"][1] - left["bbox"][3] <= 60 or
            not 0 < right["bbox"][1] <= .15 * footer["bbox"][1] or any(
                not 0 <= table["bbox"][1] - heading["bbox"][3] <= 12 for heading, table in
                ((commercial_heading, commercial), (technical_heading, left)))):
        return None
    commercial_scores = [_score(row) for row in commercial["rows"][1:-1]]
    technical_scores = [_score(row) for row in [*left["rows"][1:-1], *right["rows"][1:-1]]]
    split_score = _score(left["rows"][-1], split=True)
    tail_label, tail_content, tail_score = right["rows"][0]
    if (None in commercial_scores or None in technical_scores or split_score is None or tail_score != "" or
            tail_label.replace("\n", "") != f"方案（{split_score}分）" or not left["rows"][-1][1].endswith("，") or
            not tail_content.startswith("符合") or not tail_content.strip().endswith("。") or
            commercial["rows"][-1][2] != str(sum(commercial_scores)) or
            right["rows"][-1][2] != str(sum(technical_scores) + split_score)):
        return None
    boundary = {"rule": "same_grid_split_label_and_sentence_v1", "column_deltas_points": deltas,
                "footer": skip, "parts": [_row_part(left, 4), _row_part(right, 0)]}
    boundary_hash = _hash(boundary)
    row_slots = [[(0, 1)], [(0, 2)], [(1, 1)], [(1, 2)], [(1, 3)], [(1, 4), (2, 0)], [(2, 1)], [(2, 2)]]
    tables = (commercial, left, right)
    rows = []
    for slots in row_slots:
        members = [tables[table] for table, _ in slots]
        parts = [_row_part(tables[table], row) for table, row in slots]
        first = members[0]
        quote = "\n".join(part["quote"] for part in parts)
        row_scope = "commercial" if slots[0][0] == 0 else "technical"
        row = {"rule": _RULE, "source_id": source_id, "source_sha256": config["source_sha256"],
               "block_id": first["block_id"], "locator": first["locator"], "block_ids": [block["block_id"] for block in members],
               "quote": quote, "quote_sha256": _text_hash(quote), "parts": parts,
               "parent_quote_sha256": _text_hash("\n".join(block["text"] for block in members)),
               "tables": [grids[table] for table, _ in slots], "cell_refs": [cell for part in parts for cell in part["cell_refs"]],
               "scoring_scope": row_scope}
        if len(slots) == 2:
            row.update(continuation_proof_sha256=boundary_hash, skipped_block_ids=[footer["block_id"]])
        proof_hash = _hash(row)
        rows.append({**row, "table_row_id": "TROW-" + proof_hash[:24], "proof_sha256": proof_hash})
    proof = {"rule": _RULE, "source_id": source_id, "source_sha256": config["source_sha256"], "window_id": config["window_id"],
             "native_blocks_sha256": _hash(native_blocks), "split_row_evidence": boundary, "split_row_sha256": boundary_hash,
             "rows": rows}
    return {**proof, "proof_sha256": _hash(proof)}


def resolve_scoring_row_selectors(config: dict, raw: dict, proof: dict) -> dict | None:
    """Bind the reviewed eight selectors in the pinned original candidate order."""
    requirements = raw.get("requirements")
    if not isinstance(requirements, list) or len(requirements) < len(proof["rows"]):
        return None
    commercial, *technical = config["table_block_ids"]
    expected_categories = ["commercial"] * 2 + ["technical"] * 5 + ["qualification"]
    expected_modes = ["attachment"] * 2 + ["writing"] * 5 + ["attachment"]
    changes = []
    for index, requirement in enumerate(requirements[:len(proof["rows"])]):
        row = proof["rows"][index]
        if not isinstance(requirement, dict) or requirement.get("mandatory") is not False:
            return None
        if requirement.get("category") != expected_categories[index] or requirement.get("response_mode") != expected_modes[index]:
            return None
        selected = requirement.get("citation")
        # All eight overbroad selections came from the original block-only
        # contract. No previously supplied precise ID or coordinate is repaired.
        if selected != {"block_ids": [commercial] if index < 2 else technical}:
            return None
        citation = {key: row[key] for key in ("source_id", "block_id", "locator", "block_ids", "table_row_id")}
        changes.append({"requirement_index": index, "original_citation": selected, "citation": citation,
                        "original_category": requirement["category"], "category": row["scoring_scope"],
                        "mandatory": False, "basis": "reviewed_scoring_row_only",
                        "association_basis": ("reviewed_business_score_row_order_with_shared_contract_evidence" if index < 2
                                              else "reviewed_technical_score_row_order")})
    return {"basis": "application_reviewed_source_row_selection", "model_intent_inferred": False,
            "raw_candidate_sha256": config["raw_candidate_sha256"],
            "proof": proof, "changes": changes}
