"""Explain declaration candidates without certifying or assigning resources."""
from __future__ import annotations

import re
from collections import Counter


def material_candidate_report(requirements: list[dict], records: list[dict]) -> dict:
    """The search scope is the frozen prepared records, never the entire company."""
    queries = []
    for requirement in requirements:
        text = requirement["text"]
        kinds = []
        if re.search(r"人员|项目负责人|项目经理|职称|检测员|工程师", text):
            kinds.append("personnel_declaration")
        if re.search(r"业绩|类似项目|类似合同|已完成项目|历史项目", text):
            kinds.append("historical_performance")
        for kind in kinds:
            selected = [record for record in records if record["kind"] == kind]
            usable = [record["record_id"] for record in selected if record["status"] == "usable_provisional"]
            queries.append({"requirement_id": requirement["requirement_id"], "kind": kind,
                            "status": "declarations_require_applicability_review" if usable else "no_usable_prepared_declarations",
                            "candidate_record_ids": usable,
                            "excluded_records": [{"record_id": record["record_id"], "status": record["status"]}
                                                 for record in selected if record["status"] != "usable_provisional"],
                            "qualification_result": "not_determined", "project_assignment": "not_made"})
    return {"scope": "frozen_prepared_declarations_only", "counts": dict(Counter(record["kind"] for record in records)),
            "queries": queries, "unparsed_catalog_is_not_evidence_of_company_absence": True}
