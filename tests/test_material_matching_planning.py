"""Shared material decisions must control the actual N3 attachment output."""
import copy
import json

import pytest

from company_bid.planning import _material_match_report, plan_bid
from company_bid.runtime import Executor, NodeSpec
from test_planning import Context, add_unit, extract, fixture, no_call, requirement, response


def proof_case(tmp_path, text, title, *, description="合成证明包", organize=False):
    manifest, parsed = fixture(tmp_path)
    block = add_unit(parsed, "U1", text)
    manifest["attachments"] = [{"attachment_id": "PROOF", "section_id": "unassigned", "title": title,
        "description": description, "path": str(tmp_path / "proof.png"), "sha256": "c" * 64,
        "source_refs": ["company"], "requirement_refs": ["unassigned"], "synthetic": True}]
    ctx = Context()
    extracted = extract(ctx, parsed, manifest, lambda p: response(p, [requirement(block,
                        category="qualification", mode="attachment")]))
    before = copy.deepcopy((extracted, parsed, manifest))
    output = plan_bid(ctx, extracted, parsed, manifest, no_call, {"organize_response": organize})
    assert (extracted, parsed, manifest) == before
    return output, ctx


@pytest.mark.parametrize("organize", [False, True])
@pytest.mark.parametrize("text,title", [
    ("须提供安许证。", "7.1 安全生产许可证复印件"),
    ("须提供有效的质量管理体系认证证书。", "质量管理体系认证证书"),
    ("须提供工商营业执照。", "营业执照"),
    ("须提供高企证书。", "高新技术企业资质"),
])
def test_synonym_match_reaches_real_planning_without_model(tmp_path, text, title, organize):
    output, ctx = proof_case(tmp_path, text, title, organize=organize)
    assert [a["attachment_id"] for a in output["attachments"]] == ["PROOF"]
    assert any("适用" in i["title"] for i in output["issues"])
    report = ctx.captures["n3-material-matching"]
    assert report["assembly_candidates"][0]["qualification_result"] == "not_determined"
    assert len(report["checkpoint_units"]) == 1
    assert ctx.captures["n3-planning-report"]["attachments"][0]["matching_policy"]["rule"] == "unified_source_bound_material_match"


@pytest.mark.parametrize("organize", [False, True])
@pytest.mark.parametrize("title,description", [
    ("营业执照办理申请说明", "申请办理流程"),
    ("材料待补说明", "尚未提供营业执照"),
    ("安全生产许可证", "营业执照样本链接"),
])
def test_false_keyword_hit_never_reaches_attachment_output(tmp_path, title, description, organize):
    output, ctx = proof_case(tmp_path, "须提供营业执照。", title, description=description, organize=organize)
    assert output["attachments"] == []
    assert not ctx.captures["n3-material-matching"]["assembly_candidates"]
    assert any("未能唯一匹配" in issue["title"] for issue in output["issues"])


@pytest.mark.parametrize("organize", [False, True])
def test_negative_instruction_is_not_a_missing_material(tmp_path, organize):
    output, ctx = proof_case(tmp_path, "无需提供营业执照。", "营业执照", organize=organize)
    assert output["attachments"] == []
    assert not any("匹配" in issue["title"] or "内容待补" in issue["title"] for issue in output["issues"])
    assert ctx.captures["n3-planning-report"]["attachments"][0]["status"] == "not_required"


@pytest.mark.parametrize("organize", [False, True])
@pytest.mark.parametrize("include_safety", [False, True])
def test_complementary_and_proofs_are_not_competing_bundles(tmp_path, organize, include_safety):
    manifest, parsed = fixture(tmp_path)
    block = add_unit(parsed, "U1", "须提供营业执照及安许证。")
    titles = ["营业执照", "安全生产许可证"] if include_safety else ["营业执照"]
    manifest["attachments"] = [{"attachment_id": f"A{i}", "section_id": "unassigned", "title": title,
        "description": "合成证明包", "path": str(tmp_path / f"proof-{i}.png"), "sha256": "c" * 64,
        "source_refs": ["company"], "requirement_refs": ["unassigned"], "synthetic": True}
        for i, title in enumerate(titles)]
    ctx = Context()
    extracted = extract(ctx, parsed, manifest, lambda p: response(p, [requirement(block,
                        category="qualification", mode="attachment")]))
    output = plan_bid(ctx, extracted, parsed, manifest, no_call, {"organize_response": organize})
    assert {a["attachment_id"] for a in output["attachments"]} == {f"A{i}" for i in range(len(titles))}
    query = ctx.captures["n3-material-matching"]["proof_queries"][0]
    if include_safety:
        assert not query["missing_required_labels"]
        assert not any("未能唯一匹配" in issue["title"] for issue in output["issues"])
    else:
        assert "安全生产许可证" in query["missing_required_labels"]
        assert any("安全生产许可证" in issue["reason"] and issue["severity"] == "missing" for issue in output["issues"])


@pytest.mark.parametrize("organize", [False, True])
@pytest.mark.parametrize("damage,reason", [
    ("incomplete", "页码或页数不完整"),
    ("unbound", "未绑定完整冻结来源"),
])
def test_and_proof_with_unusable_candidate_keeps_specific_missing_issue(tmp_path, organize, damage, reason):
    manifest, parsed = fixture(tmp_path)
    block = add_unit(parsed, "U1", "须提供营业执照及安许证。")
    license_page = {"attachment_id": "BUS", "section_id": "unassigned", "title": "营业执照",
        "description": "合成证明包", "path": str(tmp_path / "license.png"), "sha256": "c" * 64,
        "source_refs": ["company"], "requirement_refs": ["unassigned"], "synthetic": True}
    safety_page = {**license_page, "attachment_id": "SAFE", "title": "安全生产许可证",
        "path": str(tmp_path / "safety.png"), "bundle_id": "SAFETY", "page_index": 1, "page_count": 1}
    if damage == "incomplete":
        safety_page["page_count"] = 2
    else:
        safety_page["source_refs"] = ["UNKNOWN_SOURCE"]
    manifest["attachments"] = [license_page, safety_page]
    ctx = Context()
    extracted = extract(ctx, parsed, manifest, lambda p: response(p, [requirement(block,
                        category="qualification", mode="attachment")]))
    output = plan_bid(ctx, extracted, parsed, manifest, no_call, {"organize_response": organize})
    assert [a["attachment_id"] for a in output["attachments"]] == ["BUS"]
    query, = ctx.captures["n3-material-matching"]["proof_queries"]
    assert query["assembly_selection"]["selection_status"] == "partial"
    # A found but unusable proof is distinct from no candidate in the library.
    assert query["missing_required_labels"] == []
    assert query["unavailable_required_labels"] == ["安全生产许可证"]
    assert ctx.captures["n3-planning-report"]["attachments"][0]["status"] == "partial"
    issue, = [i for i in output["issues"] if i["title"] == "本条证明材料尚未配齐"]
    assert issue["severity"] == "missing"
    assert "安全生产许可证" in issue["reason"] and reason in issue["reason"]


def test_match_query_checkpoint_survives_failure_and_process_restart(tmp_path, monkeypatch):
    import company_bid.material_matching as matching
    manifest, parsed = fixture(tmp_path)
    queries = [{"requirement_id": "R1", "response_mode": "attachment", "text": "须提供安许证。"},
               {"requirement_id": "R2", "response_mode": "attachment", "text": "须提供高企证书。"}]
    original = matching.material_candidate_report
    calls = []
    fail_second = True

    def flaky(requirements, *args, **kwargs):
        rid = requirements[0]["requirement_id"] if requirements else "index"
        calls.append(rid)
        if rid == "R2" and fail_second:
            raise RuntimeError("SYNTHETIC_TRANSIENT")
        return original(requirements, *args, **kwargs)

    monkeypatch.setattr(matching, "material_candidate_report", flaky)
    def handler(ctx, value):
        return _material_match_report(ctx, value["requirements"], manifest, parsed)
    nodes = [NodeSpec("N3", "matching-test-v1", handler)]
    first = Executor(tmp_path / "runtime", nodes, {})
    run = first.create_run({"requirements": queries})
    assert first.execute(run)["status"] == "failed"
    first_state = first.inspect(run)
    successful = {key: unit["output_hash"] for key, unit in first_state["nodes"][0]["units"].items()
                  if unit["status"] == "succeeded"}
    fail_second = False
    restarted = Executor(tmp_path / "runtime", nodes, {})
    resumed = restarted.resume(run)
    assert resumed["status"] == "succeeded"
    assert calls == ["index", "R1", "R2", "R2"]
    units = resumed["nodes"][0]["units"]
    assert all(units[key]["output_hash"] == value for key, value in successful.items())
    assert sorted(len(unit["attempts"]) for unit in units.values()) == [1, 1, 2]
    events = (tmp_path / "runtime" / "runs" / run / "events.jsonl").read_text(encoding="utf-8")
    assert "安许证" not in events and "高企证书" not in events
    assert any(json.loads(line)["event"] == "unit.checkpointed" for line in events.splitlines())
