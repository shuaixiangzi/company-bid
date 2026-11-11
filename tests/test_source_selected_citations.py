"""Source-selection wire protocol; synthetic sources and zero remote calls."""
import copy
import hashlib
import json

import pytest

from company_bid.extraction import (
    PlanningError, _index, _validate_window, _windows, _wire_candidate,
    extract_requirements, source_selected_output_contract,
)
from company_bid.planning import plan_bid


class Context:
    def __init__(self):
        self.units, self.captures = {}, {}

    def run_unit(self, unit_id, data, fn):
        if unit_id not in self.units:
            self.units[unit_id] = (copy.deepcopy(data), copy.deepcopy(fn()))
        assert self.units[unit_id][0] == data
        return copy.deepcopy(self.units[unit_id][1])

    def capture(self, name, payload, **kwargs):
        self.captures[name] = copy.deepcopy(payload)


def fixture(tmp_path):
    source = {"source_id": "T", "title": "虚构招标资料", "path": str(tmp_path / "source.txt"), "sha256": "a" * 64,
              "locator": "document", "kind": "tender", "provenance": "synthetic", "synthetic": True}
    manifest = {"project": {"project_id": "P", "name": "虚构项目", "source_refs": ["T"]},
                "company": {"company_id": "C", "name": "虚构测试公司", "source_refs": ["T"]},
                "sources": [source], "source_roles": {"T": "tender"}, "facts": [], "attachments": [], "synthetic": True,
                "preparation_note": "仅用于引用线协议合成测试。"}
    parsed = {"documents": [{"source_id": "T", "sha256": source["sha256"], "role": "tender", "units": []}],
              "coverage": {"required_complete": True}}
    return manifest, parsed


def block(parsed, identity, text, *, kind="paragraph", page=1, bbox=None, **extra):
    document = parsed["documents"][0]
    unit = next((unit for unit in document["units"] if unit["page"] == page), None)
    if unit is None:
        unit = {"unit_id": f"U{page}", "source_id": "T", "locator": f"pdf/page[{page}]", "page": page,
                "coverage": {"status": "read"}, "blocks": []}
        document["units"].append(unit)
    value = {"block_id": identity, "source_id": "T", "locator": f"pdf/page[{page}]/{kind}[{identity}]",
             "kind": kind, "text": text, **extra}
    if bbox is not None:
        value["bbox"] = bbox
    unit["blocks"].append(value)
    return value


def selected(*blocks, **extra):
    citation = {key: blocks[0][key] for key in ("source_id", "block_id", "locator")}
    if len(blocks) > 1:
        citation["block_ids"] = [value["block_id"] for value in blocks]
    return {**citation, **extra}


def candidate(payload, citation):
    return {"reviewed_block_ids": [value["block_id"] for value in payload["window"]["blocks"]], "requirements": [{
        "citation": citation, "category": "technical", "response_mode": "writing", "format_status": "not_prescribed"}],
        "format_chapters": [], "format_status": "none_verified"}


def window_for(parsed, manifest):
    blocks, _, documents = _index(parsed, manifest)
    return _windows(documents, blocks, {})[0][0]


def no_call(*args):
    pytest.fail("Unexpected external/mapping call")


def test_live_schema_omits_quote_but_internal_legacy_schema_is_not_relaxed():
    schema = source_selected_output_contract()
    citation = schema["$defs"]["_Citation"]
    assert "quote" not in citation["properties"] and "quote" not in citation["required"]
    assert set(citation["required"]) == {"source_id", "block_id", "locator"}
    assert citation["additionalProperties"] is False and schema["additionalProperties"] is False
    assert all(value["additionalProperties"] is False for value in schema["$defs"].values() if value.get("type") == "object")
    citation["properties"]["quote"] = {"type": "string"}
    assert "quote" not in source_selected_output_contract()["$defs"]["_Citation"]["properties"]


@pytest.mark.parametrize("caller_returns_validated", [False, True])
def test_omitted_quote_is_copied_in_full_and_revalidated_by_n3(tmp_path, caller_returns_validated):
    manifest, parsed = fixture(tmp_path)
    first = block(parsed, "B1", "仅在取得书面授权且隔离许可完整时，")
    second = block(parsed, "B2", "方可开展巡检；未取得许可不得操作，紧急情况亦不例外。")
    expected = first["text"] + "\n" + second["text"]
    ctx = Context()
    def call(ctx, task_type, unit_id, payload, validate):
        assert "quote" not in payload["output_contract"]["$defs"]["_Citation"]["properties"]
        raw = candidate(payload, selected(first, second))
        return validate(raw) if caller_returns_validated else raw
    output = extract_requirements(ctx, parsed, manifest, call, {})
    citation = output["requirements"][0]["citations"][0]
    assert citation["quote"] == expected and citation["quote_sha256"] == hashlib.sha256(expected.encode()).hexdigest()
    assert citation["start"] == 0 and citation["end"] == len(expected) and citation["basis"] == "consecutive_blocks_lf_v1"
    result = plan_bid(ctx, output, parsed, manifest, no_call, {})
    assert result["requirements"][0]["text"] == expected
    assert result["sections"][0]["writing_tasks"][0]["must_cover"] == [expected]


def test_internal_none_quote_and_single_block_copy_preserve_every_character(tmp_path):
    manifest, parsed = fixture(tmp_path)
    source = block(parsed, "B1", "  否定、条件、金额100万元及例外均不得删除。\n末行保留。  ")
    window = window_for(parsed, manifest)
    for citation in (selected(source), selected(source, quote=None)):
        validated = _validate_window(candidate({"window": window}, citation), window)
        assert validated["requirements"][0]["citation"]["quote"] == source["text"]


@pytest.mark.parametrize("case", ["unknown_id", "missing_first", "missing_middle", "out_of_order", "duplicate", "wrong_locator", "wrong_source"])
def test_selection_ids_are_not_repaired_or_completed(tmp_path, case):
    manifest, parsed = fixture(tmp_path)
    a = block(parsed, "B1", "前置条件：取得许可，")
    b = block(parsed, "B2", "并完成隔离与复核后，")
    c = block(parsed, "B3", "方可操作；否则不得开展。")
    citation = selected(a, b, c)
    if case == "unknown_id":
        citation["block_ids"][1] = "MISSING"
    elif case == "missing_first":
        citation["block_ids"] = ["B2", "B3"]
    elif case == "missing_middle":
        citation["block_ids"] = ["B1", "B3"]
    elif case == "out_of_order":
        citation["block_ids"] = ["B1", "B3", "B2"]
    elif case == "duplicate":
        citation["block_ids"] = ["B1", "B2", "B2", "B3"]
    elif case == "wrong_locator":
        citation["locator"] = "pdf/page[999]"
    else:
        citation["source_id"] = "OTHER"
    with pytest.raises(PlanningError):
        _validate_window(candidate({"window": window_for(parsed, manifest)}, citation), window_for(parsed, manifest))


@pytest.mark.parametrize("case", ["span_summary", "span_drops_condition", "single_invented", "empty"])
def test_explicit_quotes_keep_the_original_strict_checks(tmp_path, case):
    manifest, parsed = fixture(tmp_path)
    a = block(parsed, "B1", "未获得书面授权与作业许可时，")
    b = block(parsed, "B2", "不得开展操作；不存在默认授权例外。")
    if case == "span_summary":
        citation = selected(a, b, quote="未授权不得操作。")
    elif case == "span_drops_condition":
        citation = selected(a, b, quote=b["text"])
    elif case == "single_invented":
        citation = selected(a, quote="默认可以操作。")
    else:
        citation = selected(a, quote="")
    window = window_for(parsed, manifest)
    with pytest.raises(PlanningError):
        _validate_window(candidate({"window": window}, citation), window)


def test_validated_citation_round_trip_and_tampering_do_not_bypass_quote_validation(tmp_path):
    manifest, parsed = fixture(tmp_path)
    a, b = block(parsed, "B1", "条件不得遗漏，"), block(parsed, "B2", "例外必须完整保留。")
    window = window_for(parsed, manifest)
    once = _validate_window(candidate({"window": window}, selected(a, b)), window)
    assert _validate_window(_wire_candidate(once), window) == once
    once["requirements"][0]["citation"]["quote"] = "缩写摘要"
    with pytest.raises(PlanningError, match="CITATION_SPAN_MUST_QUOTE_ALL_BLOCKS"):
        _validate_window(_wire_candidate(once), window)


def test_second_validation_rejects_a_caller_that_changes_the_rebuilt_quote(tmp_path):
    manifest, parsed = fixture(tmp_path)
    a, b = block(parsed, "B1", "先取得许可，"), block(parsed, "B2", "否则不得操作。")
    def call(ctx, task_type, unit_id, payload, validate):
        validated = validate(candidate(payload, selected(a, b)))
        validated["requirements"][0]["citation"]["quote"] = b["text"]
        return validated
    with pytest.raises(PlanningError, match="JSON_CALLER_VALIDATION_RESULT_MISMATCH"):
        extract_requirements(Context(), parsed, manifest, call, {})


def test_table_selection_copies_the_entire_original_tsv_without_rewriting(tmp_path):
    manifest, parsed = fixture(tmp_path)
    rows = [["项目", "", "单位"], ["额度", "100", "万元"], ["例外：不得省略", "", ""]]
    source = block(parsed, "TABLE", "reader text is not the TSV", kind="table", rows=rows, merges=[], column_widths=[1, 1, 1])
    expected = "\n".join("\t".join(row) for row in rows)
    output = extract_requirements(Context(), parsed, manifest,
        lambda ctx, task_type, unit_id, payload, validate: validate(candidate(payload, selected(source))), {})
    citation = output["requirements"][0]["citations"][0]
    assert citation["quote"] == expected and citation["basis"] == "table_rows_tsv_v1"
    assert citation["quote"].endswith("\t\t")
    assert citation["cell_refs"] == [{"row": 0, "column": 0}, {"row": 0, "column": 2}, {"row": 1, "column": 0},
                                     {"row": 1, "column": 1}, {"row": 1, "column": 2}, {"row": 2, "column": 0}]
    assert source["rows"] == rows and source["text"] == "reader text is not the TSV"


@pytest.mark.parametrize("kind", ["paragraph", "table", "image"])
def test_a_gap_cannot_skip_text_tables_or_images(tmp_path, kind):
    manifest, parsed = fixture(tmp_path)
    a = block(parsed, "B1", "先核对前置条件，")
    extra = {"rows": [["条件", "必须满足"]], "merges": [], "column_widths": [1, 1]} if kind == "table" else {}
    block(parsed, "GAP", "中间内容不得越过。", kind=kind, **extra)
    b = block(parsed, "B2", "否则不得操作。")
    window = window_for(parsed, manifest)
    with pytest.raises(PlanningError, match="CITATION_SPAN_NOT_CONTIGUOUS"):
        _validate_window(candidate({"window": window}, selected(a, b)), window)


def test_only_geometrically_verified_page_footer_can_be_skipped(tmp_path):
    manifest, parsed = fixture(tmp_path)
    def footer(page):
        value = block(parsed, f"F{page}", f"- {page} -", page=page, bbox=[290, 773, 305, 782])
        value["locator"] = f"pdf/page[{page}]/line[footer]"
        return value
    a = block(parsed, "B1", "未获得授权时，", bbox=[60, 100, 300, 112]); f1 = footer(1)
    b = block(parsed, "B2", "不得开展操作。", page=2, bbox=[60, 100, 300, 112]); footer(2)
    block(parsed, "B3", "递交要求。", page=3, bbox=[60, 100, 300, 112]); footer(3)
    window = window_for(parsed, manifest)
    citation = _validate_window(candidate({"window": window}, selected(a, b)), window)["requirements"][0]["citation"]
    assert citation["quote"] == a["text"] + "\n" + b["text"]
    assert citation["skipped_block_ids"] == [f1["block_id"]]
    f1["bbox"] = [60, 200, 300, 212]
    window = window_for(parsed, manifest)
    with pytest.raises(PlanningError, match="CITATION_SPAN_NOT_CONTIGUOUS"):
        _validate_window(candidate({"window": window}, selected(a, b)), window)


def test_source_hash_and_unknown_citation_fields_keep_existing_boundaries(tmp_path):
    manifest, parsed = fixture(tmp_path)
    source = block(parsed, "B1", "原件hash必须保持一致。")
    window = window_for(parsed, manifest)
    with pytest.raises(PlanningError, match="EXTRACTION_SCHEMA_INVALID"):
        _validate_window(candidate({"window": window}, selected(source, source_sha256="b" * 64)), window)
    parsed["documents"][0]["sha256"] = "b" * 64
    with pytest.raises(PlanningError, match="PARSED_SOURCE_HASH_MISMATCH"):
        extract_requirements(Context(), parsed, manifest, no_call, {})


def test_nine_block_clause_needs_no_quote_echo_and_is_copied_without_shortening(tmp_path):
    manifest, parsed = fixture(tmp_path)
    values = [block(parsed, f"B{i}", f"第{i}项前置条件、否定及例外必须原样保留，不得通过摘要省略；" * 2) for i in range(9)]
    citation = selected(*values)
    expected = "\n".join(value["text"] for value in values)
    assert len(expected) > 376
    window = window_for(parsed, manifest)
    validated = _validate_window(candidate({"window": window}, citation), window)
    assert validated["requirements"][0]["citation"]["quote"] == expected
    legacy = {**citation, "quote": expected}
    assert len(json.dumps(citation, ensure_ascii=False).encode()) < len(json.dumps(legacy, ensure_ascii=False).encode())
    with pytest.raises(PlanningError, match="CITATION_SPAN_MUST_QUOTE_ALL_BLOCKS"):
        _validate_window(candidate({"window": window}, {**citation, "quote": "前置条件和否定要求的缩写。"}), window)
