import copy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from company_bid.intake import IntakeManifest
from company_bid.models import file_sha256
from company_bid.material_records import prepare_material_records


def manifest_with_history(tmp_path):
    original = tmp_path / "synthetic-bid.txt"
    original.write_text("Synthetic historical declaration", encoding="utf-8")
    source = {"source_id": "B", "title": "Synthetic source", "path": str(original), "sha256": file_sha256(original),
              "locator": "document", "kind": "company_material", "provenance": "derived_from_bid", "synthetic": True}
    tender = {**source, "source_id": "T", "kind": "tender", "provenance": "synthetic"}
    table = {"block_id": "TABLE", "source_id": "B", "locator": "body/table[0]", "kind": "table", "origin": "native", "merges": [], "rows": [
        ["序号", "项目名称", "项目负责人", "发包人", "服务地点", "服务日期", "合同额（万元）", "本项目拟投入人员是否参与"],
        ["1", "Synthetic", "Synthetic lead", "Synthetic client", "Synthetic place", "2022", "12.50", "是"]]}
    document = {"source_id": "B", "sha256": source["sha256"], "role": "reference_bid", "units": [{
        "unit_id": "U", "source_id": "B", "locator": "body/table[0]", "page": None,
        "coverage": {"status": "read", "unread_objects": []}, "blocks": [table]}]}
    context = SimpleNamespace(run_unit=lambda identity, inputs, fn: fn(), capture=lambda *args, **kwargs: None)
    record = prepare_material_records(context, [{**source, "role": "reference_bid"}], {"documents": [document]}, [
        {"kind": "historical_performance", "source_id": "B", "source_sha256": source["sha256"], "locator": table["locator"]}],
        {"B": source["sha256"]})["material_records"][0]
    return {"sources": [source, tender], "source_roles": {"B": "reference_bid", "T": "tender"},
            "project": {"project_id": "P", "name": "Synthetic", "source_refs": ["T"]},
            "company": {"company_id": "C", "name": "Synthetic", "source_refs": ["B"]},
            "material_records": [record], "synthetic": True, "preparation_note": "Synthetic citation validation."}


def test_declarations_are_frozen_without_becoming_flat_facts(tmp_path):
    result = IntakeManifest.model_validate(manifest_with_history(tmp_path))
    assert not result.facts and len(result.material_records) == 1
    assert result.material_records[0].current_project_price is False


@pytest.mark.parametrize("failure", ["hash", "unknown_source", "tender", "provenance", "duplicate", "invented_value"])
def test_invalid_declaration_source_or_identity_is_rejected(tmp_path, failure):
    value = manifest_with_history(tmp_path)
    if failure == "hash":
        value["material_records"][0]["fields"]["project_name"]["citations"][0]["source_sha256"] = "2" * 64
    elif failure == "unknown_source":
        value["material_records"][0]["citations"][0]["source_id"] = "UNKNOWN"
    elif failure == "tender":
        value["material_records"][0]["citations"][0]["source_id"] = "T"
    elif failure == "provenance":
        value["sources"][0]["provenance"] = "original"
    elif failure == "duplicate":
        value["material_records"].append(copy.deepcopy(value["material_records"][0]))
    else:
        value["material_records"][0]["fields"]["project_name"]["value"] = "Invented"
    with pytest.raises(ValidationError):
        IntakeManifest.model_validate(value)


@pytest.mark.parametrize("failure", ["cell_unit", "cell_table_hash", "cell_block", "cell_locator", "logical_row", "self_reported_unit"])
def test_native_record_internal_source_contradiction_is_rejected_at_intake(tmp_path, failure):
    value = manifest_with_history(tmp_path)
    amount = value["material_records"][0]["fields"]["contract_amount"]
    citation = amount["citations"][0]
    if failure == "cell_unit":
        citation["unit_id"] = "UNRELATED"
    elif failure == "cell_table_hash":
        citation["table_sha256"] = "0" * 64
    elif failure == "cell_block":
        citation["block_id"] = "UNRELATED"
    elif failure == "cell_locator":
        citation["locator"] = "body/unrelated"
    elif failure == "logical_row":
        citation["logical_row"] = 0
    else:
        amount["unit"] = "元"
        citation["header_quotes"] = ["合同额（元）"]
    with pytest.raises(ValidationError):
        IntakeManifest.model_validate(value)
