"""Whole-N1 source structure for empty material headings, without a model."""
import copy
import hashlib

import pytest

from company_bid.extraction import _index, _windows, extraction_window_payload
from company_bid.material_heading_sources import material_heading_source_groups
from company_bid.storage import digest
from company_bid.structured import _MATERIAL_RESPONSE_FIELDS
from test_source_selected_citations import block, fixture


DEFAULT_NAMES = ("资质材料", "业绩资料", "财务要求资料", "信誉要求资料", "人员材料", "其他资料")


def source_case(tmp_path, *, number=13, start=3, names=DEFAULT_NAMES, targets=(3, 4), toc=True):
    """Return candidate/request/parsed/manifest with a parent in an earlier window."""
    manifest, parsed = fixture(tmp_path)
    document = parsed["documents"][0]
    document["format"] = "pdf"
    parsed["coverage"] = {"required_complete": True, "status": "complete"}

    def line(identity, text, page):
        value = block(parsed, identity, text, page=page, origin="native")
        unit = next(unit for unit in document["units"] if unit["page"] == page)
        value["locator"] = f"pdf/page[{page}]/line[{len(unit['blocks']) - 1}]"
        return value

    if toc:
        line("TOC", "目 录", 1)
        line("TOC-PARENT", f"{number}. 资格审查材料", 1)
    line("REGION", "第五章 响应文件要求及格式", 3)
    line("PARENT", f"{number} 资格审查资料", 3)
    line("NOTE", "注：所有材料按照“第三部分 询比要求”中要求提供", 3)
    for index, name in enumerate(names):
        line(f"H{index + 1}", f"{number}.{start + index}、{name}：", 3 if index < 3 else 4)
    line("NEXT-PARENT", f"{number + 1} 技术服务方案", 4)
    line("END", "第六章 合成合同条款", 5)
    for unit in document["units"]:
        unit["coverage"]["image_count"] = 0
    document["expected_units"] = len(document["units"])
    source_hash = hashlib.sha256(b"Synthetic full N1 source fixture only.").hexdigest()
    document["sha256"] = manifest["sources"][0]["sha256"] = source_hash
    target_ids = [f"H{index + 1}" for index in targets]
    candidate, request = packet(parsed, manifest, target_ids)
    return candidate, request, parsed, manifest


def packet(parsed, manifest, target_ids):
    blocks, _, documents = _index(parsed, manifest)
    config = {"window_max_chars": 50000, "window_max_blocks": 2}
    windows, _ = _windows(documents, blocks, config)
    window = next(window for window in windows if set(target_ids) <= {block["block_id"] for block in window["blocks"]})
    request = {**extraction_window_payload(window, config), "task_type": "extract_requirements"}
    candidate = {"window_id": window["window_id"], "processing_declaration": "processed_not_semantically_accepted",
        "unresolved_block_ids": list(target_ids), "requirements": [], "format_chapters": [], "format_status": "none_verified"}
    return candidate, request


def values(parsed):
    return {value["block_id"]: value for doc in parsed["documents"] for unit in doc["units"] for value in unit["blocks"]}


def rebuild(case):
    candidate, _, parsed, manifest = case
    return (*packet(parsed, manifest, candidate["unresolved_block_ids"]), parsed, manifest)


def test_remote_parent_and_toc_name_are_source_bound_without_candidate_parent_or_performance_table(tmp_path):
    case = source_case(tmp_path)
    frozen = copy.deepcopy(case)
    candidate, request, parsed, _ = case
    assert "PARENT" not in {block["block_id"] for block in request["window"]["blocks"]}
    groups = material_heading_source_groups(*case)
    assert [group["block_ids"] for group in groups] == [["H4"], ["H5"]]
    assert all(group["resolution_kind"] == "source_bound_material_heading" for group in groups)
    for group in groups:
        assert group["required_response_fields"] == _MATERIAL_RESPONSE_FIELDS
        assert group["required_target_ids"] == group["block_ids"]
        proof = group["source_proof"]
        assert group["source_proof_sha256"] == digest(proof)
        assert proof["parsed_sha256"] == digest(parsed)
        assert proof["source_reading_sha256"] == digest(parsed["documents"][0])
        assert proof["parent_heading"]["block_id"] == "PARENT"
        assert proof["common_instruction"]["text"].startswith("注：所有材料按照")
        assert proof["format_ancestor"]["block_id"] == "REGION"
        assert proof["reference_title"] == "第三部分 询比要求"
        assert proof["reference_resolution_status"] == "not_resolved"
        assert proof["semantic_review"] == "requires_source_review"
        assert proof["target_heading"]["page"] == proof["next_sibling"]["page"]
        assert all(unit["image_count"] == 0 for unit in proof["boundary_units"])
        for member in (proof["parent_heading"], proof["common_instruction"], proof["target_heading"], proof["next_sibling"]):
            assert member["text_sha256"] == hashlib.sha256(member["text"].encode()).hexdigest()
    assert groups[-1]["source_proof"]["next_sibling"]["text"].endswith("其他资料：")
    assert candidate["requirements"] == []
    assert case == frozen


@pytest.mark.parametrize("number,start,names,targets", [
    (2, 1, ("资格材料", "财务资料"), (0,)),
    (31, 9, ("资质要求资料", "信誉材料", "人员资料", "其他材料"), (1,)),
    (97, 41, DEFAULT_NAMES, (3, 4)),
    (8, 4, ("资格资料", "资质材料", "财务材料", "业绩资料", "信誉资料", "人员要求资料", "其他资料"), (3, 4)),
])
def test_numbers_names_and_sibling_count_are_not_a_fixed_family(tmp_path, number, start, names, targets):
    case = source_case(tmp_path, number=number, start=start, names=names, targets=targets)
    groups = material_heading_source_groups(*case)
    assert groups is not None and len(groups) == len(targets)
    assert groups[0]["source_proof"]["sibling_numbers"] == list(range(start, start + len(names)))


def test_request_may_omit_task_type_but_no_other_request_drift_is_accepted(tmp_path):
    candidate, request, parsed, manifest = source_case(tmp_path)
    request.pop("task_type")
    assert material_heading_source_groups(candidate, request, parsed, manifest)
    request["unexpected"] = "not source proof"
    assert material_heading_source_groups(candidate, request, parsed, manifest) is None


def test_adjacent_unresolved_is_preserved_and_never_becomes_a_resolution_target(tmp_path):
    case = source_case(tmp_path)
    candidate, request, _, _ = case
    adjacent = request["window"]["adjacent_context"][0]["block_id"]
    assert adjacent not in candidate["unresolved_block_ids"]
    candidate["unresolved_block_ids"].append(adjacent)
    groups = material_heading_source_groups(*case)
    assert [group["block_ids"] for group in groups] == [["H4"], ["H5"]]
    assert candidate["unresolved_block_ids"] == ["H4", "H5", adjacent]


def test_noncritical_company_reading_limitations_do_not_negate_complete_tender(tmp_path):
    candidate, request, parsed, manifest = source_case(tmp_path)
    manifest["sources"].append({**manifest["sources"][0], "source_id": "C", "kind": "company_material", "sha256": "b" * 64})
    manifest["source_roles"]["C"] = "company_material"
    parsed["documents"].append({"source_id": "C", "sha256": "b" * 64, "format": "pdf", "role": "company_material",
        "units": [{"unit_id": "C-U", "source_id": "C", "locator": "pdf/page[1]", "page": 1,
                   "coverage": {"status": "needs_ocr"}, "blocks": []}]})
    parsed["coverage"] = {"required_complete": False, "status": "has_noncritical_limitations"}
    candidate, request = packet(parsed, manifest, ["H4", "H5"])
    assert material_heading_source_groups(candidate, request, parsed, manifest)


@pytest.mark.parametrize("damage", ["title_threshold", "title_condition", "title_negation", "unknown_title", "wrong_parent_prefix",
    "number_gap", "duplicate_number", "note_condition", "note_negative", "note_unknown", "parent_not_qualification",
    "false_format", "no_format", "intervening_chapter", "ocr_target", "ocr_parent", "ocr_note", "unread",
    "unmapped", "read_error", "missing_unit", "foreign_hash", "foreign_role", "wrong_line", "cross_page"])
def test_substantive_or_unproved_structure_never_earns_an_empty_heading_repair(tmp_path, damage):
    case = source_case(tmp_path)
    candidate, _, parsed, manifest = case
    by_id = values(parsed)
    if damage == "title_threshold": by_id["H4"]["text"] += "须具备一级资质"
    elif damage == "title_condition": by_id["H4"]["text"] += "（如适用）"
    elif damage == "title_negation": by_id["H4"]["text"] = "13.6、不得提供信誉材料："
    elif damage == "unknown_title": by_id["H4"]["text"] = "13.6、未知证明材料："
    elif damage == "wrong_parent_prefix": by_id["H4"]["text"] = "14.6、信誉材料："
    elif damage == "number_gap": by_id["H4"]["text"] = "13.99、信誉材料："
    elif damage == "duplicate_number": by_id["H5"]["text"] = by_id["H4"]["text"]
    elif damage == "note_condition": by_id["NOTE"]["text"] += "，如适用则提供"
    elif damage == "note_negative": by_id["NOTE"]["text"] = "注：所有材料不按照“第三部分 询比要求”中要求提供"
    elif damage == "note_unknown": by_id["NOTE"]["text"] = "注：材料要求待定"
    elif damage == "parent_not_qualification": by_id["PARENT"]["text"] = "13 技术材料"
    elif damage == "false_format": by_id["REGION"]["text"] = "第五章 合成合同要求"
    elif damage == "no_format": by_id["REGION"]["text"] = "说明文字"
    elif damage == "intervening_chapter": by_id["H3"]["text"] = "第六章 合成合同条款"
    elif damage.startswith("ocr_"): by_id[{"ocr_target": "H4", "ocr_parent": "PARENT", "ocr_note": "NOTE"}[damage]]["origin"] = "ocr"
    elif damage in {"unread", "unmapped", "read_error"}:
        unit = next(unit for unit in parsed["documents"][0]["units"] if unit["page"] == 4)
        if damage == "unread": unit["coverage"]["status"] = "needs_ocr"
        elif damage == "unmapped": unit["coverage"]["unmapped_glyphs"] = [{"char": "unknown"}]
        else: unit["coverage"]["error_code"] = "READ_FAILED"
    elif damage == "missing_unit": parsed["documents"][0]["expected_units"] += 1
    elif damage == "foreign_hash": manifest["sources"][0]["sha256"] = "b" * 64
    elif damage == "foreign_role": parsed["documents"][0]["role"] = "reference_bid"
    elif damage == "wrong_line": by_id["H5"]["locator"] = "pdf/page[4]/line[99]"
    elif damage == "cross_page": by_id["H5"]["locator"] = "pdf/page[5]/line[1]"
    # Rebuild the actual request when source structure permits it, so these
    # test semantic source guards rather than merely the request equality.
    try:
        case = rebuild((candidate, {}, parsed, manifest))
    except (ValueError, StopIteration):
        pass
    assert material_heading_source_groups(*case) is None


def test_two_genuine_source_parents_are_ambiguous_even_when_one_owns_the_targets(tmp_path):
    candidate, _, parsed, manifest = source_case(tmp_path)
    unit = next(unit for unit in parsed["documents"][0]["units"] if unit["page"] == 4)
    extra = [("PARENT2", "13 资格审查资料"), ("NOTE2", "注：所有材料按照“另一引用题名”中要求提供"),
             ("X1", "13.1、资格资料："), ("X2", "13.2、人员资料：")]
    insert_at = next(index for index, value in enumerate(unit["blocks"]) if value["block_id"] == "NEXT-PARENT")
    unit["blocks"][insert_at:insert_at] = [{"block_id": key, "source_id": "T", "kind": "paragraph", "origin": "native",
        "text": text, "locator": "temporary"} for key, text in extra]
    for index, value in enumerate(unit["blocks"]):
        value["locator"] = f"pdf/page[4]/line[{index}]"
    candidate, request = packet(parsed, manifest, ["H4", "H5"])
    assert material_heading_source_groups(candidate, request, parsed, manifest) is None


def test_a_non_target_siblings_existing_table_is_neither_required_nor_removed(tmp_path):
    candidate, _, parsed, manifest = source_case(tmp_path, targets=(3,))
    unit = next(unit for unit in parsed["documents"][0]["units"] if unit["page"] == 3)
    index = next(index for index, value in enumerate(unit["blocks"]) if value["block_id"] == "H2") + 1
    unit["blocks"].insert(index, {"block_id": "OTHER-BODY", "source_id": "T", "kind": "table", "origin": "native",
        "text": "", "locator": "pdf/page[3]/table[0]", "rows": [["合成材料", "说明"], ["", ""]],
        "merges": [], "header_rows": [], "column_widths": [1, 1]})
    candidate, request = packet(parsed, manifest, ["H4"])
    before = copy.deepcopy((candidate, request, parsed, manifest))
    groups = material_heading_source_groups(candidate, request, parsed, manifest)
    assert groups is not None and [group["block_ids"] for group in groups] == [["H4"]]
    assert (candidate, request, parsed, manifest) == before


def test_an_intervening_foreign_numbered_parent_reference_does_not_preserve_false_parentage(tmp_path):
    candidate, _, parsed, manifest = source_case(tmp_path)
    unit = next(unit for unit in parsed["documents"][0]["units"] if unit["page"] == 3)
    unit["blocks"].append({"block_id": "FOREIGN-CHILD", "source_id": "T", "kind": "paragraph", "origin": "native",
        "text": "14.1、资质材料：", "locator": "pdf/page[3]/line[6]"})
    candidate, request = packet(parsed, manifest, ["H4"])
    assert material_heading_source_groups(candidate, request, parsed, manifest) is None


def test_other_materials_may_only_terminate_a_known_target_never_earn_their_own_response(tmp_path):
    assert material_heading_source_groups(*source_case(tmp_path, targets=(5,))) is None


def test_more_than_eight_current_target_unresolved_headings_is_outside_this_repair_contract(tmp_path):
    names = tuple("资质材料" for _ in range(10))
    _, _, parsed, manifest = source_case(tmp_path, names=names, targets=(0,))
    blocks, _, documents = _index(parsed, manifest)
    config = {"window_max_chars": 50000, "window_max_blocks": 100}
    windows, _ = _windows(documents, blocks, config)
    target_ids = [f"H{index}" for index in range(1, 10)]
    window = next(window for window in windows if set(target_ids) <= {value["block_id"] for value in window["blocks"]})
    request = {**extraction_window_payload(window, config), "task_type": "extract_requirements"}
    candidate = {"window_id": window["window_id"], "unresolved_block_ids": target_ids, "format_status": "none_verified"}
    assert material_heading_source_groups(candidate, request, parsed, manifest) is None


@pytest.mark.parametrize("page,image_count", [(3, 1), (4, 1), (4, None), (4, -1), (4, "0"), (4, False), (4, "missing")])
def test_empty_body_requires_an_explicit_zero_image_count_on_the_boundary_page(tmp_path, page, image_count):
    candidate, _, parsed, manifest = source_case(tmp_path)
    coverage = next(unit for unit in parsed["documents"][0]["units"] if unit["page"] == page)["coverage"]
    if image_count == "missing": coverage.pop("image_count")
    else: coverage["image_count"] = image_count
    candidate, request = packet(parsed, manifest, ["H4", "H5"])
    assert material_heading_source_groups(candidate, request, parsed, manifest) is None


def test_images_elsewhere_in_the_document_do_not_globally_disable_this_source_rule(tmp_path):
    candidate, _, parsed, manifest = source_case(tmp_path)
    unit = next(unit for unit in parsed["documents"][0]["units"] if unit["page"] == 1)
    unit["coverage"]["image_count"] = 1
    unit["blocks"].append({"block_id": "TOC-IMAGE", "source_id": "T", "kind": "image", "origin": "native",
                           "text": "", "locator": "pdf/page[1]/image[0]"})
    candidate, request = packet(parsed, manifest, ["H4", "H5"])
    assert material_heading_source_groups(candidate, request, parsed, manifest)


@pytest.mark.parametrize("kind,text", [("paragraph", "须提供资质证书。"), ("paragraph", "注：仅人员达到条件时提供。"),
                                      ("table", ""), ("image", "")])
def test_real_body_between_target_and_next_sibling_cannot_be_removed(tmp_path, kind, text):
    candidate, _, parsed, manifest = source_case(tmp_path, targets=(3,))
    unit = next(unit for unit in parsed["documents"][0]["units"] if unit["page"] == 4)
    insertion = {"block_id": "BODY", "source_id": "T", "kind": kind, "text": text,
                 "origin": "native", "locator": "pdf/page[4]/line[1]"}
    if kind == "table": insertion.update(rows=[["资质要求", "说明"], ["不得少于一项", ""]], merges=[], header_rows=[], column_widths=[1, 1])
    if kind == "table": insertion["locator"] = "pdf/page[4]/table[0]"
    if kind == "image": insertion["locator"] = "pdf/page[4]/image[0]"
    unit["blocks"].insert(1, insertion)
    # Existing subsequent physical line coordinates remain consecutive only
    # for a table/image; even then their block positions defeat emptiness.
    if kind == "paragraph":
        for index, value in enumerate(unit["blocks"]):
            value["locator"] = f"pdf/page[4]/line[{index}]"
    request_candidate, request = packet(parsed, manifest, ["H4"])
    assert material_heading_source_groups(request_candidate, request, parsed, manifest) is None


@pytest.mark.parametrize("damage", ["task_type", "block_text", "block_locator", "block_removed", "fake_catalog_parent", "wrong_source_hash",
                                    "unknown_unresolved", "duplicate_unresolved", "all_targets_not_material", "wrong_window"])
def test_request_or_target_ownership_drift_is_rejected(tmp_path, damage):
    candidate, request, parsed, manifest = source_case(tmp_path)
    if damage == "task_type": request["task_type"] = "writing"
    elif damage == "block_text": request["window"]["blocks"][0]["text"] += "changed"
    elif damage == "block_locator": request["window"]["blocks"][0]["locator"] = "pdf/page[99]/line[0]"
    elif damage == "block_removed": request["window"]["blocks"].pop()
    elif damage == "fake_catalog_parent": request["window"]["source_heading_catalog"] = [{"title": "13 资格审查资料", "block_id": "fake"}]
    elif damage == "wrong_source_hash": request["source_sha256"] = "b" * 64
    elif damage == "unknown_unresolved": candidate["unresolved_block_ids"].append("missing-source")
    elif damage == "duplicate_unresolved": candidate["unresolved_block_ids"].append("H4")
    elif damage == "all_targets_not_material": candidate["unresolved_block_ids"] = ["H6"]
    elif damage == "wrong_window": candidate["window_id"] = "different-window"
    assert material_heading_source_groups(candidate, request, parsed, manifest) is None

