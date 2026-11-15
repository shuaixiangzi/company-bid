"""Derive an explicit project number from frozen tender text, never a past bid."""
from __future__ import annotations

import re

from .storage import digest


def derive_project_number(parsed, manifest, facts, scoped_source):
    from .extraction import PlanningError
    sources = {source["source_id"]: source for source in manifest["sources"]}
    members = []
    for document in parsed["documents"]:
        source = sources.get(document["source_id"])
        if document.get("role") != "tender" or not source or source["kind"] != "tender":
            continue
        if document["sha256"] != source["sha256"]:
            raise PlanningError("PROJECT_NUMBER_SOURCE_CHANGED")
        for unit in document["units"]:
            for block in unit["blocks"]:
                if (block["kind"] != "paragraph" or block.get("origin") != "native"
                        or block.get("read_disposition")):
                    continue
                text = block["text"]
                if (text.startswith("（") and text.endswith("）")) or (text.startswith("(") and text.endswith(")")):
                    text = text[1:-1]
                match = re.fullmatch(r"项目编号[：:]\s*([A-Za-z0-9][A-Za-z0-9._/-]{2,127})", text)
                if not match or not any(c.isdigit() for c in match[1]):
                    continue
                members.append({"source_id": document["source_id"], "block_id": block["block_id"],
                    "locator": block["locator"], "quote": block["text"], "value": match[1],
                    "source_sha256": source["sha256"]})
    values = {member["value"] for member in members}
    report = {"rule": "explicit_native_project_number_v1", "members": members,
              "status": "not_found" if not members else "source_conflict" if len(values) != 1 else "source_proven"}
    if len(values) != 1:
        if values:
            raise PlanningError("PROJECT_NUMBER_SOURCE_CONFLICT")
        return [], report
    value = next(iter(values))
    existing = [fact for fact in facts if fact["label"] == "项目编号"]
    if any(fact["value"] != value for fact in existing):
        raise PlanningError("PROJECT_NUMBER_FACT_CONFLICT")
    citations = [{key: member[key] for key in ("source_id", "block_id", "locator", "quote")} for member in members]
    fact = {"fact_id": "F-PROJECT-NUMBER-" + digest(members)[:20], "label": "项目编号", "value": value,
            "source_refs": list(dict.fromkeys(scoped_source(citation) for citation in citations))}
    report.update(fact_id=fact["fact_id"], source_refs=fact["source_refs"], value_sha256=digest(value))
    return [fact], report
