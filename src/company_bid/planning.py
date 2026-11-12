"""N3 deterministic bid planning from frozen, source-checked N2 candidates.

Models may map eligible empty cells, but never supply their values. Source
views and the N2 public entry are re-exported from extraction for compatibility.
"""
from __future__ import annotations

import copy
import hashlib
import re

from pydantic import Field

from .models import (PreparedBid, SourceRef, SourceContextProof, RequirementSourceContext,
                     context_digest, source_context_operator)
from .slots import has_fill_marker, whole_cell_slot
from .source_clauses import build_clause_catalog
from .extraction import (
    EXTRACTION_VERSION, PlanningError, Text, _Strict, _WindowResponse,
    _EXTRACT_INSTRUCTIONS, _json, _hash, _id, _fail, _capture, _index, _windows,
    _citation, _validate_window, _merge_results, _wire_candidate, _raw_grid,
    _heading_level, _pdf_footer_skips, extract_requirements, _validate_normalized_window_result, _close_deferred_context,
    _chapter_boundary,
)
from .table_continuations import build_table_continuations, build_table_row_catalog


PLANNING_VERSION = "bid-plan-v16"


class _Fill(_Strict):
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    fact_id: Text
    label: Text
    label_row: int = Field(ge=0)
    label_column: int = Field(ge=0)


class _GridMapping(_Strict):
    grid_sha256: Text
    fills: list[_Fill]


_LABEL_GROUPS = (
    ("项目名称", "工程名称", "标的名称"),
    ("投标人名称", "投标单位名称", "响应人名称", "供应商名称", "企业名称", "公司名称", "单位名称"),
    ("统一社会信用代码", "营业执照号", "营业执照注册号"),
    ("注册地址", "注册住所", "住所", "企业地址"),
    ("注册资本", "注册资金"),
    ("成立日期", "成立时间"),
    ("企业类型", "公司类型", "单位性质"),
    ("经营范围", "业务范围"),
    ("邮政编码", "邮编"),
)


_MODEL_DENY = re.compile(r"报价|金额|价格|税|联系人|电话|手机|代表|授权|签[字章]|盖章|日期|人数|数量|账号|银行|备注|承诺")


_VOLATILE = re.compile(r"报价|投标价|响应价|总价|单价|价款|价格|金额|税|费用|费率|成本|万元|人民币|[（(]元[）)]|[￥¥]|(?:人工|材料|机械|服务|交通|管理|运输)费|联系人|项目经理|被授权|签字|盖章|投标日期")
_CURRENT_ELIGIBILITY_STATUS = re.compile(r"经营异常|失信|信用中国|信用信息公示|信用(?:状态|记录)|信誉")


def _normal(label: str) -> str:
    return re.sub(r"[\s：:（）()、/]+", "", label).casefold()


def _title_key(title: str) -> str:
    return _normal(re.sub(r"^(?:第[零一二三四五六七八九十百0-9]+[章节篇部]|[一二三四五六七八九十百]+[、．.]|\d+(?:\.\d+)*[、．.]?\s*)\s*", "", title.strip()))


def _wire_citation(citation: dict) -> dict:
    return {key: citation[key] for key in ("source_id", "block_id", "locator", "quote", "block_ids", "clause_id", "table_row_id") if key in citation}


def _writing_space(detail: dict, chapter: dict, blocks: dict) -> list:
    # A named technical chapter is not permission to rewrite a fixed letter.
    if (not re.search(r"技术|方案|施工|实施|设计|方法", chapter["title"]) or
            re.search(r"承诺|保证|声明|授权|函", chapter["title"])):
        return []
    permission = re.compile(r"自行编(?:写|制)|自拟(?:格式|方案)|格式自拟|自定义格式|自定格式")
    denied = re.compile(r"不得|禁止|不可|不能|不允许|不应|不宜|无须|无需|严禁|不准|未经|未获|并非|而非|不是|非\s*(?:自行|自拟|由)|不接受|不予接受|无效|作废|拒绝|勿")
    other_actor = re.compile(r"采购人|招标人|业主|代理机构|招标代理|委托人|发包人|中标人|成交人|[甲乙丙丁]方|设计单位|施工单位|监理单位|第三方|其他单位|他人")
    conditional = re.compile(r"(?:^|[，,；;。\n（(])\s*(?:如|若)|仅(?:在|当)|只有|除非|前提|条件|须经|需经|经[^。；\n]{0,60}(?:同意|批准|确认|授权)|(?:同意|批准|确认|授权)后|(?:满足|符合|获准|获得|取得)[^。；\n]{0,40}(?:后|方可)|另行通知|另行约定")
    candidate_ids = list(dict.fromkeys([*(key for citation in detail["citations"]
                                        for key in (citation.get("block_ids") or [citation["block_id"]])),
                                       *chapter["content_block_ids"]]))
    footer_skips = _pdf_footer_skips(blocks)
    result = []
    for key in candidate_ids:
        block = blocks[key]
        if block["kind"] != "paragraph" or key in footer_skips or not permission.search(block.get("text", "")):
            continue
        # Candidate quotes may be arbitrary exact substrings. Permission must
        # instead come from complete frozen source blocks, including a nearby
        # antecedent such as a separate "不得" or "经书面同意后方可" paragraph.
        source = sorted((value for value in blocks.values() if value["source_id"] == block["source_id"]),
                        key=lambda value: value["ordinal"])
        position = next(i for i, value in enumerate(source) if value["block_id"] == key)
        context = [block]
        for previous in reversed(source[:position]):
            if previous["block_id"] in footer_skips:
                continue
            if previous["kind"] != "paragraph" or _heading_level(previous.get("text", "")) is not None:
                break
            context.insert(0, previous)
            if re.search(r"[。；;.!！?？]\s*$", previous.get("text", "")) or len(context) >= 7:
                break
        text = "\n".join(value.get("text", "") for value in context)
        if denied.search(text) or other_actor.search(text) or conditional.search(text):
            continue
        # An explicit but unidentified actor is not implicit bidder permission.
        ambiguous_actor = False
        for match in permission.finditer(text):
            prefix = re.split(r"[，,。；;：:（）()]", text[:match.start()])[-1]
            prefix = re.sub(r"(?:(?:可以|可|应当|必须|应|须|需|负责)\s*)+$", "", prefix).strip()
            bidder = re.search(r"响应人|投标人|供应商|投标单位|响应单位", prefix)
            implicit = not prefix or re.fullmatch(r"(?:本(?:项目)?的?)?(?:技术|服务|施工|实施|设计|响应|投标)?(?:方案|格式)|本(?:章|节|章节|部分)(?:响应)?", prefix)
            if not bidder and not implicit:
                ambiguous_actor = True
        if ambiguous_actor:
            continue
        first = context[0]
        citation = {"source_id": first["source_id"], "block_id": first["block_id"], "locator": first["locator"],
                    "quote": text, "block_ids": [value["block_id"] for value in context]}
        checked = _citation(citation, blocks, footer_skips)
        if checked not in result:
            result.append(checked)
    return result


def _canonical(label: str) -> str:
    label = _normal(label)
    return next((_normal(group[0]) for group in _LABEL_GROUPS if label in {_normal(v) for v in group}), label)


def _fact_index(manifest: dict) -> list:
    facts = copy.deepcopy(manifest.get("facts", []))
    facts.extend({"fact_id": _id("F-INPUT-", [key, manifest[key]]), "label": label,
                  "value": manifest[key]["name"], "source_refs": manifest[key]["source_refs"]}
                 for key, label in (("project", "项目名称"), ("company", "投标人名称")))
    _fail(len({f["fact_id"] for f in facts}) != len(facts), "DUPLICATE_FACT_ID")
    # Identical duplicate values can share a label. Conflicting values stay
    # ambiguous rather than selecting the first library record.
    return facts


def _label_facts(label: str, facts: list, sources: dict, source_roles: dict, *, require_project_input: bool = False) -> list:
    candidates = [fact for fact in facts if _canonical(fact["label"]) == _canonical(label)]
    if require_project_input or _VOLATILE.search(label):
        candidates = [fact for fact in candidates if all(sources.get(ref, {}).get("kind") == "manual_input" and
                                                        sources.get(ref, {}).get("provenance") != "derived_from_bid" and
                                                        source_roles.get(ref) == "project_supplement"
                                                        for ref in fact["source_refs"])]
    return candidates if len({fact["value"] for fact in candidates}) <= 1 else []


def _adjacent_labels(rows: list, row: int, column: int) -> list:
    labels = [(row, c, rows[row][c]) for c in range(column) if rows[row][c].strip()]
    if labels:
        return [labels[-1]]
    if row > 0 and rows[row - 1][column].strip():
        return [(row - 1, column, rows[row - 1][column])]
    return []


def _table_mapping(ctx, block: dict, facts: list, sources: dict, source_roles: dict, call_json, config: dict) -> tuple[dict, dict]:
    rows, merges, covered = _raw_grid(block)
    blank = {(r, c) for r, row in enumerate(rows) for c, text in enumerate(row) if whole_cell_slot(text) and (r, c) not in covered}
    fills, reasons = {}, []
    for row, column in sorted(blank):
        labels = _adjacent_labels(rows, row, column)
        if not labels:
            continue
        lr, lc, label = labels[0]
        matches = _label_facts(label, facts, sources, source_roles)
        # A row label followed by several empty cells has no deterministic
        # destination. The narrow mapping call must resolve that geometry.
        horizontal_blanks = [(r, c) for r, c in blank if r == row and c > lc] if lr == row else [(row, column)]
        if matches and len(horizontal_blanks) == 1:
            fills[(row, column)] = matches[0]["fact_id"]
    allowed = {}
    adopted_labels = set()
    for fact in facts:
        label_key = _canonical(fact["label"])
        matches = _label_facts(fact["label"], facts, sources, source_roles)
        if (not _MODEL_DENY.search(fact["label"]) and label_key not in adopted_labels and matches and
                any(label_key == _normal(group[0]) for group in _LABEL_GROUPS)):
            allowed[matches[0]["fact_id"]] = matches[0]
            adopted_labels.add(label_key)
    remaining = blank - set(fills)
    grid_hash = _hash({"rows": rows, "merges": merges, "column_widths": block.get("column_widths", [])})
    candidate_targets = [(r, c) for r, c in sorted(remaining)
                         if any(not _MODEL_DENY.search(label) and
                                any(_canonical(label) == _canonical(f["label"]) for f in allowed.values())
                                for _, _, label in _adjacent_labels(rows, r, c))]
    if candidate_targets and allowed and config.get("map_table_fields", True):
        payload = {"producer_version": PLANNING_VERSION, "grid_sha256": grid_hash, "rows": rows,
                   "merges": merges, "allowed_targets": [list(value) for value in candidate_targets],
                   "allowed_facts": [{"fact_id": key, "label": fact["label"]} for key, fact in allowed.items()],
                   "output_contract": _GridMapping.model_json_schema(),
                   "task_instructions": "仅标注空白目标格到允许fact_id，不输出值。label必须逐字来自同一行左侧或正上方原格，给出label坐标。不得改原表标签/行列/单位/合并，不标报价/人/签章/日期/银行等高风险格；不确定返回空fills。"}

        def validate(raw):
            try:
                result = _GridMapping.model_validate(raw).model_dump()
            except Exception as exc:
                raise PlanningError("TABLE_MAPPING_SCHEMA_INVALID") from exc
            _fail(result["grid_sha256"] != grid_hash, "TABLE_MAPPING_GRID_HASH_MISMATCH")
            targets = set()
            for fill in result["fills"]:
                target = (fill["row"], fill["column"])
                label_target = (fill["label_row"], fill["label_column"])
                _fail(target not in candidate_targets or target in targets or fill["fact_id"] not in allowed,
                      "TABLE_MAPPING_TARGET_INVALID")
                targets.add(target)
                labels = _adjacent_labels(rows, *target)
                _fail(not any((r, c) == label_target and text == fill["label"] for r, c, text in labels) or
                      _MODEL_DENY.search(fill["label"]) is not None or
                      _canonical(fill["label"]) != _canonical(allowed[fill["fact_id"]]["label"]) or
                      not _label_facts(fill["label"], facts, sources, source_roles), "TABLE_MAPPING_LABEL_INVALID")
            return result

        unit_id = _id("form-map-", [block["block_id"], grid_hash])
        result = ctx.run_unit(unit_id, payload, lambda: validate(call_json(ctx, "map_form_fields", unit_id, payload, validate)))
        for fill in result["fills"]:
            fills[(fill["row"], fill["column"])] = fill["fact_id"]
        reasons.append({"unit_id": unit_id, "mapped_cell_count": len(result["fills"])})
    return fills, {"block_id": block["block_id"], "grid_sha256": grid_hash,
                   "blank_anchor_count": len(blank), "filled_count": len(fills), "mapping_units": reasons}


def _reference(requirement: dict, blocks: dict, units: dict, sources: dict) -> dict | None:
    keywords = requirement.get("reference_keywords", [])
    if not keywords:
        return None
    candidates = []
    for block in blocks.values():
        if units[block["unit_id"]]["role"] != "reference_bid" or units[block["unit_id"]].get("coverage", {}).get("status") != "read":
            continue
        text = block.get("text", "")
        hits = [word for word in keywords if word in text]
        # A reference method must have specific lexical applicability and must
        # not introduce old corporate/project facts into the writing context.
        if (not hits or (max(map(len, hits)) < 4 and len(hits) < 2) or block["kind"] != "paragraph" or
                len(text) > 3500 or not re.search(r"检查|检测|记录|测量|复测|流程|方法|巡检|交付|隔离|许可", text) or
                re.search(r"我公司|本公司|有限公司|责任公司|业绩|合同金额|报价|身份证|开户|项目名称|已配备|现有|拥有|成立于|\d{3,}", text)):
            continue
        candidates.append((len(hits), -block["ordinal"], block, hits))
    if not candidates:
        return None
    _, _, block, hits = max(candidates, key=lambda value: value[:2])
    text = block["text"]
    return {"reference_id": _id("REF-", [block["source_id"], block["locator"], text]),
            "source_id": block["source_id"], "locator": block["locator"], "text": text,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "applicability": "仅适用原文关键词：" + "、".join(hits) + "；作为拟实施方法参考，不证明公司既有事实。",
            "retained_points": hits, "excluded_points": ["历史项目、公司、人员、业绩和设备事实", "旧报价、旧日期和未经本次要求授权的服务承诺"]}


def _fixed_letter_items(body: list, anchors: dict) -> dict:
    """Prove numbered declarations locally without changing source headings.

    Only a prescribed letter with an explicit declaration introducer and a
    consecutive list starting at one can override the broad heading heuristic.
    An unknown heading, number gap, other form or chapter ends that proof.
    """
    numbered = re.compile(r"^(\d{1,3})([.．、])\s*(?!\d)(\S.*)$")
    form_title = re.compile(r"(?:表|函|书|资料|材料|方案|声明|承诺|证明|清单|目录)$")
    declaration = re.compile(r"(?:承诺|声明|保证)\s*[:：]$")
    clause = re.compile(r"^(?:我(?:公司|方|单位)|本(?:公司|单位|项目)|不存在|未(?:被|在|参与|参加|发生|出现|提供|受到|列入)|不(?:存在|属于|在|得|与)|具有|具备|参加|按照|遵守|承诺|保证|声明)")
    addition_note = re.compile(r"^[（(][^。；;\n]*可(?:自行)?(?:补充|增加|添加)[^。；;\n]*[）)]$")
    result = {}
    for position, anchor in enumerate(body):
        chapter = anchors.get(anchor["block_id"])
        if (not chapter or chapter["status"] != "prescribed" or
                not re.search(r"(?:函|承诺书|声明书|保证书)$", chapter["title"].strip())):
            continue
        introducer, previous, first, marker, number = None, None, None, None, 0
        for block in body[position + 1:]:
            key, text = block["block_id"], block.get("text", "").strip()
            level = _heading_level(text)
            if key in anchors or level == 0 or block["kind"] != "paragraph":
                break
            item = numbered.fullmatch(text)
            if item:
                value, item_marker, content = int(item[1]), item[2], item[3]
                if (introducer is None or value != number + 1 or
                        marker is not None and item_marker != marker or
                        level is not None and (previous is None or form_title.search(content) or
                            not (clause.search(content) or addition_note.fullmatch(content)))):
                    break
                first = first or key
                if level is not None:
                    proof = {"rule": "fixed_letter_consecutive_declarations_v1",
                             "anchor_block_id": anchor["block_id"], "introducer_block_id": introducer,
                             "list_start_block_id": first, "previous_item_block_id": previous,
                             "item_number": value, "number_marker": item_marker,
                             "original_heading_level": level,
                             "text_sha256": hashlib.sha256(block["text"].encode()).hexdigest()}
                    result[key] = {**proof, "proof_sha256": _hash(proof)}
                number, marker, previous = value, item_marker, key
            elif level is not None:
                break
            elif not number and declaration.search(text):
                introducer = key
    return result


def _writing_instruction_only(text: str) -> bool:
    """Recognize whole drafting instructions, never a mixed fixed declaration."""
    return re.fullmatch(
        r"[（(]?\s*(?:注[：:]\s*)?(?:响应人|投标人|供应商)?\s*(?:可以|可|应当|应|须|需)?\s*"
        r"(?:本(?:项目)?的?)?(?:(?:技术|服务|施工|实施|设计)?方案)?\s*"
        r"(?:自行编(?:写|制)(?:技术方案|方案)?|自拟(?:格式|方案)|格式自拟|自定义格式|自定格式)"
        r"(?:[，,]\s*根据[^。；;！？!?\n]{1,120}(?:要求|规定)编(?:写|制))?\s*[）)]?[。.]?",
        text.strip()) is not None


_SOURCE_LINE = re.compile(r"^pdf/page\[(\d+)\]/line\[(\d+)\]$")
_SOURCE_ITEM = re.compile(r"^(?:[（(]\d+[）)]|[①②③④⑤⑥⑦⑧⑨⑩])")


def _native_source_line(block: dict) -> bool:
    match = _SOURCE_LINE.fullmatch(block.get("locator", ""))
    return bool(match and block.get("kind") == "paragraph" and block.get("origin") == "native" and
                not block.get("read_disposition") and block.get("page") == int(match[1]) and
                "\n" not in block.get("text", "") and "\r" not in block.get("text", ""))


def _native_source_span(values: list[dict]) -> bool:
    if not values or not all(_native_source_line(value) for value in values):
        return False
    first = values[0]
    if any(value.get("unit_id") != first.get("unit_id") or value["source_id"] != first["source_id"]
           for value in values):
        return False
    numbers = [int(_SOURCE_LINE.fullmatch(value["locator"])[2]) for value in values]
    ordinals = [value["ordinal"] for value in values]
    return (numbers == list(range(numbers[0], numbers[0] + len(values))) and
            ordinals == list(range(ordinals[0], ordinals[0] + len(values))))


def _format_source_roles(document: dict, body: list[dict]) -> dict:
    """Prove a small set of auxiliary roles from N1, never from model labels.

    Unsupported prelude shapes keep the existing unresolved-format behavior.
    Roles account for source; they neither create requirements nor prove delivery.
    """
    if document.get("format") != "pdf":
        return {}
    roles = {}
    by_id = {value["block_id"]: value for value in body}

    def assign(values, role, *, scope=(), container=(), **extra):
        def identities(items):
            return [{key: value.get(key) for key in ("block_id", "locator", "ordinal", "page", "unit_id")} |
                    {"text_sha256": hashlib.sha256(value["text"].encode()).hexdigest()} for value in items]
        proof = {"rule": "native_format_source_role_v1", "source_id": document["source_id"],
                 "source_sha256": document["sha256"], "role": role,
                 "members": identities(values), "scope_members": identities([by_id[key] for key in scope]),
                 "container_members": identities(container), "scope_block_ids": list(scope)}
        for value in values:
            roles[value["block_id"]] = {"source_role": role, "role_proof": proof,
                "role_proof_sha256": _hash(proof), "scope_block_ids": list(scope), **extra}

    starts = [i for i, value in enumerate(body) if _chapter_boundary(value) and
              re.search(r"(?:投标|响应|报价).{0,18}(?:格式|组成)", value["text"])]
    for start in starts:
        end = next((i for i in range(start + 1, len(body)) if _chapter_boundary(body[i])), len(body))
        assign([body[start]], "format_region_boundary")
        for i in range(start + 1, end):
            value = body[i]
            if not _native_source_line(value):
                continue
            text = value["text"].strip()
            page = [item for item in body[i:end] if item["unit_id"] == value["unit_id"]]
            if re.fullmatch(r"目\s*录", text):
                entries = [re.fullmatch(r"([1-9]\d*)[.．、]\s*([^。；;！？!?\n]{2,60})", item["text"].strip())
                           if _native_source_line(item) and not has_fill_marker(item["text"]) else None
                           for item in page[1:]]
                if (_native_source_span(page) and len(entries) >= 2 and all(entries) and
                        [int(item[1]) for item in entries] == list(range(1, len(entries) + 1)) and
                        not any(re.search(r"(?<!响)应|需|须|必须|不得|禁止|不可|签字|盖章", item[2]) for item in entries)):
                    assign(page, "source_contents")
            elif re.fullmatch(r"(?:投标|响应)文件(?:封面)?", text):
                joined = "\n".join(item["text"] for item in page)
                if (3 <= len(page) <= 8 and _native_source_span(page) and
                        re.search(r"(?:项目|采购|招标)(?:名称|编号)[：:]", joined) and
                        re.search(r"签字|盖章|盖单位章", joined) and re.search(r"年\s*月\s*日|日期[：:]", joined)):
                    assign(page, "prescribed_cover")
            match = re.match(r"^([1-9]\d*)[.．、].{0,24}(?:文件|材料)的?组成[，,：:]", text)
            if not match or not re.search(r"包含|包括", text):
                continue
            following = next((j for j in range(i + 1, end) if re.match(
                rf"^{int(match[1]) + 1}[.．、]", body[j]["text"].strip())), None)
            if following is None:
                continue
            span = body[i:following + 1]
            if not _native_source_span(span):
                continue
            assign([value], "preparation_scope", container=span)
            scope, outer_scope, leaf = [], [], []
            condition_number, condition_unknown = 0, False

            def finish():
                if leaf:
                    assign(leaf, "preparation_instruction", scope=scope, container=span,
                           instruction_block_ids=[item["block_id"] for item in leaf],
                           scope_ambiguous=condition_unknown or bool(scope and not _SOURCE_ITEM.match(leaf[0]["text"].strip())))
                    leaf.clear()

            for item in body[i + 1:following]:
                item_text = item["text"].strip()
                group = re.fullmatch(r"[A-Z][.．、].{1,60}(?:包含|包括)[：:]", item_text)
                condition = re.fullmatch(r"[（(](\d+)[）)].{1,40}适用", item_text)
                if group or condition:
                    finish()
                    if group:
                        outer_scope = [item["block_id"]] if re.search(r"不|不得|禁止|仅", item_text) else []
                        scope, condition_number, condition_unknown = list(outer_scope), 0, False
                    else:
                        number = int(condition[1])
                        condition_unknown = condition_unknown or number != condition_number + 1
                        condition_number = number
                        scope = [*outer_scope, item["block_id"]]
                    assign([item], "preparation_scope", scope=scope, container=span)
                else:
                    if _SOURCE_ITEM.match(item_text) or (leaf and re.search(r"[。；;]$", leaf[-1]["text"].strip())):
                        finish()
                    leaf.append(item)
            finish()
            tail = body[following]
            if re.fullmatch(r"\d+[.．、].{0,30}格式[（(]未提供格式[^。；;]{1,100}自定义格式[）)]", tail["text"].strip()):
                assign([tail], "preparation_instruction", container=span, instruction_block_ids=[tail["block_id"]])
    return roles


def _numbered_child_context(role: dict, requirement: dict, blocks: dict) -> dict | None:
    """Derive only two supported operators for a proved native circled child."""
    ids = role["instruction_block_ids"]
    if (role.get("scope_ambiguous") or not role.get("scope_block_ids") or
            requirement["category"] != "technical" or requirement["response_mode"] == "procedure" or
            not re.match(r"^[①②③④⑤⑥⑦⑧⑨⑩]", blocks[ids[0]]["text"].strip()) or
            len(requirement.get("citations", [])) != 1):
        return None
    proof = role["role_proof"]
    container = [blocks[member["block_id"]] for member in proof["container_members"]]
    if not _native_source_span(container):
        return None
    parents = [blocks[key] for key in role["scope_block_ids"]]
    operators = [source_context_operator(parent["text"]) for parent in parents]
    if (None in operators or len(set(operators)) != len(operators) or
            max(parent["ordinal"] for parent in parents) >= blocks[ids[0]]["ordinal"]):
        return None
    prefix = [value for value in container if max(parent["ordinal"] for parent in parents) < value["ordinal"] <= blocks[ids[0]]["ordinal"]]
    markers = ["①②③④⑤⑥⑦⑧⑨⑩".index(value["text"].strip()[0]) + 1 for value in prefix
               if re.match(r"^[①②③④⑤⑥⑦⑧⑨⑩]", value["text"].strip())]
    if markers != list(range(1, len(markers) + 1)):
        return None
    for index, value in enumerate(prefix):
        if not re.match(r"^[①②③④⑤⑥⑦⑧⑨⑩]", value["text"].strip()) and (
                _SOURCE_ITEM.match(value["text"].strip()) or _heading_level(value["text"].strip()) is not None or
                index and re.search(r"[。；;]$", prefix[index - 1]["text"].strip())):
            return None
    citation = requirement["citations"][0]
    if citation.get("clause_id") or citation.get("table_row_id"):
        return None
    selected = citation.get("block_ids") or [citation["block_id"]]
    if not set(ids) <= set(selected) or set(selected) - set([*ids, *role["scope_block_ids"]]):
        return None
    checked = _citation(_wire_citation(citation), blocks, _pdf_footer_skips(blocks))
    if (checked["quote"] != requirement["text"] or checked["start"] != 0 or
            checked["end"] != len("\n".join(blocks[key]["text"] for key in selected))):
        return None
    return {"rule": "native_numbered_child_context_v1", "source_id": proof["source_id"],
            "source_sha256": proof["source_sha256"], "requirement_id": requirement["requirement_id"],
            "requirement_quote_sha256": hashlib.sha256(requirement["text"].encode()).hexdigest(),
            "scope_members": copy.deepcopy(proof["scope_members"]),
            "container_members": copy.deepcopy(proof["container_members"]),
            "child_members": copy.deepcopy(proof["members"]),
            "selected_members": [{key: blocks[block_id].get(key) for key in
                                  ("block_id", "locator", "ordinal", "page", "unit_id")} |
                                 {"text_sha256": hashlib.sha256(blocks[block_id]["text"].encode()).hexdigest()}
                                 for block_id in selected]}


def _instruction_requirements(role: dict, extracted: dict, blocks: dict) -> tuple[list, list]:
    ids = role["instruction_block_ids"]
    text = "\n".join(blocks[key]["text"] for key in ids)
    matches = [item for item in extracted.get("requirements", []) if any(
        set(ids) <= set(citation.get("block_ids") or [citation["block_id"]]) for citation in item["citations"])]
    errors = []
    if not matches:
        errors.append("unmapped_preparation_instruction")
    if role.get("scope_ambiguous"):
        errors.append("preparation_instruction_scope_ambiguous")
    expected = []
    for pattern, modes in ((r"方案", {"writing"}), (r"授权书|承诺", {"fixed_text"}),
                           (r"报价表|偏离表", {"table"}), (r"证明|证书|执照", {"attachment"})):
        if re.search(pattern, text):
            expected.append(modes)
    multiple = bool(re.search(r"清单|汇总", text) and re.search(r"证明|证据", text))
    if multiple and "清单" in text:
        expected.append({"table", "factual_response"})
    allowed = set().union(*expected) if expected else None
    if multiple and allowed is not None:
        allowed |= {"table", "factual_response"}
    substantive = re.search(r"方案|授权书|报价表|证明|证书|执照|业绩|承诺|偏离表|资料|材料", text)
    valid = [item for item in matches if (allowed is None or item["response_mode"] in allowed) and
             (item["response_mode"] != "procedure" or not substantive)]
    if matches and (not valid or any(not modes & {item["response_mode"] for item in valid} for modes in expected)):
        errors.append("preparation_instruction_response_role_invalid")
    if multiple:
        modes = {item["response_mode"] for item in valid}
        if "attachment" not in modes or not modes & {"table", "factual_response"}:
            errors.append("preparation_instruction_component_unmapped" if "清单" in text else
                          "preparation_instruction_summary_unproved")
    results = []
    for item in valid:
        result = {"requirement_id": item["requirement_id"], "response_mode": item["response_mode"]}
        if role.get("scope_block_ids"):
            derivation = _numbered_child_context(role, item, blocks)
            if derivation:
                result["source_context_derivation"] = derivation
            else:
                errors.append("preparation_instruction_scope_unproved")
        results.append(result)
    return results, errors


def _instruction_placements(ledger, sections, attachments, actions, issues):
    """Validate actual handlers after assembly, not just matching source IDs."""
    results, errors, seen = [], [], set()
    for row in ledger:
        for item in row.get("instruction_requirements", []):
            rid, mode = item["requirement_id"], item["response_mode"]
            if (rid, mode) in seen:
                continue
            seen.add((rid, mode))
            pending = [issue["issue_id"] for issue in issues if rid in issue["requirement_refs"]]
            owners = [section for section in sections if rid in section["requirement_refs"]]
            if mode == "procedure":
                handled = any(rid in action["requirement_refs"] for action in actions)
            elif mode == "writing":
                handled = any(rid in task["requirement_refs"] for section in owners for task in section["writing_tasks"])
            elif mode == "attachment":
                handled = bool(pending) or any(rid in value["requirement_refs"] for value in attachments)
            else:
                field = {"table": "table_refs", "fixed_text": "fixed_content", "factual_response": "fact_refs"}[mode]
                handled = bool(pending) or any(section[field] for section in owners)
            results.append({"requirement_id": rid, "response_mode": mode,
                            "status": "mapped_with_issue" if pending else "mapped_pending" if handled else "unresolved",
                            "issue_refs": pending})
            if not handled:
                errors.append({"reason": "preparation_instruction_handler_missing", "requirement_id": rid})
    return results, errors


def _format_plan(extracted: dict, blocks: dict, documents: list) -> tuple[list, list, list]:
    """Account for whole structural format regions, including window continuations.

    A grounded chapter anchor is necessary. Unclaimed non-heading continuation
    blocks can be attached deterministically inside that chapter's source
    boundary; unknown headings and images never disappear into free writing.
    """
    formats = copy.deepcopy(extracted.get("format_chapters", []))
    footer_skips = _pdf_footer_skips(blocks)
    ledger, errors = list(footer_skips.values()), []
    anchors = {chapter["citation"]["block_id"]: chapter for chapter in formats}
    for anchor in set(anchors) & set(footer_skips):
        errors.append({"reason": "format_anchor_is_page_footer", "block_id": anchor})
    for document in documents:
        body = [block for block in blocks.values() if block["source_id"] == document["source_id"] and
                block["block_id"] not in footer_skips and
                not re.search(r"(?:^|/)(?:header|footer)\d*\.xml", block["locator"])]
        body.sort(key=lambda block: block["ordinal"])
        positions = {block["block_id"]: i for i, block in enumerate(body)}
        source_roles = _format_source_roles(document, body)
        auxiliary = {key for key, role in source_roles.items()
                     if role["source_role"] != "prescribed_cover"}
        for anchor in set(anchors) & auxiliary:
            errors.append({"reason": "source_role_conflict", "block_id": anchor,
                           "format_id": anchors[anchor]["format_id"],
                           "source_role": source_roles[anchor]["source_role"]})
        letter_items = _fixed_letter_items(body, anchors)

        def structural_level(block):
            return None if block["block_id"] in letter_items else _heading_level(block.get("text", ""))

        regions = []
        for i, block in enumerate(body):
            text = block.get("text", "").strip()
            if (block["kind"] == "paragraph" and _heading_level(text) == 0 and
                    re.search(r"(?:投标|响应|报价).{0,18}(?:格式|组成)", text) and not re.search(r"\.{2,}|…{2,}", text)):
                end = next((j for j in range(i + 1, len(body)) if _heading_level(body[j].get("text", "")) == 0), len(body))
                regions.append((i, end, "explicit_format_region"))
        for anchor, chapter in anchors.items():
            if anchor not in positions:
                continue
            start = positions[anchor]
            if any(a <= start < b for a, b, _ in regions):
                continue
            level = _heading_level(body[start].get("text", ""))
            end = next((j for j in range(start + 1, len(body)) if body[j]["block_id"] in anchors or
                        structural_level(body[j]) is not None and
                        structural_level(body[j]) <= (level if level is not None else 1)), len(body))
            # A following obvious form heading is not evidence that its format
            # was read. Extend the ledger so a missed form fails explicitly.
            if end < len(body) and body[end]["block_id"] not in anchors and re.search(r"(?:表|函|书|证明|承诺)$", body[end].get("text", "").strip()):
                errors.append({"reason": "unextracted_format_heading", "block_id": body[end]["block_id"], "locator": body[end]["locator"]})
            regions.append((start, end, "grounded_format_chapter"))
        for start, end, kind in regions:
            if kind == "explicit_format_region" and not any(body[i]["block_id"] in anchors for i in range(start, end)):
                errors.append({"reason": "format_region_has_no_grounded_chapter", "block_id": body[start]["block_id"],
                               "locator": body[start]["locator"]})
        relevant = {i for start, end, _ in regions for i in range(start, end)}
        owners, boundaries = {}, {}
        for chapter in formats:
            anchor = chapter["citation"]["block_id"]
            if anchor not in positions:
                continue
            anchor_position = positions[anchor]
            containing = [(a, b) for a, b, _ in regions if a <= anchor_position < b]
            region_end = min((b for _, b in containing), default=len(body))
            source_level = _heading_level(body[anchor_position].get("text", ""))
            boundary = next((j for j in range(anchor_position + 1, region_end)
                             if body[j]["block_id"] in anchors or body[j]["block_id"] in auxiliary or
                             (structural_level(body[j]) is not None and
                                structural_level(body[j]) <= (source_level if source_level is not None else 1))), region_end)
            boundaries[anchor] = boundary
            for block_id in chapter["content_block_ids"]:
                position = positions.get(block_id)
                if block_id in auxiliary:
                    errors.append({"reason": "source_role_conflict", "format_id": chapter["format_id"],
                                   "block_id": block_id, "source_role": source_roles[block_id]["source_role"]})
                elif position is None or not anchor_position <= position < boundary or block_id in owners:
                    errors.append({"reason": "format_content_outside_structural_boundary", "format_id": chapter["format_id"], "block_id": block_id})
                else:
                    owners[block_id] = chapter
        for i in sorted(relevant):
            block = body[i]
            block_id, text = block["block_id"], block.get("text", "").strip()
            source_role = source_roles.get(block_id, {})
            if (block_id not in auxiliary and structural_level(block) is not None and block_id not in anchors and
                    re.search(r"(?:表|函|书|资料|方案|声明|承诺)$", text) and
                    not (_heading_level(text) == 0 and re.search(r"(?:投标|响应|报价).{0,18}(?:格式|组成)", text))):
                errors.append({"reason": "unextracted_format_heading", "block_id": block_id, "locator": block["locator"]})
            instruction_requirements = []
            if block_id in auxiliary:
                status, owner = source_role["source_role"], None
                if status == "preparation_instruction":
                    instruction_requirements, instruction_errors = _instruction_requirements(source_role, extracted, blocks)
                    errors.extend({"reason": reason, "block_id": block_id, "locator": block["locator"],
                                   "instruction_block_ids": source_role["instruction_block_ids"],
                                   "scope_block_ids": source_role.get("scope_block_ids", [])}
                                  for reason in instruction_errors)
            elif block_id in anchors and text == anchors[block_id]["title"].strip():
                status, owner = "chapter_boundary", anchors[block_id]
            elif _heading_level(text) == 0 and re.search(r"(?:投标|响应|报价).{0,18}(?:格式|组成)", text) and block_id not in owners:
                status, owner = "format_region_boundary", None
            elif block_id in owners:
                status, owner = "declared_content", owners[block_id]
            else:
                candidates = [chapter for chapter in formats if chapter["citation"]["block_id"] in positions and
                              positions[chapter["citation"]["block_id"]] <= i and
                              i < boundaries[chapter["citation"]["block_id"]] and
                              any(a <= positions[chapter["citation"]["block_id"]] <= i < b for a, b, _ in regions)]
                owner = max(candidates, key=lambda chapter: positions[chapter["citation"]["block_id"]]) if candidates else None
                if owner and structural_level(block) is None and block["kind"] != "image" and owner["status"] == "prescribed":
                    owner["content_block_ids"].append(block_id)
                    owner["content_block_ids"].sort(key=lambda key: blocks[key]["ordinal"])
                    status = "structural_continuation"
                else:
                    status = "unresolved"
                    errors.append({"reason": "unassigned_format_block", "block_id": block_id, "locator": block["locator"]})
            if block["kind"] == "image" and status not in {"chapter_boundary", "format_region_boundary"}:
                errors.append({"reason": "unread_editable_format_image", "block_id": block_id, "locator": block["locator"]})
            row = {"source_id": block["source_id"], "block_id": block_id, "locator": block["locator"],
                   "kind": block["kind"], "status": status, "format_id": owner["format_id"] if owner else None}
            if source_role:
                row.update(copy.deepcopy(source_role))
                row["instruction_requirements"] = instruction_requirements
            if block_id in letter_items:
                row["continuation_basis"] = letter_items[block_id]
            if block_id in boundaries:
                boundary = boundaries[block_id]
                row["boundary_basis"] = {
                    "block_id": body[boundary]["block_id"] if boundary < len(body) else None,
                    "locator": body[boundary]["locator"] if boundary < len(body) else None,
                    "reason": "next_grounded_format" if boundary < len(body) and body[boundary]["block_id"] in anchors else
                              "source_heading_or_region_end" if boundary < len(body) else "source_end"}
            ledger.append(row)
    covers = [row for row in ledger if row.get("source_role") == "prescribed_cover" and
              row["block_id"] == row["role_proof"]["members"][0]["block_id"]]
    if len(covers) > 1:
        errors.append({"reason": "multiple_prescribed_covers", "block_ids": [row["block_id"] for row in covers]})
    for row in covers:
        chapter = anchors.get(row["block_id"])
        member_ids = [item["block_id"] for item in row["role_proof"]["members"]]
        expected = set(member_ids)
        if (chapter is None or chapter["status"] != "prescribed" or
                {chapter["citation"]["block_id"], *chapter["content_block_ids"]} != expected or
                any(blocks[key]["kind"] != "paragraph" for key in member_ids)):
            errors.append({"reason": "prescribed_cover_source_incomplete", "block_id": row["block_id"]})
            continue
        chapter["source_role"] = "prescribed_cover"
        chapter["cover_source_proof"] = copy.deepcopy(row["role_proof"])
    return formats, ledger, errors


def _remote_format_resolution(requirement, chapter, ledger, blocks, windows, documents, coverage):
    """Derive a remote format observation only after full N1/N2 revalidation.

    The caller has already checked global format source errors. This cannot
    resolve an unread window, an unread template or an overlapping local quote.
    """
    title = requirement.get("format_title")
    if (requirement["format_status"] != "unread" or not title or chapter["status"] != "prescribed" or
            _title_key(title) != _title_key(chapter["title"])):
        return None
    anchor = chapter["citation"]["block_id"]
    source_id = chapter["citation"]["source_id"]
    if any(citation["source_id"] != source_id for citation in requirement["citations"]):
        return None
    content_ids = chapter["content_block_ids"]
    template_ids = {anchor, *content_ids}
    selected_ids = {key for citation in requirement["citations"]
                    for key in citation.get("block_ids") or [citation["block_id"]]}
    origin_windows = [window for window in windows if window["window_id"] in requirement["window_ids"]]
    if (not origin_windows or selected_ids & template_ids or any(
            anchor in {block["block_id"] for block in window["blocks"]} for window in origin_windows)):
        return None
    rows = [row for row in ledger if row.get("format_id") == chapter["format_id"]]
    by_id = {row["block_id"]: row for row in rows}
    if (not template_ids <= set(by_id) or any(by_id[key]["status"] not in {
            "chapter_boundary", "declared_content", "structural_continuation"} for key in template_ids) or
            any(blocks[key]["kind"] == "image" or blocks[key]["source_id"] != source_id or
                blocks[key].get("read_disposition") for key in template_ids)):
        return None
    body = [blocks[key] for key in content_ids if key != anchor and
            (blocks[key]["kind"] == "table" or blocks[key].get("text", "").strip())]
    if not body:
        return None
    source = next(document for document in documents if document["source_id"] == source_id)
    return {"requirement_id": requirement["requirement_id"], "format_id": chapter["format_id"],
            "original_format_status": "unread", "resolved_format_status": "prescribed",
            "basis": "complete_source_verified_remote_format_v1", "source_id": source_id,
            "source_sha256": source["sha256"], "requirement_citations": copy.deepcopy(requirement["citations"]),
            "format_citation": copy.deepcopy(chapter["citation"]), "content_block_ids": list(content_ids),
            "origin_window_ids": list(requirement["window_ids"]),
            "format_owner_window_id": next(window["window_id"] for window in windows
                if anchor in {block["block_id"] for block in window["blocks"]}),
            "format_source_coverage": copy.deepcopy(rows), "full_N2_coverage_sha256": _hash(coverage)}


def plan_bid(ctx, extracted: dict, parsed: dict, manifest: dict, call_json, config: dict) -> dict:
    """N3: freeze an automatic PreparedBid; never let a model fill values."""
    manifest = _json(manifest)
    _fail("map_table_fields" in config and type(config["map_table_fields"]) is not bool, "PLANNING_CONFIGURATION_INVALID")
    blocks, units, documents = _index(parsed, manifest)
    _fail(extracted.get("producer_version") != EXTRACTION_VERSION or extracted.get("parsed_hash") != _hash(parsed),
          "EXTRACTION_INPUT_MISMATCH")
    _fail(extracted.get("coverage", {}).get("status") != "complete", "EXTRACTION_COVERAGE_INCOMPLETE")
    frozen_window_config = extracted.get("window_config")
    _fail(not isinstance(frozen_window_config, dict) or set(frozen_window_config) != {"window_max_chars", "window_max_blocks"},
          "EXTRACTION_WINDOW_CONFIGURATION_MISSING")
    windows, denominator = _windows(documents, blocks, frozen_window_config)
    expected_coverage = {"status": "complete", "semantic_review": "requires_source_review", "tender_unit_count": len(denominator), "window_count": len(windows),
                         "completed_window_count": len(windows), "units": denominator}
    for row in denominator:
        row["window_ids"] = [window["window_id"] for window in windows if row["unit_id"] in window["unit_ids"]]
    _fail(extracted["coverage"].get("tender_unit_count") != len(denominator) or
          len(extracted.get("window_results", [])) != len(windows) or
          {w["window_id"] for w in windows} != {w["window_id"] for w in extracted.get("window_results", [])} or
          extracted["coverage"] != expected_coverage or
          extracted.get("source_hashes") != {d["source_id"]: d["sha256"] for d in documents},
          "EXTRACTION_DENOMINATOR_MISMATCH")
    by_window = {window["window_id"]: window for window in windows}
    verified_results = []
    for result in extracted["window_results"]:
        _fail(result.get("status") != "complete", "EXTRACTION_COVERAGE_INCOMPLETE")
        validated = _validate_normalized_window_result(result, by_window[result["window_id"]])
        verified_results.append({**result, "validated": validated})
    _fail(extracted.get("deferred_context_closure") != _close_deferred_context(windows, verified_results),
          "EXTRACTION_DEFERRED_CLOSURE_MISMATCH")
    expected_requirements, expected_formats = _merge_results(verified_results)
    _fail(expected_requirements != extracted.get("requirements") or expected_formats != extracted.get("format_chapters"),
          "EXTRACTION_MERGE_MISMATCH")
    clause_by_id = {}
    for window in windows:
        for clause in build_clause_catalog(window):
            _fail(clause["clause_id"] in clause_by_id and clause_by_id[clause["clause_id"]] != clause,
                  "CLAUSE_CATALOG_ID_CONFLICT")
            clause_by_id[clause["clause_id"]] = clause
    table_continuations = [proof for document in documents for proof in build_table_continuations(
        document, [block for block in blocks.values() if block["source_id"] == document["source_id"]], _pdf_footer_skips(blocks))]
    table_rows = [row for document in documents for row in build_table_row_catalog(
        document, [block for block in blocks.values() if block["source_id"] == document["source_id"]],
        [proof for proof in table_continuations if proof["source_id"] == document["source_id"]])]

    input_data = {"producer_version": PLANNING_VERSION, "extracted": extracted, "manifest": manifest,
                  "parsed_hash": _hash(parsed), "config": config}

    def assemble():
        planned_formats, format_ledger, format_errors = _format_plan(extracted, blocks, documents)
        writing_spaces = {}
        format_links = []
        remote_format_resolutions = []
        for requirement in extracted["requirements"]:
            title = requirement.get("format_title")
            matches = [chapter for chapter in planned_formats if title and _title_key(title) == _title_key(chapter["title"])]
            if not matches and not title:
                matches = [chapter for chapter in planned_formats if len(_title_key(chapter["title"])) >= 4 and
                           _title_key(chapter["title"]) in _normal(requirement["text"])]
            if len(matches) > 1:
                format_errors.append({"reason": "ambiguous_grounded_format_title", "requirement_id": requirement["requirement_id"],
                                      "format_ids": [chapter["format_id"] for chapter in matches]})
            elif len(matches) == 1:
                chapter = matches[0]
                if requirement["requirement_id"] not in chapter["requirement_refs"]:
                    chapter["requirement_refs"].append(requirement["requirement_id"])
                format_links.append({"requirement_id": requirement["requirement_id"], "format_id": chapter["format_id"],
                                     "basis": "exact_source_title_after_number_prefix_normalization", "title": chapter["title"]})
        for result in extracted["window_results"]:
            if result["validated"]["format_status"] in {"unread", "conflict"}:
                format_errors.append({"reason": "window_format_unresolved", "window_id": result["window_id"]})
        for chapter in planned_formats:
            if chapter["status"] != "prescribed":
                format_errors.append({"reason": "format_chapter_unresolved", "format_id": chapter["format_id"], "citation": chapter["citation"]})
        format_sources_complete = not format_errors
        for requirement in extracted["requirements"]:
            if any(citation["block_id"] in {row["block_id"] for row in format_ledger if row["status"] == "skipped_page_footer"}
                   for citation in requirement["citations"]):
                format_errors.append({"reason": "requirement_anchor_is_page_footer", "requirement_id": requirement["requirement_id"]})
            unresolved = requirement["format_status"] in {"unread", "conflict"}
            needs_format = (requirement["format_status"] == "prescribed" or requirement["response_mode"] in {"table", "fixed_text"} or
                            requirement["response_mode"] == "factual_response" and bool(requirement.get("format_title")))
            matching_formats = [chapter for chapter in planned_formats if requirement["requirement_id"] in chapter["requirement_refs"] or
                                _title_key(requirement.get("format_title") or "") == _title_key(chapter["title"])]
            located = bool(matching_formats)
            if requirement["response_mode"] == "table":
                located = any(blocks[key]["kind"] == "table" for chapter in matching_formats for key in chapter["content_block_ids"])
            elif requirement["response_mode"] == "fixed_text":
                located = any(blocks[key]["kind"] == "paragraph" and blocks[key].get("text", "").strip() != chapter["title"].strip()
                              for chapter in matching_formats for key in chapter["content_block_ids"])
            elif requirement["response_mode"] == "factual_response" and needs_format:
                located = any(blocks[key]["kind"] == "table" or
                              (key != chapter["citation"]["block_id"] and blocks[key]["kind"] == "paragraph" and
                               bool(blocks[key].get("text", "").strip()) and
                               _title_key(blocks[key]["text"]) != _title_key(chapter["title"]))
                              for chapter in matching_formats for key in chapter["content_block_ids"])
            if (format_sources_complete and located and len(matching_formats) == 1 and
                    requirement["format_status"] == "unread"):
                resolution = _remote_format_resolution(requirement, matching_formats[0], format_ledger,
                    blocks, windows, documents, extracted["coverage"])
                if resolution is not None:
                    remote_format_resolutions.append(resolution)
                    unresolved = False
            if unresolved or (needs_format and not located):
                format_errors.append({"reason": "required_response_format_unresolved", "requirement_id": requirement["requirement_id"],
                                      "citations": requirement["citations"]})
            if requirement["response_mode"] == "writing" and requirement["category"] == "technical":
                for chapter in matching_formats:
                    evidence = _writing_space(requirement, chapter, blocks)
                    if not evidence:
                        format_errors.append({"reason": "technical_writing_space_not_grounded", "requirement_id": requirement["requirement_id"],
                                              "format_id": chapter["format_id"], "citation": chapter["citation"]})
                    else:
                        writing_spaces[(chapter["format_id"], requirement["requirement_id"])] = evidence
        _capture(ctx, "n3-format-coverage", {"producer_version": PLANNING_VERSION, "blocks": format_ledger,
                                             "remote_format_resolutions": remote_format_resolutions,
                                             "errors": format_errors, "status": "failed" if format_errors else "complete"})
        _fail(bool(format_errors), "REQUIRED_FORMAT_UNRESOLVED")
        sources = copy.deepcopy(manifest["sources"])
        source_map = {item["source_id"]: item for item in sources}
        facts = _fact_index(manifest)
        fact_map = {fact["fact_id"]: fact for fact in facts}
        requirements, details = [], {}
        sections, tables, attachments, issues, references = [], [], [], [], []
        prescribed_cover_section_id = None
        table_owners, attachment_owners, section_formats = {}, {}, {}
        placements, table_reports, attachment_reports = [], [], []
        writing_space_reports = []
        instruction_omissions = []
        factual_response_reports = []

        def scoped_source(citation):
            checked = _citation(_wire_citation(citation), blocks, _pdf_footer_skips(blocks), list(clause_by_id.values()), table_continuations, table_rows)
            source_id = _id("SCOPE-", checked)
            if source_id not in source_map:
                parent = source_map[checked["source_id"]]
                item = {**parent, "source_id": source_id,
                        "locator": f"{checked['locator']}; basis={checked['basis']}; {'selected_row_view_chars' if checked.get('table_row_id') else 'chars'}[{checked['start']}:{checked['end']}]; quote_sha256={checked['quote_sha256']}"}
                if checked.get("cell_refs"):
                    item["locator"] += "; cells=" + ",".join(
                        (cell["block_id"] + ":" if cell.get("block_id") else "") + f"r{cell['row']}c{cell['column']}"
                        for cell in checked["cell_refs"])
                if checked.get("block_ids"):
                    item["locator"] += "; through=" + checked["end_locator"] + "; blocks=" + ",".join(checked["block_ids"])
                SourceRef.model_validate(item)
                source_map[source_id] = item
                sources.append(item)
            return source_id

        context_derivations, source_context_proofs = {}, {}
        for row in format_ledger:
            for handler in row.get("instruction_requirements", []):
                derivation = handler.get("source_context_derivation")
                if derivation:
                    rid = handler["requirement_id"]
                    _fail(rid in context_derivations and context_derivations[rid] != derivation,
                          "SOURCE_CONTEXT_DERIVATION_AMBIGUOUS")
                    context_derivations[rid] = derivation

        for raw in extracted["requirements"]:
            citations = raw.get("citations", [])
            _fail(not citations or any(c["quote"] != raw["text"] for c in citations), "REQUIREMENT_CITATION_MISMATCH")
            requirement = {key: raw[key] for key in ("requirement_id", "text", "category", "mandatory", "response_mode")}
            requirement["source_refs"] = list(dict.fromkeys(scoped_source(c) for c in citations))
            requirement["source_contexts"] = []
            if raw["requirement_id"] in context_derivations:
                payload = {**context_derivations[raw["requirement_id"]], "child_source_refs": requirement["source_refs"]}
                proof_sha = context_digest(payload)
                proof = {**payload, "proof_id": "CTXP-" + proof_sha[:24], "proof_sha256": proof_sha}
                source_context_proofs[proof["proof_id"]] = SourceContextProof.model_validate(proof).model_dump(mode="json")
                for parent in proof["scope_members"]:
                    block = blocks[parent["block_id"]]
                    operator = source_context_operator(block["text"])
                    context = {"kind": "applicability_condition" if operator == "service_applicability" else "assembly_constraint",
                        "operator": operator, "text": block["text"], "parent_block_id": block["block_id"],
                        "source_refs": [scoped_source({key: block[key] for key in ("source_id", "block_id", "locator")})],
                        "proof_ref": proof["proof_id"], "proof_sha256": proof_sha, "review_status": "not_checked"}
                    context["context_id"] = "CTX-" + context_digest(context)[:24]
                    requirement["source_contexts"].append(RequirementSourceContext.model_validate(context).model_dump(mode="json"))
            requirements.append(requirement)
            # The original N2 observation is immutable; only N3's derived
            # planning view adopts a separately captured source resolution.
            details[raw["requirement_id"]] = ({**raw, "format_status": "prescribed"} if any(
                item["requirement_id"] == raw["requirement_id"] for item in remote_format_resolutions) else raw)

        review_actions = []
        for requirement in requirements:
            if requirement["response_mode"] != "procedure":
                continue
            commercial = requirement["category"] == "commercial"
            review_actions.append({"action_id": _id("RA-", requirement["requirement_id"]),
                "kind": "commercial_constraint" if commercial else "procedure",
                "status": "not_checked" if commercial else "pending_manual",
                "requirement_refs": [requirement["requirement_id"]], "source_refs": requirement["source_refs"],
                "source_requirement": requirement["text"],
                "instruction": "按来源规则核对本项目明确输入；尚未判断满足，不以规则值代替报价或履约承诺。" if commercial else
                               "按来源要求线下办理并核对；本记录不代表事项已经完成。"})

        def issue(section, refs, title, reason, action):
            value = {"issue_id": f"M-{len(issues) + 1:03d}", "title": title, "section_id": section["section_id"],
                     "requirement_refs": list(dict.fromkeys(refs)), "reason": reason, "action": action,
                     "source_requirement": "；".join(details[ref]["text"] for ref in refs), "severity": "missing"}
            issues.append(value)
            return value["issue_id"]

        def adopt_facts_or_mark_missing(section, rid, detail):
            labels = detail.get("required_fact_labels", [])
            current_status = (detail["response_mode"] == "factual_response" and
                              bool(_CURRENT_ELIGIBILITY_STATUS.search(detail["text"])))
            decisions = []
            for label in labels:
                matches = _label_facts(label, facts, source_map, manifest.get("source_roles", {}),
                                       require_project_input=current_status)
                decisions.append({"label": label,
                    "candidate_fact_ids": [fact["fact_id"] for fact in facts if _canonical(fact["label"]) == _canonical(label)],
                    "adopted_fact_ids": [matches[0]["fact_id"]] if matches else [],
                    "status": "source_fact_selected_for_review" if matches else "missing_unique_eligible_fact"})
                if matches:
                    if matches[0]["fact_id"] not in section["fact_refs"]:
                        section["fact_refs"].append(matches[0]["fact_id"])
                else:
                    issue(section, [rid], label + "待补", "冻结资料中无唯一、可采用的明确事实值。", "补充该字段原件或本项目明确输入。")
            if not labels:
                issue(section, [rid], "事实响应资料待完善", "该要求不是可自由起草的技术方法，且无明确可填字段。", "补充有来源的事实或指定响应格式。")
            if detail["response_mode"] == "factual_response":
                factual_response_reports.append({"requirement_id": rid, "section_id": section["section_id"],
                    "adoption_policy": "explicit_project_status_input_required" if current_status else "source_backed_unique_fact",
                    "semantic_review": "requires_source_review", "decisions": decisions,
                    "status": "no_fact_labels_pending_manual" if not labels else "facts_checked_for_eligibility"})

        def new_section(identity, title, refs, level=1, source_refs=None):
            section = {"section_id": _id("SEC-", identity), "title": title, "kind": "facts",
                       "requirement_refs": list(dict.fromkeys(refs)), "source_refs": source_refs or [],
                       "fact_refs": [], "table_refs": [], "attachment_refs": [], "writing_tasks": [],
                       "fixed_content": [], "nonwriting_requirement_refs": [], "level": level,
                       "start_on_new_page": False, "render_body": True}
            sections.append(section)
            return section

        assigned = set()
        for chapter in sorted(planned_formats, key=lambda value: blocks[value["citation"]["block_id"]]["ordinal"]):
            title_key = _title_key(chapter["title"])
            refs = list(dict.fromkeys([*chapter["requirement_refs"],
                         *(r["requirement_id"] for r in extracted["requirements"] if _title_key(r.get("format_title") or "") == title_key)]))
            refs = [ref for ref in refs if ref not in assigned]
            # The prescribed chapter itself is a grounded structural obligation,
            # even if no semantic requirement was extracted beside its heading.
            structural_citation = chapter["citation"]
            if chapter.get("source_role") == "prescribed_cover":
                cover_ids = [item["block_id"] for item in chapter["cover_source_proof"]["members"]]
                first = blocks[cover_ids[0]]
                structural_citation = _citation({"source_id": first["source_id"], "block_id": cover_ids[0],
                    "locator": first["locator"], "block_ids": cover_ids}, blocks, _pdf_footer_skips(blocks))
            structural_id = _id("R-FORM-", [chapter["format_id"], structural_citation])
            structural = {"requirement_id": structural_id, "text": structural_citation["quote"],
                          "category": "fixed_table" if any(blocks[key]["kind"] == "table" for key in chapter["content_block_ids"]) else "procedure",
                          "mandatory": True, "response_mode": "fixed_text",
                          "source_refs": [scoped_source(structural_citation)]}
            requirements.append(structural)
            details[structural_id] = {**structural, "format_status": chapter["status"]}
            refs.append(structural_id)
            section = new_section(chapter["format_id"], chapter["title"], refs, chapter["level"], structural["source_refs"])
            if chapter.get("source_role") == "prescribed_cover":
                _fail(prescribed_cover_section_id is not None, "MULTIPLE_PRESCRIBED_COVERS")
                prescribed_cover_section_id = section["section_id"]
            section_formats[section["section_id"]] = chapter["status"]
            assigned.update(refs)
            permission_blocks = {key for (format_id, _), citations in writing_spaces.items()
                                 if format_id == chapter["format_id"] for citation in citations
                                 for key in citation.get("block_ids", [citation["block_id"]])}
            for block_id in chapter["content_block_ids"]:
                block = blocks[block_id]
                source_ref = scoped_source({"source_id": block["source_id"], "block_id": block_id,
                                            "locator": block["locator"], "quote": block["text"]}) if block.get("text", "").strip() else block["source_id"]
                if block["kind"] == "paragraph":
                    if not block.get("text", "").strip() or block["text"].strip() == chapter["title"].strip():
                        continue
                    if block_id in permission_blocks and _writing_instruction_only(block["text"]):
                        # This is the grounded permission to draft, not text
                        # that the final bidder should present as its answer.
                        instruction_omissions.append({"format_id": chapter["format_id"], "block_id": block_id,
                            "source_ref": source_ref, "basis": "whole_grounded_drafting_instruction",
                            "text_sha256": hashlib.sha256(block["text"].encode()).hexdigest()})
                        continue
                    section["fixed_content"].append({"kind": "paragraph", "text": block["text"], "source_refs": [source_ref]})
                    cover_blank = (chapter.get("source_role") == "prescribed_cover" and (
                        re.fullmatch(r"[^：:\n]{1,60}[：:]\s*", block["text"]) or
                        re.fullmatch(r"\s*年\s*月\s*日\s*", block["text"]) or "盖单位章" in block["text"]))
                    if has_fill_marker(block["text"]) or re.search(r"签字|盖章", block["text"]) or cover_blank:
                        issue(section, refs, "指定文字格式仍有待填写或签章字段", "原文格式按原样保留；未推定空白、日期或签章。", "按原格式填写明确输入并依法完成签章。")
                elif block["kind"] == "table":
                    _fail(block_id in table_owners, "PRESCRIBED_TABLE_MULTIPLE_OWNERS")
                    table_owners[block_id] = section["section_id"]
                    rows, merges, covered = _raw_grid(block)
                    fills, report = _table_mapping(ctx, block, facts, source_map, manifest.get("source_roles", {}), call_json, config)
                    table_reports.append(report)
                    table_id = _id("TABLE-", [block["source_id"], block_id])
                    cells = [[{"text": text, "fact_refs": [], "issue_refs": []} for text in row] for row in rows]
                    for r, row in enumerate(rows):
                        for c, text in enumerate(row):
                            if (r, c) in covered:
                                continue
                            if (r, c) in fills:
                                fact = fact_map[fills[(r, c)]]
                                cells[r][c] = {"text": fact["value"], "fact_refs": [fact["fact_id"]], "issue_refs": []}
                            elif whole_cell_slot(text) or has_fill_marker(text):
                                marker_id = issue(section, refs, f"{chapter['title']}原表第{r + 1}行第{c + 1}列待填写",
                                                  "原表空白格无唯一、允许使用的明确事实值。", "补充本项目明确输入；保持原表标签、单位和行列结构。")
                                cells[r][c] = {"text": text, "fact_refs": [], "issue_refs": [marker_id]}
                    header_rows = block.get("header_rows", [])
                    _fail(any(type(i) is not int or i < 0 or i >= len(rows) for i in header_rows), "SOURCE_TABLE_HEADER_INVALID")
                    repeat = 0
                    while repeat in header_rows:
                        repeat += 1
                    tables.append({"table_id": table_id, "section_id": section["section_id"], "title": chapter["title"],
                                   "columns": [f"C{i + 1}" for i in range(len(rows[0]))], "rows": cells,
                                   "source_refs": [source_ref], "requirement_refs": refs, "merges": merges,
                                   "column_widths": block.get("column_widths", []), "header_from_columns": False,
                                   "repeat_header_rows": repeat})
                    section["table_refs"].append(table_id)
                    section["fixed_content"].append({"kind": "table_ref", "table_ref": table_id, "source_refs": [source_ref]})
                else:
                    issue(section, refs, "指定格式图片待处理", "图片格式不能冒充可编辑文字或表格。", "补充可读取原格式或明确允许采用的图片页。")

        for requirement in extracted["requirements"]:
            rid = requirement["requirement_id"]
            if rid in assigned:
                continue
            section = new_section(rid, requirement["text"].strip().replace("\n", " ")[:70], [rid],
                                  source_refs=next(r["source_refs"] for r in requirements if r["requirement_id"] == rid))
            assigned.add(rid)
            if requirement["response_mode"] == "procedure":
                section["render_body"] = False

        for section in sections:
            for rid in section["requirement_refs"]:
                detail = details[rid]
                placements.append({"requirement_id": rid, "section_id": section["section_id"],
                                   "response_mode": detail["response_mode"], "render_body": section["render_body"]})
                if rid.startswith("R-FORM-") or detail["response_mode"] == "procedure":
                    continue
                if section_formats.get(section["section_id"]) in {"unread", "conflict"}:
                    raise PlanningError("REQUIRED_FORMAT_UNRESOLVED")
                if detail.get("format_status") in {"unread", "conflict"}:
                    raise PlanningError("REQUIRED_FORMAT_UNRESOLVED")
                if detail.get("format_status") == "prescribed" and section["section_id"] not in section_formats:
                    raise PlanningError("REQUIRED_FORMAT_UNRESOLVED")
                mode = detail["response_mode"]
                if mode == "writing" and detail["category"] == "technical":
                    if section["level"] >= 3:
                        issue(section, [rid], "技术写作层级待安排", "原格式已到第三级，当前合同不能再生成第四级标题。", "调整目录落点并保持招标格式。")
                        continue
                    reference = _reference(detail, blocks, units, source_map)
                    if reference and reference["reference_id"] not in {item["reference_id"] for item in references}:
                        references.append(reference)
                    task = {"unit_id": _id("WRITE-", [section["section_id"], rid]),
                            "title": detail["text"].strip().replace("\n", " ")[:60],
                            "objective": "依据该招标原文编写本项目拟实施方法，明确流程、控制点、记录和交付边界。",
                            "must_cover": [detail["text"]], "requirement_refs": [rid],
                            "reference_refs": [reference["reference_id"]] if reference else [],
                            "forbidden_fields": ["不得虚构公司既有人员、设备、资质、业绩或完成情况", "不得生成未明确输入的报价、人名、证照号或日期",
                                                 "不得扩大维修、更换材料、即时处置等未经授权的义务", "不得改变指定格式、标签、单位或合并"],
                            "completion_criteria": ["覆盖原文要求并标识拟实施方法", "给出可操作流程和记录交付，异常按授权边界报告", "不引入历史项目或公司事实"],
                            "operation": "reference_adaptation" if reference else "bounded_new_draft",
                            "format_status": "not_prescribed",
                            "no_reference_reason": None if reference else "冻结参考资料中未检索到具有明确关键词适用性且不混入历史事实的可读方法片段。",
                            "table_refs": []}
                    section["kind"] = "technical"
                    section["writing_tasks"].append(task)
                    evidence = [citation for (format_id, requirement_id), citations in writing_spaces.items()
                                if requirement_id == rid for citation in citations]
                    evidence_refs = [scoped_source(citation) for citation in evidence]
                    section["source_refs"] = list(dict.fromkeys([*section["source_refs"], *evidence_refs]))
                    writing_space_reports.append({"unit_id": task["unit_id"], "requirement_id": rid, "section_id": section["section_id"],
                        "section_format_status": detail["format_status"], "task_body_format_status": "not_prescribed",
                        "decision": "grounded_open_body_in_prescribed_section" if evidence else "no_prescribed_body_after_complete_extraction",
                        "source_refs": evidence_refs, "citations": evidence})
                elif mode == "factual_response":
                    _fail(detail["category"] not in {"qualification", "commercial"}, "FACTUAL_RESPONSE_CATEGORY_INVALID")
                    # Preserve the complete condition, including time and
                    # exceptions, as a source rule rather than a company claim.
                    requirement = next(item for item in requirements if item["requirement_id"] == rid)
                    section["fixed_content"].append({"kind": "paragraph", "text": "响应要求（招标原文）：\n" + detail["text"],
                                                     "source_refs": requirement["source_refs"]})
                    adopt_facts_or_mark_missing(section, rid, detail)
                elif mode == "attachment":
                    keywords = detail.get("attachment_keywords", [])
                    matches = [item for item in manifest.get("attachments", []) if keywords and
                               all(word in (item["title"] + " " + item["description"]) for word in keywords)]
                    def proof_group(item):
                        return ("bundle", item["bundle_id"]) if item.get("bundle_id") else ("single", item["attachment_id"])
                    bundle_keys = {proof_group(item) for item in matches}
                    attachment_reports.append({"requirement_id": rid, "status": "matched" if len(bundle_keys) == 1 else "ambiguous" if matches else "no_match",
                                               "candidate_ids": [item["attachment_id"] for item in matches],
                                               "candidate_bundle_ids": [identity for _, identity in sorted(bundle_keys)],
                                               "candidate_groups": [{"kind": kind, "identity": identity} for kind, identity in sorted(bundle_keys)],
                                               "scope": "冻结证明附件匹配；不构成额外资格或公司主体核验"})
                    if len(bundle_keys) == 1:
                        bundle = next(iter(bundle_keys))
                        selected_pages = sorted([item for item in manifest["attachments"]
                            if proof_group(item) == bundle], key=lambda item: item.get("page_index", 1))
                        if re.search(r"有效|未过期|等级|级资质|到期|不少于|至少", detail["text"]):
                            issue(section, [rid], "证明适用条件仍需明确资料", "已定位证明，但当前快照没有可核验的有效期、等级或数量条件字段。", "补充条件字段及原件定位；不能仅凭关键词判定满足资格。")
                        cross_referenced = set()
                        for selected in selected_pages:
                            owner = attachment_owners.get(selected["attachment_id"])
                            if owner:
                                adopted = next(a for a in attachments if a["attachment_id"] == selected["attachment_id"])
                                if rid not in adopted["requirement_refs"]:
                                    adopted["requirement_refs"].append(rid)
                                if owner is not section and owner["section_id"] not in cross_referenced:
                                    section["fixed_content"].append({"kind": "paragraph", "text": "证明材料见“" + owner["title"] + "”章节。",
                                                                     "source_refs": selected["source_refs"]})
                                    cross_referenced.add(owner["section_id"])
                            else:
                                adopted = {**selected, "section_id": section["section_id"], "requirement_refs": [rid]}
                                attachments.append(adopted)
                                section["attachment_refs"].append(selected["attachment_id"])
                                attachment_owners[selected["attachment_id"]] = section
                    else:
                        issue(section, [rid], "所需证明附件未能唯一匹配", "冻结库无匹配证明或存在多个候选，未按文件名猜选。", "补充或明确选择符合本次要求的证明页。")
                elif mode in {"table", "fixed_text"}:
                    if not section["fixed_content"]:
                        issue(section, [rid], "指定响应格式未定位", "要求需要原文格式，但没有通过引用校验的格式内容。", "补充并定位指定格式，不能改为自由写作。")
                else:
                    adopt_facts_or_mark_missing(section, rid, detail)
            if section["writing_tasks"]:
                # Structural obligations/fixed items are already represented by
                # fixed_content; the existing task contract demands exact cover.
                writing_refs = {ref for task in section["writing_tasks"] for ref in task["requirement_refs"]}
                other_refs = set(section["requirement_refs"]) - writing_refs
                section["nonwriting_requirement_refs"] = sorted(other_refs)
            if section["render_body"] and not any((section["fact_refs"], section["table_refs"], section["attachment_refs"],
                                                     section["writing_tasks"], section["fixed_content"],
                                                     any(item["section_id"] == section["section_id"] for item in issues))):
                issue(section, section["requirement_refs"], "指定章节内容待补", "已定位章节标题，但没有可装配的正文、原表或证明内容。", "补充该章节原始格式与资料后重新读取。")

        requirement_map = {item["requirement_id"]: item for item in requirements}
        for section in sections:
            for rid in section["requirement_refs"]:
                for context in requirement_map[rid].get("source_contexts", []):
                    section["source_refs"] = list(dict.fromkeys([*section["source_refs"], *context["source_refs"]]))
                    issues.append({"issue_id": f"M-{len(issues) + 1:03d}", "section_id": section["section_id"],
                        "requirement_refs": [rid], "severity": "warning", "source_requirement": context["text"],
                        "title": "服务适用条件待核验" if context["operator"] == "service_applicability" else "技术文件报价分离待核对",
                        "reason": "原文父条件已保留，尚未核验本项目适用性或最终正文的实质符合性。",
                        "action": "按原文确认适用范围并审查实际响应；该提醒不替代技术正文或代表已满足。"})
            for task in section["writing_tasks"]:
                contexts = [context for requirement in requirements if requirement["requirement_id"] in task["requirement_refs"]
                            for context in requirement.get("source_contexts", [])]
                task["source_context_refs"] = list(dict.fromkeys(context["context_id"] for context in contexts))
                task["forbidden_fields"].extend(context["text"] for context in contexts
                                                if context["operator"] == "exclude_commercial_quotation")
                if contexts:
                    task["completion_criteria"].append("父条件适用性尚未核验，不得声称已确认适用或将拟实施内容表述为公司事实。")

        instruction_handling, instruction_errors = _instruction_placements(
            format_ledger, sections, attachments, review_actions, issues)
        _capture(ctx, "n3-instruction-handling", {"items": instruction_handling, "errors": instruction_errors,
                                               "status": "failed" if instruction_errors else "mapped_pending"})
        _fail(bool(instruction_errors), "PREPARATION_INSTRUCTION_HANDLING_UNRESOLVED")
        output = PreparedBid.model_validate({"input_kind": "automatic", "synthetic": manifest.get("synthetic", False),
                    "project": manifest["project"], "company": manifest["company"], "sources": sources, "facts": facts,
                    "requirements": requirements, "attachments": attachments, "issues": issues,
                    "source_context_proofs": list(source_context_proofs.values()),
                    "review_actions": review_actions,
                    "prescribed_cover_section_id": prescribed_cover_section_id,
                    "reference_excerpts": references, "style": manifest.get("style", {}), "sections": sections, "tables": tables,
                    "preparation_note": manifest.get("preparation_note") or "系统依据冻结招标原文及固定公司资料自动编排；缺项按编号保留。"}).model_dump(mode="json")
        report = {"producer_version": PLANNING_VERSION, "parsed_hash": _hash(parsed),
                  "requirement_placements": placements, "tables": table_reports, "attachments": attachment_reports,
                  "format_block_coverage": format_ledger,
                  "instruction_handling": instruction_handling,
                  "source_context_proofs": list(source_context_proofs.values()),
                  "format_links": format_links, "writing_space_decisions": writing_space_reports,
                  "remote_format_resolutions": remote_format_resolutions,
                  "prescribed_cover": {"section_id": prescribed_cover_section_id,
                      "source_proofs": [chapter["cover_source_proof"] for chapter in planned_formats
                                        if chapter.get("source_role") == "prescribed_cover"],
                      "values_policy": "preserve_source_slots_with_explicit_missing_issues_no_guessed_values"},
                  "drafting_instruction_omissions": instruction_omissions,
                  "factual_responses": factual_response_reports,
                  "review_actions": review_actions,
                  "counts": {"requirements": len(requirements), "sections": len(sections), "writing_tasks": sum(len(s["writing_tasks"]) for s in sections),
                             "tables": len(tables), "attachments": len(attachments), "issues": len(issues),
                             "review_actions": len(review_actions), "pending_review": len(review_actions),
                             "commercial_constraints": sum(action["kind"] == "commercial_constraint" for action in review_actions),
                             "procedure_actions": sum(action["kind"] == "procedure" for action in review_actions)},
                  "coverage": extracted["coverage"], "status": "ok_with_issues" if issues or review_actions else "ok"}
        from .material_matching import material_candidate_report
        report["material_candidates"] = material_candidate_report(requirements, manifest.get("material_records", []))
        _capture(ctx, "n3-planning-report", report)
        return {"prepared": output, "report": report}

    return ctx.run_unit("plan-assemble", input_data, assemble)["prepared"]
