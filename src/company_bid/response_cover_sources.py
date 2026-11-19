"""Source proof for a split native response cover during authenticated replay."""
from __future__ import annotations

import hashlib
import math
import re

from .storage import digest


RESPONSE_COVER_CONFIG_KEY = "response_cover_source_resolution"
_ID_FIELDS = ("source_id", "window_id", "continuation_owner_window_id")
_HASH_FIELDS = ("source_sha256", "raw_candidate_sha256", "source_proof_sha256")


def normalize_response_cover_config(config):
    if (not isinstance(config, dict) or set(config) != {*_ID_FIELDS, *_HASH_FIELDS, "physical_page", "tender_name_block_ids"} or
            any(not isinstance(config[key], str) or not 1 <= len(config[key]) <= 120 for key in _ID_FIELDS) or
            config["window_id"] == config["continuation_owner_window_id"] or
            any(not isinstance(config[key], str) or not re.fullmatch(r"[a-f0-9]{64}", config[key]) for key in _HASH_FIELDS) or
            type(config["physical_page"]) is not int or config["physical_page"] <= 1):
        raise ValueError("RESPONSE_COVER_SOURCE_CONFIG_INVALID")
    ids = config["tender_name_block_ids"]
    if (not isinstance(ids, list) or len(ids) != 3 or
            any(not isinstance(key, str) or not 1 <= len(key) <= 120 for key in ids) or len(set(ids)) != 3):
        raise ValueError("RESPONSE_COVER_SOURCE_CONFIG_INVALID")
    return {**config, "tender_name_block_ids": list(ids)}


def _native_lines(blocks, source_id, page, numbers):
    return len(blocks) == len(numbers) and len({block.get("unit_id") for block in blocks}) == 1 and all(
        block.get("source_id") == source_id and block.get("page") == page and
        block.get("locator") == f"pdf/page[{page}]/line[{number}]" and block.get("ordinal") == blocks[0]["ordinal"] + index and
        block.get("kind") == "paragraph" and block.get("origin") == "native" and
        not block.get("read_disposition") and not block.get("illustration_source_use") and
        isinstance(block.get("text"), str) and block["text"].strip() == block["text"] and "\n" not in block["text"] and
        isinstance(box := block.get("bbox"), list) and len(box) == 4 and
        all(type(value) in (float, int) and math.isfinite(value) and value >= 0 for value in box) and
        box[0] < box[2] and box[1] < box[3]
        for index, (block, number) in enumerate(zip(blocks, numbers, strict=True)))


def _member(block, role):
    return {key: block[key] for key in ("block_id", "locator", "ordinal", "unit_id", "page", "bbox")} | {
        "role": role, "text_sha256": hashlib.sha256(block["text"].encode()).hexdigest()}


def build_response_cover_proof(config, native, tender, footer):
    """Reconstruct roles from all lines of one original response-format page."""
    source_id, page = config["source_id"], config["physical_page"]
    if (not _native_lines(native, source_id, page, range(9)) or not _native_lines(tender, source_id, 1, range(4)) or
            [block["block_id"] for block in tender[:3]] != config["tender_name_block_ids"] or
            tender[3]["text"] != "询比采购文件"):
        return None
    region, *body, page_number = native
    name, title, bidder, signer, date = body[:3], *body[3:]
    if (region["text"] != "响应文件格式" or re.sub(r"\s+", "", title["text"]) != "响应文件" or
            not re.fullmatch(r"响应人[：:]\s*[（(]盖单位章[）)]", bidder["text"]) or
            not re.fullmatch(r"法定代表人或其委托代理人[：:]\s*[（(]签字或盖章[）)]", signer["text"]) or
            not re.fullmatch(r"年\s+月\s+日", date["text"]) or
            not isinstance(footer, dict) or footer.get("rule") != "pdf-page-footer-v1" or
            footer.get("printed_page") is None or str(footer["printed_page"]) != page_number["text"] or
            any(footer.get(key) != page_number[key] for key in ("block_id", "locator", "page", "source_id", "bbox"))):
        return None
    for sequence in (name, tender[:3]):
        text = "".join(block["text"] for block in sequence)
        if (not re.fullmatch(r"[\u3400-\u9fff0-9\s、（）()\-—]{12,120}项目", text) or
                any(re.search(r"(?:响应文件|盖章|签字|响应人|法定代表人)", block["text"]) for block in sequence)):
            return None
    boxes = [block["bbox"] for block in native]
    first, second, third = boxes[1:4]
    midpoint = (first[0] + first[2]) / 2
    heights = [box[3] - box[1] for box in (first, second, third)]
    pitches = [second[1] - first[1], third[1] - second[1]]
    if (not all(right[1] > left[1] for left, right in zip(boxes, boxes[1:])) or
            min(heights) < 20 or max(heights) - min(heights) > 2 or min(pitches) < 20 or abs(pitches[0] - pitches[1]) > 2 or
            abs(first[0] - second[0]) > 2 or abs(first[2] - second[2]) > 2 or not first[0] < third[0] < third[2] < first[2] or
            any(abs((box[0] + box[2]) / 2 - midpoint) > 3 for box in (third, boxes[4], boxes[8])) or
            min(first[1] - boxes[0][3], boxes[4][1] - third[3], boxes[5][1] - boxes[4][3]) < 30 or
            min(boxes[6][1] - boxes[5][3], boxes[7][1] - boxes[6][3], boxes[8][1] - boxes[7][3]) < 5):
        return None
    cover_name = "".join(block["text"] for block in name)
    tender_name = "".join(block["text"] for block in tender[:3])
    roles = ("format_region_boundary", "printed_project_name", "printed_project_name", "printed_project_name",
             "response_document_title", "bidder_stamp_slot", "representative_signature_or_stamp_slot", "date_slots", "page_footer")
    proof = {"rule": "native_split_response_cover_v1", "source_id": source_id, "source_sha256": config["source_sha256"],
             "physical_page": page, "members": [_member(block, role) for block, role in zip(native, roles, strict=True)],
             "content_block_ids": [block["block_id"] for block in native[:-1]], "footer_proof": footer,
             "name_comparison": {"status": "same_printed_name" if cover_name == tender_name else "different_printed_names_preserved",
                 "response_cover_printed_name": cover_name, "tender_cover_printed_name": tender_name,
                 "tender_members": [_member(block, "independent_tender_cover") for block in tender]},
             "scope": "original_response_cover_only", "unfilled_fields": ["bidder_stamp", "representative_signature_or_stamp", "date"]}
    return {**proof, "proof_sha256": digest(proof)}
