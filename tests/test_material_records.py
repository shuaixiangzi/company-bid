"""Synthetic native declarations with strict scopes; no remote calls."""
import copy
import hashlib

import pytest
from pydantic import TypeAdapter, ValidationError

from company_bid.material_records import MaterialRecord, MaterialRecordError, RecordScope, prepare_material_records
from company_bid.storage import digest


class Context:
    def __init__(self):
        self.units, self.captures = {}, {}

    def run_unit(self, identity, data, fn):
        if identity in self.units:
            assert self.units[identity][0] == data
            return copy.deepcopy(self.units[identity][1])
        result = fn()
        self.units[identity] = (copy.deepcopy(data), copy.deepcopy(result))
        return result

    def capture(self, name, payload, **kwargs):
        self.captures[name] = copy.deepcopy(payload)


def personnel_table(locator="body/table[0]"):
    rows = [["层级", "职务", "姓名", "职称", "执业或职业资格证明", "", "", "", "备注"],
            ["", "", "", "", "证书名称", "级别", "证号", "专业", ""],
            ["历史服务层", "项目负责人", "测试甲", "工程师", "CP3", "三级", "TEST-A", "检测", ""],
            ["", "", "", "", "工程师", "中级", "TEST-TITLE", "机械", ""],
            ["", "检测员", "测试乙", "技术员", "CP2", "二级", "TEST-B", "检测", ""]]
    merges = [{"row": 0, "column": c, "row_span": 2, "col_span": 1} for c in (0, 1, 2, 3, 8)]
    merges += [{"row": 0, "column": 4, "row_span": 1, "col_span": 4}, {"row": 2, "column": 0, "row_span": 3, "col_span": 1}]
    merges += [{"row": 2, "column": c, "row_span": 2, "col_span": 1} for c in (1, 2, 3)]
    return {"block_id": locator, "source_id": "BID", "locator": locator, "kind": "table", "origin": "native", "rows": rows, "merges": merges}


def history_table(locator="body/table[2]"):
    return {"block_id": locator, "source_id": "BID", "locator": locator, "kind": "table", "origin": "native", "merges": [], "rows": [
        ["序号", "项目名称", "项目\n负责人", "发包人", "服务地点", "服务日期", "合同额\n（万元）", "本项目拟投入人员是否参与"],
        ["1", "虚构历史项目", "测试甲", "虚构客户", "测试地点", "2020.01—2021.02", "12.50", "是"],
        ["2", "另一虚构历史项目", "测试乙", "另一虚构客户", "测试地点", "2022年", "99.00", "否"]]}


def inputs(tables):
    sources = [{"source_id": "BID", "sha256": "a" * 64, "role": "reference_bid", "provenance": "derived_from_bid"}]
    parsed = {"documents": [{"source_id": "BID", "sha256": "a" * 64, "role": "reference_bid", "units": [
        {"unit_id": "U" + str(i), "source_id": "BID", "locator": table["locator"], "page": None, "coverage": {"status": "read", "unread_objects": []},
         "blocks": [table]} for i, table in enumerate(tables)]}]}
    return sources, parsed


def scope(kind, table, **extra):
    return {"kind": kind, "source_id": "BID", "source_sha256": "a" * 64, "locator": table["locator"], **extra}


def prepare(tables, scopes, *, allowed=True, parsed_change=None):
    sources, parsed = inputs(tables)
    if parsed_change:
        parsed_change(parsed)
    ctx = Context()
    result = prepare_material_records(ctx, sources, parsed, scopes, {"BID": "a" * 64} if allowed else {})
    return result, ctx


def test_merged_names_have_multiple_certificate_declarations_and_original_cell_quotes():
    table = personnel_table()
    result, _ = prepare([table], [scope("personnel_declaration", table)])
    assert len(result["material_records"]) == 2
    person = result["material_records"][0]
    assert person["fields"]["name"]["value"] == "测试甲"
    assert len(person["fields"]["certificate_declarations"]) == 2
    citations = person["fields"]["name"]["citations"]
    assert {c["logical_row"] for c in citations} == {2, 3} and {c["row"] for c in citations} == {2}
    assert all(c["row_span"] == 2 and c["quote"] == "测试甲" for c in citations)
    assert person["current_project_assignment"] is False and person["independently_verified"] is False
    assert person["assurance"] == "user_authorized_declaration_not_independent_proof"
    assert TypeAdapter(MaterialRecord).validate_python(person).kind == "personnel_declaration"


def test_fully_duplicate_tables_merge_same_value_but_keep_both_locations():
    a, b = personnel_table(), personnel_table("body/table[1]")
    result, _ = prepare([a, b], [scope("personnel_declaration", a, merge_group="STAFF"), scope("personnel_declaration", b, merge_group="STAFF")])
    assert len(result["material_records"]) == 2
    assert result["record_processing"][1]["duplicate_table_of"] == result["record_processing"][0]["scope_id"]
    person = result["material_records"][0]
    assert {c["locator"] for c in person["citations"]} == {a["locator"], b["locator"]}
    assert all(len(declaration["citations"]) == 2 for declaration in person["fields"]["certificate_declarations"])
    assert len(person["table_evidence"]) == 1 and len(person["table_evidence"][0]["locations"]) == 2


def test_same_name_different_tables_does_not_silently_become_one_person():
    a, b = personnel_table(), personnel_table("body/table[1]")
    b["rows"][2][3] = "高级工程师"
    b["rows"][2][6] = "DIFFERENT-NUMBER"
    result, _ = prepare([a, b], [scope("personnel_declaration", a, merge_group="STAFF"), scope("personnel_declaration", b, merge_group="STAFF")])
    candidates = [record for record in result["material_records"] if record["fields"]["name"]["value"] == "测试甲"]
    assert len(candidates) == 2 and all(record["status"] == "conflict" for record in candidates)
    assert all("same_native_name_in_distinct_source_groups_not_resolved" in record["issues"] for record in candidates)


def test_separate_same_name_groups_in_one_table_are_not_globally_merged():
    table = personnel_table()
    table["rows"][4][2] = "测试甲"
    result, _ = prepare([table], [scope("personnel_declaration", table)])
    assert len(result["material_records"]) == 2
    assert all(record["status"] == "conflict" for record in result["material_records"])


def test_adjacent_complete_same_name_rows_without_native_name_merge_remain_distinct():
    table = personnel_table()
    table["merges"] = [merge for merge in table["merges"] if (merge["row"], merge["column"]) not in {(2, 1), (2, 2), (2, 3)}]
    table["rows"][3][1:4] = ["另一历史职务", "测试甲", "另一历史职称"]
    result, _ = prepare([table], [scope("personnel_declaration", table)])
    same_name = [record for record in result["material_records"] if record["fields"]["name"]["value"] == "测试甲"]
    assert len(same_name) == 2 and all(record["status"] in {"conflict", "unresolved"} for record in same_name)
    assert all(len(record["fields"]["roles"]) == 1 and len(record["fields"]["certificate_declarations"]) == 1 for record in same_name)


def test_same_person_can_have_more_than_one_title_in_an_explicit_name_merge():
    table = personnel_table()
    table["merges"] = [merge for merge in table["merges"] if (merge["row"], merge["column"]) != (2, 3)]
    table["rows"][3][3] = "高级技师"
    result, _ = prepare([table], [scope("personnel_declaration", table)])
    person = result["material_records"][0]
    assert {value["value"] for value in person["fields"]["titles"]} == {"工程师", "高级技师"}
    assert person["status"] == "usable_provisional"


def test_unmerged_blank_name_is_not_forward_filled():
    table = personnel_table()
    table["merges"] = [merge for merge in table["merges"] if (merge["row"], merge["column"]) != (2, 2)]
    result, _ = prepare([table], [scope("personnel_declaration", table)])
    assert result["status"] == "partial"
    assert any(row["status"] == "missing_name" for row in result["record_processing"][0]["row_processing"])
    assert len(result["material_records"][0]["fields"]["certificate_declarations"]) == 1


def test_history_preserves_date_amount_unit_and_reference_bid_staff_flag():
    table = history_table()
    result, _ = prepare([table], [scope("historical_performance", table)])
    record = result["material_records"][0]
    assert record["fields"]["contract_amount"]["value"] == "12.50" and record["fields"]["contract_amount"]["unit"] == "万元"
    assert record["fields"]["service_date"]["value"] == "2020.01—2021.02"
    assert record["fields"]["historical_bid_staff_participation"]["value"] == "是"
    assert record["current_project_assignment"] is False and record["current_project_price"] is False
    assert record["citations"][0]["row_values"] == table["rows"][1]
    assert "fields" in TypeAdapter(MaterialRecord).validate_python(record).model_dump()


def test_conflicting_history_keeps_both_original_amounts_and_never_selects_one():
    a, b = history_table(), history_table("body/table[3]")
    b["rows"][1][6] = "25.00"
    result, _ = prepare([a, b], [scope("historical_performance", a), scope("historical_performance", b)])
    candidates = [r for r in result["material_records"] if r["fields"]["project_name"]["value"] == "虚构历史项目"]
    assert {r["fields"]["contract_amount"]["value"] for r in candidates} == {"12.50", "25.00"}
    assert all(r["status"] == "conflict" for r in candidates)
    assert result["status"] == "conflict"


@pytest.mark.parametrize("case,status", [("header", "unknown_headers"), ("unread", "unread"), ("ocr", "unsupported_origin"), ("no_adoption", "not_authorized")])
def test_unknown_headers_unread_and_ocr_never_produce_adopted_records(case, status):
    table = personnel_table()
    if case == "header":
        table["rows"][0][2] = "不支持的字段"
    if case == "ocr":
        table["origin"] = "ocr"
    change = (lambda p: p["documents"][0]["units"][0]["coverage"].update(status="needs_ocr", unread_objects=["image"])) if case == "unread" else None
    result, _ = prepare([table], [scope("personnel_declaration", table)], allowed=case != "no_adoption", parsed_change=change)
    assert not result["material_records"] and result["record_processing"][0]["status"] == status


def test_hash_scope_and_strict_record_models_reject_forgery():
    table = history_table()
    with pytest.raises(MaterialRecordError, match="MATERIAL_SCOPE_HASH_OR_SOURCE_MISMATCH"):
        prepare([table], [{**scope("historical_performance", table), "source_sha256": "b" * 64}])
    with pytest.raises(ValidationError):
        RecordScope.model_validate({**scope("historical_performance", table), "select_all": True})
    result, _ = prepare([table], [scope("historical_performance", table)])
    record = result["material_records"][0]
    record["independently_verified"] = True
    with pytest.raises(ValidationError):
        TypeAdapter(MaterialRecord).validate_python(record)


def test_native_value_quote_and_unit_cannot_be_changed_independently():
    table = history_table()
    result, _ = prepare([table], [scope("historical_performance", table)])
    for mutation in (lambda r: r["fields"]["contract_amount"].update(value="999"), lambda r: r["fields"]["contract_amount"].update(unit="美元")):
        record = copy.deepcopy(result["material_records"][0]); mutation(record)
        with pytest.raises(ValidationError):
            TypeAdapter(MaterialRecord).validate_python(record)


def test_default_scopes_do_not_scan_any_personnel_or_history_table():
    sources, parsed = inputs([personnel_table(), history_table()])
    ctx = Context()
    result = prepare_material_records(ctx, sources, parsed, [], {"BID": "a" * 64})
    assert not result["material_records"] and not result["record_processing"] and not ctx.units


@pytest.mark.parametrize("field,value", [("unit_id", "UNRELATED"), ("block_id", "UNRELATED"), ("locator", "body/other"),
    ("page", 99), ("source_id", "OTHER"), ("source_sha256", "b" * 64), ("table_sha256", "0" * 64)])
def test_field_citation_must_join_the_same_native_table_and_record_row(field, value):
    table = history_table()
    result, _ = prepare([table], [scope("historical_performance", table)])
    record = result["material_records"][0]
    record["fields"]["contract_amount"]["citations"][0][field] = value
    with pytest.raises(ValidationError):
        TypeAdapter(MaterialRecord).validate_python(record)


def test_changing_unit_and_self_reported_header_cannot_override_the_native_grid():
    table = history_table()
    result, _ = prepare([table], [scope("historical_performance", table)])
    for change_header_map in (False, True):
        record = copy.deepcopy(result["material_records"][0])
        amount = record["fields"]["contract_amount"]
        amount["unit"] = "元"
        amount["citations"][0]["header_quotes"] = ["合同额（元）"]
        if change_header_map:
            header = next(header for header in record["table_evidence"][0]["header_map"] if header["field"] == "contract_amount")
            header.update(quotes=["合同额（元）"], unit="元")
        with pytest.raises(ValidationError):
            TypeAdapter(MaterialRecord).validate_python(record)


def test_native_cell_and_record_row_values_must_both_match_the_table_grid():
    table = history_table()
    result, _ = prepare([table], [scope("historical_performance", table)])
    for mode in ("cell", "row"):
        record = copy.deepcopy(result["material_records"][0])
        if mode == "cell":
            amount = record["fields"]["contract_amount"]
            amount["value"] = amount["citations"][0]["quote"] = "999.00"
            amount["citations"][0]["quote_sha256"] = hashlib.sha256(b"999.00").hexdigest()
        else:
            row = record["citations"][0]
            row["row_values"][6] = "999.00"
            row["row_sha256"] = digest(row["row_values"])
        with pytest.raises(ValidationError):
            TypeAdapter(MaterialRecord).validate_python(record)


def test_certificate_field_cannot_borrow_another_declaration_row():
    table = personnel_table()
    result, _ = prepare([table], [scope("personnel_declaration", table)])
    record = result["material_records"][0]
    number = record["fields"]["certificate_declarations"][0]["number"]
    number["value"] = "TEST-TITLE"
    citation = number["citations"][0]
    citation.update(row=3, logical_row=3, quote="TEST-TITLE", quote_sha256=hashlib.sha256(b"TEST-TITLE").hexdigest())
    with pytest.raises(ValidationError):
        TypeAdapter(MaterialRecord).validate_python(record)


def test_certificate_row_must_be_one_exact_row_of_the_person_record():
    table = personnel_table()
    result, _ = prepare([table], [scope("personnel_declaration", table)])
    record = result["material_records"][0]
    record["fields"]["certificate_declarations"][0]["citations"][0]["unit_id"] = "UNRELATED"
    with pytest.raises(ValidationError):
        TypeAdapter(MaterialRecord).validate_python(record)


def test_legitimate_shared_role_merge_has_anchor_support_in_complete_grid():
    table = personnel_table()
    table["merges"] = [merge for merge in table["merges"] if (merge["row"], merge["column"]) != (2, 1)]
    table["merges"].append({"row": 2, "column": 1, "row_span": 3, "col_span": 1})
    table["rows"][4][1] = ""
    result, _ = prepare([table], [scope("personnel_declaration", table)])
    second = result["material_records"][1]
    role = second["fields"]["roles"][0]["citations"][0]
    assert role["row"] == 2 and role["logical_row"] == 4 and role["row_span"] == 3
    assert second["table_evidence"][0]["rows"][2][1] == role["quote"]
    assert TypeAdapter(MaterialRecord).validate_python(second).fields.name.value == "测试乙"


@pytest.mark.parametrize("case", ["grid_hash", "header_map", "unused_location", "fake_merge"])
def test_complete_table_evidence_rejects_internal_contradictions(case):
    table = history_table()
    result, _ = prepare([table], [scope("historical_performance", table)])
    record = result["material_records"][0]
    evidence = record["table_evidence"][0]
    if case == "grid_hash":
        evidence["rows"][0][6] = "合同额（元）"
    elif case == "header_map":
        evidence["header_map"][6]["column"] = 4
    elif case == "unused_location":
        evidence["locations"].append({**evidence["locations"][0], "unit_id": "UNRELATED"})
    else:
        record["fields"]["contract_amount"]["citations"][0]["row_span"] = 2
    with pytest.raises(ValidationError):
        TypeAdapter(MaterialRecord).validate_python(record)
