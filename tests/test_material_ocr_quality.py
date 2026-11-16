import copy
import json

import pytest

from company_bid.material_ocr_quality import assess_ocr_response, MaterialOCRQualityFailure


IDENTITY = {"image_sha256": "1" * 64}


def page(text, finish="stop"):
    return {"page_count": 1, "pages": [{"index": 0, "finish_reason": finish, "text": text}]}


def block(text, box=None, kind="text"):
    return "<｜ref｜>" + kind + "<｜/ref｜><｜box｜>" + json.dumps([box or [10, 20, 900, 950]]) + "<｜/box｜> " + text + "\n"


def assess(text, **kwargs):
    return assess_ocr_response(page(text), width=1200, height=1800, identity=IDENTITY, **kwargs)


def reference(kind, expected, **scope):
    return {"image_sha256": IDENTITY["image_sha256"], "checks": [
        {"field_id": "field-1", "kind": kind, "expected_text": expected, **scope}]}


def table(rows):
    return "<table>" + "".join("<tr>" + "".join("<td>" + cell + "</td>" for cell in row) + "</tr>" for row in rows) + "</table>"


def test_regions_preserve_text_and_coordinate_scope_without_invented_word_boxes():
    result = assess(block("证书编号 ABC-0123", [100, 200, 800, 600]))
    assert result["text"] == "证书编号 ABC-0123"
    assert result["lines"][0]["bounding_box"] == [120, 360, 960, 1080]
    assert result["lines"][0]["words"] == []
    assert result["regions"][0]["normalized_boxes"] == [[100, 200, 800, 600]]
    assert result["numeric_validation"]["status"] == "pending_source_check"
    assert not result["numeric_validation"]["source_accuracy_established_for_complete_page"]
    assert result["qualification_result"] == "not_determined"


@pytest.mark.parametrize("finish", ["length", "error", None, ""])
def test_incomplete_response_never_uses_partial_text(finish):
    with pytest.raises(MaterialOCRQualityFailure, match="MATERIAL_OCR_OUTPUT_INCOMPLETE"):
        assess_ocr_response(page(block("already readable"), finish), width=10, height=10, identity=IDENTITY)


@pytest.mark.parametrize("change", [
    lambda p: p.update(page_count=2), lambda p: p.update(page_count=True),
    lambda p: p["pages"][0].update(index=1), lambda p: p["pages"][0].update(index=False),
    lambda p: p.update(pages=[]), lambda p: p.update(error={"code": "timeout"}),
])
def test_invalid_page_denominator_or_body_error(change):
    payload = page(block("text"))
    change(payload)
    with pytest.raises(MaterialOCRQualityFailure):
        assess_ocr_response(payload, width=10, height=10, identity=IDENTITY)


@pytest.mark.parametrize("box", [[-1, 0, 5, 5], [0, 0, 1001, 1000], [5, 2, 5, 8],
    [4, 3, 1, 8], [0, False, 7, 8], [0, 0, float("nan"), 8], [0, 0, 5]])
def test_invalid_coordinates_rejected(box):
    with pytest.raises(MaterialOCRQualityFailure, match="MATERIAL_OCR_COORDINATES_INVALID"):
        assess(block("text", box))


@pytest.mark.parametrize("raw", ["<｜ref｜>text", "<｜box｜>[[0,0,2,2]]<｜/box｜>",
    "<|ref|>text<|/ref|>", "unlocated prefix" + block("text"), block("ok") + "<｜ref｜>text"])
def test_malformed_or_incomplete_markup_not_stripped_into_success(raw):
    with pytest.raises(MaterialOCRQualityFailure, match="MATERIAL_OCR_MARKUP_INVALID"):
        assess(raw)


def test_repeated_empty_coordinates_fail_even_with_stop():
    with pytest.raises(MaterialOCRQualityFailure, match="MATERIAL_OCR_REPETITIVE_OUTPUT") as error:
        assess(block("") * 7)
    assert error.value.details["repeated_region_count"] == 7


def test_repeated_same_short_text_same_region_fails():
    with pytest.raises(MaterialOCRQualityFailure, match="MATERIAL_OCR_REPETITIVE_OUTPUT"):
        assess(block("0") * 5)


def test_repeated_same_image_region_is_not_hidden_as_nontext():
    with pytest.raises(MaterialOCRQualityFailure, match="MATERIAL_OCR_REPETITIVE_OUTPUT"):
        assess(block("", kind="image") * 5)


def test_equal_values_in_different_rows_are_not_a_loop():
    result = assess("".join(block("0", [0, i * 20, 100, i * 20 + 10]) for i in range(30)))
    assert len(result["regions"]) == 30


def test_unbroken_long_phrase_loop_fails_without_coordinates():
    with pytest.raises(MaterialOCRQualityFailure, match="MATERIAL_OCR_REPETITIVE_OUTPUT"):
        assess("this repeated paragraph is a bad output. " * 20)


def test_empty_text_is_unknown_not_original_blank():
    result = assess("")
    assert result["reading_status"] == "ocr_no_text_unresolved"
    assert result["reading_completeness"] == "not_established"


def test_plain_text_can_be_candidate_without_fabricated_geometry():
    result = assess("Amount: 100.25 元")
    assert result["lines"][0]["bounding_box"] is None
    assert result["lines"][0]["words"] == []


def test_table_preserves_merge_and_original_empty_cell_distinction():
    markup = "<table><tr><td>名称</td><td>数量</td><td>金额</td></tr><tr><td rowspan='2'>材料</td><td></td><td>0</td></tr><tr><td>2</td><td>4</td></tr></table>"
    result = assess(block(markup, kind="table"))
    t = result["tables"][0]
    assert t["grid"][1][0] == t["grid"][2][0]
    assert t["cells"][t["grid"][1][1]]["empty_cell"] is True
    assert t["cells"][t["grid"][1][2]]["text"] == "0"
    assert "not_independent_source_blank_confirmation" in t["empty_cells_are"]
    assert all("bounding_box" not in cell for cell in t["cells"])


@pytest.mark.parametrize("markup", [
    "<script>alert(1)</script>", "<img src='https://example.test/a'>", "<!DOCTYPE table><table></table>",
    "<table><tr><td onclick='x'>a</td></tr></table>",
    "<table><tr><td rowspan='99'>a</td></tr></table>",
    "<table><tr><td rowspan='2'>a</td></tr></table>",
    "<table><tr><td>a<td>b</td></tr></table>",
    "<table><tr><td>a</td><td>b</td></tr><tr><td>c</td></tr></table>",
    "<table><tr><td><table><tr><td>a</td></tr></table></td></tr></table>",
    "<table><tr><td>a<!--secret--></td></tr></table>",
    "<table", "<table><tr><td>value", "<td",
])
def test_unsafe_or_malformed_tables_are_not_silently_repaired(markup):
    with pytest.raises(MaterialOCRQualityFailure):
        assess(block(markup, kind="table"))


def test_coordinates_not_counted_as_numeric_text():
    result = assess(block("No numbers here", [123, 234, 567, 890]))
    assert result["numeric_validation"]["inventory"] == []


def test_numeric_units_remain_distinct_and_unverified_without_reference():
    result = assess(block("面积740m²；体积1100m³；报价12.50元"))
    measures = [i for i in result["numeric_validation"]["inventory"] if i["kind"] == "measure"]
    assert {(i["number"], i["unit"]) for i in measures} == {("740", "m2"), ("1100", "m3"), ("12.50", "元")}
    assert all(i["source_status"] == "pending_source_check" for i in measures)


def test_invalid_calendar_date_requires_review_without_repair():
    result = assess(block("有效日期：2026年2月30日"))
    assert result["numeric_validation"]["status"] == "review_required"
    assert result["numeric_validation"]["findings"][0]["code"] == "OCR_DATE_INVALID"
    assert "2026年2月30日" in result["text"]


def test_explicit_arithmetic_is_checked_but_not_source_truth():
    good = assess(block(table([["数量", "单价", "金额"], ["3", "2.50", "7.50"]]), kind="table"))
    assert good["numeric_validation"]["arithmetic_checks"][0]["status"] == "consistent_not_source_verified"
    assert good["numeric_validation"]["status"] == "pending_source_check"
    bad = assess(block(table([["数量", "单价", "金额"], ["3", "2.50", "8.00"]]), kind="table"))
    assert bad["numeric_validation"]["findings"][0]["code"] == "OCR_TABLE_ARITHMETIC_INCONSISTENT"
    assert "8.00" in bad["text"]


def test_blank_quantity_or_unrecognized_headers_never_invent_arithmetic():
    for rows in ([["数量", "单价", "金额"], ["", "0", "0"]], [["A", "B", "C"], ["3", "2", "9"]]):
        result = assess(block(table(rows), kind="table"))
        assert result["numeric_validation"]["arithmetic_checks"] == []


def test_matching_reviewed_measure_covers_only_named_cell():
    ref = reference("measure", "740 m²", table_index=0, row=1, column=1)
    result = assess(block(table([["事项", "备注"], ["恢复", "地砖恢复740m2；其他未知"]]), kind="table"), numeric_reference=ref)
    check = result["numeric_validation"]
    assert check["reviewed_reference_checks"][0]["status"] == "matches_supplied_reviewed_reference_only"
    assert check["status"] == "pending_source_check"
    assert not check["source_accuracy_established_for_complete_page"]


def test_reviewed_area_vs_volume_conflict_fails_visible_without_guessing():
    ref = reference("measure", "740 m²", table_index=0, row=1, column=1)
    with pytest.raises(MaterialOCRQualityFailure, match="MATERIAL_OCR_NUMERIC_REFERENCE_MISMATCH") as error:
        assess(block(table([["事项", "备注"], ["恢复", "地砖恢复740m3"]]), kind="table"), numeric_reference=ref)
    assert error.value.details["field_id"] == "field-1"
    assert "740" not in str(error.value)


def test_correct_value_in_another_cell_does_not_satisfy_reference():
    ref = reference("measure", "740 m²", table_index=0, row=1, column=1)
    with pytest.raises(MaterialOCRQualityFailure, match="MATERIAL_OCR_NUMERIC_REFERENCE_MISMATCH"):
        assess(block(table([["事项", "备注"], ["740m²", "740m3"]]), kind="table"), numeric_reference=ref)


def test_same_value_mixed_units_in_one_scope_requires_narrower_reference():
    ref = reference("measure", "740 m²", region_index=0)
    with pytest.raises(MaterialOCRQualityFailure, match="MATERIAL_OCR_NUMERIC_REFERENCE_AMBIGUOUS"):
        assess(block("面积740m²，其他740m3"), numeric_reference=ref)


def test_reviewed_blank_cell_and_missing_text_are_distinct():
    ref = reference("expected_blank", "", table_index=0, row=1, column=1)
    result = assess(block(table([["项目", "数量"], ["免费", ""]]), kind="table"), numeric_reference=ref)
    assert result["numeric_validation"]["reviewed_reference_checks"][0]["kind"] == "expected_blank"
    with pytest.raises(MaterialOCRQualityFailure, match="MATERIAL_OCR_NUMERIC_REFERENCE_SCOPE_MISSING"):
        assess("", numeric_reference=ref)


@pytest.mark.parametrize("kind,expected,text", [("number", "25.00", "金额 25"),
    ("date", "2026年2月28日", "日期2026-02-28"), ("identifier", "AB-102", "证号AB-102")])
def test_controlled_same_value_reference_formats(kind, expected, text):
    result = assess(block(text), numeric_reference=reference(kind, expected, region_index=0))
    assert len(result["numeric_validation"]["reviewed_reference_checks"]) == 1


@pytest.mark.parametrize("kind,expected,text", [("number", "25", "金额250"),
    ("date", "2026年2月28日", "日期2026-02-27"), ("identifier", "AB-102", "证号AB-1029")])
def test_nearby_digits_not_accepted_as_equal_reference(kind, expected, text):
    with pytest.raises(MaterialOCRQualityFailure, match="MATERIAL_OCR_NUMERIC_REFERENCE_MISMATCH"):
        assess(block(text), numeric_reference=reference(kind, expected, region_index=0))


def test_reference_wrong_source_or_missing_scope_is_rejected():
    ref = reference("number", "25", region_index=0)
    ref["image_sha256"] = "2" * 64
    with pytest.raises(MaterialOCRQualityFailure, match="MATERIAL_OCR_NUMERIC_REFERENCE_INVALID"):
        assess(block("25"), numeric_reference=ref)
    ref = reference("number", "25", region_index=4)
    with pytest.raises(MaterialOCRQualityFailure, match="MATERIAL_OCR_NUMERIC_REFERENCE_SCOPE_MISSING"):
        assess(block("25"), numeric_reference=ref)


def test_input_payload_identity_and_reviewed_reference_not_mutated():
    payload = page(block("25元"))
    identity = copy.deepcopy(IDENTITY)
    ref = reference("number", "25", region_index=0)
    before = copy.deepcopy((payload, identity, ref))
    assess_ocr_response(payload, width=100, height=200, identity=identity, numeric_reference=ref)
    assert (payload, identity, ref) == before


def test_control_character_not_silently_removed():
    with pytest.raises(MaterialOCRQualityFailure, match="MATERIAL_OCR_CONTROL_CHARACTER_INVALID"):
        assess("12\x00元")
