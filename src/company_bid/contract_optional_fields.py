"""Source proof for two stock contract lists explicitly marked with a slash."""
from __future__ import annotations

import math
import re

from .storage import digest


WINDOW_CONTEXT_KEY = "native_contract_optional_field_context"
RESOLUTION_KEY = "native_contract_optional_field_resolution"
RULE = "native_numbered_contract_slash_marked_inventories_v1"
_ARTICLE_TITLES = ("术语", "技术文件", "合同生效及其他事项")
_ARTICLE_HEADING = re.compile(r"^第[0-9零〇一二两三四五六七八九十百千]+条")
_PROMPTS = (
    re.compile(r"【双方(?:确定|确认)：本合同[及与](?:相关)?附件(?:中)?(?:所)?涉及的(?:有关)?"
               r"(?:名词|词语)[和及]技术术语，其(?:定义|含义)[和及]解释如下：[/／]"),
    re.compile(r"【(?:和|与)履行本合同(?:有|相)关的(?:下列|以下)技术文件，"
               r"(?:为|属于)本合同(?:的)?组成部分：[/／]"),
)
_SLOTS = (*(f"{i}．【】；" for i in range(1, 5)), "5．【】。】", "6.其他：【】")


def _compact(text):
    return re.sub(r"[ \t　]", "", text)


def _evidence(source_id, blocks, span):
    block = blocks[span["block_id"]]
    page, box = block["page"], block.get("bbox")
    if (block["kind"] != "paragraph" or block["origin"] != "native" or block["source_id"] != source_id or
            block.get("read_disposition") or block.get("illustration_source_use") or
            block["locator"] != f"pdf/page[{page}]/line[{block['unit_ordinal']}]" or
            any(span[key] != block[key] for key in ("locator", "page", "text")) or
            span["start_char"] != 0 or span["end_char"] != len(block["text"]) or
            not isinstance(box, list) or len(box) != 4 or
            any(type(value) not in {float, int} or not math.isfinite(value) for value in box) or
            not (box[0] < box[2] and box[1] < box[3])):
        return None
    return {key: block[key] for key in (
        "block_id", "text", "page", "locator", "unit_id", "ordinal", "unit_ordinal", "bbox", "source_id", "kind", "origin")}


def _ordered_lines(lines):
    for left, right in zip(lines, lines[1:]):
        if right["ordinal"] != left["ordinal"] + 1:
            return False
        if left["page"] == right["page"]:
            if left["bbox"][3] > right["bbox"][1]:
                return False
        elif right["page"] != left["page"] + 1 or right["unit_ordinal"] != 0:
            return False
    return True


def _group(source_id, blocks, chapter, next_heading, prompt):
    intro, slots = chapter["intro_spans"], chapter["items"]
    if (chapter["entry_ids"] or chapter["layout_evidence"] or not 1 <= len(intro) <= 3 or
            not prompt.fullmatch(_compact("".join(line["text"] for line in intro))) or
            chapter["next_boundary"]["kind"] != "next_chapter" or
            chapter["next_boundary"]["block_id"] != next_heading["block_id"] or
            [slot["item_number"] for slot in slots] != list(map(str, range(1, 7))) or
            any(not slot["complete"] or slot["items"] or len(slot["spans"]) != 1 for slot in slots)):
        return None
    slot_spans = [slot["spans"][0] for slot in slots]
    if (tuple(_compact(line["text"]) for line in slot_spans) != _SLOTS or
            chapter["spans"] != [chapter["heading"], *intro, *slot_spans]):
        return None
    lines = [_evidence(source_id, blocks, span) for span in chapter["spans"]]
    end = _evidence(source_id, blocks, next_heading)
    footer = [_evidence(source_id, blocks, span) for span in chapter["skipped"]]
    if end is None or any(line is None for line in [*lines, *footer]):
        return None
    if (len(footer) > 1 or any(line["bbox"][1] < 650 for line in footer) or
            not _ordered_lines(sorted([*lines, *footer, end], key=lambda line: line["ordinal"]))):
        return None
    starts = [line["bbox"][0] for line in lines[-6:]]
    if max(starts) - min(starts) > 4:
        return None
    slash = lines[len(intro)]
    if not _compact(slash["text"]).endswith(("：/", "：／")):
        return None
    return {"article_number": chapter["chapter_number"], "chapter_proof_sha256": chapter["proof_sha256"],
            "heading": lines[0], "prompt": lines[1:1 + len(intro)], "slots": lines[-6:],
            "slash_block_id": slash["block_id"], "next_heading": end, "separated_footers": footer,
            "source_disposition": "slash_marked_inventory_with_empty_entries",
            "handling": "preserve_source_placeholders_without_bidder_input_or_inferred_values"}


def bind_optional_contract_fields(windows, documents, indexes, blocks):
    """Bind complete stock inventories from frozen N1, outside the model payload."""
    by_source = {document["source_id"]: document for document in documents if document.get("format") == "pdf"}
    for index in indexes:
        source_id = index["source_id"]
        if source_id not in by_source:
            continue
        document = by_source[source_id]
        for section in index["sections"]:
            chapters = section["chapters"][-3:]
            if len(chapters) != 3:
                continue
            numbers = [chapter["chapter_number"] for chapter in chapters]
            if (numbers != list(range(numbers[0], numbers[0] + 3)) or
                    tuple(_ARTICLE_HEADING.sub("", _compact(chapter["heading"]["text"]), count=1)
                          for chapter in chapters) != _ARTICLE_TITLES or
                    section["next_boundary"]["kind"] != "contract_body_end" or
                    any(issue.get("section_id") == section["section_id"] and issue.get("chapter_number") in numbers[:2]
                        for issue in index["unresolved"])):
                continue
            groups = [_group(source_id, blocks, chapter, following["heading"], prompt)
                      for chapter, following, prompt in zip(chapters[:2], chapters[1:], _PROMPTS, strict=True)]
            if any(group is None for group in groups):
                continue
            section_start = _evidence(source_id, blocks, section["heading"])
            section_end = _evidence(source_id, blocks, section["next_boundary"])
            if section_start is None or section_end is None:
                continue
            unit_ids = {line["unit_id"] for group in groups for line in
                        [group["heading"], *group["prompt"], *group["slots"], group["next_heading"], *group["separated_footers"]]}
            coverage = {unit["unit_id"]: unit["coverage"] for unit in document["units"] if unit["unit_id"] in unit_ids}
            if (coverage.keys() != unit_ids or any(c.get("status") != "read" or c.get("origin") != "native" or
                    any(c.get(key) for key in ("unread_objects", "unmapped_glyphs", "error_code", "ignored_objects"))
                    for c in coverage.values())):
                continue
            for window in windows:
                if window["source_id"] != source_id:
                    continue
                target = {block["block_id"] for block in window["blocks"]}
                local = [group for group in groups if
                         {group["slash_block_id"], *(slot["block_id"] for slot in group["slots"])} <= target]
                if local:
                    proof = {"rule": RULE, "source_id": source_id, "source_sha256": document["sha256"],
                             "index_sha256": index["index_sha256"], "contract_heading": section_start,
                             "contract_body_end": section_end, "groups": local,
                             "paired_chapter_proof_sha256": [group["chapter_proof_sha256"] for group in groups],
                             "native_unit_coverage_sha256": {key: digest(c) for key, c in coverage.items()}}
                    window.setdefault(WINDOW_CONTEXT_KEY, []).append({**proof, "proof_sha256": digest(proof)})


def resolve_optional_contract_fields(raw, data, window, critical):
    """Prove all critical targets without editing any model choice."""
    if not critical or WINDOW_CONTEXT_KEY not in window:
        return None
    selected = {identity for item in [*data["requirements"], *data["format_chapters"]]
                for identity in (item["citation"]["block_ids"] or [item["citation"]["block_id"]])}
    selected.update(identity for chapter in data["format_chapters"] for identity in chapter["content_block_ids"])
    targets = {block["block_id"]: block for block in window["blocks"]}
    reported, matched = set(critical), []
    for proof in window[WINDOW_CONTEXT_KEY]:
        if (proof["source_id"] != window["source_id"] or proof["source_sha256"] != window["source_sha256"] or
                proof["proof_sha256"] != digest({key: value for key, value in proof.items() if key != "proof_sha256"})):
            continue
        accounted = set()
        for group in proof["groups"]:
            lines = [*group["prompt"], *group["slots"]]
            full_group = {line["block_id"] for line in lines} | {group["heading"]["block_id"]}
            target_lines = [line for line in lines if line["block_id"] in targets]
            essential = {group["slash_block_id"], *(slot["block_id"] for slot in group["slots"])}
            if (essential <= reported and not selected.intersection(full_group) and
                    all(all(targets[line["block_id"]].get(k) == v for k, v in line.items() if k != "bbox")
                        for line in target_lines)):
                accounted.update(line["block_id"] for line in target_lines if line["block_id"] in reported)
        if accounted == reported:
            matched.append(proof)
    if len(matched) != 1:
        return None
    return {"rule": RULE, "window_id": window["window_id"], "proof": matched[0],
            "resolved_block_ids": list(critical), "raw_candidate_sha256": digest(raw),
            "model_candidate_mutation": False, "semantic_review": "requires_source_review"}
