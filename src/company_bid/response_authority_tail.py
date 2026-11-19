"""Prove ownership of the remaining blocks of a native authorization form."""
from __future__ import annotations

import re

from .storage import digest


def authority_tail_proof(window, raw):
    chapters, targets = raw.get("format_chapters"), window["blocks"]
    if not chapters or not targets or window.get("source_format") != "pdf":
        return None
    page = targets[0]["page"]
    head = [b for b in window.get("adjacent_context", []) if b.get("page") == page]
    owned = [b for b in targets if b.get("page") == page]
    if len(head) != 3 or len(owned) != 11 or targets[:11] != owned or len(targets) <= 11:
        return None
    tail, footer, following = owned[:-1], owned[-1], targets[11]
    whole = head + owned
    locators = [f"line[{i}]" for i in range(8)] + ["table[0]"] + [f"line[{i}]" for i in range(8, 13)]
    if any(b["source_id"] != window["source_id"] or b["origin"] != "native" or b["unit_id"] != head[0]["unit_id"]
           or b["unit_ordinal"] != i or b["ordinal"] != head[0]["ordinal"] + i
           or b["locator"] != f"pdf/page[{page}]/{locator}" or b.get("read_disposition") or b.get("illustration_source_use")
           for i, (b, locator) in enumerate(zip(whole, locators, strict=True))):
        return None
    title = head[0]["text"]
    if (not re.fullmatch(r"[0-9]+[.．]\s*授权委托书", title) or head[1]["text"] != "法定代表人授权委托书"
            or following["page"] != page + 1 or following["source_id"] != window["source_id"]
            or following["origin"] != "native" or following["locator"] != f"pdf/page[{page + 1}]/line[0]"
            or following["ordinal"] != footer["ordinal"] + 1 or not re.fullmatch(r"[0-9]+[.．]\s*企业基本概况", following["text"])):
        return None
    stamp, representative, agent, date = (b["text"] for b in tail[-4:])
    if (not stamp.startswith("响应人：") or "盖单位章" not in stamp
            or not representative.startswith("法定代表人：") or "签字或盖章" not in representative
            or not agent.startswith("委托代理人：") or "签字或盖章" not in agent
            or date.split() != ["年", "月", "日"]):
        return None
    table = whole[8]
    if (table["kind"] != "table" or len(table["rows"]) != 2
            or any(len(row) != 2 for row in table["rows"]) or table.get("merges")
            or not all(who in text and side in text for row, side in zip(table["rows"], ("正面", "反面"), strict=True)
                       for who, text in zip(("法人", "委托代理人"), row, strict=True))):
        return None
    skips = [s for s in window.get("safe_skip_blocks", []) if s["block_id"] == footer["block_id"]]
    if (len(skips) != 1 or skips[0].get("rule") != "pdf-page-footer-v1" or skips[0]["locator"] != footer["locator"]
            or str(skips[0]["printed_page"]) != footer["text"]):
        return None
    tail_ids = [b["block_id"] for b in tail]
    expected = {"title": title, "level": 2, "status": "prescribed", "citation": {"block_ids": tail_ids},
                "content_block_ids": tail_ids, "requirement_indices": [0]}
    if chapters[0] != expected or len({b["block_id"] for b in whole}) != len(whole):
        return None
    proof = {"rule": "native_authorization_form_tail_v1", "source_id": window["source_id"],
             "source_sha256": window["source_sha256"], "window_id": window["window_id"], "physical_page": page,
             "title": title, "owner_ids": [b["block_id"] for b in head], "tail_ids": tail_ids,
             "footer_id": footer["block_id"], "following_heading_id": following["block_id"],
             "source_block_digests": [digest(b) for b in [*whole, following]]}
    return {**proof, "proof_sha256": digest(proof)}
