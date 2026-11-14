"""Conservative, source-bound refinement of N3 technical writing units.

This module changes the plan, never an N2 requirement or its source. Closed
administrative sentences become linked pending controls; unsupported language
keeps its original unit. A reference is optional and never proves company facts.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re


WRITING_PLAN_VERSION = "writing-plan-v55-2"


def _compact(text):
    return re.sub(r"\s+", "", text)


def _unique(values):
    return list(dict.fromkeys(values))


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _identity(prefix, value):
    return prefix + _sha(json.dumps(value, ensure_ascii=False, sort_keys=True))[:20]


def _source_checked(detail, blocks, units, sources):
    """Require the complete frozen quote in its declared, readable source span.

    N2 has already checked the full citation/catalog contract. This additional
    gate checks its local inputs rather than trusting a classification or hash
    in the refinement report. Selected native table rows remain complete TSV.
    """
    citations = detail.get("citations", [])
    if not citations:
        return False
    for citation in citations:
        ids = citation.get("block_ids") or [citation.get("block_id")]
        if not ids or ids[0] != citation.get("block_id") or len(set(ids)) != len(ids):
            return False
        span = [blocks.get(key) for key in ids]
        if any(block is None for block in span):
            return False
        source_id = citation.get("source_id")
        source = sources.get(source_id, {})
        if not source.get("sha256") or any(block.get("source_id") != source_id for block in span):
            return False
        if span[0].get("locator") != citation.get("locator"):
            return False
        if any(units.get(block.get("unit_id"), {}).get("role") != "tender" or
               units.get(block.get("unit_id"), {}).get("coverage", {}).get("status") != "read" or
               block.get("read_disposition") for block in span):
            return False
        if ids != [b["block_id"] for b in sorted(span, key=lambda b: b.get("ordinal", -1))]:
            return False
        quote = citation.get("quote")
        text = "\n".join(block.get("text", "") for block in span)
        if (quote != detail.get("text") or not quote or text.count(quote) != 1 or
                citation.get("quote_sha256") != _sha(quote)):
            return False
    return True


def _control(text):
    text = _compact(text)
    if re.fullmatch(r"(?:\d+(?:\.\d+)*[、.．]?)?技术服务方案[（(]格式自拟[，,]"
                    r"根据技术方案及详细评审中的技术部分要求编制[）)]", text):
        return "section_compilation"
    if re.fullmatch(r"(?:序号评审项目评审标准)?技术标准和要求符合(?:采购|招标)文件技术标准及要求的规定[，,]"
                    r"无重大偏离[（(]不满足施工或技术服务核心要求的[，,]视为重大偏离[）)]"
                    r"[、，,]无实质性不响应[。.]?", text):
        return "initial_technical_conformity"
    if re.fullmatch(r"[①②③④⑤⑥⑦⑧⑨⑩]?与技术要求对应的服务方案[，,](?:响应人|投标人|供应商)"
                    r"为完成本(?:项)?项目而计划采取的措施和方案[，,]包括工期计划安排[、，,]"
                    r"质量保证措施等必要内容[。.]?", text):
        return "service_assembly"
    return None


def _standards_key(text):
    text = _compact(text)
    match = re.fullmatch(r"(?:[一二三四五六七八九十百]+[、.．]|\d+[、.．])?"
                         r"(?:主要)?技术要求或技术方案[：:]?(满足《.+》.+等规范要求[，,]详见技术方案[。.]?)", text)
    if match and match[1].count("《") >= 2:
        return match[1]
    return None


_SCORE_TOPICS = {
    "项目管理机构设置": "management",
    "工程进度计划与措施": "schedule",
    "质量管理体系与保证措施": "quality",
    "技术方案与技术措施": "technical_route",
}


def _topic(detail):
    text = _compact(detail["text"])
    fields = [_compact(field) for field in detail["text"].split("\t") if field.strip()]
    if (len(fields) == 4 and fields[0].isdigit() and fields[-1].isdigit() and
            fields[1] in _SCORE_TOPICS and "得" in fields[2] and "分" in fields[2]):
        return _SCORE_TOPICS[fields[1]]
    if _standards_key(detail["text"]):
        return "standards"
    if ("阴极保护系统" in text and "恒电位仪" in text and "牺牲阳极" in text and
            "填写" in text and "巡检内容检查表" in text):
        return "cp_inspection"
    if "杂散电流" in text and "排流器" in text and "调查评价报告" in text:
        return "stray_current"
    if "阴保站标准化建设" in text and "上墙文件" in text and "标识" in text and "填写" in text:
        return "standardization"
    if all(word in text for word in ("整改", "复核", "问题反复", "长效管控机制")):
        return "rectification_review"
    if re.fullmatch(r"(?:\d+[）)]|[（(]\d+[）)])?提供防腐专业巡检培训[。.]?", text):
        return "training"
    return None


_TITLES = {
    "technical_route": "技术依据 实施路线与成果交付",
    "cp_inspection": "阴极保护系统设备巡检",
    "stray_current": "杂散电流防护效果与排流器测试",
    "standardization": "阴保站标准化建设检查",
    "rectification_review": "历史巡检问题整改复核",
    "training": "防腐专业巡检培训",
    "management": "项目管理机构与岗位职责",
    "schedule": "工程进度计划与控制措施",
    "quality": "质量管理 风险控制与保证措施",
    "standards": "技术标准与适用依据",
}

_CRITERIA = {
    "technical_route": ["说明技术标准与工作对象的对应关系、实施前后依赖，以及原始记录如何进入评价报告和培训交付。",
                        "报告审核、问题反馈、文件交付和培训目标有实际步骤，不以评分句或全面满足代替方法。"],
    "cp_inspection": ["恒电位仪、阳极地床、长效参比、绝缘接头、牺牲阳极逐项有检查或测试动作、记录字段和异常复核。",
                      "说明完好性及运行状态评价、巡检内容检查表填写和结果交付；停送电、拆接线和调参须先取得运行与作业许可。"],
    "stray_current": ["核对任务大表、治理管段和作业区现状，说明防护效果与排流器有效性测试、数据可比性和异常复核。",
                      "形成有原始记录依据的调查评价报告；参考测试时长、采样值与阈值未经适用性核对不得变成强制承诺或合格判据。"],
    "standardization": ["对照手册逐项核对上墙文件与设备设施标识，说明现场比对、差异记录、照片依据和汇总复核。",
                        "填写阴保站标准化建设检查表并交付检查结果；不以参考中的其他统计表替代指定表，不扩大制作安装或维修范围。"],
    "rectification_review": ["关联历史问题与整改凭据，分别核对完成情况、措施有效性、问题反复和长效管控机制，形成可追溯复核记录。",
                            "缺历史台账或整改依据明确待补，未闭合问题按授权上报并纳入要求规定的考核用途，不承诺代为整改至到位。"],
    "training": ["说明培训需求与对象确认、具体课程、演示或案例、考核或反馈、培训记录和资料交付。",
                 "课程关联实际巡检对象与异常处理；未知时间地点、规模和人员明确待补，不虚构签到或合格成绩。"],
    "management": ["按拟配置岗位说明组织、专业、职责分工、接口和五项工作及成果审核责任，不能仅列组织口号。",
                   "实际人员、数量、证号及资质仅来自明确事实，缺配置或任命保留待补；组织描述不替代人员评分所需证明。"],
    "schedule": ["依据冻结服务期限安排准备、现场检测、复核、报告和培训的先后依赖及里程碑，缺签署日或工作量不得猜填日期。",
                 "说明准入协调、天气、仪器故障、数据复测等进度风险的记录、预警、责任与调整方法，不移用参考旧日程。"],
    "quality": ["明确质量职责与程序，将仪器状态、方法适用、原始记录、异常复测、数据报告一致和交付审核落实到本项目对象。",
                "给出漏项、数据失真、记录不全、方法误用和异常误判等风险的控制与复核记录；保留质量承诺和违约责任评分要素。",
                "质量及违约承诺以本次招标文件和合同约定为边界，未约定值待核；不引入终身、无条件、额外时限或绝对质量指标。"],
    "standards": ["逐字保留冻结标准名称与编号并说明适用工作对象，不从参考猜补年版、条款号、阈值或合格结论。"],
}

_OBJECTS = {
    "technical_route": ("巡检报告", "成果文件", "技术方案"),
    "cp_inspection": ("恒电位仪", "阳极地床", "参比电极", "绝缘接头", "牺牲阳极"),
    "stray_current": ("杂散电流", "排流器", "排流设施", "固态去耦合器"),
    "standardization": ("上墙文件", "设备标识", "阴保站标准化", "阴极保护间"),
    "rectification_review": ("整改情况", "整改复核", "复核记录"),
    "training": ("培训",),
    "management": ("岗位职责", "项目经理", "技术负责人"),
    "quality": ("原始记录", "质量风险", "检测数据", "成果文件", "报告"),
}


def _concrete_method(topic, text):
    """Exclude headings, lists of subjects and a duty named out of context."""
    if topic == "technical_route":
        return (bool(re.search(r"收集|汇总|整理", text)) and "报告" in text and
                bool(re.search(r"编制|形成", text)) and bool(re.search(r"审核|复核", text)) and
                bool(re.search(r"上报|提交|呈送|交付", text)))
    if topic == "cp_inspection":
        return bool(re.search(r"逐项检测|(?:采用|使用|用).{0,35}(?:测试|测量)|对.{0,25}(?:校核|测量|校准)", text))
    if topic == "stray_current":
        return (bool(re.search(r"使用|采用|通过|进行", text)) and
                bool(re.search(r"测量|检测|测试", text)) and bool(re.search(r"记录|导出|汇总|复核|评价", text)))
    if topic == "standardization":
        return bool(re.search(r"检查|核对|核查", text)) and bool(re.search(r"记录|拍照|建档|汇总", text))
    if topic == "rectification_review":
        return "整改" in text and bool(re.search(r"复核|复查|验证", text)) and bool(re.search(r"记录|台账|报告", text))
    if topic == "training":
        return "培训" in text and bool(re.search(r"考试|实操|考核", text)) and bool(re.search(r"评价|记录|反馈|结果", text))
    if topic == "management":
        return (bool(re.search(r"组织|职责|岗位|分工", text)) and bool(re.search(r"负责|承担|协调", text)) and
                bool(re.search(r"记录|台账|接口|报告", text)))
    if topic == "quality":
        return bool(re.search(r"审核|核对|复核|抽检", text)) and bool(re.search(r"依据|一致|异常|质量", text))
    return False


def _method_reference(topic, details, blocks, units, sources):
    """Select one complete self-contained block, with no numeric parameter reuse.

    This deliberately falls back to bounded drafting for diagrams, abbreviated
    steps, old commitments, generic keyword hits and merely repeated requests.
    """
    originals = {_compact(detail["text"]).rstrip("。；;.") for detail in details}
    candidates = []
    for block in blocks.values():
        unit = units.get(block.get("unit_id"), {})
        source = sources.get(block.get("source_id"), {})
        text = block.get("text", "")
        if (unit.get("role") != "reference_bid" or unit.get("coverage", {}).get("status") != "read" or
                not source.get("sha256") or block.get("kind") != "paragraph" or block.get("read_disposition") or
                not 20 <= len(text) <= 3500 or _compact(text).rstrip("。；;.") in originals):
            continue
        hits = [word for word in _OBJECTS.get(topic, ()) if word in text]
        if (not hits or not _concrete_method(topic, text) or
                not re.search(r"记录|比对|复核|审核|报告|评价|反馈|考核|考试|检验|建档", text) or
                re.search(r"我公司|本公司|有限公司|责任公司|业绩|报价|身份证|开户|已配备|现有|拥有|成立于|"
                          r"终身|无条件|免费|直至|\d|如下|下图|上图|以上|见表|见图|[：:]\s*$", text)):
            continue
        # A full source paragraph which restates one required sentence is still
        # not a method, even when prefixed with an item number in either file.
        bare = re.sub(r"^(?:\d+[）)]|[（(]\d+[）)])", "", _compact(text)).rstrip("。；;.")
        if any(bare in original or original in bare for original in originals):
            continue
        candidates.append((len(hits), -block.get("ordinal", 0), block, hits))
    if not candidates:
        return None
    _, _, block, hits = max(candidates, key=lambda value: value[:2])
    text = block["text"]
    return {"reference_id": _identity("REF-", [block["source_id"], block["locator"], text]),
            "source_id": block["source_id"], "locator": block["locator"], "text": text, "text_sha256": _sha(text),
            "applicability": "仅作为本任务对象的拟实施方法参考：" + "、".join(hits) + "；不证明公司既有事实，条件与判据仍待来源核对。",
            "retained_points": hits,
            "excluded_points": ["历史公司、项目、人员、设备和完成事实", "参考旧日期、阈值、报价与未经本次要求授权的服务承诺"]}


def _merge(tasks, section_id):
    result = copy.deepcopy(tasks[0])
    for field in ("must_cover", "requirement_refs", "reference_refs", "forbidden_fields", "completion_criteria", "table_refs", "source_context_refs"):
        result[field] = _unique(value for task in tasks for value in task.get(field, []))
    result["unit_id"] = _identity("WRITE-", [WRITING_PLAN_VERSION, section_id, "technical_route", sorted(result["requirement_refs"])])
    return result


def _schedule_context(task, section, details, blocks, units, sources, report):
    """Link a read service-period requirement without interpreting its dates.

    A schedule score alone does not supply a service period. Keep the complete
    original requirement and its existing pending action, including ambiguous
    or conflicting text; the writer may not choose between competing periods.
    """
    selected = [detail for detail in details.values()
                if re.match(r"^(?:\d+[、.．])?服务期限[：:]", _compact(detail.get("text", "")))
                and not detail.get("source_contexts")
                and _source_checked(detail, blocks, units, sources)]
    if len(task["must_cover"]) + len(selected) > 30:
        selected = []
    if not selected:
        task["completion_criteria"].append("本任务未取得已校验的服务期限原文，仅写相对阶段安排并明确期限待核，不猜定日期。")
    else:
        task["completion_criteria"].append("服务期限以本任务附带的招标原字要求为边界；签署日未知不得猜填，多个期限矛盾时全部保留待核，不择一承诺。")
    for detail in selected:
        rid = detail["requirement_id"]
        task["requirement_refs"] = _unique([*task["requirement_refs"], rid])
        task["must_cover"] = _unique([*task["must_cover"], detail["text"]])
        section["requirement_refs"] = _unique([*section["requirement_refs"], rid])
        report["schedule_context"].append({"unit_id": task["unit_id"], "requirement_id": rid,
            "text_sha256": _sha(detail["text"]), "source_citations": copy.deepcopy(detail["citations"]),
            "source_hashes": {c["source_id"]: sources[c["source_id"]]["sha256"] for c in detail["citations"]},
            "source_proven": True, "interpretation": "verbatim_only", "review_status": "not_checked",
            "satisfaction_status": "not_determined"})


def refine_writing_plan(sections, details, blocks, units, sources, references):
    """Refine dictionaries in place and return an independently inspectable report.

    Call after the source-context attachment pass and before instruction
    placement and project-constraint plans. Context-bearing control requirements
    remain on the overview unit to satisfy the existing exact context contract;
    they never receive a separate paid unit. Other units inherit explicit
    constraints without falsely claiming those context references as their own.
    """
    source_map = sources if isinstance(sources, dict) else {s["source_id"]: s for s in sources}
    report = {"schema_version": WRITING_PLAN_VERSION, "controls": [], "groups": [], "references": [], "schedule_context": [],
              "unsupported": [], "n2_candidate_mutation": False, "review_status": "not_checked",
              "satisfaction_status": "not_determined"}
    for section in sections:
        original = section.get("writing_tasks", [])
        if section.get("kind") != "technical" or not original:
            continue
        entries = []
        for task in original:
            rids = task.get("requirement_refs", [])
            detail = details.get(rids[0]) if len(rids) == 1 else None
            checked = bool(detail and detail.get("category") == "technical" and detail.get("response_mode") == "writing" and
                           task.get("must_cover") == [detail.get("text")] and
                           _source_checked(detail, blocks, units, source_map))
            entries.append({"task": copy.deepcopy(task), "detail": detail, "topic": _topic(detail) if checked else None,
                            "control": _control(detail["text"]) if checked else None})
        standard_entries = [e for e in entries if e["topic"] == "standards"]
        routes = [e for e in entries if e["topic"] == "technical_route"]
        overview = None
        keys = {_standards_key(e["detail"]["text"]) for e in standard_entries}
        if standard_entries and len(keys) == 1 and len(routes) == 1:
            members = [e for e in entries if e in standard_entries or e in routes]
            overview = {"task": _merge([e["task"] for e in members], section["section_id"]), "topic": "technical_route",
                        "detail": None, "control": None}
            entries = [e for e in entries if e not in members]
            entries.insert(0, overview)
        substantive = [e for e in entries if e["topic"] and not e["control"]]
        removed = []
        for entry in list(entries):
            if not entry["control"] or not substantive or entry["task"].get("table_refs"):
                continue
            task = entry["task"]
            # Without an overview, keep a context-bound original unit intact.
            if task.get("source_context_refs") and overview is None:
                continue
            if task.get("source_context_refs"):
                for field in ("requirement_refs", "must_cover", "source_context_refs", "forbidden_fields", "completion_criteria"):
                    overview["task"][field] = _unique([*overview["task"].get(field, []), *task.get(field, [])])
            entries.remove(entry)
            removed.append(entry)
            section["nonwriting_requirement_refs"] = _unique([*section.get("nonwriting_requirement_refs", []), *task["requirement_refs"]])
        inherited = [field for entry in removed for field in entry["task"].get("forbidden_fields", [])]
        if any(e["control"] == "service_assembly" and e["task"].get("source_context_refs") for e in removed):
            inherited.append("本任务受技术部分服务类适用及不带商务报价的父条件约束，适用性尚未核验；不得输出商务报价或声称已确认符合。")
        for entry in entries:
            task, topic = entry["task"], entry["topic"]
            if not topic:
                report["unsupported"].append({"section_id": section["section_id"], "unit_id": task["unit_id"],
                                               "requirement_refs": task["requirement_refs"], "decision": "original_unit_preserved"})
                continue
            task["title"] = _TITLES[topic]
            task["completion_criteria"] = _unique([*task.get("completion_criteria", []), *_CRITERIA[topic]])
            task["forbidden_fields"] = _unique([*task.get("forbidden_fields", []), *inherited])
            if topic == "schedule":
                _schedule_context(task, section, details, blocks, units, source_map, report)
            task_details = [details[rid] for rid in task["requirement_refs"]]
            reference = _method_reference(topic, task_details, blocks, units, source_map)
            task["reference_refs"] = [reference["reference_id"]] if reference else []
            task["operation"] = "reference_adaptation" if reference else "bounded_new_draft"
            task["no_reference_reason"] = None if reference else "冻结参考中未证成同时具有本任务对象、具体方法与完整记录或复核步骤且不混入旧事实承诺的独立片段。"
            if reference and reference["reference_id"] not in {r["reference_id"] for r in references}:
                references.append(reference)
            report["groups"].append({"section_id": section["section_id"], "unit_id": task["unit_id"], "topic": topic,
                                     "requirement_refs": task["requirement_refs"], "must_cover_sha256": [_sha(t) for t in task["must_cover"]],
                                     "source_context_refs": task.get("source_context_refs", []), "review_status": "not_checked"})
            if reference:
                report["references"].append({"unit_id": task["unit_id"], "reference_id": reference["reference_id"],
                    "source_id": reference["source_id"], "source_sha256": source_map[reference["source_id"]]["sha256"],
                    "locator": reference["locator"], "text_sha256": reference["text_sha256"], "complete_source_block": True})
        section["writing_tasks"] = [entry["task"] for entry in entries]
        linked = [task["unit_id"] for task in section["writing_tasks"]]
        for entry in removed:
            task = entry["task"]
            for rid in task["requirement_refs"]:
                report["controls"].append({"requirement_id": rid, "section_id": section["section_id"],
                    "control": entry["control"], "linked_writing_unit_ids": linked,
                    "original_writing_unit_id": task["unit_id"], "source_citations": copy.deepcopy(details[rid]["citations"]),
                    "source_hashes": {c["source_id"]: source_map[c["source_id"]]["sha256"] for c in details[rid]["citations"]},
                    "source_context_refs": task.get("source_context_refs", []), "original_must_cover": task["must_cover"],
                    "parent_condition_handling": {
                        "source_context_refs": task.get("source_context_refs", []),
                        "source_contexts": copy.deepcopy(details[rid].get("source_contexts", [])),
                        "original_constraint_fields": task.get("forbidden_fields", []),
                        "original_completion_criteria": task.get("completion_criteria", []),
                        "propagated_constraint_fields": inherited if task.get("source_context_refs") else [],
                        "review_status": "not_checked"},
                    "retained_on_overview_for_context_contract": bool(task.get("source_context_refs")),
                    "source_proven": True, "decision": "linked_pending_control_without_independent_writing_unit",
                    "review_status": "not_checked", "satisfaction_status": "not_determined"})
        before = {rid for task in original for rid in task["requirement_refs"]}
        after = {rid for task in section["writing_tasks"] for rid in task["requirement_refs"]}
        retained = set(section.get("nonwriting_requirement_refs", []))
        if not before <= after | retained:
            raise ValueError("WRITING_REFINEMENT_REQUIREMENT_LOSS")
    used = {rid for section in sections for task in section.get("writing_tasks", []) for rid in task.get("reference_refs", [])}
    references[:] = [reference for reference in references if reference["reference_id"] in used]
    return report
