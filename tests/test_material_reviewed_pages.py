from copy import deepcopy

import pytest

from company_bid.evidence import EvidenceSelection
from company_bid.material_recall import annotate_material_issues
from company_bid.material_reviewed_pages import ReviewedProofPage, bind_reviewed_pages
from company_bid.provider import ProviderFailure
from company_bid.storage import digest


@pytest.fixture
def reviewed_personnel():
    source = {"source_id": "S-company", "sha256": "a" * 64, "kind": "company_material"}
    selection = EvidenceSelection(selection_id="personnel-pack", source_id=source["source_id"],
        source_sha256=source["sha256"], title="Personnel proofs", selection_reason="Preselected proof images",
        format="docx_images", package_parts=["word/media/card.png", "word/media/social.png"])
    group_id = selection.selection_id
    target, sibling = selection.package_parts[1], selection.package_parts[0]
    attachment_ids = ["PROOF-" + digest([group_id, f"docx/{part}"])[:24] for part in [sibling, target]]
    attachments = [{"attachment_id": aid, "sha256": char * 64}
                   for aid, char in zip(attachment_ids, "bc")]
    origin = {"selection_id": group_id}
    group = {"group_id": group_id, "origin": origin, "attachment_ids": attachment_ids, "source_bound": True}
    units = [{"source_id": source["source_id"], "source_sha256": source["sha256"],
              "unit_id": part.split("/")[-1], "format": selection.format, "target": part,
              "ocr_image_sha256": attachment["sha256"], "group_ids": [group_id],
              "transcription_status": "ocr_failed_pending_review" if part == target else "ocr_no_text_unresolved"}
             for part, attachment in zip([sibling, target], attachments)]
    requirement = "项目负责人应有截止日前的社会保险证明。"
    query = {"requirement_id": "R-social", "request": {"kinds": ["social_insurance_proof"], "original_text": requirement},
             "search_status": "no_candidate", "candidate_group_ids": []}
    report = {"proof_queries": [query], "assembly_candidates": [],
              "proof_groups": [{**group, "role_exclusions": [], "proof_kind": "personnel_proof_package", "title": selection.title}],
              "original_content_index": {"groups": [{**group, "units": []}], "units": units,
                                         "coverage": {"selected_unit_count": 2, "status_counts": {}, "unread_unit_count": 2}}}
    manifest = {"sources": [source], "attachments": attachments, "evidence_selections": [selection.model_dump(mode="json")]}
    config = dict(requirement_id="R-social", requirement_text_sha256=digest(requirement),
        proof_kind="social_insurance_proof", source_id=source["source_id"], source_sha256=source["sha256"],
        selection_sha256=digest(selection.model_dump(mode="json")), image_sha256=attachments[1]["sha256"],
        content_unit_id="social.png", attachment_id=attachment_ids[1], review_id="checked-image",
        evidence_sha256="d" * 64)
    return manifest, report, config


def test_candidate_does_not_promote_personnel_group_or_change_original_issue(reviewed_personnel):
    manifest, report, config = reviewed_personnel
    before = deepcopy(report)
    candidates = bind_reviewed_pages(manifest, report, [ReviewedProofPage.model_validate(config)])
    candidate, = candidates
    assert report == before
    assert candidate["locator"] == "docx/word/media/social.png"
    assert candidate["transcription_status"] == "ocr_failed_pending_review"
    assert candidate["qualification_result"] == "not_determined"
    assert not candidate["automatic_assembly_allowed"] and not candidate["automatic_fact_adoption"]
    report["proof_queries"][0]["reviewed_page_candidates"] = candidates
    social = {"issue_id": "M-1", "requirement_refs": ["R-social"], "reason": "仍需缴费凭据。", "severity": "missing"}
    other = {"issue_id": "M-2", "requirement_refs": ["R-other"], "reason": "另一要求。", "severity": "missing"}
    change, = annotate_material_issues([social, other], report)
    assert change["before"]["reason"] == "仍需缴费凭据。"
    assert change["requirement_contexts"][0]["reviewed_page_candidates"] == candidates
    assert "social.png" in social["reason"] and "实际缴费凭据" in social["reason"]
    assert "card.png" not in social["reason"] and social["severity"] == "missing"
    assert other["reason"] == "另一要求。"


@pytest.mark.parametrize("changes, error", [
    ({"requirement_id": "R-other"}, "REQUIREMENT_MISMATCH"),
    ({"requirement_text_sha256": "e" * 64}, "REQUIREMENT_MISMATCH"),
    ({"proof_kind": "environment_management_certificate"}, "REQUIREMENT_MISMATCH"),
    ({"source_sha256": "e" * 64}, "SOURCE_MISMATCH"),
    ({"selection_sha256": "e" * 64}, "SELECTION_MISMATCH"),
    ({"image_sha256": "b" * 64}, "SOURCE_MISMATCH"),
])
def test_reject_stale_binding(reviewed_personnel, changes, error):
    manifest, report, config = reviewed_personnel
    with pytest.raises(ProviderFailure, match="MATERIAL_REVIEWED_PAGE_" + error):
        bind_reviewed_pages(manifest, report, [ReviewedProofPage.model_validate({**config, **changes})])


def test_identical_sibling_image_cannot_substitute_attachment(reviewed_personnel):
    manifest, report, config = reviewed_personnel
    sibling = manifest["attachments"][0]
    sibling["sha256"] = config["image_sha256"]
    attempted = {**config, "attachment_id": sibling["attachment_id"]}
    with pytest.raises(ProviderFailure, match="MATERIAL_REVIEWED_PAGE_SELECTION_MISMATCH"):
        bind_reviewed_pages(manifest, report, [ReviewedProofPage.model_validate(attempted)])
