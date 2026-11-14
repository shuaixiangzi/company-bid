"""Synthetic source-bound writing plan refinement, no network or real files."""
import copy
import hashlib

import pytest

from company_bid.models import SectionPlan, WritingTask
from company_bid.writing_plan import refine_writing_plan


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def case():
    details, blocks, units = {}, {}, {}
    sources = {"T": {"source_id": "T", "sha256": "a" * 64},
               "REF": {"source_id": "REF", "sha256": "b" * 64}}
    section = {"section_id": "SEC-synthetic", "title": "技术服务方案", "kind": "technical", "level": 1,
               "requirement_refs": [], "writing_tasks": [], "nonwriting_requirement_refs": []}

    def add(rid, text, *, reference_keywords=None, contexts=None):
        block_id = "block-" + rid
        locator = "synthetic/" + rid
        unit_id = "unit-" + rid
        units[unit_id] = {"unit_id": unit_id, "role": "tender", "coverage": {"status": "read"}}
        blocks[block_id] = {"block_id": block_id, "source_id": "T", "locator": locator, "unit_id": unit_id,
                            "text": text, "ordinal": len(blocks), "kind": "table" if "\t" in text else "paragraph"}
        details[rid] = {"requirement_id": rid, "text": text, "category": "technical", "response_mode": "writing",
                        "reference_keywords": reference_keywords or [], "citations": [{"source_id": "T", "block_id": block_id,
                            "block_ids": [block_id], "locator": locator, "quote": text, "quote_sha256": sha(text)}]}
        section["requirement_refs"].append(rid)
        task = {"unit_id": "WRITE-" + rid, "title": text[:60], "objective": "编写拟实施方法。", "must_cover": [text],
                "requirement_refs": [rid], "reference_refs": [], "forbidden_fields": ["不得虚构事实"],
                "completion_criteria": ["覆盖原文"], "operation": "bounded_new_draft", "format_status": "not_prescribed",
                "no_reference_reason": "没有可用参考", "table_refs": [], "source_context_refs": contexts or []}
        section["writing_tasks"].append(task)
        return task

    add("FORMAT", "7 技术服务方案\n（格式自拟，根据技术方案及详细评审中的技术部分要求编制）")
    add("CP", "1）按照《合成巡检手册》检查阴极保护系统恒电位仪、阳极地床、长效参比、绝缘接头、牺牲阳极，填写《巡检内容检查表》。")
    add("STRAY", "2）对照任务大表，开展杂散电流防护效果和排流器有效性测试，形成调查评价报告。")
    add("STANDARDIZATION", "3）对照手册检查阴保站标准化建设，上墙文件与设备标识均填写检查表。")
    add("REVIEW", "4）对历史问题整改情况进行复核，核查问题反复、整改完成与措施有效性、长效管控机制。")
    add("TRAINING", "5）提供防腐专业巡检培训。")
    body = "满足《合成技术标准》（SYN-A-2040）、《另一技术标准》（SYN-B）等规范要求，详见技术方案。"
    add("STANDARDS-A", "3、主要技术要求或技术方案：\n" + body)
    add("CONFORMITY", "序号\t评审项目\t\t评审标准\n\t\t技术标准和要求\t符合采购文件技术标准及要求的规定，无重大偏离（不满足施工或技术服务核心要求的，视为重大偏离）、无实质性不响应")
    add("MANAGEMENT", "1\t项目管理\n机构设置\t机构设置合理、职责明确得高分；无相关内容得0分。\t10")
    add("SCHEDULE", "3\t工程进度\n计划与措施\t进度控制全面得高分；无相关内容得0分。\t5")
    add("QUALITY", "4\t质量管理体系与保证措施\t质量程序、风险控制、质量和违约承诺完整得高分；无相关内容得0分。\t10")
    add("ROUTE", "5\t技术方案与技术措施\t技术路线清晰，文件交付和培训目标明确得高分；无相关内容得0分。\t10")
    add("STANDARDS-B", "一、主要技术要求或技术方案\n" + body)
    assembly = add("ASSEMBLY", "②与技术要求对应的服务方案，响应人为完成本项项目而计划采取的措施和方案，包括工期计划安排、质量保证措施等必要内容。",
                   contexts=["CTX-quotation", "CTX-service"])
    assembly["forbidden_fields"].append("B．不带商务报价的技术部分包括：")
    assembly["completion_criteria"].append("父条件适用性尚未核验。")
    return [section], details, blocks, units, sources, [], add


def refine(data):
    return refine_writing_plan(*data[:6])


def reference(data, text, *, rid="old", read=True, role="reference_bid"):
    blocks, units, references = data[2], data[3], data[5]
    block_id, unit_id = "reference-" + rid, "ref-unit-" + rid
    units[unit_id] = {"role": role, "coverage": {"status": "read" if read else "unread"}}
    blocks[block_id] = {"block_id": block_id, "source_id": "REF", "locator": "reference/" + rid,
                        "unit_id": unit_id, "ordinal": len(blocks), "kind": "paragraph", "text": text}
    excerpt = {"reference_id": "REF-" + rid, "source_id": "REF", "locator": "reference/" + rid,
               "text": text, "text_sha256": sha(text), "applicability": "旧泛词匹配", "retained_points": ["技术要求"],
               "excluded_points": []}
    references.append(excerpt)
    return excerpt


def test_fourteen_units_become_nine_and_three_linked_pending_controls():
    data = case()
    original_details = copy.deepcopy(data[1])
    original_covers = {t["requirement_refs"][0]: t["must_cover"][:] for t in data[0][0]["writing_tasks"]}
    report = refine(data)
    section = data[0][0]
    assert len(section["writing_tasks"]) == 9
    assert len(report["controls"]) == 3
    assert {c["requirement_id"] for c in report["controls"]} == {"FORMAT", "CONFORMITY", "ASSEMBLY"}
    assert len(report["groups"]) == 9
    assert set(section["requirement_refs"]) == set(original_details)
    paid = {rid for task in section["writing_tasks"] for rid in task["requirement_refs"]}
    assert paid | set(section["nonwriting_requirement_refs"]) == set(original_details)
    assert paid & set(section["nonwriting_requirement_refs"]) == {"ASSEMBLY"}
    overview = next(task for task in section["writing_tasks"] if "STANDARDS-A" in task["requirement_refs"])
    assert section["writing_tasks"][0] is overview
    assert set(overview["requirement_refs"]) == {"STANDARDS-A", "STANDARDS-B", "ROUTE", "ASSEMBLY"}
    assert overview["source_context_refs"] == ["CTX-quotation", "CTX-service"]
    assert set(overview["must_cover"]) == {original_covers[rid][0] for rid in overview["requirement_refs"]}
    assert data[1] == original_details
    for control in report["controls"]:
        assert set(control["linked_writing_unit_ids"]) == {t["unit_id"] for t in section["writing_tasks"]}
        assert control["source_citations"] == original_details[control["requirement_id"]]["citations"]
        assert control["source_proven"] is True
        assert control["satisfaction_status"] == "not_determined"
        assert control["review_status"] == "not_checked"
    for task in section["writing_tasks"]:
        WritingTask.model_validate(task)
        assert len(task["title"]) < 30 and "\t" not in task["title"] and "得" not in task["title"]
        assert any("父条件" in value for value in task["forbidden_fields"])
        assert any("商务报价" in value for value in task["forbidden_fields"])
        if task is not overview:
            assert not task["source_context_refs"]
    SectionPlan.model_validate(section)


def _service_period(data, rid="PERIOD", text="2、服务期限：\n合同签订之日起至2040年10月30日。"):
    data[6](rid, text)
    # A separate commercial pending action, not an existing technical task.
    data[0][0]["writing_tasks"].pop()
    data[0][0]["requirement_refs"].remove(rid)
    data[1][rid].update(category="commercial", response_mode="procedure")
    return data[1][rid]


def test_schedule_receives_verbatim_service_period_without_reclassifying_requirement():
    data = case()
    detail = _service_period(data)
    before = copy.deepcopy(data[1])
    report = refine(data)
    schedule = next(t for t in data[0][0]["writing_tasks"] if "SCHEDULE" in t["requirement_refs"])
    assert schedule["requirement_refs"] == ["SCHEDULE", "PERIOD"]
    assert schedule["must_cover"][-1] == detail["text"]
    assert "PERIOD" in data[0][0]["requirement_refs"]
    assert data[1] == before
    assert report["schedule_context"][0]["source_citations"] == detail["citations"]
    assert report["schedule_context"][0]["satisfaction_status"] == "not_determined"
    assert len(data[0][0]["writing_tasks"]) == 9
    SectionPlan.model_validate(data[0][0])


@pytest.mark.parametrize("damage", ["hash", "quote", "unread", "reference_role", "different_label", "parent_condition"])
def test_schedule_does_not_use_unverified_or_out_of_scope_period(damage):
    data = case()
    detail = _service_period(data)
    if damage == "hash":
        detail["citations"][0]["quote_sha256"] = "0" * 64
    elif damage == "quote":
        detail["citations"][0]["quote"] += "额外日期"
    elif damage == "unread":
        data[3]["unit-PERIOD"]["coverage"]["status"] = "unread"
    elif damage == "reference_role":
        data[3]["unit-PERIOD"]["role"] = "reference_bid"
    elif damage == "parent_condition":
        detail["source_contexts"] = [{"context_id": "unresolved-parent"}]
    else:
        detail["text"] = detail["text"].replace("服务期限", "历史合同期限")
        detail["citations"][0].update(quote=detail["text"], quote_sha256=sha(detail["text"]))
        data[2]["block-PERIOD"]["text"] = detail["text"]
    report = refine(data)
    schedule = next(t for t in data[0][0]["writing_tasks"] if "SCHEDULE" in t["requirement_refs"])
    assert "PERIOD" not in schedule["requirement_refs"]
    assert not report["schedule_context"]
    assert any("期限待核" in text for text in schedule["completion_criteria"])


def test_conflicting_service_periods_are_both_retained_with_pending_interpretation():
    data = case()
    first = _service_period(data)
    second = _service_period(data, "PERIOD-2", "服务期限：合同签订后六十天内完成，另有约定除外。")
    report = refine(data)
    schedule = next(t for t in data[0][0]["writing_tasks"] if "SCHEDULE" in t["requirement_refs"])
    assert schedule["must_cover"][-2:] == [first["text"], second["text"]]
    assert len(report["schedule_context"]) == 2
    assert any("矛盾时全部保留待核" in text for text in schedule["completion_criteria"])


@pytest.mark.parametrize("suffix", ["另须新增设备维修义务。", "不得按此执行。", "如适用。"])
def test_mixed_container_sentence_is_not_closed_control(suffix):
    data = case()
    task = data[0][0]["writing_tasks"][0]
    detail = data[1]["FORMAT"]
    detail["text"] += suffix
    task["must_cover"] = [detail["text"]]
    citation = detail["citations"][0]
    citation["quote"] = detail["text"]
    citation["quote_sha256"] = sha(detail["text"])
    data[2][citation["block_id"]]["text"] = detail["text"]
    before = copy.deepcopy(task)
    report = refine(data)
    assert not any(c["requirement_id"] == "FORMAT" for c in report["controls"])
    assert next(t for t in data[0][0]["writing_tasks"] if t["unit_id"] == before["unit_id"]) == before


@pytest.mark.parametrize("damage", ["quote", "hash", "source", "unread", "missing"])
def test_unproven_control_does_not_remove_original_unit(damage):
    data = case()
    task = data[0][0]["writing_tasks"][0]
    before = copy.deepcopy(task)
    citation = data[1]["FORMAT"]["citations"][0]
    if damage == "quote":
        citation["quote"] += "篡改"
    elif damage == "hash":
        citation["quote_sha256"] = "0" * 64
    elif damage == "source":
        citation["source_id"] = "REF"
    elif damage == "unread":
        data[3][data[2][citation["block_id"]]["unit_id"]]["coverage"]["status"] = "unread"
    else:
        data[1]["FORMAT"]["citations"] = []
    report = refine(data)
    assert not any(c["requirement_id"] == "FORMAT" for c in report["controls"])
    assert next(t for t in data[0][0]["writing_tasks"] if t["unit_id"] == before["unit_id"]) == before


@pytest.mark.parametrize("replacement", ["SYN-A-2041", "SYN-A-2040（不适用）", "SYN-A-2040（仅甲方）"])
def test_standard_year_polarity_or_owner_difference_prevents_merge(replacement):
    data = case()
    detail = data[1]["STANDARDS-B"]
    detail["text"] = detail["text"].replace("SYN-A-2040", replacement)
    citation = detail["citations"][0]
    citation.update(quote=detail["text"], quote_sha256=sha(detail["text"]))
    data[2][citation["block_id"]]["text"] = detail["text"]
    data[0][0]["writing_tasks"][-2]["must_cover"] = [detail["text"]]
    report = refine(data)
    assert len(data[0][0]["writing_tasks"]) == 12
    assert any(t["requirement_refs"] == ["STANDARDS-A"] for t in data[0][0]["writing_tasks"])
    assert any(t["requirement_refs"] == ["STANDARDS-B"] for t in data[0][0]["writing_tasks"])
    assert any(t["requirement_refs"] == ["ASSEMBLY"] for t in data[0][0]["writing_tasks"])
    assert not any(c["requirement_id"] == "ASSEMBLY" for c in report["controls"])


def test_score_false_mandatory_still_has_real_task_and_topic_criteria():
    data = case()
    data[1]["QUALITY"]["mandatory"] = False
    refine(data)
    quality = next(t for t in data[0][0]["writing_tasks"] if "QUALITY" in t["requirement_refs"])
    assert any("违约" in text for text in quality["completion_criteria"])
    assert any("异常复测" in text for text in quality["completion_criteria"])
    assert any("未约定值待核" in text for text in quality["completion_criteria"])


@pytest.mark.parametrize("text", [
    "使用万用表测量固态去耦合器，记录技术要求并根据产品说明核对结果。",
    "采用24小时连续测试的方式检测杂散电流，记录并复核异常测量数据。",
    "我公司已配备恒电位仪，检测后记录状态并复核异常。",
    "恒电位仪检查异常时免费维修，记录并复核至设备完全恢复。",
    "恒电位仪的测量步骤如下：根据下图记录并复核。",
])
def test_wrong_topic_numeric_fact_commitment_and_incomplete_reference_rejected(text):
    data = case()
    excerpt = reference(data, text)
    cp = next(t for t in data[0][0]["writing_tasks"] if t["requirement_refs"] == ["CP"])
    cp.update(operation="reference_adaptation", reference_refs=[excerpt["reference_id"]], no_reference_reason=None)
    refine(data)
    cp = next(t for t in data[0][0]["writing_tasks"] if t["requirement_refs"] == ["CP"])
    assert cp["operation"] == "bounded_new_draft"
    assert cp["reference_refs"] == []
    assert cp["no_reference_reason"]


def test_reference_restatement_is_not_a_method():
    data = case()
    reference(data, data[1]["CP"]["text"])
    refine(data)
    cp = next(t for t in data[0][0]["writing_tasks"] if t["requirement_refs"] == ["CP"])
    assert cp["operation"] == "bounded_new_draft"
    assert not cp["reference_refs"]


@pytest.mark.parametrize("read,role", [(False, "reference_bid"), (True, "tender")])
def test_reference_requires_read_reference_role(read, role):
    data = case()
    reference(data, "用万用表测量恒电位仪的输出并记录，将实测值与显示值比对，异常复核后形成检查记录。", read=read, role=role)
    refine(data)
    cp = next(t for t in data[0][0]["writing_tasks"] if t["requirement_refs"] == ["CP"])
    assert cp["operation"] == "bounded_new_draft"


def test_complete_object_action_reference_preserves_exact_block_and_hash():
    data = case()
    text = "用万用表测量恒电位仪的输出并记录，将实测值与显示值比对，异常复核后形成检查记录。"
    reference(data, text)
    report = refine(data)
    cp = next(t for t in data[0][0]["writing_tasks"] if t["requirement_refs"] == ["CP"])
    ref = next(r for r in data[5] if r["reference_id"] == cp["reference_refs"][0])
    assert cp["operation"] == "reference_adaptation"
    assert ref["text"] == text and ref["text_sha256"] == sha(text)
    assert ref["locator"] == "reference/old"
    record = next(r for r in report["references"] if r["unit_id"] == cp["unit_id"])
    assert record["source_sha256"] == "b" * 64
    assert record["complete_source_block"] is True


def test_unknown_task_with_its_reference_is_unchanged():
    data = case()
    task = data[-1]("UNKNOWN", "应制定特殊设备的专门排查办法，依附件个别条款执行。")
    excerpt = reference(data, "特殊设备检查后记录缺陷并核对资料，按明确程序形成专门报告。")
    task.update(operation="reference_adaptation", reference_refs=[excerpt["reference_id"]], no_reference_reason=None)
    before = copy.deepcopy(task)
    refine(data)
    assert next(t for t in data[0][0]["writing_tasks"] if t["unit_id"] == task["unit_id"]) == before
    assert excerpt in data[5]


def test_output_is_deterministic_with_separate_copies():
    first, second = case(), case()
    refine(first)
    refine(second)
    assert first[:6] == second[:6]


@pytest.mark.parametrize("text", [
    "杂散电流干扰：交直流干扰评价指标、测试方法、排流设施检测与维护。",
    "《标准化管理手册》：阴保站标准化建设、上墙文件、设备标识、台账记录。",
    "根据巡检问题编制报告，完成后由项目技术负责人审核并上报。",
])
def test_subject_lists_and_incidental_job_name_are_not_methods(text):
    data = case()
    reference(data, text)
    refine(data)
    assert not data[5]


def test_reference_source_hash_required():
    data = case()
    reference(data, "用万用表测量恒电位仪的输出并记录，将实测值与显示值比对，异常复核后形成检查记录。")
    data[4]["REF"].pop("sha256")
    refine(data)
    assert not data[5]


def test_control_with_assigned_table_is_not_removed():
    data = case()
    task = data[0][0]["writing_tasks"][0]
    task["table_refs"] = ["TABLE-must-stay"]
    before = copy.deepcopy(task)
    report = refine(data)
    assert not any(c["requirement_id"] == "FORMAT" for c in report["controls"])
    assert next(t for t in data[0][0]["writing_tasks"] if t["unit_id"] == before["unit_id"]) == before


def test_all_removed_control_forbidden_fields_survive_on_real_units():
    data = case()
    data[0][0]["writing_tasks"][0]["forbidden_fields"].append("不得将编写指令当作已完成事实。")
    refine(data)
    assert all("不得将编写指令当作已完成事实。" in task["forbidden_fields"] for task in data[0][0]["writing_tasks"])


def test_control_does_not_hide_single_task_section_and_unknown_topic():
    data = case()
    data[0][0]["writing_tasks"] = data[0][0]["writing_tasks"][:1]
    before = copy.deepcopy(data[0][0])
    report = refine(data)
    assert not report["controls"]
    assert data[0][0] == before


def test_changed_task_must_cover_is_not_accepted_as_source_control():
    data = case()
    task = data[0][0]["writing_tasks"][0]
    task["must_cover"].append("未经来源支持的额外工作。")
    before = copy.deepcopy(task)
    report = refine(data)
    assert not any(c["requirement_id"] == "FORMAT" for c in report["controls"])
    assert next(t for t in data[0][0]["writing_tasks"] if t["unit_id"] == before["unit_id"]) == before
