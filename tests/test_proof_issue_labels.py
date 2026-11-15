"""Human proof labels follow explicit attachment edges, not shared requirements."""
from copy import deepcopy

import pytest

from company_bid.extraction import _id
from company_bid.response_organization import organize_response, _proof_display_labels


def fixture():
    requirement = {"requirement_id": "R", "text": "提供资格证明材料。", "category": "qualification",
                   "response_mode": "attachment", "source_refs": ["T"]}
    family = {"section_id": _id("SEC-", "F"), "title": "7 资格审查材料", "kind": "facts",
        "requirement_refs": ["R"], "source_refs": ["T"], "fact_refs": [], "table_refs": [], "attachment_refs": ["STRAY"],
        "writing_tasks": [], "fixed_content": [{"kind": "paragraph", "text": "原格式说明。", "source_refs": ["T"]}],
        "nonwriting_requirement_refs": ["R"], "level": 1, "start_on_new_page": False, "render_body": True}
    formats = [{"format_id": "F", "title": family["title"],
                "source_derivation": {"kind": "native_material_family_prefix", "sibling_block_ids": []}}]
    supplied = [{"attachment_id": aid, "bundle_id": bundle, "title": title, "source_refs": [source],
                 "sha256": str(index) * 64} for index, (aid, bundle, title, source) in enumerate([
        ("CERT-1", "CERT", "合成高新技术证书", "CERT-SOURCE"),
        ("CERT-2", "CERT", "合成高新技术证书", "CERT-SOURCE"),
        ("QUALITY", "QUALITY", "合成质量管理证书", "QUALITY-SOURCE"),
        ("STRAY", "STRAY", "未被此装配选中的同R材料", "OTHER-SOURCE")], 1)]
    attachments = [{**supplied[-1], "section_id": family["section_id"], "requirement_refs": ["R"]}]
    issues = [{"issue_id": "M-001", "section_id": family["section_id"], "requirement_refs": ["R"],
        "title": "所需证明附件未能唯一匹配", "reason": "旧检索未匹配。", "action": "补充。", "severity": "missing",
        "source_requirement": requirement["text"]}]
    candidates = [{"requirement_id": "R", "group_id": "CERT", "attachment_ids": ["CERT-1", "CERT-2"],
        "title": "不应覆盖实际附件名的候选标题", "pending_checks": ["核对证书有效期。"]},
        {"requirement_id": "R", "group_id": "QUALITY", "attachment_ids": ["QUALITY"], "pending_checks": ["核对适用范围。"]}]
    return [family], [requirement], formats, issues, attachments, {"attachments": supplied}, {"assembly_candidates": candidates}


def organize(data):
    sections, requirements, formats, issues, attachments, manifest, material_report = data
    return organize_response(sections, requirements, {r["requirement_id"]: r for r in requirements},
                             formats, {}, issues, attachments, manifest, material_report)


def test_each_shared_requirement_marker_names_only_its_actual_bundle_and_keeps_label_origin():
    data = fixture()
    supplied_before = deepcopy(data[5]);candidates_before = deepcopy(data[6])
    report = organize(data)
    issues = {i["issue_id"]: i for i in data[3]}
    first, second = report["proof_assembly"]
    assert issues[first["issue_id"]]["title"] == issues[second["issue_id"]]["title"] == "已附证明的适用条件仍待核对"
    assert "合成高新技术证书" in issues[first["issue_id"]]["reason"]
    assert "合成质量管理证书" not in issues[first["issue_id"]]["reason"]
    assert "合成质量管理证书" in issues[second["issue_id"]]["reason"]
    assert "合成高新技术证书" not in issues[second["issue_id"]]["reason"]
    assert first["display_labels"] == ["合成高新技术证书"]
    assert first["display_label_provenance"] == [{"title": "合成高新技术证书", "bundle_id": "CERT",
        "attachment_ids": ["CERT-1", "CERT-2"], "source_refs": ["CERT-SOURCE"], "label_source": "attachment.title"}]
    assert all("不应覆盖" not in item["reason"] and "未被此装配选中" not in item["reason"] for item in data[3])
    assert len(data[4]) == 4 and {a["attachment_id"] for a in data[4]} == {"CERT-1", "CERT-2", "QUALITY", "STRAY"}
    assert data[5] == supplied_before and data[6] == candidates_before
    assert all(i["severity"] == "warning" for i in data[3])
    assert report["satisfaction_status"] == "not_determined"


def test_lookup_reclassification_labels_use_only_its_explicit_assembled_attachment_ids():
    data = fixture();report = organize(data)
    row, = report["lookup_issue_reclassifications"]
    assert row["issue_id"] == "M-001" and row["attachment_ids"] == ["CERT-1", "CERT-2", "QUALITY"]
    assert row["display_labels"] == ["合成高新技术证书", "合成质量管理证书"]
    assert row["current"]["reason"].count("合成高新技术证书") == 1
    assert "合成质量管理证书" in row["current"]["reason"]
    assert "未被此装配选中" not in row["current"]["reason"]
    assert [o["attachment_ids"] for o in row["display_label_provenance"]] == [["CERT-1", "CERT-2"], ["QUALITY"]]


@pytest.mark.parametrize("field", ["title", "basis"])
def test_missing_attachment_title_can_use_only_the_same_explicit_candidate_label(field):
    actual = {"A": {"attachment_id": "A", "path": "do-not-derive-person-from-filename.png", "source_refs": ["S"]}}
    candidate = {"group_id": "G", "source_refs": ["C"], **({"title": "明确候选名"} if field == "title" else {"basis": {"maintained_title": "明确候选名"}})}
    labels, origins = _proof_display_labels(["A"], actual, candidate)
    assert labels == ["明确候选名"] and origins[0]["attachment_ids"] == ["A"]
    assert origins[0]["label_source"] == "assembly_candidate." + ("title" if field == "title" else "basis.maintained_title")
    assert _proof_display_labels(["A"], actual) == ([], [])


def test_partial_lookup_coverage_does_not_gain_labels_or_reclassify_from_shared_R_material():
    data = fixture()
    data[3][0]["requirement_refs"] = ["R", "UNASSEMBLED"]
    report = organize(data)
    assert report["lookup_issue_reclassifications"] == []
    assert data[3][0]["title"] == "所需证明附件未能唯一匹配"
    assert data[3][0]["reason"] == "旧检索未匹配。" and data[3][0]["severity"] == "missing"
