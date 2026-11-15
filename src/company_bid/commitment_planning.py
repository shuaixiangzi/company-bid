"""Pure source-bound planning of two independent proposed commitment letters.

No provider construction, runtime, filesystem, or external calls. Existing
technical tasks and their prompt remain untouched.
"""
from __future__ import annotations

import re
import hashlib

from .models import PreparedBid, WritingTask
from .provider import ProviderFailure
from .storage import digest

PENDING_LABEL = "【拟承诺草稿，待公司确认并签章】"


def _require(condition, code):
    if not condition:
        raise ProviderFailure("COMMITMENT_" + code)


def _compact(text):
    return re.sub(r"\s+", "", text)


def _category(title):
    title = re.sub(r"^[\d一二三四五六七八九十\s、.．()（）]+", "", title)
    return {"质量保证承诺书": "quality", "质量保证承诺": "quality",
            "售后服务承诺书": "after_service", "售后服务承诺": "after_service"}.get(title)


def _source_categories(text):
    """Select whole frozen clauses, never reconstruct or trim their conditions."""
    text = _compact(text)
    result = set()
    if "质量保证期" in text and ("验收" in text or "质量标准" in text):
        result.add("warranty")
    if "咨询" in text and ("答复" in text or "回复" in text):
        result.add("consultation")
    if "免费服务" in text and ("报告" in text or "项目结束" in text):
        result.add("free_support")
    if "返还" in text and "资料" in text and "留存" in text:
        result.add("return_materials")
    if re.search(r"验收标准[：:]", text) and "技术服务" in text:
        result.add("acceptance_standard")
    if "验收程序" in text and "验收不合格" in text and ("修改" in text or "重新" in text):
        result.add("acceptance_correction")
    if "任何一方未履行" in text and "全部损失" in text and "义务" in text:
        result.add("liability")
    if "技术服务内容：" in text or "技术服务内容:" in text or "技术服务要求" in text and ("GB/T" in text or "Q/" in text):
        result.add("service_scope")
    return result


def commitment_nonwriting_heading_refs(prepared) -> dict[str, list[str]]:
    """Prove only redundant format headings, not substantive chapter duties."""
    prepared = PreparedBid.model_validate(prepared)
    requirements = {item.requirement_id: item for item in prepared.requirements}
    sources = {item.source_id: item for item in prepared.sources}
    result = {}
    for section in prepared.sections:
        if _category(section.title) is None or section.document_role != "response" or not section.render_body:
            continue
        refs = []
        for ref in section.requirement_refs:
            item = requirements[ref]
            if not ref.startswith("R-FORM-") or item.text != section.title:
                continue
            quote_hash = hashlib.sha256(item.text.encode("utf-8")).hexdigest()
            _require(item.category == "procedure" and item.response_mode == "fixed_text" and not item.source_contexts
                     and all(sources[sid].kind == "tender" and
                         re.search(r"(?:^|; )quote_sha256=" + quote_hash + r"(?:;|$)", sources[sid].locator)
                         and f"chars[0:{len(item.text)}]" in sources[sid].locator for sid in item.source_refs),
                     "FORMAT_HEADING_PROOF_INVALID")
            refs.append(ref)
        result[section.section_id] = refs
    return result


def _readonly_scope_requirements(prepared, section):
    sources = {item.source_id: item for item in prepared.sources}
    tagged = [(item, _source_categories(item.text)) for item in prepared.requirements
              if all(sources[ref].kind == "tender" for ref in item.source_refs)]
    # Prefer complete atomic clauses when an extraction review also retained a
    # parent container with the identical clause. This is deduplication only:
    # never truncate a selected requirement or drop unique source conditions.
    tagged = [(item, {tag for tag in tags if not any(
        tag in other_tags and len(_compact(other.text)) < len(_compact(item.text))
        and _compact(other.text) in _compact(item.text) for other, other_tags in tagged)})
        for item, tags in tagged]
    found = set().union(*(tags for _, tags in tagged))
    _require({"warranty", "consultation", "free_support", "return_materials"} <= found,
             "SOURCE_SCOPE_INCOMPLETE")
    wanted = {"warranty", "consultation", "free_support", "return_materials", "acceptance_standard",
              "acceptance_correction", "liability", "service_scope"}
    wanted_refs = set(section.requirement_refs) | {item.requirement_id for item, tags in tagged if tags & wanted}
    selected = [item for item in prepared.requirements if item.requirement_id in wanted_refs]
    _require(len(selected) <= 30, "SOURCE_SCOPE_TOO_LARGE")
    return selected


def plan_commitment_tasks(prepared: PreparedBid | dict) -> dict[str, WritingTask]:
    """Pure N3 helper. Only chapter duties are writing-task requirement refs.

    Complete contract clauses are must-cover readonly context. Their existing
    pending duties/blank fields stay in their original review/manual pathway;
    producing these proposed letters cannot mark those duties as drafted/met.
    Project-constraint forbidden suffixes are appended by the existing N3
    constraint planner and independently rebuilt by the partition preflight.
    """
    prepared = PreparedBid.model_validate(prepared)
    requirements = {item.requirement_id: item for item in prepared.requirements}
    sources = {item.source_id: item for item in prepared.sources}
    nonwriting = commitment_nonwriting_heading_refs(prepared)
    targets = {}
    for section in prepared.sections:
        category = _category(section.title)
        if category is None or section.document_role != "response" or not section.render_body:
            continue
        _require(category not in targets, "SECTION_AMBIGUOUS")
        own = [requirements[ref] for ref in section.requirement_refs]
        instructions = [item for item in own if "格式自拟" in _compact(item.text)
                        and _category(re.split(r"[\n（(]", item.text.strip())[0]) == category]
        if not instructions:
            continue
        _require(all(sources[ref].kind == "tender" for item in own for ref in item.source_refs),
                 "FORMAT_SOURCE_INVALID")
        # A contract clause cannot be smuggled into the primary chapter refs.
        _require(not any(_source_categories(item.text) for item in own), "CONTRACT_REFERENCE_NOT_READONLY")
        targets[category] = section
    if set(targets) != {"quality", "after_service"}:
        return {}
    result = {}
    for category, section in targets.items():
        selected = _readonly_scope_requirements(prepared, section)
        own = [item for item in prepared.requirements if item.requirement_id in section.requirement_refs
               and item.requirement_id not in nonwriting.get(section.section_id, [])]
        refs = [item.requirement_id for item in own]
        unit_id = "COMMIT-" + digest({"section_id": section.section_id, "category": category})[:20]
        task = WritingTask(unit_id=unit_id,
            title="质量保证拟承诺（待公司确认）" if category == "quality" else "售后服务拟承诺（待公司确认）",
            objective="依据完整冻结招标条款形成独立拟承诺函正文，明确条件、流程、责任与记录；待公司确认并签章后方可采用。",
            must_cover=[item.text for item in selected], requirement_refs=refs,
            operation="bounded_new_draft", format_status="not_prescribed",
            no_reference_reason="仅借鉴人工稿函件结构与具体条款深度；其额外商业承诺、既有事实及旧日期未经本项目确认，不作为事实来源或原文改编。",
            source_context_refs=[context.context_id for item in own for context in item.source_contexts],
            forbidden_fields=["不得宣称公司已经承诺、已满足或已签章；不得编造当前人员设备、资质、价格、日期。",
                "不得新增冻结招标未支持的响应时限、到场时限、长期免费或终身服务承诺。",
                "原合同空白是待补范围，不是读取失败；不得猜填。保留源条款条件、费用、起算及例外。"],
            completion_criteria=[PENDING_LABEL, "独立函件：称谓/项目事项、具体条款、责任边界、待签章结尾。",
                "每条有适用对象、条件、拟执行动作及记录或交付；质量和售后各有主题，不以泛泛套话代替。",
                "覆盖完整原条款，coverage仅定位待审内容；公司确认和要求满足均未确定。"])
        existing = [item for item in section.writing_tasks if item.unit_id != unit_id]
        _require(not existing, "TARGET_ALREADY_HAS_WRITING")
        result[section.section_id] = task
    return result


def commitment_source_scope(prepared, task):
    prepared = PreparedBid.model_validate(prepared)
    sources = {item.source_id: item for item in prepared.sources}
    sid = next(sid for sid, planned in plan_commitment_tasks(prepared).items() if planned.unit_id == task.unit_id)
    section = next(section for section in prepared.sections if section.section_id == sid)
    selected = [item.model_dump(mode="json") for item in _readonly_scope_requirements(prepared, section)]
    refs = list(dict.fromkeys(ref for item in selected for ref in [*item["source_refs"],
        *(ref for context in item.get("source_contexts", []) for ref in context["source_refs"])]))
    return {"role": "readonly_frozen_tender_requirements", "requirements": selected,
        "sources": [{"source_id": ref, "source_sha256": sources[ref].sha256,
                     "locator": sources[ref].locator} for ref in refs]}


def commitment_allowlist(prepared):
    """Pin the final task including independently checked constraint suffixes.

    This builder is not authorization: preflight compares the actual task to the
    independently rebuilt base task plus proved project constraints.
    """
    prepared = PreparedBid.model_validate(prepared)
    actual = {task.unit_id: task for section in prepared.sections for task in section.writing_tasks}
    return {task.unit_id: {"section_id": sid,
                          "task_sha256": digest(actual.get(task.unit_id, task).model_dump(mode="json")),
                          "source_scope_sha256": digest(commitment_source_scope(prepared, task)),
                          "identity_sha256": digest(commitment_identity(prepared))}
            for sid, task in plan_commitment_tasks(prepared).items()}


def commitment_identity(prepared):
    """Frozen known names; pending confirmation applies to promises, not names."""
    prepared = PreparedBid.model_validate(prepared)
    sources = {item.source_id: item for item in prepared.sources}
    result = {"role": "frozen_known_project_company_identity"}
    for key in ("project", "company"):
        identity = getattr(prepared, key)
        result[key] = {"value": identity.model_dump(mode="json"),
            "sources": [{"source_id": ref, "source_sha256": sources[ref].sha256,
                         "locator": sources[ref].locator} for ref in identity.source_refs]}
    return result


