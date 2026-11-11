from copy import deepcopy

import pytest
from pydantic import ValidationError

from company_bid.models import PreparedBid, ProposalContent
from company_bid.sample import create_synthetic_sample


def test_manual_prepared_input_round_trips_and_sources_are_explicit(tmp_path):
    prepared = create_synthetic_sample(tmp_path / "input")
    restored = PreparedBid.model_validate_json(prepared.model_dump_json())
    assert restored == prepared
    assert restored.input_kind == "manual_prepared"
    assert all(fact.source_refs for fact in restored.facts)
    assert restored.issues[0].marker == "【待补 M-001：服务总价】"
    assert all(source.synthetic for source in restored.sources)


def test_technical_provider_cannot_invent_fact_ids_or_document_blocks():
    with pytest.raises(ValidationError):
        ProposalContent.model_validate({"paragraphs": ["拟先核对作业许可。"], "fact_refs": ["F-INVENTED"]})
    with pytest.raises(ValidationError):
        ProposalContent.model_validate({"paragraphs": ["拟安排异常复测。"], "blocks": [{"kind": "table"}]})


def test_unknown_fact_reference_is_rejected(tmp_path):
    data = create_synthetic_sample(tmp_path / "input").model_dump()
    data["sections"][0]["fact_refs"].append("F-UNKNOWN")
    with pytest.raises(ValidationError, match="unknown fact reference"):
        PreparedBid.model_validate(data)


def test_requirement_without_planned_response_is_rejected(tmp_path):
    data = create_synthetic_sample(tmp_path / "input").model_dump()
    data["sections"][1]["requirement_refs"] = ["R-001"]
    with pytest.raises(ValidationError, match="every requirement"):
        PreparedBid.model_validate(data)


def test_fixed_table_shape_and_original_field_order_are_not_silently_repaired(tmp_path):
    data = create_synthetic_sample(tmp_path / "input").model_dump()
    broken = deepcopy(data)
    broken["tables"][0]["rows"][0].pop()
    with pytest.raises(ValidationError, match="original column count"):
        PreparedBid.model_validate(broken)


def test_source_less_company_fact_is_rejected(tmp_path):
    data = create_synthetic_sample(tmp_path / "input").model_dump()
    data["facts"][0]["source_refs"] = []
    with pytest.raises(ValidationError):
        PreparedBid.model_validate(data)


def test_fixed_table_merge_cannot_discard_content(tmp_path):
    data = create_synthetic_sample(tmp_path / "input").model_dump()
    data["tables"][0]["merges"] = [{"row": 0, "column": 0, "col_span": 2}]
    with pytest.raises(ValidationError, match="covered merged cells must be empty"):
        PreparedBid.model_validate(data)
