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
    _chapter_boundary, _repair_numbered_procedure_spans,
)
from .table_continuations import build_table_continuations, build_table_row_catalog
from .preparation_rules import preparation_rule, manual_signing_submission, proof_compilation
from .plan_component_controls import collect_plan_controls, component_targets
from .project_constraint_runtime import plan_project_constraints
from .format_continuation_sources import fixed_letter_items as _fixed_letter_items
from .contract_pending_plan import contract_pending_plan
from .contract_review_slots import _CONTRACT as _CONTRACT_SOURCE_HEADING


PLANNING_VERSION = "bid-plan-v27"
# Preserve the unchanged, independently checkpointed field-mapping request.
MAPPING_VERSION = "bid-plan-v24"


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


def _response_writing_control(requirement: dict, blocks: dict) -> str | None:
    """Recognize complete instructions about a response, not a method to draft."""
    if requirement["category"] != "technical" or requirement["response_mode"] != "writing":
        return None
    patterns = {
        "single_response_scheme": r"(?:\d+(?:\.\d+)*\s*)?响应人只能提出唯一的响应方案[。.]响应人在响应文件中提出多个响应方案的[，,]其响应文件将被视为无效[。.]",
        "substantive_response_scope": r"(?:\d+(?:\.\d+)*\s*)?响应文件应当对采购文件有关工期[、,，]质量要求[、,，]技术标准和要求[、,，]采购范围等实质性内容作出响应[。.]",
    }
    text = re.sub(r"\s+", "", requirement["text"])
    rule = next((key for key, pattern in patterns.items() if re.fullmatch(pattern, text)), None)
    if not rule or len(requirement["citations"]) != 1:
        return None
    citation = _citation(_wire_citation(requirement["citations"][0]), blocks, _pdf_footer_skips(blocks))
    values = [blocks[key] for key in (citation.get("block_ids") or [citation["block_id"]])]
    if (not _native_source_span(values) or citation["quote"] != requirement["text"] or
            citation["quote"] != "\n".join(value["text"] for value in values)):
        return None
    return rule


def _material_match_report(ctx, requirements: list, manifest: dict, parsed: dict, *, recall_settings=None, recall_handled=None) -> dict:
    """Persist one shared inventory and independently resumable query results.

    Full text stays in controlled inputs/outputs; ordinary unit events contain
    only identifiers and hashes.  Even an empty result has a checkpoint so a
    completed search is distinguishable from an interrupted one.
    """
    from .material_matching import material_candidate_report, MATERIAL_MATCHING_VERSION as MATCHING_VERSION

    records = manifest.get("material_records", [])
    scope = {"attachments": manifest.get("attachments", []), "sources": manifest["sources"],
             "evidence_selections": manifest.get("evidence_selections"),
             "proof_derivations": manifest.get("proof_derivations", []), "parsed": parsed}
    scope_hash = _hash({"records": records, **scope})
    base = ctx.run_unit("material-index", {"matching_version": MATCHING_VERSION,
        "scope_sha256": scope_hash, "parsed_sha256": _hash(parsed)},
        lambda: material_candidate_report([], records, **scope))
    report = copy.deepcopy(base)
    query_keys = ("queries", "proof_queries", "assembly_candidates")
    for key in query_keys:
        report[key] = []
    report["checkpoint_units"] = []
    for requirement in requirements:
        unit_id = _id("material-query-", requirement["requirement_id"])
        def evaluate(requirement=requirement):
            result = material_candidate_report([requirement], records, **scope)
            return {key: result[key] for key in query_keys}
        result = ctx.run_unit(unit_id, {"matching_version": MATCHING_VERSION,
            "scope_sha256": scope_hash, "inventory_sha256": _hash(base), "requirement": requirement}, evaluate)
        for key in query_keys:
            report[key].extend(result[key])
        report["checkpoint_units"].append({"unit_id": unit_id, "requirement_id": requirement["requirement_id"],
                                           "output_sha256": _hash(result)})
    report["matching_version"] = MATCHING_VERSION
    if recall_settings is not None:
        from .material_recall import integrate_material_recall
        report = integrate_material_recall(ctx, requirements, manifest, parsed, report, recall_settings, recall_handled)
        if recall_settings.get("read_selected_originals", False):
            from .material_content_matching import integrate_original_content
            _capture(ctx, "n3-material-matching-catalog-stage", report)
            report = integrate_original_content(ctx, requirements, manifest, parsed, report,
                                                recall_settings.get("original_ocr"))
    _capture(ctx, "n3-material-matching", report)
    return report


def _attachment_candidates(detail: dict, attachments: list, material_report: dict) -> tuple[list, dict]:
    """Consume only the shared source-bound matcher's assembly decisions."""
    rid = detail["requirement_id"]
    query = next((row for row in material_report["proof_queries"] if row["requirement_id"] == rid), None)
    allowed = {aid for row in material_report["assembly_candidates"] if row["requirement_id"] == rid
               for aid in row["attachment_ids"]}
    policy = {"rule": "unified_source_bound_material_match", "matching_version": material_report["matching_version"],
              "query_status": query["status"] if query else "not_searched",
              "request": copy.deepcopy(query["request"]) if query else None,
              "eligibility_status": "not_determined", "query": copy.deepcopy(query)}
    return [item for item in attachments if item["attachment_id"] in allowed], policy


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
        payload = {"producer_version": MAPPING_VERSION, "grid_sha256": grid_hash, "rows": rows,
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


def _guard_historical_table_fills(block, blocks, fills, fact_map, manifest):
    """A generic project identity cannot identify a row of historical work."""
    rows, _, _ = _raw_grid(block)
    prior = [b for b in blocks.values() if b["source_id"] == block["source_id"] and
             b.get("unit_id") == block.get("unit_id") and b.get("page") == block.get("page") and
             b.get("ordinal", -1) < block.get("ordinal", -1)]
    caption = max(prior, key=lambda b: b["ordinal"]) if prior else None
    headers = [_normal(value) for value in rows[0]]
    columns = [c for c, label in enumerate(rows[0]) if _canonical(label) == _canonical("项目名称")]
    record_labels = (bool(set(headers) & {"发包人", "建设单位", "业主"}),
                     bool(set(headers) & {"服务日期", "服务时间", "合同日期"}),
                     any(re.fullmatch(r"合同(?:额|金额)(?:万元|元)?", value) for value in headers))
    historical = (caption is not None and caption["kind"] == "paragraph" and
        caption.get("origin") == block.get("origin") == "native" and
        re.fullmatch(r"(?:响应人|投标人|供应商|企业)?(?:历史业绩|业绩|已完成项目)(?:统计|一览|汇总|情况)表(?:后附业绩材料)?",
                     _title_key(caption["text"])) is not None and bool(columns) and sum(record_labels) >= 2)
    report = {"rule": "source_historical_project_row_v1", "block_id": block["block_id"],
              "source_id": block["source_id"], "locator": block["locator"],
              "source_file_sha256": next(s["sha256"] for s in manifest["sources"] if s["source_id"] == block["source_id"]),
              "source_grid_sha256": _hash(rows), "scope": "historical_performance" if historical else "not_proven_historical",
              "source_caption": ({**{key: caption[key] for key in
                  ("block_id", "source_id", "unit_id", "page", "ordinal", "locator", "text")},
                  "text_sha256": hashlib.sha256(caption["text"].encode()).hexdigest()} if historical else None),
              "blocked": [], "original_filled_count": len(fills), "retained_filled_count": len(fills)}
    kept = dict(fills)
    if historical:
        for (row, column), fact_id in fills.items():
            fact = fact_map[fact_id]
            if row <= 0 or column not in columns or _canonical(fact["label"]) != _canonical("项目名称"):
                continue
            # Atomic fact labels have no historical row/tuple binding. Retain
            # source-printed history, but never turn a current identity (or an
            # otherwise unbound name) into the first past-performance record.
            del kept[(row, column)]
            report["blocked"].append({"row": row, "column": column, "fact": copy.deepcopy(fact),
                "is_current_project_identity": fact["value"] == manifest["project"]["name"],
                "reason": "historical_row_requires_explicit_source_bound_record_not_generic_project_identity"})
    report["retained_filled_count"] = len(kept)
    return kept, report


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


def _writing_instruction_only(text: str) -> bool:
    """Recognize whole drafting instructions, never a mixed fixed declaration."""
    return re.fullmatch(
        r"[（(]?\s*(?:注[：:]\s*)?(?:响应人|投标人|供应商)?\s*(?:可以|可|应当|应|须|需)?\s*"
        r"(?:本(?:项目)?的?)?(?:(?:技术|服务|施工|实施|设计)?方案)?\s*"
        r"(?:自行编(?:写|制)(?:技术方案|方案)?|自拟(?:格式|方案)|格式自拟|自定义格式|自定格式)"
        r"(?:[，,]\s*根据[^。；;！？!?\n]{1,120}(?:要求|规定)编(?:写|制))?\s*[）)]?[。.]?",
        text.strip()) is not None


def _signable_drafting_instruction_only(text: str) -> bool:
    """A whole drafting/signing instruction is not a bidder declaration."""
    return re.fullmatch(
        r"[（(]?\s*(?:格式自拟|格式自定|自定格式|自拟格式)[，,]\s*"
        r"(?:要求|须有|应有|须|应)(?:响应人|投标人|供应商)?"
        r"(?:法定代表人|法人代表)(?:或授权代表)?(?:签字|签署)并加盖(?:单位)?公章"
        r"\s*[）)]?[。.]?", text.strip()) is not None


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
    # These closed administrative sentences can have a deterministic N3
    # handler. They do not repair or rewrite the semantic N2 candidates.
    rule = preparation_rule(text) if not role.get("scope_ambiguous") else None
    derived = None
    if rule and not matches:
        first = blocks[ids[0]]
        citation = _citation({**{key: first[key] for key in ("source_id", "block_id", "locator")},
                              "block_ids": ids}, blocks, _pdf_footer_skips(blocks))
        derived = {"requirement_id": _id("R-PREP-", [role["role_proof_sha256"], rule]),
            "text": text, "citations": [citation], "response_mode": "procedure",
            "category": "commercial" if "商务类" in text else "procedure",
            "mandatory": rule != "unsupplied_format_permission", "confidence": "high",
            "format_status": "not_prescribed", "format_title": None,
            "required_fact_labels": [], "attachment_keywords": [], "reference_keywords": [], "window_ids": []}
        matches = [derived]
    pure_manual = bool(matches and manual_signing_submission(text) and
        any(item["response_mode"] == "procedure" for item in matches) and all(
        item["category"] == "procedure" and item["response_mode"] in {"procedure", "fixed_text"} and item["text"] == text and
        (item["response_mode"] == "procedure" or _title_key(item.get("format_title") or "") in {"响应文件", "投标文件"}) and
        all((citation.get("block_ids") or [citation["block_id"]]) == ids for citation in item["citations"])
        for item in matches))
    errors = []
    if not matches:
        errors.append("unmapped_preparation_instruction")
    if role.get("scope_ambiguous") and not pure_manual:
        errors.append("preparation_instruction_scope_ambiguous")
    expected = []
    for pattern, modes in ((r"方案", {"writing"}), (r"授权书|承诺", {"fixed_text"}),
                           (r"报价表|偏离表", {"table"}), (r"证明|证书|执照", {"attachment"})):
        if re.search(pattern, text):
            expected.append(modes)
    compilation = proof_compilation(text)
    multiple = bool(re.search(r"清单|汇总", text) and re.search(r"证明|证据", text)) and not compilation
    if multiple and "清单" in text:
        expected.append({"table", "factual_response"})
    allowed = set().union(*expected) if expected else None
    if multiple and allowed is not None:
        allowed |= {"table", "factual_response"}
    substantive = re.search(r"方案|授权书|报价表|证明|证书|执照|业绩|承诺|偏离表|资料|材料", text)
    valid = [item for item in matches if (allowed is None or item["response_mode"] in allowed) and
             (item["response_mode"] != "procedure" or not substantive or rule)]
    if rule:
        allowed_rule_modes = {"procedure", "attachment"} if rule == "additional_materials_review" else {"procedure"}
        valid = [item for item in valid if item["response_mode"] in allowed_rule_modes and item["text"] == text]
    if pure_manual:
        # A separately retained fixed-cover candidate does not remove the
        # identical manual action or prove the preceding conditional scope.
        valid = [item for item in valid if item["response_mode"] == "procedure"]
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
        if rule and item["response_mode"] == "procedure" or pure_manual:
            result.update(source_rule=rule or "manual_signing_submission", source_scope_status=(
                "unresolved" if role.get("scope_ambiguous") else "not_checked") if role.get("scope_block_ids") else "not_applicable",
                source_scope_block_ids=list(role.get("scope_block_ids", [])), source_role_proof=copy.deepcopy(role["role_proof"]),
                source_role_proof_sha256=role["role_proof_sha256"])
            if derived:
                result["derived_requirement"] = derived
        elif role.get("scope_block_ids"):
            derivation = _numbered_child_context(role, item, blocks)
            if derivation:
                result["source_context_derivation"] = derivation
            else:
                errors.append("preparation_instruction_scope_unproved")
        if compilation:
            result["proof_compilation"] = True
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
                selected_actions = [action for action in actions if rid in action["requirement_refs"]]
                handled = bool(selected_actions) and all(action.get("source_scope_status", "not_applicable") ==
                    item.get("source_scope_status", "not_applicable") for action in selected_actions)
            elif mode == "writing":
                handled = any(rid in task["requirement_refs"] for section in owners for task in section["writing_tasks"])
            elif mode == "attachment":
                handled = bool(pending) or any(rid in value["requirement_refs"] for value in attachments)
                if item.get("proof_compilation"):
                    handled = any(rid in value["requirement_refs"] for value in attachments) or any(
                        rid in value["requirement_refs"] and value.get("severity", "missing") == "missing" for value in issues)
            else:
                field = {"table": "table_refs", "fixed_text": "fixed_content", "factual_response": "fact_refs"}[mode]
                handled = bool(pending) or any(section[field] for section in owners)
            result = {"requirement_id": rid, "response_mode": mode,
                            "status": "mapped_with_issue" if pending else "mapped_pending" if handled else "unresolved",
                            "issue_refs": pending}
            if item.get("source_rule"):
                result.update(source_rule=item["source_rule"], source_scope_status=item["source_scope_status"],
                              satisfaction_status="not_determined")
            if item.get("proof_compilation"):
                result.update(proof_compilation_status="pending_review", satisfaction_status="not_determined",
                              attachment_refs=[value["attachment_id"] for value in attachments if rid in value["requirement_refs"]])
            results.append(result)
            if not handled:
                errors.append({"reason": "preparation_instruction_handler_missing", "requirement_id": rid})
    return results, errors


def _source_format_additions(extracted, blocks, documents, formats):
    """Recover only source-proven outer sections, never invent a response body.

    A complete N2 requirement must already quote the native source. The two
    accepted forms are a bounded self-drafting instruction and the existing
    independently proved qualification-material family. Unknown headings,
    images, partial reading and arbitrary unextracted forms still fail.
    """
    from .material_heading_sources import _family, _PARENT
    additions = []
    existing = {f["citation"]["block_id"] for f in formats}
    skips = _pdf_footer_skips(blocks)
    for doc in documents:
        if doc.get("format") != "pdf" or any(
                u.get("coverage", {}).get("status") not in {"read", "empty"} or
                u.get("coverage", {}).get("unread_objects") or u.get("coverage", {}).get("unmapped_glyphs") or
                u.get("coverage", {}).get("error_code") for u in doc["units"]):
            continue
        body = sorted((b for b in blocks.values() if b["source_id"] == doc["source_id"] and b["block_id"] not in skips),
                      key=lambda b: b["ordinal"])
        units = {u["unit_id"]: u for u in doc["units"]}
        roles = _format_source_roles(doc, body)
        for index, heading in enumerate(body):
            if heading["block_id"] in existing or not _native_source_line(heading):
                continue
            ancestors = [b for b in body[:index] if _chapter_boundary(b)]
            if (not ancestors or not re.search(r"(?:投标|响应|报价).{0,18}(?:格式|组成)", ancestors[-1]["text"]) or
                    heading["block_id"] in roles or not re.match(r"^[1-9]\d*(?:[.、．]|\s)\s*[^\d]", heading["text"])):
                continue
            stop = next((i for i in range(index + 1, len(body)) if _chapter_boundary(body[i]) or
                         re.match(r"^[1-9]\d*(?:[.、．]|\s)\s*[^\d]", body[i]["text"])), len(body))
            members, kind, extra = [], None, {}
            span = body[index:stop]
            if (len(span) == 2 and _native_source_span(span) and
                    (_signable_drafting_instruction_only(span[1]["text"]) or
                     _writing_instruction_only(span[1]["text"]) and re.search(r"技术|服务|施工|实施|设计|方案", heading["text"]))):
                members, kind = span, "native_self_drafting_section"
            match = _PARENT.fullmatch(heading["text"])
            if match and (family := _family(body, index, match, units, doc["source_id"])):
                first_anchor = next((i for i in range(index + 1, stop) if body[i]["block_id"] in existing), None)
                if first_anchor is not None:
                    members, kind = body[index:first_anchor], "native_material_family_prefix"
                    extra = {"reference_text": family["reference"], "reference_resolution": "not_determined",
                             "sibling_block_ids": [b["block_id"] for b, _ in family["siblings"]]}
            if not kind or not _native_source_span(members):
                continue
            # Require a complete selected source span, not title similarity.
            member_ids = {b["block_id"] for b in members}
            matches = [r for r in extracted["requirements"] if any(
                set(c.get("block_ids") or [c["block_id"]]) <= member_ids and
                heading["block_id"] in (c.get("block_ids") or [c["block_id"]]) for c in r["citations"])]
            if not matches:
                continue
            covered = set()
            for requirement in matches:
                for citation in requirement["citations"]:
                    checked = _citation(_wire_citation(citation), blocks, skips)
                    if checked["quote"] == requirement["text"] and checked["start"] == 0:
                        covered.update(checked.get("block_ids") or [checked["block_id"]])
            required = member_ids if kind == "native_self_drafting_section" else {family["parent"]["block_id"], family["note"]["block_id"]}
            if not required <= covered:
                continue
            proof = {"rule": "native_source_format_addition_v1", "kind": kind, "source_id": doc["source_id"],
                     "source_sha256": doc["sha256"], "region_heading_id": ancestors[-1]["block_id"],
                     "members": [{k:b[k] for k in ("block_id", "locator", "ordinal", "unit_id")} |
                                 {"text_sha256": hashlib.sha256(b["text"].encode()).hexdigest()} for b in members], **extra}
            citation = _citation({k:heading[k] for k in ("source_id", "block_id", "locator")}, blocks, skips)
            refs = [r["requirement_id"] for r in matches]
            additions.append({"format_id": _id("FMT-SOURCE-", proof), "title": heading["text"], "level": 1,
                "status": "prescribed", "citation": citation, "content_block_ids": [b["block_id"] for b in members],
                "requirement_refs": refs, "source_derivation": proof, "source_derivation_sha256": _hash(proof)})
    return additions


def _format_plan(extracted: dict, blocks: dict, documents: list) -> tuple[list, list, list]:
    """Account for whole structural format regions, including window continuations.

    A grounded chapter anchor is necessary. Unclaimed non-heading continuation
    blocks can be attached deterministically inside that chapter's source
    boundary; unknown headings and images never disappear into free writing.
    """
    formats = copy.deepcopy(extracted.get("format_chapters", []))
    formats.extend(_source_format_additions(extracted, blocks, documents, formats))
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


def _joint_response_formats(requirement, formats, ledger, blocks, units, documents, coverage):
    """Prove one joint obligation's two distinct native response tables.

    This recognizes a complete positive source sentence and a unique role pair,
    not a general equivalence between different deviation-table titles. The
    original N2 requirement and its citation remain unchanged.
    """
    if (requirement["category"] != "commercial" or requirement["response_mode"] != "table" or
            requirement["format_status"] not in {"prescribed", "not_prescribed"} or requirement.get("format_title")):
        return None, None
    positive = re.compile(r"^(?:\d+(?:\.\d+)*[、．.]?)?响应文件对采购文件的全部偏差[，,]?"
                          r"均应在响应文件的商务和技术偏差表中列明[。.]")
    source_text = re.sub(r"\s+", "", requirement["text"])
    # Only this complete follow-up structure is known to preserve the joint
    # obligation: omitted entries are presumed responsive, with review after
    # clarification of inconsistency or missing supporting documents. Unknown
    # suffixes stay unresolved; a growing denial blacklist cannot prove them.
    follow_up = re.compile(
        r"响应文件偏差表中未列明的(?:内容|事项)[，,]将视为响应人响应采购文件的要求[；;]"
        r"但如在(?:询比采购|采购)活动中发现响应文件的其他部分与商务和技术偏差表的(?:描述|记载)不一致"
        r"或响应人的响应缺乏支持性(?:文件|材料)[，,]则(?:采购评审小组|评审小组)有权要求响应人"
        r"对相关(?:问题|事项)进行澄清[，,]并根据澄清结果对响应人的响应文件进行评审[。.]"
    )

    def supported_statement(text):
        first = positive.match(text)
        return first is not None and (first.end() == len(text) or follow_up.fullmatch(text[first.end():]) is not None)

    pricing = re.fullmatch(r"(?:[（(]\d+[）)])?(?:询比|采购|招标)文件要求的各类报价表格[（(]格式见"
                           r"(?:响应|投标)文件部分格式([1-9]\d*)[、，,]([1-9]\d*)[）)]", source_text)
    if pricing and pricing[1] == pricing[2]:
        return None, None
    if not pricing and not supported_statement(source_text):
        return None, None
    rule = "explicit_numbered_quotation_pair_v1" if pricing else "joint_business_technical_deviation_pair_v1"
    error = {"reason": "numbered_quotation_format_pair_unresolved" if pricing else "joint_deviation_format_pair_unresolved",
             "requirement_id": requirement["requirement_id"]}
    citations = requirement["citations"]
    source_ids = {citation["source_id"] for citation in citations}
    if len(source_ids) != 1:
        return None, error
    source_id = next(iter(source_ids))
    document = next((item for item in documents if item["source_id"] == source_id), None)
    if document is None or document.get("format") != "pdf":
        return None, error

    def native(block, kind):
        label = "table" if kind == "table" else "line"
        return (block["source_id"] == source_id and block["kind"] == kind and block.get("origin") == "native" and
                re.fullmatch(rf"pdf/page\[\d+\]/{label}\[\d+\]", block["locator"]) is not None and
                not block.get("read_disposition") and units[block["unit_id"]].get("coverage", {}).get("status") == "read")

    # Every cited native block must be present in full. Selecting only a
    # positive substring cannot hide a source waiver or activation condition;
    # line wrapping of a complete supported follow-up remains harmless.
    for citation in citations:
        source_blocks = [blocks[key] for key in (citation.get("block_ids") or [citation["block_id"]])]
        if (any(not native(block, "paragraph") for block in source_blocks) or
                re.sub(r"\s+", "", "".join(block["text"] for block in source_blocks)) != source_text or
                re.sub(r"\s+", "", citation["quote"]) != source_text):
            return None, error
    role_titles = {"business": "商务条款偏离表", "technical": "技术规格偏离表"}
    required_headers = {"business": {"询比文件的商务条款", "响应文件的商务条款", "偏离", "说明"},
                        "technical": {"询比规格", "响应文件规格", "偏离", "说明"}}
    if pricing:
        role_titles = {"quotation_summary": "报价表", "quotation_breakdown": "分项报价明细表"}
        required_headers = {"quotation_summary": {"项目名称", "响应人名称", "报价"},
                            "quotation_breakdown": {"序号", "内容", "不含税报价万元", "备注"}}
    targets = []
    for role, title in role_titles.items():
        candidates = [chapter for chapter in formats if _title_key(chapter["title"]) == title and
                      chapter["citation"]["source_id"] == source_id]
        if pricing:
            number = pricing[1] if role == "quotation_summary" else pricing[2]
            candidates = [chapter for chapter in candidates if re.match(r"^" + re.escape(number) + r"(?:[.、．]|\s)\s*[^\d]", chapter["title"])]
        if len(candidates) != 1:
            return None, {**error, "role": role, "candidate_format_ids": [item["format_id"] for item in candidates]}
        chapter = candidates[0]
        anchor = chapter["citation"]["block_id"]
        content_ids = list(dict.fromkeys([anchor, *chapter["content_block_ids"]]))
        by_id = {row["block_id"]: row for row in ledger if row.get("format_id") == chapter["format_id"]}
        if (chapter["status"] != "prescribed" or not native(blocks[anchor], "paragraph") or
                blocks[anchor]["text"].strip() != chapter["title"].strip() or not set(content_ids) <= set(by_id) or
                any(by_id[key]["status"] not in {"chapter_boundary", "declared_content", "structural_continuation"} or
                    not native(blocks[key], blocks[key]["kind"]) for key in content_ids)):
            return None, {**error, "role": role, "format_id": chapter["format_id"]}
        table_blocks = [blocks[key] for key in content_ids if blocks[key]["kind"] == "table"]
        if len(table_blocks) != 1:
            return None, {**error, "role": role, "format_id": chapter["format_id"], "table_count": len(table_blocks)}
        table = table_blocks[0]
        rows, merges, _ = _raw_grid(table)
        headers = {_normal(row[0]) for row in rows} if role == "quotation_summary" else {_normal(cell) for cell in rows[0]}
        if len(rows) < 2 or not required_headers[role] <= headers:
            return None, {**error, "role": role, "format_id": chapter["format_id"], "detail": "native_role_columns_unproven"}
        grid = {"rows": rows, "merges": merges, "column_widths": table.get("column_widths", [])}
        targets.append({"role": role, "format_id": chapter["format_id"], "source_title": chapter["title"],
            "format_citation": copy.deepcopy(chapter["citation"]), "content_block_ids": list(chapter["content_block_ids"]),
            "source_blocks": [{"block_id": key, "locator": blocks[key]["locator"],
                               "text_sha256": hashlib.sha256(blocks[key]["text"].encode()).hexdigest()} for key in content_ids],
            "tables": [{"block_id": table["block_id"], "locator": table["locator"], **grid,
                        "grid_sha256": _hash(grid), "table_id": _id("TABLE-", [source_id, table["block_id"]])}],
            "format_source_coverage": copy.deepcopy(list(by_id.values()))})
    if len({target["tables"][0]["block_id"] for target in targets}) != 2:
        return None, error
    footer_skips = _pdf_footer_skips(blocks)
    body = sorted((block for block in blocks.values() if block["source_id"] == source_id and
                   block["block_id"] not in footer_skips), key=lambda block: block["ordinal"])
    positions = {block["block_id"]: i for i, block in enumerate(body)}
    anchor_positions = [positions[target["format_citation"]["block_id"]] for target in targets]
    regions = []
    for i, block in enumerate(body):
        text = block.get("text", "").strip()
        if (native(block, "paragraph") and _heading_level(text) == 0 and
                re.search(r"(?:投标|响应|报价).{0,18}(?:格式|组成)", text) and not re.search(r"\.{2,}|…{2,}", text)):
            end = next((j for j in range(i + 1, len(body)) if _heading_level(body[j].get("text", "")) == 0), len(body))
            if all(i < position < end for position in anchor_positions):
                regions.append((i, end))
    if len(regions) != 1:
        return None, {**error, "detail": "shared_native_format_region_unproven"}
    start, end = regions[0]
    region_members = [{"block_id": block["block_id"], "locator": block["locator"],
                       "text_sha256": hashlib.sha256(block["text"].encode()).hexdigest()} for block in body[start:end]]
    proof = {"rule": rule, "requirement_id": requirement["requirement_id"],
        "source_id": source_id, "source_sha256": document["sha256"], "requirement_citations": copy.deepcopy(citations),
        "format_region": {"anchor_block_id": body[start]["block_id"], "members": region_members,
            "end_block_id": body[end]["block_id"] if end < len(body) else None,
            "end_locator": body[end]["locator"] if end < len(body) else None,
            "end_text_sha256": hashlib.sha256(body[end]["text"].encode()).hexdigest() if end < len(body) else None,
            "members_sha256": _hash(region_members), "boundary_basis": "next_source_major_heading_or_source_end"},
        "targets": targets, "full_N2_coverage_sha256": _hash(coverage), "status": "source_proven",
        "satisfaction_status": "not_determined", "n2_candidate_mutation": False}
    proof["proof_sha256"] = _hash(proof)
    return proof, None


def _format_region_instruction(requirement, ledger, blocks):
    """Bind a complete procedural chapter reference, not a bidder response form."""
    if requirement["category"] != "procedure" or requirement["response_mode"] != "procedure":
        return None
    statement = re.sub(r"\s+", "", requirement["text"])
    match = re.fullmatch(r"(?:\d+(?:\.\d+)*[、．.]?)?(响应|投标)文件应按(第[零一二三四五六七八九十百\d]+章)"
        r"[“\"]\1文件格式[”\"]进行编写[，,]如有必要[，,]可以增加附页[，,]作为\1文件的组成部分[。.]", statement)
    if not match or len(requirement["citations"]) != 1:
        return None
    citation = requirement["citations"][0]
    ids = citation.get("block_ids") or [citation["block_id"]]
    if any(not _native_source_line(blocks[key]) for key in ids):
        return None
    checked = _citation(_wire_citation(citation), blocks, _pdf_footer_skips(blocks))
    if checked["quote"] != "\n".join(blocks[key]["text"] for key in ids):
        return None
    candidates = [row for row in ledger if row.get("source_role") == "format_region_boundary" and
        row["source_id"] == citation["source_id"] and re.fullmatch(re.escape(match[2] + match[1]) +
        r"文件(?:要求及)?格式", re.sub(r"\s+", "", blocks[row["block_id"]]["text"]))]
    if len(candidates) != 1:
        return None
    row = candidates[0]
    proof = {"rule": "native_format_region_instruction_v1", "requirement_id": requirement["requirement_id"],
        "requirement_citations": copy.deepcopy(requirement["citations"]), "source_role_proof": copy.deepcopy(row["role_proof"]),
        "source_role_proof_sha256": row["role_proof_sha256"], "status": "source_proven_pending_manual",
        "n2_candidate_mutation": False, "satisfaction_status": "not_determined"}
    proof["proof_sha256"] = _hash(proof)
    return proof


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
    source_manifest = copy.deepcopy(manifest)
    if config.get("semantic_material_recall") is not None:
        from .material_recall import RecallOptions
        recall_options = RecallOptions.model_validate(config["semantic_material_recall"])
        if recall_options.additional_evidence:
            from .material_source_selection import extend_material_evidence
            manifest = extend_material_evidence(ctx, manifest, parsed, recall_options.additional_evidence)
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
    merge_results, source_repairs = _repair_numbered_procedure_spans(windows, verified_results)
    _fail(extracted.get("source_clause_repairs", []) != source_repairs, "EXTRACTION_SOURCE_CLAUSE_REPAIRS_MISMATCH")
    expected_requirements, expected_formats = _merge_results(merge_results)
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

    input_data = {"producer_version": PLANNING_VERSION, "extracted": extracted, "manifest": source_manifest,
                  "parsed_hash": _hash(parsed), "config": config}
    if manifest != source_manifest:
        input_data["material_manifest"] = manifest

    def assemble():
        planned_formats, format_ledger, format_errors = _format_plan(extracted, blocks, documents)
        source_format_derivations = [{"format_id": f["format_id"], "source_proof": f["source_derivation"],
            "source_proof_sha256": f["source_derivation_sha256"], "requirement_refs": f["requirement_refs"]}
            for f in planned_formats if f.get("source_derivation")]
        _capture(ctx, "n3-source-format-derivations", {"items": source_format_derivations,
            "n2_candidate_mutation": False, "n2_formats_sha256": _hash(extracted["format_chapters"])})
        source_rules = {}
        for row in format_ledger:
            for handler in row.get("instruction_requirements", []):
                if handler.get("source_rule"):
                    rid = handler["requirement_id"]
                    _fail(rid in source_rules and source_rules[rid] != handler, "PREPARATION_SOURCE_RULE_CONFLICT")
                    source_rules[rid] = copy.deepcopy(handler)
        derived_requirements = [item["derived_requirement"] for item in source_rules.values() if item.get("derived_requirement")]
        existing_ids = {item["requirement_id"] for item in extracted["requirements"]}
        _fail(any(item["requirement_id"] in existing_ids for item in derived_requirements), "PREPARATION_SOURCE_RULE_CONFLICT")
        planned_requirements = [*extracted["requirements"], *derived_requirements]
        # This finite catalogue has an explicit contract-chapter scope. Other
        # document formats/chapters keep their existing planning paths; they do
        # not earn a claim that every possible contract field was checked.
        contract_scope_present = any(_CONTRACT_SOURCE_HEADING.fullmatch(block["text"]) for block in blocks.values()
                                     if block["source_id"] in {document["source_id"] for document in documents
                                                               if document.get("format") == "pdf"})
        contract_pending = contract_pending_plan(planned_requirements, parsed, manifest) if contract_scope_present else {
            "rule": "contract_pending_plan_v1", "derived_requirements": [], "bindings": {}, "binding_proofs": {},
            "derivations": {}, "errors": [], "n2_candidate_mutation": False,
            "catalog_summary": {"exhaustive": False, "coverage_status": "supported_contract_scope_not_detected"},
            "limitations": ["Only explicitly scoped native contract chapters are supported; other possible fields remain unclassified."]}
        _capture(ctx, "n3-contract-pending-plan", contract_pending)
        _fail(bool(contract_pending["errors"]), "CONTRACT_PENDING_REQUIRES_SOURCE_REVIEW")
        planned_requirements.extend(contract_pending["derived_requirements"])
        contract_source_review = contract_pending.get("source_review", {})
        planned_requirements.extend(contract_source_review.get("derived_requirements", []))
        contract_review_bindings = contract_source_review.get("bindings", {})
        contract_source_review_handling = []
        contract_bindings = contract_pending["bindings"]
        contract_pending_handling = []
        from .reviewed_source_actions import reviewed_source_actions
        reviewed_additions, reviewed_source_report = reviewed_source_actions(
            config.get("reviewed_source_actions"), blocks, documents, planned_requirements)
        planned_requirements.extend(reviewed_additions)
        reviewed_actions_by_id = {item["requirement_id"]: item for item in reviewed_source_report["items"]}
        _capture(ctx, "n3-reviewed-source-actions", reviewed_source_report)
        source_controls = collect_plan_controls(planned_requirements, blocks, units, documents, _native_source_span)
        global_control_ids = {item["requirement_id"] for item in source_controls["global_controls"]}
        global_control_handling = []
        _capture(ctx, "n3-source-component-controls", {"producer_version": PLANNING_VERSION,
            **source_controls, "n2_candidate_mutation": False,
            "n2_requirements_sha256": _hash(extracted["requirements"]), "status": "source_proven_pending_review"})
        _capture(ctx, "n3-preparation-source-rules", {"producer_version": PLANNING_VERSION,
            "derived_requirements": derived_requirements, "rules": list(source_rules.values()),
            "n2_candidate_mutation": False, "n2_requirements_sha256": _hash(extracted["requirements"]),
            "status": "pending_review"})
        writing_spaces = {}
        pending_response_bodies = set()
        pending_body_reports = []
        format_links = []
        remote_format_resolutions = []
        format_associations = []
        format_region_resolutions = []
        joint_formats = {}
        for result in extracted["window_results"]:
            if result["validated"]["format_status"] in {"unread", "conflict"}:
                format_errors.append({"reason": "window_format_unresolved", "window_id": result["window_id"]})
        for chapter in planned_formats:
            if chapter["status"] != "prescribed":
                format_errors.append({"reason": "format_chapter_unresolved", "format_id": chapter["format_id"], "citation": chapter["citation"]})
        if not format_errors:
            for requirement in planned_requirements:
                proof, error = _joint_response_formats(requirement, planned_formats, format_ledger, blocks, units,
                                                       documents, extracted["coverage"])
                if error:
                    format_errors.append(error)
                if proof:
                    format_associations.append(proof)
                    joint_formats[requirement["requirement_id"]] = {target["format_id"] for target in proof["targets"]}
        for requirement in planned_requirements:
            title = requirement.get("format_title")
            joint_ids = joint_formats.get(requirement["requirement_id"], set())
            matches = [chapter for chapter in planned_formats if chapter["format_id"] in joint_ids] if joint_ids else [
                chapter for chapter in planned_formats if title and _title_key(title) == _title_key(chapter["title"])]
            if not matches and not title:
                matches = [chapter for chapter in planned_formats if len(_title_key(chapter["title"])) >= 4 and
                           chapter.get("source_role") != "prescribed_cover" and
                           _title_key(chapter["title"]) in _normal(requirement["text"])]
            if len(matches) > 1 and not joint_ids:
                format_errors.append({"reason": "ambiguous_grounded_format_title", "requirement_id": requirement["requirement_id"],
                                      "format_ids": [chapter["format_id"] for chapter in matches]})
            else:
                for chapter in matches:
                    if requirement["requirement_id"] not in chapter["requirement_refs"]:
                        chapter["requirement_refs"].append(requirement["requirement_id"])
                    format_links.append({"requirement_id": requirement["requirement_id"], "format_id": chapter["format_id"],
                        "basis": next(p["rule"] for p in format_associations if p["requirement_id"] == requirement["requirement_id"]) if joint_ids else
                                 "exact_source_title_after_number_prefix_normalization", "title": chapter["title"]})
        format_sources_complete = not format_errors
        for requirement in planned_requirements:
            if any(citation["block_id"] in {row["block_id"] for row in format_ledger if row["status"] == "skipped_page_footer"}
                   for citation in requirement["citations"]):
                format_errors.append({"reason": "requirement_anchor_is_page_footer", "requirement_id": requirement["requirement_id"]})
            unresolved = requirement["format_status"] in {"unread", "conflict"}
            needs_format = (requirement["format_status"] == "prescribed" or requirement["response_mode"] in {"table", "fixed_text"} or
                            requirement["response_mode"] == "factual_response" and bool(requirement.get("format_title")))
            region_resolution = _format_region_instruction(requirement, format_ledger, blocks) if format_sources_complete else None
            if region_resolution is not None:
                format_region_resolutions.append(region_resolution)
                needs_format = unresolved = False
            matching_formats = [chapter for chapter in planned_formats if requirement["requirement_id"] in chapter["requirement_refs"] or
                                _title_key(requirement.get("format_title") or "") == _title_key(chapter["title"])]
            located = bool(matching_formats)
            if requirement["response_mode"] == "table":
                located = bool(matching_formats) and all(any(blocks[key]["kind"] == "table" for key in chapter["content_block_ids"])
                                                        for chapter in matching_formats)
                if len(matching_formats) > 1 and requirement["requirement_id"] not in joint_formats:
                    format_errors.append({"reason": "ambiguous_grounded_format_title", "requirement_id": requirement["requirement_id"],
                                          "format_ids": [chapter["format_id"] for chapter in matching_formats]})
                if requirement["requirement_id"] in joint_formats:
                    located = located and {chapter["format_id"] for chapter in matching_formats} == joint_formats[requirement["requirement_id"]]
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
            if (format_sources_complete and not matching_formats and requirement["response_mode"] == "fixed_text" and
                    requirement["category"] in {"qualification", "commercial"} and
                    requirement["format_status"] == "not_prescribed" and not requirement.get("format_title")):
                # The source requirement is read; the bidder's complete
                # declaration is missing. Preserve every facet as pending,
                # without drafting a claim or classifying it as a procedure.
                needs_format = False
                pending_response_bodies.add(requirement["requirement_id"])
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
        _capture(ctx, "n3-format-associations", {"producer_version": PLANNING_VERSION, "items": format_associations,
            "errors": format_errors, "n2_candidate_mutation": False, "n2_requirements_sha256": _hash(extracted["requirements"]),
            "status": "failed" if format_errors else "source_proven" if format_associations else "not_needed"})
        _capture(ctx, "n3-format-coverage", {"producer_version": PLANNING_VERSION, "blocks": format_ledger,
                                             "format_associations": format_associations,
                                             "format_region_resolutions": format_region_resolutions,
                                             "remote_format_resolutions": remote_format_resolutions,
                                             "errors": format_errors, "status": "failed" if format_errors else "complete"})
        _fail(bool(format_errors), "REQUIRED_FORMAT_UNRESOLVED")
        recall_handled = {}
        for route, bindings in (("source_bound_contract_pending", contract_bindings),
                                ("source_bound_contract_review", contract_review_bindings),
                                ("source_bound_preparation_action", source_rules),
                                ("source_bound_reviewed_action", reviewed_actions_by_id)):
            for rid, binding in bindings.items():
                recall_handled[rid] = {"route": route, "binding_sha256": _hash(binding)}
        material_report = _material_match_report(ctx, planned_requirements, manifest, parsed,
            recall_settings=config.get("semantic_material_recall"), recall_handled=recall_handled)
        sources = copy.deepcopy(manifest["sources"])
        source_map = {item["source_id"]: item for item in sources}
        facts = _fact_index(manifest)
        fact_map = {fact["fact_id"]: fact for fact in facts}
        requirements, details = [], {}
        sections, tables, attachments, issues, references = [], [], [], [], []
        prescribed_cover_section_id = None
        table_owners, attachment_owners, section_formats = {}, {}, {}
        placements, table_reports, attachment_reports = [], [], []
        deferred_table_issues = []
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

        for raw in planned_requirements:
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
            rule = source_rules.get(requirement["requirement_id"])
            if rule:
                action = review_actions[-1]
                action["source_scope_status"] = rule["source_scope_status"]
                action["source_scope_contexts"] = []
                for key in rule["source_scope_block_ids"]:
                    block = blocks[key]
                    action["source_scope_contexts"].append({"text": block["text"], "source_ref": scoped_source(
                        {field: block[field] for field in ("source_id", "block_id", "locator")})})
                action["instruction"] = {
                    "additional_materials_review": "核对招标另列的具体资料要求，并线下决定是否补充其他材料；未认定材料齐备，不据此生成新的证明、事实或承诺。",
                    "unsupplied_format_permission": "只对已独立证成未提供格式的内容使用默认版式；原有指定格式必须保留，未读或冲突不能据此解除。",
                    "manual_signing_submission": "线下核对封面签署、盖章及递交事项；适用父层范围仍未确定，本项不用于选择正文、报价或判定资格，尚未执行任何事项。",
                }[rule["source_rule"]]
            if requirement["requirement_id"] in contract_bindings:
                action = review_actions[-1]
                action["instruction"] = "按保留的完整合同原文逐项明确待约定字段及来源回指；甲方信息由甲方确认，未确定值或适用性，不将本记录视为已履行或已满足。"
            if requirement["requirement_id"] in reviewed_actions_by_id:
                review_actions[-1]["instruction"] = reviewed_actions_by_id[requirement["requirement_id"]]["instruction"]

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
                if (_normal(label) == "乙方名称" and re.search(r"[（(]\s*乙方名称\s*[）)]", detail["text"])):
                    decisions.append({"label": label, "candidate_fact_ids": [], "adopted_fact_ids": [],
                        "status": "known_company_pending_manual_entry", "available_company": copy.deepcopy(manifest["company"])})
                    issue(section, [rid], label + "待补", "原合同字段尚未填写，已有公司名称可供填写。",
                          "在原合同“（乙方名称）”字段线下填写“" + manifest["company"]["name"] + "”。")
                    continue
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
                         *(r["requirement_id"] for r in planned_requirements if _title_key(r.get("format_title") or "") == title_key)]))
            refs = [ref for ref in refs if ref not in assigned or chapter["format_id"] in joint_formats.get(ref, set())]
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
            body_blocks = [blocks[key] for key in chapter["content_block_ids"]
                           if blocks[key].get("text", "").strip() and blocks[key]["text"].strip() != chapter["title"].strip()]
            instruction_only = bool(body_blocks) and all(block["kind"] == "paragraph" and (
                _signable_drafting_instruction_only(block["text"]) or _writing_instruction_only(block["text"]))
                for block in body_blocks)
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
                    if instruction_only and (_signable_drafting_instruction_only(block["text"]) or _writing_instruction_only(block["text"])):
                        # The section and the original instruction remain
                        # accounted for, but no declaration body was supplied.
                        # This route never licenses writing a company fact.
                        instruction_omissions.append({"format_id": chapter["format_id"], "block_id": block_id,
                            "source_ref": source_ref, "basis": "whole_drafting_instruction_body_pending",
                            "text_sha256": hashlib.sha256(block["text"].encode()).hexdigest()})
                        marker = issue(section, refs, "本章节完整响应正文待补", "原文仅提供编写说明，尚无可装配的承诺或响应正文。",
                                       "依据全部招标要求补充完整正文，核实公司声明并按原文完成签字盖章；不能只提交编写说明。")
                        next(value for value in issues if value["issue_id"] == marker)["source_requirement"] = block["text"]
                        pending_body_reports.append({"section_id": section["section_id"], "requirement_refs": refs,
                            "source_refs": [source_ref], "source_instruction": block["text"],
                            "decision": "instruction_preserved_as_evidence_body_missing", "issue_id": marker,
                            "satisfaction_status": "not_determined"})
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
                    fills, guard = _guard_historical_table_fills(block, blocks, fills, fact_map, manifest)
                    report.update(filled_count=len(fills), context_guard=guard)
                    table_reports.append(report)
                    blocked = {(item["row"], item["column"]): item for item in guard["blocked"]}
                    table_id = _id("TABLE-", [block["source_id"], block_id])
                    cells = [[{"text": text, "fact_refs": [], "issue_refs": []} for text in row] for row in rows]
                    for r, row in enumerate(rows):
                        for c, text in enumerate(row):
                            if (r, c) in covered:
                                continue
                            if (r, c) in fills:
                                fact = fact_map[fills[(r, c)]]
                                cells[r][c] = {"text": fact["value"], "fact_refs": [fact["fact_id"]], "issue_refs": []}
                            elif (r, c) in blocked:
                                deferred_table_issues.append({"table_id": table_id, "row": r, "column": c,
                                    "requirement_refs": refs, "guard": blocked[(r, c)]})
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

        outline_groups, writing_control_handling = [], []
        writing_controls = {r["requirement_id"]: rule for r in planned_requirements
                            if (rule := _response_writing_control(r, blocks))}
        contract_sections = {}
        for requirement in planned_requirements:
            rid = requirement["requirement_id"]
            if rid in assigned:
                continue
            refs = next(r["source_refs"] for r in requirements if r["requirement_id"] == rid)
            guarded = contract_bindings.get(rid, []) + contract_review_bindings.get(rid, [])
            scope_keys = {_hash([item["source_proof"]["source_id"], item["scope"]]) for item in guarded}
            section, grouping = None, None
            if len(scope_keys) == 1:
                key = next(iter(scope_keys))
                section = contract_sections.get(key)
                if section is None:
                    section = new_section(["contract-review-group", key],
                        guarded[0]["scope"]["title"] + "：原文与待核事项", [])
                    contract_sections[key] = section
                grouping = {"rule": "same_proven_contract_scope", "scope": guarded[0]["scope"],
                            "source_proof_sha256s": sorted({item["source_proof_sha256"] for item in guarded})}
            elif (not guarded and rid not in writing_controls and rid not in global_control_ids and
                    requirement["category"] == "technical" and requirement["response_mode"] == "writing" and
                    requirement["format_status"] == "not_prescribed" and not requirement.get("format_title")):
                candidates = [(chapter, evidence) for chapter in planned_formats
                    if {c["source_id"] for c in requirement["citations"]} == {chapter["citation"]["source_id"]}
                    and _title_key(chapter["title"]) in {"技术服务方案", "技术方案", "施工方案", "实施方案"}
                    and (evidence := _writing_space(requirement, chapter, blocks))]
                if len(candidates) == 1:
                    chapter, evidence = candidates[0]
                    section = next(item for item in sections if item["section_id"] == _id("SEC-", chapter["format_id"]))
                    writing_spaces[(chapter["format_id"], rid)] = evidence
                    grouping = {"rule": "unique_open_technical_chapter", "format_id": chapter["format_id"],
                                "permission_citations": evidence}
            if section is None:
                section = new_section(rid, requirement["text"].strip().replace("\n", " ")[:70], [rid], source_refs=refs)
            else:
                section["requirement_refs"].append(rid)
                section["source_refs"] = list(dict.fromkeys([*section["source_refs"], *refs]))
                outline_groups.append({"requirement_id": rid, "section_id": section["section_id"], **grouping,
                    "n2_candidate_mutation": False, "satisfaction_status": "not_determined"})
            assigned.add(rid)
            if requirement["response_mode"] == "procedure" and not guarded:
                section["render_body"] = False

        for section in sections:
            for rid in section["requirement_refs"]:
                detail = details[rid]
                placements.append({"requirement_id": rid, "section_id": section["section_id"],
                                   "response_mode": detail["response_mode"], "render_body": section["render_body"]})
                if rid in contract_review_bindings:
                    section["render_body"] = placements[-1]["render_body"] = True
                    section["kind"] = "facts"
                    section["nonwriting_requirement_refs"] = list(dict.fromkeys([*section["nonwriting_requirement_refs"], rid]))
                    for item in contract_review_bindings[rid]:
                        source_ref = scoped_source(item["citation"])
                        section["fixed_content"].append({"kind": "paragraph",
                            "text": "合同条款原文（履约及适用性待核对）：\n" + item["text"], "source_refs": [source_ref]})
                        section["source_refs"] = list(dict.fromkeys([*section["source_refs"], source_ref]))
                        title = "合同原文第0条回指待澄清" if item["literal_zero_reference_count"] else "合同条款履约及适用性待核对"
                        marker = issue(section, [rid], title,
                            "完整合同条款及其否定、例外和原文异常均保留为本次响应待核项；尚未核对履约适用性或判定满足，不将合同条款扩写为技术方案。",
                            "按完整合同原文核对履约条件并澄清异常引用；不猜填、不解释第0条、不代为承诺或认定已满足。")
                        next(value for value in issues if value["issue_id"] == marker)["source_requirement"] = item["text"]
                        contract_source_review_handling.append({"requirement_id": rid, "section_id": section["section_id"],
                            "review_id": item["review_id"], "source_proof_sha256": item["source_proof_sha256"],
                            "issue_ids": [marker], "response_status": "pending_manual", "satisfaction_status": "not_determined",
                            "writing_unit_ids": [], "adopted_fact_ids": [], "values_inferred": False,
                            "literal_zero_reference_count": item["literal_zero_reference_count"]})
                    if rid not in contract_bindings:
                        continue
                if rid in contract_bindings:
                    pending_items = contract_bindings[rid]
                    requirement = next(item for item in requirements if item["requirement_id"] == rid)
                    section["render_body"] = True
                    section["nonwriting_requirement_refs"] = list(dict.fromkeys([*section["nonwriting_requirement_refs"], rid]))
                    section["fixed_content"].append({"kind": "paragraph", "text": "合同待约定原文（已读文本未给值）：\n" + detail["text"],
                                                     "source_refs": requirement["source_refs"]})
                    contexts = {}
                    for pending in pending_items:
                        proof = pending["source_proof"]
                        for member in [proof["scope_heading"], proof["article_heading"], *proof["article_preamble"], *proof["parent_context"]]:
                            contexts[member["block_id"]] = member
                    ordered_context = sorted(contexts.values(), key=lambda member: member["ordinal"])
                    context_refs = [scoped_source({key: member[key] for key in ("source_id", "block_id", "locator")}) for member in ordered_context]
                    if ordered_context:
                        section["fixed_content"].append({"kind": "paragraph", "text": "合同来源范围及父条件（原文）：\n" + "\n".join(member["text"] for member in ordered_context),
                                                         "source_refs": context_refs})
                        section["source_refs"] = list(dict.fromkeys([*section["source_refs"], *context_refs]))
                    for pending in pending_items:
                        scope = pending["scope"]
                        clause_label = ("合同" if scope["kind"] == "main_contract" else scope["title"]) + "第" + pending["clause_number"] + "条"
                        if pending["subclause_number"]:
                            clause_label += "（" + pending["subclause_number"] + "）"
                        owner = pending["owner_party"]
                        fields = "、".join(field["field"] for field in pending["pending_fields"])
                        marker = issue(section, [rid], clause_label + "：" + ("甲方待约定" if owner == "甲方" else "合同待约定") + "：" + fields,
                            "已读原文未给出这些字段的值，需明确约定或补充信息；物理留白未判定，尚未确定具体值、适用性或符合性。",
                            "由甲方确认并提供本项资料、工作条件或协作内容；不列为公司缺材料，不猜填。" if owner == "甲方" else
                            "按完整原文及全部备选核对并明确约定；不猜填数值、不代选选项，不将本项视为已满足。")
                        row = {"requirement_id": rid, "section_id": section["section_id"], "catalog_pending_id": pending["pending_id"],
                            "issue_ids": [marker], "source_proof_sha256": pending["source_proof_sha256"], "scope": copy.deepcopy(scope),
                            "pending_fields": copy.deepcopy(pending["pending_fields"]), "owner_party": owner,
                            "reading_status": "read", "value_status": pending["value_status"], "response_status": "pending_manual",
                            "physical_blank_status": "not_determined", "satisfaction_status": "not_determined",
                            "writing_unit_ids": [], "adopted_fact_ids": [], "values_inferred": False}
                        for blocking in pending["blocking_issues"]:
                            clarification = issue(section, [rid], clause_label + "：原文第0条回指待澄清",
                                "完整来源含字面“第0条”回指，引用对象尚未证成；原文、全部备选及固定比例均已保留。",
                                "向合同提供方澄清真实引用条款；不解释第0条、不确定知识产权归属，也不据此判定任何备选已经适用。")
                            row["issue_ids"].append(clarification)
                            row.setdefault("blocking_issues", []).append({**copy.deepcopy(blocking), "issue_id": clarification})
                        contract_pending_handling.append(row)
                    continue
                if rid in global_control_ids:
                    requirement = next(item for item in requirements if item["requirement_id"] == rid)
                    _fail(bool(requirement.get("source_contexts")), "GLOBAL_CONTROL_CONTEXT_UNRESOLVED")
                    # Preserve the complete original rule as neutral source and
                    # an explicit pending control. In particular this remains a
                    # facts section, avoiding N4's task-less technical fallback.
                    section["render_body"] = True
                    section["fixed_content"].append({"kind": "paragraph",
                        "text": "关键条款全局规则（招标原文）：\n" + detail["text"],
                        "source_refs": requirement["source_refs"]})
                    marker = issue(section, [rid], "关键条款逐项响应及证据待核对",
                        "本条仅规定全局响应规则，不是可独立起草的实施方法；尚未核验具体关键条款及相应证据。",
                        "按完整招标原文逐项定位实际关键条款、响应和所需证明；未定位或缺响应的项目保留待补，不以通用承诺代替。")
                    next(item for item in issues if item["issue_id"] == marker)["severity"] = "warning"
                    global_control_handling.append({"requirement_id": rid, "section_id": section["section_id"],
                        "issue_id": marker, "decision": "original_rule_preserved_as_pending_control",
                        "writing_unit_ids": [], "n2_candidate_mutation": False,
                        "satisfaction_status": "not_determined"})
                    continue
                if rid in writing_controls:
                    original = next(item for item in requirements if item["requirement_id"] == rid)
                    section["fixed_content"].append({"kind": "paragraph",
                        "text": "响应编写规则（招标原文，待核对）：\n" + detail["text"],
                        "source_refs": original["source_refs"]})
                    section["nonwriting_requirement_refs"].append(rid)
                    marker = issue(section, [rid], "响应编写规则待核对",
                        "本条约束整份响应，不提供可以扩写的具体技术方法；保留原文并等待最终内容核对。",
                        "核对最终响应是否符合本规则，不能用一段通用承诺代替逐项检查。")
                    next(item for item in issues if item["issue_id"] == marker)["severity"] = "warning"
                    writing_control_handling.append({"requirement_id": rid, "section_id": section["section_id"],
                        "rule": writing_controls[rid], "issue_id": marker, "writing_unit_ids": [],
                        "source_refs": original["source_refs"], "satisfaction_status": "not_determined"})
                    continue
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
                    matches, matching_policy = _attachment_candidates(detail, manifest.get("attachments", []), material_report)
                    def proof_group(item):
                        return ("bundle", item["bundle_id"]) if item.get("bundle_id") else ("single", item["attachment_id"])
                    bundle_keys = {proof_group(item) for item in matches}
                    query = matching_policy["query"] or {}
                    related_ids = {aid for group in material_report["proof_groups"]
                                   if group["group_id"] in query.get("candidate_group_ids", []) for aid in group["attachment_ids"]}
                    related = [a for a in manifest.get("attachments", []) if a["attachment_id"] in related_ids]
                    related_groups = {proof_group(item) for item in related}
                    missing_labels = query.get("missing_required_labels", [])
                    ambiguous_labels = query.get("ambiguous_required_labels", [])
                    unavailable = query.get("unavailable_required_details", [])
                    unavailable_reason = "".join("已找到但暂不能装配的候选：" + item["label"] + "。" +
                        "".join(item["reasons"]) for item in unavailable)
                    attachment_reports.append({"requirement_id": rid, "status": "partial" if matches and (missing_labels or ambiguous_labels or unavailable)
                                               else "matched" if matches else "ambiguous" if ambiguous_labels else "no_match",
                                               "matching_policy": matching_policy,
                                               "candidate_ids": [item["attachment_id"] for item in related],
                                               "selected_ids": [item["attachment_id"] for item in matches],
                                               "candidate_bundle_ids": [identity for _, identity in sorted(related_groups)],
                                               "candidate_groups": [{"kind": kind, "identity": identity} for kind, identity in sorted(related_groups)],
                                               "scope": "冻结证明附件匹配；不构成额外资格或公司主体核验"})
                    if matches:
                        # Distinct required proof kinds can each contribute a
                        # complete package. Competing candidates within one
                        # kind have already been withheld by the shared matcher.
                        selected_pages = sorted(matches, key=lambda item: (proof_group(item), item.get("page_index", 1)))
                        marker = issue(section, [rid], "证明材料适用性待核对",
                            "按维护的证明类别装配了原件页；尚未据此判断证照状态或本次资格已满足。",
                            "结合原件和本次招标条件核对适用性，不以附件存在代替资格结论。")
                        next(item for item in issues if item["issue_id"] == marker)["severity"] = "warning"
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
                        if missing_labels or ambiguous_labels or unavailable:
                            reason = ("尚未找到可装配的证明：" + "、".join(missing_labels) + "。" if missing_labels else "")
                            reason += ("同一证明类别存在多个候选，尚未确定：" + "、".join(ambiguous_labels) + "。" if ambiguous_labels else "")
                            reason += unavailable_reason
                            issue(section, [rid], "本条证明材料尚未配齐", reason,
                                  "补充缺失证明或明确候选；已附部分材料不代表本条全部要求已满足。")
                    elif (matching_policy["request"] or {}).get("rule") == "negative_proof_instruction_not_searched":
                        # The source explicitly says no proof is required. It
                        # remains traceable, but is not a missing company file.
                        attachment_reports[-1]["status"] = "not_required"
                        requirement = next(item for item in requirements if item["requirement_id"] == rid)
                        section["fixed_content"].append({"kind": "paragraph", "text": "证明提交说明（招标原文）：\n" + detail["text"],
                                                         "source_refs": requirement["source_refs"]})
                    else:
                        reason = ("当前表达尚未完成可支持的材料检索，不能据此认定公司没有材料。"
                                  if matching_policy["query_status"] == "not_searched" else
                                  "已找到相关材料，但其来源、类别或适用条件不足以支持本条装配。"
                                  if query.get("candidate_group_ids") and not matches else
                                  "本条有多个可用候选，尚未确定唯一证明包。" if ambiguous_labels else
                                  "当前冻结的已检索范围未找到可装配证明；不代表公司不存在相应资料。")
                        if missing_labels:
                            reason += "尚缺对应证明：" + "、".join(missing_labels) + "。"
                        if ambiguous_labels:
                            reason += "待明确候选类别：" + "、".join(ambiguous_labels) + "。"
                        reason += unavailable_reason
                        if query.get("unread_scope_present"):
                            reason += "仍有资料未完成读取，应先补齐对应读取范围。"
                        issue(section, [rid], "所需证明附件未能唯一匹配", reason,
                              "依据匹配记录核对候选、读取状态和本条条件；补充或明确选择对应证明页。")
                elif mode in {"table", "fixed_text"}:
                    if rid in pending_response_bodies:
                        requirement = next(item for item in requirements if item["requirement_id"] == rid)
                        section["fixed_content"].append({"kind": "paragraph", "text": "响应要求（招标原文）：\n" + detail["text"],
                                                         "source_refs": requirement["source_refs"]})
                        marker = issue(section, [rid], "本条全文响应正文待补", "已完整读取要求，但尚无经确认的公司声明或承诺正文。",
                                       "逐项完成本条全部声明、告知及申报义务，核实事实并签章；未生成或确认任何公司结论。")
                        pending_body_reports.append({"section_id": section["section_id"], "requirement_refs": [rid],
                            "source_refs": requirement["source_refs"], "decision": "read_requirement_full_response_missing",
                            "source_requirement": detail["text"], "issue_id": marker, "satisfaction_status": "not_determined"})
                    elif not section["fixed_content"] and not any(
                            rid in item["requirement_refs"] and item["severity"] == "missing" for item in issues):
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

        component_matrix = []
        linked_formats = [{**chapter, "planned_section_id": _id("SEC-", chapter["format_id"])}
                          for chapter in planned_formats]
        for group in source_controls["component_groups"]:
            for component in group["components"]:
                targets = component_targets(component, group, linked_formats, sections, tables, issues, _title_key, blocks)
                source_ref = scoped_source(component["citation"])
                section = new_section([group["group_id"], component["number"]],
                    component["source_name"] + "：响应组成待核对", group["requirement_refs"], source_refs=[source_ref])
                section["fixed_content"].append({"kind": "paragraph",
                    "text": "响应组成要求（招标原文）：\n" + component["source_text"], "source_refs": [source_ref]})
                conditional = component["component"] in {"authorization", "joint_bid"}
                additional = component["component"] == "other_materials"
                reason = ("原文为条件性组成项；尚未根据本项目明确输入核验签署或联合体适用性，不能据资料缺少推定不适用。"
                          if conditional else "本项需与前附表及独立的具体资料要求逐项核对；兜底清单不能证明存在额外证明要求，也不能证明资料齐备。"
                          if additional else "已列出候选原格式落点，仍须核对实际响应、空白字段及证明；标题或空表不代表该组成项已完成。"
                          if targets else "尚无已证成的独立组件正文或资料落点；组成清单和程序待办本身不能代替响应。")
                marker = issue(section, group["requirement_refs"], component["source_name"] +
                    ("适用性待核对" if conditional else "范围及完整性待核对" if additional else
                     "完整响应待核对" if targets else "响应内容待补"), reason,
                    "逐项核对本项实际正文、原格式和资料；缺项补有来源内容，未知条件保留待核，不编造公司事实或履约承诺。")
                next(item for item in issues if item["issue_id"] == marker).update(
                    source_requirement=component["source_text"], severity="warning" if conditional or additional or targets else "missing")
                row = {"group_id": group["group_id"], **component, "requirement_refs": group["requirement_refs"],
                    "source_ref": source_ref, "section_id": section["section_id"], "format_candidates": targets,
                    "issue_id": marker, "decision": "source_component_requires_explicit_review_or_missing_response",
                    "reason": reason, "status": "pending_manual", "satisfaction_status": "not_determined"}
                row["proof_sha256"] = _hash(row)
                component_matrix.append(row)
                placements.extend({"requirement_id": rid, "section_id": section["section_id"],
                    "response_mode": details[rid]["response_mode"], "render_body": True} for rid in group["requirement_refs"])
        conflict_handling = []
        for conflict in source_controls["number_name_conflicts"]:
            refs = conflict["requirement_refs"]
            source_refs = list(dict.fromkeys(ref for item in requirements if item["requirement_id"] in refs
                                            for ref in item["source_refs"]))
            section = new_section(["source-number-conflict", conflict["proof_sha256"]],
                "响应组成原文编号冲突待核对", refs, source_refs=source_refs)
            marker = issue(section, refs, "第3.1.1（4）项名称与例外回指冲突",
                "组成清单第（4）项为商务和技术偏差表，后文却将同一编号称为响应保证金；不得按错误编号删除偏差表。",
                "保留商务及技术偏离表与两处原文，线下核对编号冲突；不静默改写招标要求，不据此判断保证金或公司响应已满足。")
            next(item for item in issues if item["issue_id"] == marker)["severity"] = "warning"
            conflict_handling.append({**conflict, "section_id": section["section_id"], "issue_id": marker,
                                      "satisfaction_status": "not_determined"})
            placements.extend({"requirement_id": rid, "section_id": section["section_id"],
                "response_mode": details[rid]["response_mode"], "render_body": True} for rid in refs)
        _capture(ctx, "n3-component-dispositions", {"producer_version": PLANNING_VERSION,
            "items": component_matrix, "number_name_conflicts": conflict_handling,
            "n2_candidate_mutation": False, "status": "pending_manual" if component_matrix else "not_needed"})
        _capture(ctx, "n3-global-critical-controls", {"producer_version": PLANNING_VERSION,
            "source_controls": source_controls["global_controls"], "handling": global_control_handling,
            "marker_observations": [{"source_id": block["source_id"], "block_id": block["block_id"],
                "locator": block["locator"], "text_sha256": hashlib.sha256(block["text"].encode()).hexdigest(),
                "requirement_refs": [raw["requirement_id"] for raw in planned_requirements if any(
                    block["block_id"] in (citation.get("block_ids") or [citation["block_id"]]) for citation in raw["citations"])]}
                for block in blocks.values() if block["source_id"] in {doc["source_id"] for doc in documents}
                and re.search(r"[＊*★]", block["text"])],
            "target_interpretation": "pending_source_review_markers_are_not_automatically_concrete_clauses",
            "n2_candidate_mutation": False, "satisfaction_status": "not_determined"})

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

        compilations = {item["requirement_id"] for row in format_ledger for item in row.get("instruction_requirements", [])
                        if item.get("proof_compilation")}
        for rid in compilations:
            section = next(section for section in sections if rid in section["requirement_refs"])
            marker = issue(section, [rid], "证明材料汇编待核对", "证明材料的完整性及其与统计表的对应关系尚未核对。",
                           "逐项核对实际证明页和招标另行指定的统计表；缺件补原件，空表字段补有来源事实。")
            next(value for value in issues if value["issue_id"] == marker)["severity"] = "warning"

        writing_refinement = {}
        if config.get("refine_writing_tasks", False):
            from .writing_plan import refine_writing_plan
            writing_refinement = refine_writing_plan(sections, details, blocks, units, source_map, references)
            for control in writing_refinement.get("controls", []):
                section = next(s for s in sections if s["section_id"] == control["section_id"])
                rid = control["requirement_id"]
                marker = issue(section, [rid], "技术编制及覆盖范围待核对",
                    "该要求控制本章的编写范围；具体内容由所列技术单元响应，仍需核对正文而非以编写说明代替响应。",
                    "对照本条原文核对各技术单元、适用条件及评分内容；当前未认定已满足。")
                next(i for i in issues if i["issue_id"] == marker)["severity"] = "warning"
                control["issue_id"] = marker
            _capture(ctx, "n3-writing-refinement", writing_refinement)
        instruction_handling, instruction_errors = _instruction_placements(
            format_ledger, sections, attachments, review_actions, issues)
        _capture(ctx, "n3-instruction-handling", {"items": instruction_handling, "errors": instruction_errors,
                                               "status": "failed" if instruction_errors else "mapped_pending"})
        _fail(bool(instruction_errors), "PREPARATION_INSTRUCTION_HANDLING_UNRESOLVED")
        _capture(ctx, "n3-pending-response-bodies", {"producer_version": PLANNING_VERSION,
            "items": pending_body_reports, "n2_candidate_mutation": False, "status": "pending_manual" if pending_body_reports else "not_needed"})
        association_placements = []
        association_errors = []
        for proof in format_associations:
            rid = proof["requirement_id"]
            for target in proof["targets"]:
                section_id = _id("SEC-", target["format_id"])
                section = next((item for item in sections if item["section_id"] == section_id), None)
                for native_table in target["tables"]:
                    matches = [item for item in tables if item["table_id"] == native_table["table_id"]]
                    handled = (len(matches) == 1 and section is not None and rid in section["requirement_refs"] and
                               rid in matches[0]["requirement_refs"] and matches[0]["section_id"] == section_id and
                               native_table["table_id"] in section["table_refs"])
                    association_placements.append({"requirement_id": rid, "format_id": target["format_id"],
                        "section_id": section_id, "table_id": native_table["table_id"], "proof_sha256": proof["proof_sha256"],
                        "status": "mapped" if handled else "unresolved", "satisfaction_status": "not_determined"})
                    if not handled:
                        association_errors.append({"requirement_id": rid, "format_id": target["format_id"],
                                                   "table_id": native_table["table_id"]})
        _capture(ctx, "n3-format-association-placement", {"items": association_placements, "errors": association_errors,
            "status": "failed" if association_errors else "mapped" if association_placements else "not_needed"})
        _fail(bool(association_errors), "JOINT_FORMAT_ASSOCIATION_INCOMPLETE")
        response_organization = {}
        if config.get("organize_response", False):
            from .response_organization import organize_response
            response_organization = organize_response(sections, requirements, details, planned_formats, blocks,
                issues, attachments, manifest, material_report)
            placements = [{"requirement_id": rid, "section_id": section["section_id"],
                "response_mode": details[rid]["response_mode"], "render_body": section["render_body"],
                "document_role": section.get("document_role", "response")}
                for section in sections for rid in section["requirement_refs"]]
            _capture(ctx, "n3-response-organization", response_organization)
        # Append new context-guard markers after the existing list so retained
        # M numbers and unrelated review links do not shift in a child run.
        for pending in deferred_table_issues:
            table = next(t for t in tables if t["table_id"] == pending["table_id"])
            section = next(s for s in sections if s["section_id"] == table["section_id"])
            marker = issue(section, pending["requirement_refs"], "历史业绩项目名称待填写",
                "原表要求填写历史业绩项目；当前投标项目名称不能作为历史业绩，尚无与本行完整对应的明确历史记录。",
                "依据本行业绩证明填写对应历史项目名称及其余记录字段；无明确对应资料时保留待补。")
            table["rows"][pending["row"]][pending["column"]]["issue_refs"] = [marker]
            pending["guard"]["issue_id"] = marker
        _capture(ctx, "n3-table-context-guard", {"tables": [report["context_guard"] for report in table_reports],
            "n2_candidate_mutation": False, "mapping_requests_unchanged": True,
            "historical_tuple_adoption": "not_supported_by_generic_fact_label_mapping"})
        native_formats = {}
        if config.get("native_response_formats", False):
            # Paragraph identity fills are separate from the already-frozen
            # table mapping requests. Do not expand unrelated model inputs.
            from .project_identity import derive_project_number
            identity_facts, identity_report = derive_project_number(parsed, manifest, facts, scoped_source)
            facts.extend(identity_facts)
            _capture(ctx, "n3-project-identity", identity_report)
            from .source_format import recover_source_formats
            native_formats = recover_source_formats(ctx, sections, planned_formats, blocks, source_map, tables, facts, manifest, scoped_source)
        output_values = {"input_kind": "automatic", "synthetic": manifest.get("synthetic", False),
                    "project": manifest["project"], "company": manifest["company"], "sources": sources, "facts": facts,
                    "requirements": requirements, "attachments": attachments, "issues": issues,
                    "source_context_proofs": list(source_context_proofs.values()),
                    "review_actions": review_actions, "prescribed_cover_section_id": prescribed_cover_section_id,
                    "reference_excerpts": references, "style": manifest.get("style", {}), "sections": sections, "tables": tables,
                    "preparation_note": manifest.get("preparation_note") or "系统依据冻结招标原文及固定公司资料自动编排；缺项按编号保留。"}
        commitment_plan = {"status": "disabled", "tasks": [], "issue_changes": []}
        if config.get("draft_source_commitments", False):
            from .commitment_planning import plan_commitment_tasks, commitment_nonwriting_heading_refs
            tasks = plan_commitment_tasks(output_values)
            nonwriting_headings = commitment_nonwriting_heading_refs(output_values)
            commitment_plan["status"] = "planned_pending_company_confirmation" if tasks else "no_matching_source_formats"
            for section in sections:
                task = tasks.get(section["section_id"])
                if task is None:
                    continue
                section["kind"] = "technical"
                section["writing_tasks"] = [task.model_dump(mode="json")]
                section["requirement_refs"] = list(dict.fromkeys([*section["requirement_refs"], *task.requirement_refs]))
                section["nonwriting_requirement_refs"] = list(dict.fromkeys([
                    *section["nonwriting_requirement_refs"], *nonwriting_headings.get(section["section_id"], [])]))
                section["source_refs"] = list(dict.fromkeys([*section["source_refs"],
                    *(ref for item in requirements if item["requirement_id"] in task.requirement_refs for ref in item["source_refs"])]))
                commitment_plan["tasks"].append({"section_id": section["section_id"], "unit_id": task.unit_id,
                    "task_sha256": _hash(task.model_dump(mode="json")), "requirement_refs": task.requirement_refs,
                    "company_confirmation": "pending", "satisfaction_status": "not_determined"})
                for item in issues:
                    if item["section_id"] != section["section_id"] or item["title"] != "本章节完整响应正文待补":
                        continue
                    before = copy.deepcopy(item)
                    item.update(title="拟承诺待公司确认并签章", severity="warning",
                        reason="本章已安排依据招标原文起草独立拟承诺；草稿仍不代表公司已经确认或完成签章。",
                        action="核对本章拟承诺与完整招标条款、原合同留空范围及实际履约安排，确认后填写授权人和日期并签章。")
                    commitment_plan["issue_changes"].append({"before": before, "after": copy.deepcopy(item)})
        project_constraints = plan_project_constraints(parsed, planned_requirements, manifest, sections)
        if config.get("draft_source_commitments", False):
            final_tasks = {task["unit_id"]: task for section in sections for task in section["writing_tasks"]}
            for row in commitment_plan["tasks"]:
                row["task_sha256"] = _hash(final_tasks[row["unit_id"]])
            _capture(ctx, "n3-commitment-writing-plan", commitment_plan)
        _capture(ctx, "n3-project-constraints", project_constraints)
        _capture(ctx, "n3-outline-grouping", {"producer_version": PLANNING_VERSION,
            "placements": outline_groups, "writing_controls": writing_control_handling,
            "n2_candidate_mutation": False, "satisfaction_status": "not_determined"})
        material_recall_issue_changes = []
        if config.get("semantic_material_recall") is not None:
            from .material_recall import annotate_material_issues
            material_recall_issue_changes = annotate_material_issues(issues, material_report)
            _capture(ctx, "n3-material-recall-issues", {"changes": material_recall_issue_changes,
                "qualification_result": "not_determined", "assembly_candidates_added": 0})
        output = PreparedBid.model_validate(output_values).model_dump(mode="json")
        report = {"producer_version": PLANNING_VERSION, "parsed_hash": _hash(parsed),
                  "writing_refinement": writing_refinement, "response_organization": response_organization,
                  "native_response_formats": native_formats,
                  "commitment_writing_plan": commitment_plan,
                  "reviewed_source_actions": reviewed_source_report,
                  "material_matching": material_report,
                  "material_recall_issue_changes": material_recall_issue_changes,
                  "outline_grouping": outline_groups, "response_writing_controls": writing_control_handling,
                  "source_format_derivations": source_format_derivations,
                  "requirement_placements": placements, "tables": table_reports, "attachments": attachment_reports,
                  "format_block_coverage": format_ledger,
                  "instruction_handling": instruction_handling,
                  "preparation_source_rules": list(source_rules.values()),
                  "source_context_proofs": list(source_context_proofs.values()),
                  "format_links": format_links, "format_associations": format_associations,
                  "format_region_resolutions": format_region_resolutions,
                  "format_association_placements": association_placements, "writing_space_decisions": writing_space_reports,
                  "remote_format_resolutions": remote_format_resolutions,
                  "prescribed_cover": {"section_id": prescribed_cover_section_id,
                      "source_proofs": [chapter["cover_source_proof"] for chapter in planned_formats
                                        if chapter.get("source_role") == "prescribed_cover"],
                      "values_policy": "preserve_source_slots_with_explicit_missing_issues_no_guessed_values"},
                  "drafting_instruction_omissions": instruction_omissions,
                  "factual_responses": factual_response_reports,
                  "contract_pending_plan": contract_pending,
                  "contract_pending_handling": contract_pending_handling,
                  "contract_source_review_handling": contract_source_review_handling,
                  "review_actions": review_actions,
                  "component_dispositions": component_matrix, "component_number_conflicts": conflict_handling,
                  "global_critical_controls": global_control_handling,
                  "project_constraints": project_constraints,
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
