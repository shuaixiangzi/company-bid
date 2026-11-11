from company_bid.material_matching import material_candidate_report


def test_declared_candidates_are_not_promoted_to_qualifications_or_assignments():
    report = material_candidate_report([
        {"requirement_id": "R1", "text": "人员须持有有效职称证书。"},
        {"requirement_id": "R2", "text": "须提交类似项目业绩。"},
    ], [{"record_id": "P1", "kind": "personnel_declaration", "status": "usable_provisional"},
        {"record_id": "P2", "kind": "personnel_declaration", "status": "conflict"},
        {"record_id": "H1", "kind": "historical_performance", "status": "unresolved"}])
    people, history = report["queries"]
    assert people["candidate_record_ids"] == ["P1"]
    assert people["excluded_records"] == [{"record_id": "P2", "status": "conflict"}]
    assert history["candidate_record_ids"] == [] and history["status"] == "no_usable_prepared_declarations"
    assert all(row["qualification_result"] == "not_determined" and row["project_assignment"] == "not_made" for row in report["queries"])
    assert report["unparsed_catalog_is_not_evidence_of_company_absence"] is True
