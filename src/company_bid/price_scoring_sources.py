"""Frozen source proof for a split, three-column price evaluation rule."""
from __future__ import annotations

import math
import re
from decimal import Decimal

from .table_continuations import _footer, _hash, _text_hash


PRICE_SCORING_CONFIG_KEY = "price_scoring_source_resolution"
_KEYS = {"source_id", "source_sha256", "heading_block_id", "table_block_id",
         "continuation_block_id", "raw_candidate_sha256"}
_RULE = "native_pdf_price_scoring_source_resolution_v1"


def normalize_price_scoring_config(value: dict) -> dict:
    if (not isinstance(value, dict) or set(value) != _KEYS or
            any(not isinstance(item, str) or not 1 <= len(item) <= 120 for item in value.values()) or
            any(not re.fullmatch(r"[a-f0-9]{64}", value[key]) for key in ("source_sha256", "raw_candidate_sha256")) or
            len({value[key] for key in ("heading_block_id", "table_block_id", "continuation_block_id")}) != 3):
        raise ValueError("PRICE_SCORING_SOURCE_CONFIGURATION_INVALID")
    return dict(value)


def _box(value):
    return (isinstance(value, list) and len(value) == 4 and
            all(type(n) in (int, float) and math.isfinite(n) for n in value) and
            0 <= value[0] < value[2] and 0 <= value[1] < value[3])


def _grid(block: dict, *, continuation: bool) -> list[float] | None:
    rows, widths = block.get("rows"), block.get("column_widths")
    if (block.get("kind") != "table" or block.get("text_basis") != "table_rows_tsv_v1" or
            not isinstance(rows, list) or len(rows) != 2 or
            any(not isinstance(row, list) or len(row) != 3 or
                any(not isinstance(cell, str) or len(cell) > 1500 for cell in row) for row in rows) or
            not isinstance(widths, list) or len(widths) != 3 or
            any(type(width) not in (int, float) or not math.isfinite(width) or width <= 0 for width in widths) or
            block.get("merges") != ([{"row": 1, "column": 0, "row_span": 1, "col_span": 2}] if continuation else []) or
            block.get("header_rows", []) != [] or block.get("text") != "\n".join("\t".join(row) for row in rows)):
        return None
    x0, _, x1, _ = block["bbox"]
    if abs(math.fsum(widths) - (x1 - x0)) > .25:
        return None
    return [x0, x0 + widths[0], x1 - widths[-1], x1]


# Fingerprints of the visually reviewed wording and formula structure, with
# numeric tokens removed. Only those two specific cell templates are proved.
# Numeric parameters are checked separately against each other and the scores.
_NUMBER = r"[1-9][0-9]{0,2}(?:\.[0-9]{1,3})?|0\.[0-9]{1,3}"
_NUMERIC_TOKEN = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_START_TEMPLATE = "86e5e4667149a52ba95e8c0b2aaed4d858da8106947fc572fd30cc62d5d9ff82"
_END_TEMPLATE = "7d40ebfd0e137aa0b60cae57f2d32f6d1031e5c18238beb98c47b8f36ad6b088"


def _numeric_parameters(cell: str, template_sha256: str) -> list[str] | None:
    # Match native PDF folds inside one cell. Original citation text is intact.
    text = cell.replace("\n", "")
    if _text_hash(_NUMERIC_TOKEN.sub("#", text)) != template_sha256:
        return None
    return _NUMERIC_TOKEN.findall(text)


def _terms(left: list, right: list) -> dict | None:
    if left[0] != ["评价项目", "评价内容", "分值"] or right[0][0] != "" or right[0][2] != "" or right[1][:2] != ["合计", ""]:
        return None
    label, score = left[1][0].replace("\n", ""), left[1][2]
    if not re.fullmatch(_NUMBER, score) or label != f"报价得分（{score}分）" or right[1][2] != score:
        return None
    head = _numeric_parameters(left[1][1], _START_TEMPLATE)
    tail = _numeric_parameters(right[0][1], _END_TEMPLATE)
    if head is None or tail is None or len(head) != 15 or len(tail) != 3:
        return None
    (first, threshold, threshold_repeat, second, deviation_multiplier, third, equal, above_increment, higher,
     below_increment, lower, floor, above_base, above_multiplier, above_factor) = head
    below_base, below_multiplier, below_factor = tail
    if ([first, second, third] != ["1", "2", "3"] or above_increment != "1" or below_increment != "1" or
            [deviation_multiplier, above_multiplier, below_multiplier] != ["100", "100", "100"] or
            threshold != threshold_repeat or not re.fullmatch(r"[1-9][0-9]?", threshold) or int(threshold) < 3 or
            any(not re.fullmatch(_NUMBER, value) for value in (equal, higher, lower, floor, above_base, above_factor, below_base, below_factor)) or
            not Decimal(equal) == Decimal(above_base) == Decimal(below_base) == Decimal(score) or
            Decimal(higher) != Decimal(above_factor) or Decimal(lower) != Decimal(below_factor) or
            not Decimal(higher) > Decimal(lower) > 0 or not 0 < Decimal(floor) < Decimal(score)):
        return None
    return {"basis": "tax_exclusive", "trim_at_valid_quote_count": int(threshold),
            "trimmed_highest_count": 1, "trimmed_lowest_count": 1,
            "below_threshold": "arithmetic_mean", "at_benchmark_points": score,
            "above_benchmark_deduction_per_percent": higher, "below_benchmark_deduction_per_percent": lower,
            "minimum_points": floor, "rounding": {"mode": "half_up", "decimal_places": 2},
            "total_points": right[1][2]}


def prove_price_scoring_source(config: dict, native_blocks: list, footer_evidence: dict) -> dict | None:
    """Require an exact source scope, consecutive geometry and complementary formulas."""
    if not isinstance(native_blocks, list) or len(native_blocks) != 4 or any(not isinstance(block, dict) for block in native_blocks):
        return None
    heading, left, footer, right = native_blocks
    source_id, page = config["source_id"], heading.get("page")
    if (type(page) is not int or page < 1 or
            [block.get("block_id") for block in (heading, left, right)] !=
            [config[key] for key in ("heading_block_id", "table_block_id", "continuation_block_id")] or
            [block.get("page") for block in native_blocks] != [page, page, page, page + 1] or
            any(block.get("source_id") != source_id or block.get("origin") != "native" or
                block.get("read_disposition") or block.get("illustration_source_use") or not _box(block.get("bbox")) or
                type(block.get("ordinal")) is not int or type(block.get("unit_ordinal")) is not int or
                not isinstance(block.get("unit_id"), str) or not block["unit_id"] or
                not isinstance(block.get("block_id"), str) or not block["block_id"] for block in native_blocks) or
            len({block["block_id"] for block in native_blocks}) != 4 or
            [block["ordinal"] for block in native_blocks] != list(range(heading["ordinal"], heading["ordinal"] + 4)) or
            [block["unit_ordinal"] for block in native_blocks] != list(range(heading["unit_ordinal"], heading["unit_ordinal"] + 3)) + [0] or
            len({block["unit_id"] for block in native_blocks[:3]}) != 1 or right["unit_id"] == left["unit_id"] or
            heading.get("kind") != "paragraph" or heading.get("text") != "价格评价条款" or
            not re.fullmatch(rf"pdf/page\[{page}\]/line\[\d+\]", str(heading.get("locator"))) or
            not re.fullmatch(rf"pdf/page\[{page}\]/table\[\d+\]", str(left.get("locator"))) or
            right.get("locator") != f"pdf/page[{page + 1}]/table[0]"):
        return None
    if not isinstance(footer_evidence, dict) or footer.get("bbox") != footer_evidence.get("bbox"):
        return None
    skipped = _footer(footer, footer_evidence, source_id, page)
    if skipped is None:
        return None
    a, b = _grid(left, continuation=False), _grid(right, continuation=True)
    if a is None or b is None:
        return None
    deltas = [abs(x - y) for x, y in zip(a, b)]
    heading_box, left_box, footer_box, right_box = (block["bbox"] for block in native_blocks)
    heading_shift = abs((heading_box[0] + heading_box[2] - left_box[0] - left_box[2]) / 2)
    if (max(deltas) > 1 or heading_shift > 2 or not 0 <= left_box[1] - heading_box[3] <= 12 or
            not 0 < footer_box[1] - left_box[3] <= 60 or not 0 < right_box[1] <= .15 * footer_box[1]):
        return None
    terms = _terms(left["rows"], right["rows"])
    if terms is None:
        return None
    members = (heading, left, right)
    member_ids = [block["block_id"] for block in members]
    quote = "\n".join(block["text"] for block in members)
    table_quote = left["text"] + "\n" + right["text"]
    tables = [{"block_id": block["block_id"], "locator": block["locator"], "page": block["page"],
               "text_sha256": _text_hash(block["text"]),
               "grid_sha256": _hash({key: block.get(key, []) for key in ("rows", "merges", "column_widths", "header_rows")})}
              for block in (left, right)]
    proof = {"rule": _RULE, "source_id": source_id, "source_sha256": config["source_sha256"],
             "block_ids": member_ids, "quote_sha256": _text_hash(quote),
             "heading_text_sha256": _text_hash(heading["text"]), "tables": tables, "skipped_blocks": [skipped],
             "geometry": {"bounding_boxes": [block["bbox"] for block in native_blocks],
                          "column_edges_points": [a, b], "absolute_column_deltas_points": deltas,
                          "maximum_allowed_column_delta_points": 1},
             "cell_template_sha256": [_START_TEMPLATE, _END_TEMPLATE],
             "quoted_evaluation_cells": [{"block_id": left["block_id"], "row": 1, "column": 1},
                                         {"block_id": right["block_id"], "row": 0, "column": 1}],
             "terms": terms}
    proof_hash = _hash(proof)
    return {**proof, "proof_sha256": proof_hash,
            "table_continuation": {"rule": _RULE, "source_id": source_id, "source_sha256": config["source_sha256"],
                                   "block_ids": member_ids[1:], "quote": table_quote,
                                   "quote_sha256": _text_hash(table_quote), "proof_sha256": proof_hash}}
