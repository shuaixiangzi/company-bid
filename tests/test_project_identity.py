from copy import deepcopy

import pytest

from company_bid.extraction import PlanningError
from company_bid.project_identity import derive_project_number


def fixture(lines=("项目编号：TEST-2026-89", "项目编号：TEST-2026-89")):
    source = {"source_id": "T", "kind": "tender", "sha256": "a" * 64}
    blocks = [{"block_id": f"B{i}", "kind": "paragraph", "origin": "native", "text": text,
               "locator": f"pdf/page[{i+1}]/line[0]"} for i, text in enumerate(lines)]
    parsed = {"documents": [{"source_id": "T", "role": "tender", "sha256": source["sha256"],
        "units": [{"blocks": [block]} for block in blocks]}]}
    return parsed, {"sources": [source]}


def test_exact_tender_number_binds_every_matching_original_without_changing_input():
    parsed, manifest = fixture()
    before = deepcopy(parsed)
    citations = []
    def scoped(citation):
        citations.append(citation)
        return "SCOPE-" + citation["block_id"]
    facts, report = derive_project_number(parsed, manifest, [], scoped)
    assert len(facts) == 1 and facts[0]["value"] == "TEST-2026-89"
    assert facts[0]["source_refs"] == ["SCOPE-B0", "SCOPE-B1"]
    assert [item["quote"] for item in citations] == ["项目编号：TEST-2026-89"] * 2
    assert report["status"] == "source_proven" and parsed == before


@pytest.mark.parametrize("text", ["项目编号：", "项目编号：________", "项目编号：待填写", "合同编号：TEST-89",
    "示例项目编号：TEST-89", "项目编号：TEST-89（填写说明）", "项目编号：TEST", "（项目编号：TEST-89)"])
def test_no_inference_from_blanks_examples_other_identifiers_or_instructions(text):
    parsed, manifest = fixture((text,))
    facts, report = derive_project_number(parsed, manifest, [], lambda _: pytest.fail("unexpected source adoption"))
    assert facts == [] and report["status"] == "not_found"


@pytest.mark.parametrize("wrapped", ["（项目编号：TEST-2026-89）", "(项目编号：TEST-2026-89)"])
def test_number_inside_complete_printed_parentheses_keeps_original_quote(wrapped):
    parsed, manifest = fixture((wrapped,))
    seen = []
    def scoped(citation):
        seen.append(citation)
        return "SOURCE"
    facts, _ = derive_project_number(parsed, manifest, [], scoped)
    assert facts[0]["value"] == "TEST-2026-89" and seen[0]["quote"] == wrapped


@pytest.mark.parametrize("change", ["reference_document", "company_source", "ocr", "omitted"])
def test_number_from_non_tender_or_unread_scope_is_not_adopted(change):
    parsed, manifest = fixture(("项目编号：OLD-2025-1",))
    document = parsed["documents"][0]
    if change == "reference_document": document["role"] = "reference_bid"
    if change == "company_source": manifest["sources"][0]["kind"] = "company"
    if change == "ocr": document["units"][0]["blocks"][0]["origin"] = "ocr"
    if change == "omitted": document["units"][0]["blocks"][0]["read_disposition"] = "failed"
    assert derive_project_number(parsed, manifest, [], lambda _: "unused")[0] == []


@pytest.mark.parametrize("conflict", ["source", "fact", "source_hash"])
def test_conflicts_stop_instead_of_picking_first_number(conflict):
    parsed, manifest = fixture()
    facts = []
    if conflict == "source": parsed["documents"][0]["units"][1]["blocks"][0]["text"] = "项目编号：OTHER-90"
    if conflict == "fact": facts = [{"label": "项目编号", "value": "OTHER-90"}]
    if conflict == "source_hash": parsed["documents"][0]["sha256"] = "b" * 64
    with pytest.raises(PlanningError, match="PROJECT_NUMBER_"):
        derive_project_number(parsed, manifest, facts, lambda _: pytest.fail("conflict must precede source adoption"))
