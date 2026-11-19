"""Source proof for the reviewed three-page preliminary response form."""
from __future__ import annotations

import re

from .price_scoring_sources import _box
from .table_continuations import _footer, _hash, _row_part, _text_hash


RESPONSE_REVIEW_TABLE_CONFIG_KEY = "response_review_table_source_resolution"
RESPONSE_REVIEW_TABLE_RULE = "native_pdf_preliminary_response_table_v1"
RESPONSE_REVIEW_TABLE_TITLE = "1.初步评审应答表"
_KEYS = {"source_id", "source_sha256", "window_id", "physical_page", "raw_candidate_sha256",
         "source_proof_sha256", "cover_owner_window_id", "letter_continuation_owner_window_id"}


def normalize_response_review_table_config(value):
    if (not isinstance(value, dict) or set(value) != _KEYS or
            any(not isinstance(value[k], str) or not 1 <= len(value[k]) <= 120 for k in _KEYS - {"physical_page"}) or
            any(not re.fullmatch(r"[a-f0-9]{64}", value[k]) for k in
                ("source_sha256", "raw_candidate_sha256", "source_proof_sha256")) or
            type(value["physical_page"]) is not int or value["physical_page"] < 2 or
            len({value[k] for k in ("window_id", "cover_owner_window_id", "letter_continuation_owner_window_id")}) != 3):
        raise ValueError("RESPONSE_REVIEW_TABLE_CONFIGURATION_INVALID")
    return dict(value)


def _native_scope(native, source_id, page):
    layouts = ([f"line[{i}]" for i in range(9)], ["line[0]", "table[0]", "line[1]"], ["table[0]", "line[0]"],
               ["table[0]", "line[0]", "line[1]", "line[2]"], [f"line[{i}]" for i in range(19)], ["line[0]"])
    locators = [(p, i, locator) for p, layout in enumerate(layouts, page - 1) for i, locator in enumerate(layout)]
    if (not isinstance(native, list) or len(native) != len(locators) or
            any(not isinstance(block, dict) for block in native) or
            type(native[0].get("ordinal")) is not int or native[0]["ordinal"] < 0 or
            any(not isinstance(block.get(key), str) or not block[key] for block in native for key in ("unit_id", "block_id")) or
            len({block["block_id"] for block in native}) != len(native)):
        return False
    first = native[0]["ordinal"]
    for offset, (block, (p, i, locator)) in enumerate(zip(native, locators, strict=True)):
        if (block.get("source_id") != source_id or block.get("origin") != "native" or block.get("page") != p or
                block.get("locator") != f"pdf/page[{p}]/{locator}" or block.get("ordinal") != first + offset or
                block.get("unit_ordinal") != i or block.get("kind") != ("table" if locator.startswith("table") else "paragraph") or
                block.get("read_disposition") or block.get("illustration_source_use") or
                not _box(block.get("bbox")) or not isinstance(block.get("text"), str)):
            return False
    return len({block["unit_id"] for block in native}) == len({(block["page"], block["unit_id"]) for block in native}) == len(layouts)


def response_review_native_blocks(blocks, source_id, page):
    return [block for block in blocks if block["source_id"] == source_id and type(block.get("page")) is int and
            (page - 1 <= block["page"] <= page + 3 or block["locator"] == f"pdf/page[{page + 4}]/line[0]")]


def _member(block, role):
    return {key: block[key] for key in ("block_id", "locator", "ordinal", "unit_id", "page", "bbox")} | {
        "role": role, "text_sha256": _text_hash(block["text"])}


def _table_metadata(table, merges, count):
    rows, widths = table.get("rows"), table.get("column_widths")
    if (table.get("text_basis") != "table_rows_tsv_v1" or not isinstance(rows, list) or len(rows) != count or
            any(not isinstance(row, list) or len(row) != 5 or not all(isinstance(cell, str) for cell in row) for row in rows) or
            any(row[3:] != ["", ""] for row in rows[int(count == 9):]) or
            table.get("header_rows") != [] or table.get("merges") != merges or
            table["text"] != "\n".join("\t".join(row) for row in rows) or not isinstance(widths, list) or len(widths) != 5 or
            any(type(width) not in (int, float) or not 24 <= width <= 260 for width in widths) or
            abs(sum(widths) - (table["bbox"][2] - table["bbox"][0])) > .25):
        return None
    return {**_member(table, "preliminary_response_table"), "row_count": count, "column_widths": widths,
            "grid_sha256": _hash({key: table[key] for key in ("rows", "merges", "column_widths", "header_rows")})}


def _merge(row, column, row_span, col_span=1):
    return {"row": row, "column": column, "row_span": row_span, "col_span": col_span}


def _review_rows(tables, metadata, source, groups):
    result = []
    for role, positions in groups:
        parts = [_row_part(tables[t], r) for t, r in positions]
        parents = list(dict.fromkeys(t for t, _ in positions))
        quote = "\n".join(part["quote"] for part in parts)
        first = tables[parents[0]]
        row = source | {"rule": "native_pdf_reviewed_preliminary_response_rows_v1", "role": role,
                       "block_id": first["block_id"], "locator": first["locator"],
                       "block_ids": [tables[t]["block_id"] for t in parents],
                       "quote": quote, "quote_sha256": _text_hash(quote),
                       "parent_quote_sha256": _text_hash("\n".join(tables[t]["text"] for t in parents)),
                       "tables": [metadata[t] for t in parents], "parts": parts,
                       "cell_refs": [cell for part in parts for cell in part["cell_refs"]]}
        proof_hash = _hash(row)
        result.append({**row, "table_row_id": "TROW-" + proof_hash[:24], "proof_sha256": proof_hash})
    return result


def build_response_review_table_proof(config, native, footer_proofs):
    """Rebuild the bounded chapter, its actual split row, cover and adjacent letter."""
    source_id, page = config["source_id"], config["physical_page"]
    if not _native_scope(native, source_id, page) or not isinstance(footer_proofs, dict):
        return None
    cover, title, first, _, middle, _, last, stamp, date, _ = native[:9], *native[9:18]
    letter, following = native[18:37], native[37]
    footers = [native[index] for index in (8, 11, 13, 17, 36)]
    skipped = [_footer(block, footer_proofs.get(block["block_id"]), source_id, block["page"]) for block in footers]
    if any(proof is None or footer_proofs[block["block_id"]].get("bbox") != block["bbox"]
           for block, proof in zip(footers, skipped, strict=True)):
        return None
    numbers = [entry["footer_proof"]["printed_page"] for entry in skipped]
    if numbers != list(range(numbers[0], numbers[0] + len(numbers))):
        return None
    if (cover[0]["text"] != "响应文件格式" or re.sub(r"\s+", "", cover[4]["text"]) != "响应文件" or
            cover[5]["text"] != "响应人： （盖单位章）" or cover[6]["text"] != "法定代表人或其委托代理人： （签字或盖章）" or
            cover[7]["text"] != "年 月 日" or title["text"] != RESPONSE_REVIEW_TABLE_TITLE or
            stamp["text"] != "响应人： （盖单位章）" or date["text"] != "年 月 日" or
            letter[0]["text"] != "2. 商务报价函" or following["text"] != "报价明细表" or
            any(not re.fullmatch(rf"（{i}）我方承诺[^。\n]+。", block["text"]) for i, block in enumerate(letter[12:15], 2)) or
            [block["text"] for block in letter[15:18]] != [stamp["text"], "法定代表人（或其委托代理人）： （签字或盖章）", date["text"]]):
        return None
    validity = re.fullmatch(r"2\.我方承诺商务报价函有效期为([1-9][0-9]{0,2})\s*个日历天。", letter[8]["text"])
    if (validity is None or
            not 0 < stamp["bbox"][1] - last["bbox"][3] <= 70 or
            not 0 < date["bbox"][1] - stamp["bbox"][3] <= 25 or date["bbox"][0] <= stamp["bbox"][0] or
            not 0 < letter[-2]["bbox"][1] - letter[-3]["bbox"][3] <= 25):
        return None
    tables = (first, middle, last)
    metadata = [_table_metadata(table, merges, count) for table, merges, count in zip(tables, (
        [_merge(1, 0, 4), _merge(5, 0, 4)], [_merge(0, 0, 5), _merge(5, 0, 3), _merge(4, 1, 1, 2)], [],
    ), (9, 8, 1), strict=True)]
    if any(parent is None for parent in metadata):
        return None
    a, b, c = (table["rows"] for table in tables)
    if (a[0] != ["条款号", "评审因素", "评审标准", "对应\n页码", "是否\n响应"] or
            [row[1].replace("\n", "") for row in a[1:]] != ["响应人名称", "报价函签字盖章", "响应文件格式", "报价唯一",
                "资质要求", "项目经理（负责人）资格要求", "业绩要求", "财务要求"] or
            [row[1].replace("\n", "") for row in b] != ["", "信誉要求", "其他要求（需提供承诺书）", "联合体",
                b[4][1], "投标报价", "计划实施周期", "项目地点"] or not re.fullmatch(r"符合[^。\n]+其他条件。", b[4][1]) or
            a[1][0] != "形式评\n审标准" or a[5][0] != "资格评\n审标准" or b[5][0] != "响应性\n评审标\n准" or
            b[0][:2] != ["", ""] or not re.fullmatch(r"[^（）\n]{1,24}）", b[0][2]) or
            a[8][2].count("（") != a[8][2].count("）") + 1 or not b[7][2].endswith("。") or
            c[0] != ["", "投标有效期", f"{validity[1]}个日历天。", "", ""]):
        return None
    edges = [[table["bbox"][0] + sum(parent["column_widths"][:i]) for i in range(6)]
             for table, parent in zip(tables, metadata, strict=True)]
    if (any(max(abs(a - b) for a, b in zip(edges[0], line, strict=True)) > .75 for line in edges[1:]) or
            not 0 <= first["bbox"][1] - title["bbox"][1] <= 35 or
            any(not 0 < footer["bbox"][1] - table["bbox"][3] <= 65 for footer, table in zip(footers[1:3], tables[:2], strict=True)) or
            any(not 0 < table["bbox"][1] <= .15 * footer["bbox"][1] for table, footer in zip(tables[1:], footers[1:3], strict=True))):
        return None

    source = {"source_id": source_id, "source_sha256": config["source_sha256"]}
    # The financial requirement is the sole physical row split across pages.
    # Other rows are independent even when they share a merged group label.
    groups = (
        ("qualification_evidence", [(0, i) for i in range(5, 9)] + [(1, 0)]),
        ("credit_and_commitment_evidence", [(1, 1), (1, 2)]),
        ("joint_bidding_and_legal_conditions", [(1, 3), (1, 4)]),
        ("price_control", [(1, 5)]),
    )
    independent = (
        ("company_qualification", [(0, 5)]), ("project_manager_qualification", [(0, 6)]),
        ("company_experience", [(0, 7)]), ("financial_evidence", [(0, 8), (1, 0)]),
        ("credit_evidence", [(1, 1)]), ("other_commitments", [(1, 2)]),
        ("joint_bidding", [(1, 3)]), ("legal_conditions", [(1, 4)]),
    )
    section = [title, first, middle, last, stamp, date]
    section_ids = [block["block_id"] for block in section]
    table_ids = section_ids[1:4]
    table_quote = "\n".join(table["text"] for table in tables)
    result = source | {"rule": RESPONSE_REVIEW_TABLE_RULE, "physical_page": page, "content_block_ids": section_ids,
        "members": [_member(block, role) for block, role in zip(section,
                    ("heading", "table_first", "table_middle", "table_last", "external_stamp", "external_date"), strict=True)],
        "column_edges_points": edges, "tables": metadata, "footer_proofs": skipped,
        "financial_requirement_continuation": {"left": _row_part(first, 8), "right": _row_part(middle, 0), "column": 2},
        "last_row_is_independent": {"preceding": _row_part(middle, 7), "last": _row_part(last, 0)},
        "cover_members": [_member(block, "cover") for block in cover[:-1]],
        "letter_members": [_member(block, "letter") for block in letter[:-1]],
        "following_heading": _member(following, "following_price_schedule"),
        "rows": _review_rows(tables, metadata, source, groups),
        "independent_requirement_rows": _review_rows(tables, metadata, source, independent)}
    proof_hash = _hash(result)
    return {**result, "proof_sha256": proof_hash, "table_continuation": source | {
        "rule": RESPONSE_REVIEW_TABLE_RULE, "block_ids": table_ids, "quote": table_quote, "quote_sha256": _text_hash(table_quote),
        "exact_citation_block_ids": section_ids, "proof_sha256": proof_hash}}
