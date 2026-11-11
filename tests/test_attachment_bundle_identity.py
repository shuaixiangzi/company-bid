"""Proof grouping through native reading, extraction and the real planner."""
from docx import Document
from PIL import Image
import pytest

from company_bid.intake import IntakeManifest
from company_bid.models import file_sha256
from company_bid.planning import extract_requirements, plan_bid
from company_bid.reading import read_documents


class Context:
    def __init__(self):
        self.captures = {}

    def run_unit(self, unit_id, input_data, fn):
        return fn()

    def capture(self, name, payload, **kwargs):
        self.captures[name] = payload
        return {"status": "captured"}


def prepare(tmp_path, candidates):
    tender = tmp_path / "tender.docx"
    document = Document(); document.add_paragraph("Provide LICENSE copy."); document.save(tender)
    proof = tmp_path / "source-proof.png"; Image.new("RGB", (90, 120), "red").save(proof)
    def source(identity, path, kind):
        return {"source_id": identity, "title": "Synthetic", "path": str(path), "sha256": file_sha256(path),
                "locator": "document", "kind": kind, "provenance": "synthetic", "synthetic": True}
    attachments = [{"attachment_id": identity, "section_id": "unassigned", "title": title,
        "description": title, "path": str(proof), "sha256": file_sha256(proof), "source_refs": ["P"],
        "requirement_refs": ["unassigned"], "synthetic": True, **bundle}
        for identity, title, bundle in candidates]
    manifest = IntakeManifest.model_validate({"project": {"project_id": "TEST", "name": "Synthetic", "source_refs": ["T"]},
        "company": {"company_id": "FIXED", "name": "Synthetic", "source_refs": ["P"]},
        "sources": [source("T", tender, "tender"), source("P", proof, "company_material")],
        "source_roles": {"T": "tender", "P": "provenance_only"}, "attachments": attachments,
        "synthetic": True, "preparation_note": "Synthetic grouping test, not a real qualification."}).model_dump(mode="json")
    ctx = Context()
    parsed = read_documents(ctx, [{**manifest["sources"][0], "role": "tender"}], {})
    def extract(ctx, task_type, unit_id, payload, validate):
        block = payload["window"]["blocks"][0]
        return validate({"reviewed_block_ids": [b["block_id"] for b in payload["window"]["blocks"]],
            "requirements": [{"citation": {key: block[key] for key in ("source_id", "block_id", "locator")} | {"quote": block["text"]},
                              "category": "qualification", "response_mode": "attachment", "format_status": "not_prescribed",
                              "attachment_keywords": ["LICENSE"]}], "format_chapters": [], "format_status": "none_verified"})
    extracted = extract_requirements(ctx, parsed, manifest, extract, {})
    def no_mapping(*args):
        pytest.fail("Attachment grouping cannot invoke a model")
    output = plan_bid(ctx, extracted, parsed, manifest, no_mapping, {})
    return output, ctx.captures["n3-planning-report"]["attachments"][0]


def bundle(identity="B", title="LICENSE copy"):
    return [(f"{identity}-P{page}", title, {"bundle_id": identity, "page_index": page, "page_count": 2}) for page in (2, 1)]


def test_single_attachment_identity_equal_to_bundle_identity_is_ambiguous(tmp_path):
    output, report = prepare(tmp_path, [("B", "LICENSE copy", {}), *bundle()])
    assert output["attachments"] == [] and len(output["issues"]) == 1
    assert report["status"] == "ambiguous"
    assert report["candidate_groups"] == [{"kind": "bundle", "identity": "B"}, {"kind": "single", "identity": "B"}]


@pytest.mark.parametrize("matched_kind", ["single", "bundle"])
def test_selected_group_does_not_pull_unmatched_same_string_group(tmp_path, matched_kind):
    single_title = "LICENSE copy" if matched_kind == "single" else "OTHER proof"
    bundle_title = "LICENSE copy" if matched_kind == "bundle" else "OTHER proof"
    output, report = prepare(tmp_path, [("B", single_title, {}), *bundle(title=bundle_title)])
    assert not output["issues"] and report["status"] == "matched"
    expected = ["B"] if matched_kind == "single" else ["B-P1", "B-P2"]
    assert [item["attachment_id"] for item in output["attachments"]] == expected
    assert report["candidate_groups"] == [{"kind": matched_kind, "identity": "B"}]


def test_two_matching_bundles_remain_ambiguous_without_arbitrary_selection(tmp_path):
    output, report = prepare(tmp_path, [*bundle("B"), *bundle("C")])
    assert output["attachments"] == [] and len(output["issues"]) == 1
    assert report["status"] == "ambiguous"
    assert report["candidate_groups"] == [{"kind": "bundle", "identity": "B"}, {"kind": "bundle", "identity": "C"}]
