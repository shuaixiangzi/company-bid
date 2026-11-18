import copy
import hashlib

import pytest

from company_bid.extraction import PlanningError, extract_requirements
from company_bid.planning import plan_bid
from company_bid.storage import digest
from test_source_selected_citations import Context


LINES = [
    ("A", "2.3.1 响应文件按指定渠道提交。"),
    ("B", "2.3.2 应核对加密状态。"),
    ("H1", "2.3.3 响应文件应加盖数字印章，"),
    ("H2", "法定代表人（负"),
    ("T1", "责人）签署时应提供身份证明，"),
    ("T2", "代理人签署时应附授权委托书。"),
    ("C", "2.3.4 以平台上传的文件为准。"),
]


def make_source(change):
    lines = dict(LINES)
    if change == "balanced":
        lines["H2"] = "法定代表人（负责人）签署时，"
        lines["T1"] = "应提供身份证明，"
    if change == "sibling":
        lines["C"] = "2.3.5 以平台上传的文件为准。"
    if change == "new_clause":
        lines["T1"] = "2.3.4 责人）签署时应提供身份证明，"
    source_hash = hashlib.sha256("\n".join(lines.values()).encode()).hexdigest()
    manifest = {"sources": [{"source_id": "T", "sha256": source_hash}], "source_roles": {"T": "tender"}}
    parsed = {"coverage": {"required_complete": True}, "documents": [{
        "source_id": "T", "sha256": source_hash, "role": "tender", "format": "pdf",
        "units": [{"unit_id": "U", "source_id": "T", "locator": "pdf/page[1]", "page": 1,
                   "coverage": {"status": "read", "image_count": 0},
                   "blocks": [{"block_id": key, "source_id": "T", "kind": "paragraph",
                               "origin": "ocr" if change == "non_native" and key == "T1" else "native",
                               "locator": f"pdf/page[1]/line[{n}]", "text": text}
                              for n, (key, text) in enumerate(lines.items())]}]}]}
    return parsed, manifest


def extract(change=None):
    parsed, manifest = make_source(change)

    def call(ctx, task, unit, request, validate):
        def item(ids, mode, attachments):
            return {"citation": {"block_ids": ids}, "category": "procedure", "response_mode": mode,
                    "mandatory": True, "confidence": "high", "format_status": "not_prescribed",
                    "format_title": None, "required_fact_labels": [], "attachment_keywords": attachments,
                    "reference_keywords": []}

        head = item(["H1", "H2"], "procedure", [])
        tail_ids = ["T1", "T2"] if change == "no_overlap" else ["H2", "T1", "T2"]
        tail = item(tail_ids, "attachment", ["身份证明", "授权委托书"])
        return validate({"window_id": unit, "format_status": "none_verified", "format_chapters": [],
                         "processing_declaration": "processed_not_semantically_accepted",
                         "unresolved_block_ids": [], "requirements": [head, tail]})

    ctx = Context()
    return extract_requirements(ctx, parsed, manifest, call, {}), parsed, manifest, ctx


def test_repairs_only_merged_procedure_and_proves_retained_attachment():
    result, _, _, ctx = extract()
    proof, = result["source_clause_repairs"]
    procedure, attachment = result["requirements"]
    window, = result["window_results"]
    assert proof["rule"] == "native_numbered_procedure_with_selected_attachment_tail_v1"
    assert proof["clause_number"] == "2.3.3"
    assert proof["added_block_ids"] == ["T1", "T2"]
    assert proof["sibling_block_ids"] == ["B", "C"]
    assert proof["original"]["block_ids"] == ["H1", "H2"]
    assert proof["restored"]["block_ids"] == ["H1", "H2", "T1", "T2"]
    assert proof["supporting_attachment"]["block_ids"] == ["H2", "T1", "T2"]
    assert proof["restored"]["requirement_id"] == procedure["requirement_id"]
    assert proof["supporting_attachment"]["requirement_id"] == attachment["requirement_id"]
    assert proof["original"]["requirement_id"] != procedure["requirement_id"]
    assert proof["window_result_sha256"] == digest(window)
    assert proof["proof_sha256"] == digest({k: v for k, v in proof.items() if k != "proof_sha256"})
    assert ctx.captures["n2-source-clause-repairs"]["items"] == [proof]
    assert procedure["text"] == "\n".join(text for _, text in LINES[2:6])
    assert attachment["citations"] == [window["validated"]["requirements"][1]["citation"]]
    assert window["raw_model_candidate"]["requirements"][0]["citation"]["block_ids"] == ["H1", "H2"]
    assert window["validated"]["requirements"][0]["citation"]["quote"].endswith("（负")


@pytest.mark.parametrize("change", ["balanced", "sibling", "non_native", "new_clause", "no_overlap"])
def test_requires_exact_same_page_native_attachment_overlap_and_sibling_bounds(change):
    result, _, _, _ = extract(change)
    assert "source_clause_repairs" not in result
    assert result["requirements"][0]["text"] == result["window_results"][0]["validated"]["requirements"][0]["citation"]["quote"]


def test_n3_rejects_changed_source_repair_proof_before_business_matching():
    result, parsed, manifest, _ = extract()
    altered = copy.deepcopy(result)
    altered["source_clause_repairs"][0]["supporting_attachment"]["block_ids"] = ["B"]
    with pytest.raises(PlanningError, match="EXTRACTION_SOURCE_CLAUSE_REPAIRS_MISMATCH"):
        plan_bid(Context(), altered, parsed, manifest, None, {})
