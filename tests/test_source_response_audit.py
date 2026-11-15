from copy import deepcopy
import hashlib

import pytest

from company_bid.source_response_audit import build_source_response_audit


def fixture(text="必须巡检；每月提交报告。", quote=None, *, table=False):
    quote = text if quote is None else quote
    source = {"source_id": "T", "kind": "tender", "sha256": "a" * 64}
    block = {"source_id": "T", "block_id": "B", "locator": "pdf/page[1]/line[0]",
             "kind": "paragraph", "text": text, "origin": "native"}
    if table:
        block.update(kind="table", rows=[["名称", "要求"], ["巡检", ""], ["报告", "提交月报"]],
                     merges=[], column_widths=[1, 1], header_rows=1, locator="pdf/page[1]/table[0]")
        text = "名称\t要求\n巡检\t\n报告\t提交月报"
        quote = "提交月报"
    unit = {"source_id": "T", "unit_id": "U", "page": 1, "locator": "pdf/page[1]", "blocks": [block],
            "coverage": {"status": "read"}}
    parsed = {"documents": [{"source_id": "T", "role": "tender", "sha256": source["sha256"],
                             "format": "pdf", "units": [unit]}]}
    manifest = {"sources": [source], "source_roles": {"T": "tender"}}
    scope = {**source, "source_id": "S", "locator": block["locator"] +
             f"; basis={'table_rows_tsv_v1' if table else 'native_block_text_v1'}; chars[{text.index(quote)}:{text.index(quote)+len(quote)}]; quote_sha256=" +
             hashlib.sha256(quote.encode()).hexdigest()}
    package = {"sources": [source, scope], "requirements": [{"requirement_id": "R", "text": quote, "source_refs": ["S"]}],
               "sections": [{"section_id": "SEC", "blocks": [{"kind": "heading", "text": "施工", "requirement_refs": ["R"]}]}],
               "issues": [], "review_actions": []}
    return parsed, manifest, package


def test_exact_quote_is_not_semantic_acceptance_and_heading_is_not_response():
    args = fixture()
    before = deepcopy(args)
    report = build_source_response_audit(*args)
    assert report["quotation_counts"] == {"fully_quoted": 1}
    assert report["semantic_completeness"] == "not_determined"
    assert report["without_destination_count"] == 1
    assert report["requirements"][0]["satisfaction_status"] == "not_determined"
    assert args == before


def test_partial_quote_leaves_independent_unquoted_obligation_visible():
    report = build_source_response_audit(*fixture(quote="必须巡检"))
    unit = report["source_units"][0]
    assert unit["quotation_status"] == "partly_quoted"
    assert "".join(r["text"] for r in unit["unquoted_ranges"]) == "；每月提交报告。"


def test_source_denominator_includes_every_table_cell_and_blank_is_not_read_failure():
    report = build_source_response_audit(*fixture(table=True))
    assert report["source_unit_count"] == 6
    assert report["quotation_counts"] == {"not_quoted": 4, "source_blank": 1, "fully_quoted": 1}
    quoted, = [row for row in report["source_units"] if row["quotation_status"] == "fully_quoted"]
    assert (quoted["row"], quoted["column"]) == (2, 1)


def test_actual_response_and_pending_action_stay_separate_with_no_compliance_claim():
    parsed, manifest, package = fixture()
    package["sections"][0]["blocks"].append({"kind": "paragraph", "text": "拟组织巡检。", "requirement_refs": ["R"]})
    package["review_actions"].append({"action_id": "A", "requirement_refs": ["R"], "status": "pending_manual"})
    report = build_source_response_audit(parsed, manifest, package)
    row = report["requirements"][0]
    assert [d["kind"] for d in row["destinations"]] == ["paragraph", "pending_action"]
    assert row["destination_status"] == "linked_for_review" and row["semantic_coverage"] == "not_determined"


def test_original_chapter_title_has_structural_destination_not_substantive_response():
    parsed, manifest, package = fixture("8 技术服务方案")
    package["sections"][0].update(title="8 技术服务方案", requirement_refs=["R"])
    report = build_source_response_audit(parsed, manifest, package)
    row = report["requirements"][0]
    assert row["exact_heading_section_ids"] == ["SEC"]
    assert row["destination_status"] == "heading_only_requires_scope_review"
    assert row["destinations"] == [] and report["without_destination_count"] == 0


def test_parent_context_is_not_counted_as_quoted_child_requirement():
    parsed, manifest, package = fixture(quote="必须巡检")
    source = deepcopy(package["sources"][1])
    source["source_id"] = "CTX"
    source["locator"] = source["locator"].replace("chars[0:4]", "chars[5:12]").replace(
        hashlib.sha256("必须巡检".encode()).hexdigest(), hashlib.sha256("每月提交报告。".encode()).hexdigest())
    package["sources"].append(source)
    package["requirements"][0]["source_contexts"] = [{"text": "每月提交报告。", "source_refs": ["CTX"]}]
    unit = build_source_response_audit(parsed, manifest, package)["source_units"][0]
    assert unit["quotation_status"] == "partly_quoted"
    assert len(unit["context_links"]) == 1 and unit["unquoted_ranges"]


@pytest.mark.parametrize("tamper", ["quote", "hash", "range", "source", "locator", "ids"])
def test_changed_source_binding_is_rejected(tamper):
    parsed, manifest, package = fixture()
    scope = package["sources"][1]
    if tamper == "quote": package["requirements"][0]["text"] = "已经完成巡检。"
    if tamper == "hash": scope["sha256"] = "b" * 64
    if tamper == "range": scope["locator"] = scope["locator"].replace("chars[0:", "chars[1:")
    if tamper == "source": package["requirements"][0]["source_refs"] = ["UNKNOWN"]
    if tamper == "locator": scope["locator"] = scope["locator"].replace("line[0]", "line[1]")
    if tamper == "ids": scope["locator"] += "; blocks=B,B"
    with pytest.raises(ValueError, match="SOURCE_AUDIT_"):
        build_source_response_audit(parsed, manifest, package)


def test_ignored_source_is_reported_even_when_quoted():
    parsed, manifest, package = fixture()
    unit = parsed["documents"][0]["units"][0]
    unit["coverage"]["ignored_objects"] = [{"locator": unit["blocks"][0]["locator"], "reason": "unreadable glyph"}]
    report = build_source_response_audit(parsed, manifest, package)
    assert report["quotation_counts"] == {"reader_ignored_requires_review": 1}


def test_read_failure_is_not_a_source_blank_or_covered_requirement():
    parsed, manifest, package = fixture(table=True)
    parsed["documents"][0]["units"][0]["coverage"].update(status="failed", error_code="READ_FAILED")
    report = build_source_response_audit(parsed, manifest, package)
    assert report["quotation_counts"] == {"reading_incomplete": 6}


@pytest.mark.parametrize("status,expected", [("failed", "reading_incomplete"), ("empty", "source_empty_page"),
                                            ("read", "read_without_blocks_requires_review")])
def test_pages_without_blocks_remain_in_reading_denominator(status, expected):
    parsed, manifest, package = fixture()
    document = parsed["documents"][0]
    document["expected_units"] = 2
    document["units"].append({"source_id": "T", "unit_id": "U2", "locator": "pdf/page[2]", "page": 2,
                              "blocks": [], "coverage": {"status": status}})
    report = build_source_response_audit(parsed, manifest, package)
    assert report["reading_unit_count"] == 2 and report["reading_counts"][expected] == 1
    assert report["reading_units"][1]["page"] == 2 and report["reading_denominator_status"] == "matched"


def test_missing_reading_units_are_explicit_not_a_complete_denominator():
    parsed, manifest, package = fixture()
    parsed["documents"][0]["expected_units"] = 2
    report = build_source_response_audit(parsed, manifest, package)
    assert report["reading_denominator_status"] == "requires_review"
    assert report["reading_documents"][0]["denominator_status"] == "mismatch"
