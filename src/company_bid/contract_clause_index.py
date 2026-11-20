"""Source-order catalog of native contract chapters and numbered clauses.

The inputs are the frozen N1 document and its blocks from the extraction source
index. The caller authenticates the original file and N1 checkpoint. Scope and
position proofs here derive only from the indexed reader output.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import hashlib
import math
import re

from .storage import digest


CONTRACT_CLAUSE_INDEX_VERSION = "native-contract-clause-index-v1"
_CN = "零〇一二两三四五六七八九十百千"
_NUMERIC = str.maketrans("０１２３４５６７８９．", "0123456789.")
_OUTER = re.compile(rf"^第[0-9{_CN}]+(?:部分|篇|卷)(?:[ \t　]+|(?=[^的所条款约]))[^。；;：:!?！？\n]{{1,65}}$")
_CHAPTER = re.compile(rf"^第(?P<number>[0-9{_CN}]+)(?P<kind>条|章|节)(?P<space>[ \t　]*)(?P<title>[^。；;：:!?！？\n]{{1,65}})$")
_CONTRACT_TITLE = re.compile(r"^合同(?:文本|正文|(?:主要)?条款(?:及格式)?|格式|书|协议书|样本|范本|示范文本)(?:[（(](?:示范文本|范本|草案)[）)])?$")
_INTRO = re.compile(r"^本[^。]{0,24}合同[^。]{0,28}(?:双方|各方)[^。]{0,8}(?:签署|订立|签订)[：:]?$")
_PARTY = re.compile(r"^(甲|乙)方(?:[（(][^）)]{1,20}[）)])?[：:]")
_TOC = re.compile(r"^目[ \t　]*录$")
_TOC_LEADERS = re.compile(r"(?:[.·…．‥]\s*){3,}[-— ]*\d{1,4}[-— ]*$")
_CLOSING = re.compile(r"^(?:[（(]?\s*(?:(?:以下|本页|下页)无(?:合同)?正文(?:[，,、；;]\s*(?:为|系)?签[署章]页)?|签[章署]页|合同正文结束)\s*[）)]?[。.]?)$")
_ANNEX = re.compile(rf"^(?:合同附件|附件|附录)[ \t　]*[0-9{_CN}]+[：:、.． \t　]+\S")
_DECIMAL = re.compile(r"^(?P<number>[1-9][0-9]{0,2}(?:\.[1-9][0-9]{0,2}){1,4})(?![0-9.])(?P<tail>.*)$")
_REFERENCE = re.compile(r"^(?:(?:条(?:款|项)?|款|项)(?:之|规定|约定|所述|的|$)|所述)")
_QUANTITY = re.compile(r"^(?:万?元|千?克|毫?米|厘米|个|台|份|次|年|日|天|月|小时|公里|吨|℃|％|%|‰|°|kg|mm|cm|km|m²|m³)(?=$|[、，。；:：）】\s]|[每为及或至以])", re.IGNORECASE)
_ITEM = re.compile(r"^[【\[]?[ \t　]*[（(](?P<paren>[1-9][0-9]{0,2})[）)][ \t　]*(?=\S)")
_SCALAR_ITEM = re.compile(r"^(?P<scalar>[1-9][0-9]{0,2})[.、](?![0-9.])[ \t　]*(?=\S)")
_LETTER_ITEM = re.compile(r"^[（(](?P<letter>[a-zA-Z])[）)][ \t　]*(?=\S)")
_INLINE_ITEM = re.compile(r"(?P<sep>[：:；;])[ \t　]*(?P<marker>[（(](?P<number>[1-9][0-9]{0,2})[）)])(?=[ \t　]*\S)")
_PAGE_NUMBER = re.compile(r"^[-－—]?\s*([0-9]{1,4})\s*[-－—]?$")
_PDF_LINE = re.compile(r"^pdf/page\[([0-9]+)\]/line\[([0-9]+)\]$")
_SENTENCE_END = re.compile(r"[。！？；;.!?】）)》”]\s*$")


@dataclass(frozen=True)
class _Line:
    block: dict
    start: int
    end: int
    unit_ordinal: int

    @property
    def text(self):
        return self.block.get("text", "")[self.start:self.end]

    @property
    def display(self):
        return self.text.strip().translate(_NUMERIC)

    @property
    def native(self):
        return (self.block.get("origin") == "native" and self.block["kind"] in {"paragraph", "table"} and
                not self.block.get("read_disposition") and not self.block.get("illustration_source_use"))

    @property
    def heading_text(self):
        return self.display if self.native and self.block["kind"] == "paragraph" else ""

    def position(self, *, include_text=False):
        pos = {k: self.block[k] for k in ("block_id", "locator", "page", "kind")}
        pos.update(start_char=self.start, end_char=self.end)
        if include_text:
            pos["text"] = self.text
        return pos


def _source_lines(document, blocks):
    lines = []
    seen = set()
    for unit in document["units"]:
        for index, source in enumerate(unit["blocks"]):
            block_id = source["block_id"]
            block = blocks.get(block_id)
            if (block_id in seen or not block or block["block_id"] != block_id or
                    block["source_id"] != document["source_id"] or block["page"] != unit.get("page") or
                    block["unit_id"] != unit["unit_id"] or block["locator"] != source["locator"] or
                    block["kind"] != source["kind"] or block.get("origin") != source.get("origin") or
                    block.get("bbox") != source.get("bbox") or
                    (block["kind"] == "paragraph" and block.get("text", "") != source.get("text", ""))):
                raise ValueError("CONTRACT_CLAUSE_INDEX_SOURCE_MISMATCH")
            seen.add(block_id)
            body = block.get("text", "")
            if block["kind"] == "table" and (block.get("rows") != source.get("rows") or
                    block.get("merges") != source.get("merges") or
                    body != "\n".join("\t".join(row) for row in source.get("rows", []))):
                raise ValueError("CONTRACT_CLAUSE_INDEX_SOURCE_MISMATCH")
            if block["kind"] != "paragraph":
                lines.append(_Line(block, 0, len(body), index))
                continue
            for line in re.finditer(r"[^\r\n]*(?:\r\n|\r|\n|$)", body):
                if line.start() < len(body):
                    end = line.start() + len(line[0].rstrip("\r\n"))
                    lines.append(_Line(block, line.start(), end, index))
            if not body:
                lines.append(_Line(block, 0, 0, index))
    return lines


def _chapter_number(value):
    if value.isascii() and value.isdecimal():
        return int(value)
    digits = dict(zip("零一二三四五六七八九", range(10), strict=True))
    digits.update({"〇": 0, "两": 2})
    units = {"十": 10, "百": 100, "千": 1000}
    result, digit, previous_unit = 0, None, 10000
    for part in value:
        if part in digits:
            if digit not in (None, 0):
                return None
            digit = digits[part]
        else:
            unit = units[part]
            if unit >= previous_unit or digit == 0:
                return None
            result += (digit if digit is not None else 1) * unit
            digit, previous_unit = None, unit
    return result + (digit or 0)


def _chapter(line):
    text = line.heading_text
    m = _CHAPTER.fullmatch(text)
    if (not m or _TOC_LEADERS.search(text) or
            (not m["space"] and m["title"].startswith(("规定", "约定", "所述", "之", "的", "中")))):
        return None
    return _chapter_number(m["number"])


def _decimal(line):
    text = line.heading_text
    m = _DECIMAL.fullmatch(text)
    if not m or _TOC_LEADERS.search(text):
        return None
    tail = m["tail"].strip()
    if tail and (tail.startswith(("-", "－", "—", "–", "/", "／", "%", "％", "×")) or
                 _REFERENCE.match(tail) or _QUANTITY.match(tail)):
        return None
    return tuple(map(int, m["number"].split(".")))


def _bbox(line):
    box = line.block.get("bbox")
    if (isinstance(box, list) and len(box) == 4 and
            all(type(v) in (int, float) and math.isfinite(v) for v in box) and
            box[0] < box[2] and box[1] < box[3]):
        return box
    return None


def _page_furniture(lines):
    by_page = defaultdict(list)
    for index, line in enumerate(lines):
        if type(line.block["page"]) is int:
            by_page[line.block["page"]].append((index, line))
    candidates, possible = [], set()
    for page, page_lines in by_page.items():
        index, line = page_lines[-1]
        match, box = _PAGE_NUMBER.fullmatch(line.heading_text), _bbox(line)
        native_locator = _PDF_LINE.fullmatch(line.block["locator"])
        if (not match or not box or not native_locator or int(native_locator[1]) != page or
                line.start != 0 or line.end != len(line.block["text"]) or
                box[1] < 650 or box[3] - box[1] > 18 or box[2] - box[0] > 60):
            continue
        possible.add(index)
        others = [_bbox(other) for _, other in page_lines[:-1]]
        if not others or any(other is None or other[3] > box[1] - 10 for other in others):
            continue
        candidates.append((index, page, int(match[1]), box))

    proven = {}
    for index, page, printed, box in candidates:
        family = [(other_page, lines[other_index].block["block_id"]) for other_index, other_page, other_printed, other
                  in candidates if other_page - other_printed == page - printed and
                  abs(other[1] - box[1]) <= 3 and abs(other[0] + other[2] - box[0] - box[2]) <= 16]
        if len(family) >= 3 and any(abs(other_page - page) == 1 for other_page, _ in family):
            proven[index] = {**lines[index].position(include_text=True), "printed_page": printed, "bbox": box,
                             "rule": "native_pdf_bottom_number_with_matching_page_sequence",
                             "evidence_pages": [other_page for other_page, _ in family],
                             "evidence_block_ids": [block_id for _, block_id in family]}
    return proven, possible - proven.keys()


def _blank_grid_overlays(lines):
    overlays = {}
    for index in range(1, len(lines) - 1):
        before, grid, after = lines[index - 1:index + 2]
        if (grid.block["kind"] != "table" or not grid.native or not before.heading_text or not after.heading_text or
                grid.block["page"] is None or before.block["page"] != grid.block["page"] or
                after.block["page"] != grid.block["page"]):
            continue
        box, previous_box, owner_box = _bbox(grid), _bbox(before), _bbox(after)
        rows = grid.block.get("rows", [])
        if not box or not previous_box or not owner_box or len(rows) != 1 or any(cell.strip() for cell in rows[0]):
            continue
        overlap = min(box[3], owner_box[3]) - max(box[1], owner_box[1])
        if (previous_box[3] >= box[1] or overlap < .75 * min(box[3] - box[1], owner_box[3] - owner_box[1]) or
                box[3] - box[1] > 1.25 * (owner_box[3] - owner_box[1]) or
                not owner_box[0] < box[0] < box[2] < owner_box[2] or box[2] - box[0] > .5 * (owner_box[2] - owner_box[0])):
            continue
        overlays[index] = {**grid.position(), "rule": "empty_native_grid_overlays_following_paragraph",
                           "bbox": box, "grid_sha256": digest([rows, grid.block.get("merges", []), box]),
                           "owner": after.position(), "owner_bbox": owner_box}
    return overlays


def _toc_lines(lines):
    by_page = defaultdict(list)
    for index, line in enumerate(lines):
        page = line.block["page"]
        if type(page) is int:
            by_page[page].append((index, line))
    excluded = set()
    for page_lines in by_page.values():
        first = next((i for i, (_, line) in enumerate(page_lines) if _TOC.fullmatch(line.heading_text)), None)
        if first is None:
            continue
        following = page_lines[first + 1:]
        if (not any(_INTRO.fullmatch(line.heading_text) for _, line in following) and
                sum(bool(_OUTER.match(line.heading_text) or _chapter(line) or _TOC_LEADERS.search(line.heading_text))
                    for _, line in following) >= 2 and not any(_decimal(line) for _, line in following)):
            excluded.update(index for index, _ in page_lines[first:])
    return excluded


def _quoted_lines(lines):
    """Treat a fully delimited multiline quotation as body content."""
    excluded, stack = set(), []
    pairs = {"“": "”", "‘": "’", "「": "」", "『": "』", "《": "》"}
    for index, line in enumerate(lines):
        for char in line.heading_text:
            if stack and char == stack[-1][0]:
                _, start = stack.pop()
                excluded.update(range(start + 1, index + 1))
            elif char in pairs:
                stack.append((pairs[char], index))
    return excluded


def _scope_markers(lines, toc):
    discovered = {}
    for index, line in enumerate(lines):
        text = line.heading_text
        if index in toc or not text or _TOC_LEADERS.search(text) or _ANNEX.match(text):
            continue
        outer = _OUTER.match(text)
        name = re.sub(rf"^第[0-9{_CN}]+(?:部分|篇|卷)[ \t　]*", "", text) if outer else text
        if _CONTRACT_TITLE.fullmatch(name):
            discovered[index] = "contract_heading"
        elif (index == 0 or outer) and re.fullmatch(r"[^。，；：、！？\n]{1,65}合同", name):
            parties = {m[1] for candidate in lines[index + 1:index + 21]
                       if (m := _PARTY.match(candidate.heading_text))}
            if parties == {"甲", "乙"}:
                discovered[index] = "contract_heading"
        elif outer:
            discovered[index] = "other_document_part"
    return discovered


def _chapter_heads(lines, start, end, toc, footer):
    candidates = [(index, number) for index in range(start, end) if index not in toc
                  and (number := _chapter(lines[index]))]
    for (index, number), boundary in zip(candidates, [i for i, _ in candidates[1:]] + [end], strict=True):
        following = (lines[i] for i in range(index + 1, boundary) if i not in footer and i not in toc)
        first = next((line for line in following if line.display or not line.native), None)
        heading = _CHAPTER.fullmatch(lines[index].heading_text)
        if first is not None and (heading["space"] or _decimal(first) == (number, 1)):
            yield index, number


def _annex_starts(lines, index, end, footer, chapter):
    line = lines[index]
    if line.unit_ordinal > 1 or not _ANNEX.match(line.heading_text):
        return False
    # A contract may list its own attachments before the following decimal
    # clause. Such an inventory and the separate annex body have different roles.
    following = next((lines[j] for j in range(index + 1, end)
                      if j not in footer and lines[j].display and not _ANNEX.match(lines[j].heading_text)), None)
    number = _decimal(following) if following else None
    previous = "".join(lines[i].heading_text for i in range(max(0, index - 3), index) if i not in footer)
    return not ((number and number[0] == chapter) or re.search(r"(?:附件|附录)(?:如下)?[：:]$", previous))


def _positions(lines, start, end, footer):
    spans = []
    for index in range(start, end):
        if index in footer:
            continue
        line = lines[index]
        if spans and spans[-1].block is line.block:
            spans[-1] = _Line(line.block, spans[-1].start, line.end, line.unit_ordinal)
        else:
            spans.append(line)
    return [span.position(include_text=True) for span in spans]


def _boundary(lines, index, kind, **value):
    return {**(lines[index].position(include_text=True) if index < len(lines) else {}), "kind": kind, **value}


def _range(lines, start, end, boundary, footer):
    spans = _positions(lines, start, end, footer)
    block_ids = list(dict.fromkeys(span["block_id"] for span in spans))
    quote = "\n".join(span["text"] for span in spans)
    return {"start": lines[start].position(), "end": {key: val for key, val in spans[-1].items() if key != "text"},
            "spans": spans, "block_ids": block_ids,
            "quote": quote, "quote_sha256": hashlib.sha256(quote.encode("utf-8")).hexdigest(), "next_boundary": boundary,
            "skipped": [footer[i] for i in range(start, end) if i in footer and "printed_page" in footer[i]],
            "layout_evidence": [proof for proof in footer.values() if proof.get("owner", {}).get("block_id") in block_ids]}


def _issue(lines, start, end, reason, **details):
    return {"start": lines[start].position(), "end": lines[max(start, end - 1)].position(),
            "reason": reason, **details}


def _bad_range(lines, start, end, footer, possible_footer):
    issues = []
    for index in range(start, end):
        if index in footer:
            continue
        line = lines[index]
        if not line.native:
            issues.append(_issue(lines, index, index + 1, "non_native_or_unread_contract_content"))
        elif index in possible_footer:
            issues.append(_issue(lines, index, index + 1, "unproven_page_footer"))
        if index > start:
            a, b = lines[index - 1].block["page"], line.block["page"]
            if type(a) is int and type(b) is int and b not in {a, a + 1}:
                issues.append(_issue(lines, index - 1, index + 1, "non_consecutive_physical_pages"))
    return issues


def _inline_item_splits(lines, start, end, leading):
    """Recognize a complete colon-led and semicolon-separated same-level list."""
    matches = []
    pairs = {"“": "”", "‘": "’", "「": "」", "『": "』", "《": "》", "【": "】", "（": "）", "(": ")"}
    stack = []
    for index in range(start, end):
        text = lines[index].text if lines[index].heading_text else ""
        previous = 0
        for match in _INLINE_ITEM.finditer(text):
            for char in text[previous:match.start("marker")]:
                if stack and char == stack[-1]:
                    stack.pop()
                elif char in pairs:
                    stack.append(pairs[char])
            if not stack:
                matches.append((index, match))
            previous = match.end()
        for char in text[previous:]:
            if stack and char == stack[-1]:
                stack.pop()
            elif char in pairs:
                stack.append(pairs[char])
    if not matches:
        return {}, False
    first_index, first = matches[0]
    intro = lines[first_index].text[:first.start("marker")]
    last = next((line.display for line in reversed(lines[start:end]) if line.display and not
                 _PAGE_NUMBER.fullmatch(line.display)), "")
    order = sorted([(index, match.start("marker"), int(match["number"]), match["sep"]) for index, match in matches] +
                   [(index, 0, int(number), None) for index, _, number in leading])
    if (len(order) < 2 or stack or order[0][3] not in {None, "：", ":"} or
            not all(item[3] in {None, "；", ";"} for item in order[1:]) or
            [item[2] for item in order] != list(range(1, len(order) + 1)) or
            re.search(r"举例|例如|示例|参见|参照", intro) or not _SENTENCE_END.search(last)):
        return {}, True
    splits = defaultdict(list)
    for index, match in matches:
        splits[index].append(match.start("marker"))
    return splits, False


def _intro_spans(lines, start, end, first_item, footer):
    if not first_item:
        return _positions(lines, start, end, footer)
    for index in range(start, end):
        line = lines[index]
        first = first_item["start"]
        if first["block_id"] != line.block["block_id"] or not line.start <= first["start_char"] < line.end:
            continue
        if first["start_char"] == line.start:
            return _positions(lines, start, index, footer)
        prefix = _Line(line.block, line.start, first["start_char"], line.unit_ordinal)
        return _positions([*lines[start:index], prefix], 0, index - start + 1,
                          {i - start: value for i, value in footer.items() if start <= i < index})
    raise ValueError("CONTRACT_CLAUSE_INDEX_ITEM_POSITION_MISMATCH")


def _items(lines, start, end, owner, boundary, footer, issues, *, allow_scalar=False, letter_only=False):
    markers = []
    numbering_basis = "native_line_start_and_consecutive_numbering"
    quoted = _quoted_lines(lines[start:end])
    for index in range(start, end):
        if index - start in quoted:
            continue
        text = lines[index].heading_text
        match = (_LETTER_ITEM if letter_only else _ITEM).match(text)
        if allow_scalar and not match:
            match = _SCALAR_ITEM.match(text)
        if match:
            style, number = next((key, value.lower()) for key, value in match.groupdict().items() if value)
            markers.append((index, style, number))
    if not letter_only and not allow_scalar:
        splits, uncertain = _inline_item_splits(lines, start, end, markers)
        if uncertain:
            issues.append(_issue(lines, start, end, "inline_numbered_item_boundaries_unconfirmed"))
        if splits:
            segments, new_footer = [], {}
            leading = {index: (style, number) for index, style, number in markers}
            markers = []
            for index in range(start, end):
                line = lines[index]
                if index in footer:
                    new_footer[len(segments)] = footer[index]
                offsets = [line.start, *(line.start + offset for offset in splits.get(index, [])), line.end]
                for a, b in zip(offsets, offsets[1:]):
                    segment = _Line(line.block, a, b, line.unit_ordinal)
                    if a == line.start and index in leading:
                        markers.append((len(segments), *leading[index]))
                    elif a != line.start:
                        match = _ITEM.match(segment.heading_text)
                        markers.append((len(segments), "paren", match["paren"]))
                    segments.append(segment)
            lines, start, end, footer = segments, 0, len(segments), new_footer
            numbering_basis = "native_colon_and_semicolon_delimited_list"
    if not markers:
        return []
    styles = {style for _, style, _ in markers}
    expected = [chr(97 + i) if letter_only else str(i + 1) for i in range(len(markers))]
    starts = [i for i, _, _ in markers]
    xs = [box[0] for i in starts if (box := _bbox(lines[i]))]
    if (len(styles) != 1 or [n for _, _, n in markers] != expected or
            (numbering_basis == "native_line_start_and_consecutive_numbering" and xs and max(xs) - min(xs) > 4)):
        issues.append(_issue(lines, starts[0], end, "numbered_item_group_ambiguous",
                             observed_numbers=[n for _, _, n in markers]))
        return []
    entries = []
    for rank, ((i, style, item_number), limit) in enumerate(zip(markers, starts[1:] + [end], strict=True)):
        next_boundary = (_boundary(lines, limit, "next_numbered_item", item_number=expected[rank + 1])
                         if limit < end else boundary)
        part = _range(lines, i, limit, next_boundary, footer)
        item_id = "contract-item-" + digest([owner, item_number, lines[i].position()])
        complete = next_boundary["kind"] != "unconfirmed_section_end"
        if any(span["kind"] == "table" for span in part["spans"]):
            issues.append(_issue(lines, i, limit, "numbered_item_table_association_unconfirmed", item_id=item_id))
            complete = False
        nested = _items(lines, i + 1, limit, {**owner, "parent_item_id": item_id}, next_boundary,
                        footer, issues, letter_only=True) if not letter_only and complete else []
        entry = {**owner, **part, "item_number": item_number, "item_style": style, "item_id": item_id,
                 "numbering_basis": numbering_basis, "complete": complete, "items": nested,
                 "intro_spans": _intro_spans(lines, i, limit, nested[0] if nested else None, footer)}
        entries.append({**entry, "proof_sha256": digest(entry)})
    return entries


def _chapter_entries(lines, start, end, scope, boundary, footer, possible_footer, quoted):
    issues = _bad_range(lines, start, end, footer, possible_footer)
    root = scope["chapter_number"]
    candidates, unexpected = [], []
    for i in range(start + 1, end):
        number = None if i in quoted else _decimal(lines[i])
        if number:
            (candidates if number[0] == root else unexpected).append((i, number))
    for i, number in unexpected:
        issues.append(_issue(lines, i, i + 1, "decimal_candidate_outside_chapter", candidate=list(number)))
    expected_by_parent = defaultdict(lambda: 1)
    seen = set()
    previous = ()
    for i, number in candidates:
        parent = number[:-1]
        if (number[-1] != expected_by_parent[parent] or (len(parent) > 1 and
                (parent not in seen or previous[:len(parent)] != parent)) or number in seen):
            issues.append(_issue(lines, i, i + 1, "decimal_numbering_not_confirmed", candidate=list(number)))
        expected_by_parent[parent] = number[-1] + 1
        seen.add(number)
        previous = number
    if any(issue["reason"] in {"decimal_candidate_outside_chapter", "decimal_numbering_not_confirmed"} for issue in issues):
        return [], _positions(lines, start + 1, end, footer), [], issues

    clauses = []
    for order, (i, number) in enumerate(candidates):
        next_i, next_number = candidates[order + 1] if order + 1 < len(candidates) else (end, None)
        clause_number = ".".join(map(str, number))
        next_boundary = (_boundary(lines, next_i, "next_decimal_clause", clause_number=".".join(map(str, next_number)))
                         if next_number else boundary)
        owner = {**scope, "parent_clause_number": ".".join(map(str, number[:-1])) if len(number) > 2 else None}
        part = _range(lines, i, next_i, next_boundary, footer)
        clause_id = "contract-clause-" + digest([scope, clause_number, part["start"]])
        finding = _bad_range(lines, i, next_i, footer, possible_footer)
        complete = not finding and next_boundary["kind"] != "unconfirmed_section_end"
        children = _items(lines, i, next_i, {**scope, "parent_clause_number": clause_number}, next_boundary,
                          footer, issues) if complete else []
        entry = {**owner, **part, "clause_id": clause_id, "clause_number": clause_number,
                 "block_id": lines[i].block["block_id"], "locator": lines[i].block["locator"],
                 "items": children, "intro_spans": _intro_spans(lines, i, next_i, children[0] if children else None, footer),
                 "complete": complete}
        clauses.append({**entry, "proof_sha256": digest(entry)})

    lead_end = candidates[0][0] if candidates else end
    chapter_items = _items(lines, start + 1, lead_end, {**scope, "parent_clause_number": None},
                           _boundary(lines, lead_end, "next_decimal_clause", clause_number=clauses[0]["clause_number"])
                           if clauses else boundary, footer, issues, allow_scalar=True) if not _bad_range(
        lines, start + 1, lead_end, footer, possible_footer) else []
    if not clauses and not chapter_items:
        issues.append(_issue(lines, start, end, "chapter_has_no_supported_decimal_or_independent_items"))
    return clauses, _intro_spans(lines, start + 1, lead_end, chapter_items[0] if chapter_items else None, footer), chapter_items, issues


def build_contract_clause_index(document: dict, blocks: dict) -> dict:
    """Return ordered decimal entries, chapter ranges, proven items and open ranges.

    Each span uses zero-based Python character offsets in one indexed N1 block.
    Span text is exact. A quote joins its spans with LF across block boundaries.
    A number is promoted within a recognized contract scope and coherent chapter
    numbering. Unknown boundaries and unsupported material remain in unresolved.
    """
    identity = {k: document[k] for k in ("source_id",)}
    identity["source_sha256"] = document["sha256"]
    result = {"rule": CONTRACT_CLAUSE_INDEX_VERSION, **identity, "entries": [], "sections": [], "unresolved": []}
    lines = _source_lines(document, blocks)
    if not lines:
        return {**result, "index_sha256": digest(result)}

    footer, possible_footer = _page_furniture(lines)
    footer.update(_blank_grid_overlays(lines))
    toc, quoted = _toc_lines(lines), _quoted_lines(lines)
    markers = _scope_markers(lines, toc | quoted)
    starts = list(markers)
    for order, heading in enumerate(starts):
        if markers[heading] != "contract_heading":
            continue
        end = starts[order + 1] if order + 1 < len(starts) else len(lines)
        end_kind = markers[end] if end < len(lines) else "document_end"
        body_chapter = None
        for i in range(heading + 1, end):
            line = lines[i]
            if i in toc or i in quoted:
                continue
            number = _chapter(line)
            if number == 1 or body_chapter and number == body_chapter + 1:
                body_chapter = number
            if _CLOSING.fullmatch(line.heading_text):
                end, end_kind = i, "contract_body_end"
                break
            if body_chapter and _annex_starts(lines, i, end, footer, body_chapter):
                end, end_kind = i, "attachment_start"
                break

        section_id = "contract-section-" + digest([identity, lines[heading].position()])
        scope = {**identity, "section_id": section_id}
        if end == len(lines):
            last = next((lines[i] for i in range(end - 1, heading, -1)
                         if i not in footer and lines[i].display), None)
            if last is None or not last.native or not _SENTENCE_END.search(last.display):
                end_kind = "unconfirmed_section_end"
        end_boundary = _boundary(lines, end, end_kind)
        heads = list(_chapter_heads(lines, heading + 1, end, toc | quoted, footer))
        section = {**scope, "heading": lines[heading].position(include_text=True), "next_boundary": end_boundary,
                   "chapters": [], "entry_ids": []}
        if not heads or heads[0][1] != 1:
            result["unresolved"].append(_issue(lines, heading, end, "contract_chapter_start_unconfirmed", **scope))
            result["sections"].append(section)
            continue
        if end_kind == "unconfirmed_section_end":
            result["unresolved"].append(_issue(lines, heads[-1][0], end, end_kind, **scope))

        section["preamble_spans"] = [lines[i].position(include_text=True) for i in range(heading + 1, heads[0][0])
                                     if i not in footer and i not in toc]
        for rank, (start, chapter_number) in enumerate(heads):
            limit, next_number = heads[rank + 1] if rank + 1 < len(heads) else (end, None)
            if chapter_number != rank + 1:
                result["unresolved"].append(_issue(lines, start, end, "contract_chapter_sequence_unconfirmed", **scope))
                break
            chapter_scope = {**scope, "chapter_number": chapter_number,
                             "chapter_id": "contract-chapter-" + digest([scope, chapter_number, lines[start].position()])}
            boundary = (_boundary(lines, limit, "next_chapter", chapter_number=next_number)
                        if next_number else end_boundary)
            if next_number is not None and next_number != chapter_number + 1:
                boundary["kind"] = "unconfirmed_section_end"
            clauses, intro, items, issues = _chapter_entries(lines, start, limit, chapter_scope, boundary, footer,
                                                           possible_footer, quoted)
            result["unresolved"].extend({**issue, **chapter_scope} for issue in issues)
            chapter = {**chapter_scope, **_range(lines, start, limit, boundary, footer),
                       "heading": lines[start].position(include_text=True), "intro_spans": intro,
                       "entry_ids": [clause["clause_id"] for clause in clauses], "items": items}
            section["chapters"].append({**chapter, "proof_sha256": digest(chapter)})
            section["entry_ids"].extend(chapter["entry_ids"])
            result["entries"].extend(clauses)
        result["sections"].append(section)

    return {**result, "index_sha256": digest(result)}
