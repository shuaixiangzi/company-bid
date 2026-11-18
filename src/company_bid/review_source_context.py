"""Source-bound corrections for the checklist display."""
from __future__ import annotations

import re

from .observability import redact_payload


_ISSUE_RULE = "append_scoped_stage_context_preserve_missing_conditions_and_provenance"
_STAGE_REDACTION = re.compile(r"【([^｜]+)｜[^\r\n]+\[REDACTED_URL\]")
_STAGE_STATUS = (
    "词典阶段：表达未支持，未检索；"
    "语义目录补查：已补查冻结目录，未找到可用候选（复用历史响应）；"
    "原件读取后匹配：未形成候选。"
)


def _unchanged_stage_context(context, requirement):
    """Accept only the stage combination whose rendering can be reconstructed."""
    semantic = context.get("semantic_recall", {})
    current = context.get("current_content_matching", {})
    return (
        context.get("original_requirement") == requirement.text
        and context.get("deterministic_search_status") == "not_searched"
        and semantic.get("requirement_id") == requirement.requirement_id
        and semantic.get("catalog_stage_dictionary_search_status") == "not_searched"
        and semantic.get("catalog_stage_dictionary_candidate_group_ids") == []
        and semantic.get("catalog_stage_status") == semantic.get("status") == "no_candidate_in_frozen_catalog"
        and semantic.get("historical_response_reused") is True
        and semantic.get("model_called") is False
        and semantic.get("candidates") == []
        and semantic.get("qualification_result") == "not_determined"
        and semantic.get("absence_conclusion") == "not_established"
        and current.get("candidate_group_ids") == []
    )


def material_stage_display(reason, issue, catalog, material_recall_changes):
    """Recover a redacted generated stage line from its exact N3 issue context."""
    if "[REDACTED_URL]" not in reason or not material_recall_changes:
        return reason, [], []
    changes = [row for row in material_recall_changes if row.get("issue_id") == issue.issue_id]
    if len(changes) != 1:
        return reason, [], []
    recorded = changes[0]
    if recorded.get("rule") != _ISSUE_RULE or recorded.get("after") != issue.model_dump(mode="json"):
        return reason, [], []
    before = recorded.get("before", {}).get("reason")
    if not isinstance(before, str) or not issue.reason.startswith(before + "\n"):
        return reason, [], []
    original_lines = issue.reason.splitlines()
    display_lines = reason.splitlines(keepends=True)
    if len(original_lines) != len(display_lines):
        return reason, [], []

    transformations, quote_ids = [], []
    for number in range(len(before.splitlines()), len(original_lines)):
        original = original_lines[number]
        match = _STAGE_REDACTION.fullmatch(original)
        display_line = display_lines[number]
        if not match or display_line.rstrip("\r\n") != original or original_lines.count(original) != 1:
            continue
        rid = match[1]
        if rid not in issue.requirement_refs or rid not in recorded.get("requirement_refs", []):
            continue
        contexts = [row for row in recorded.get("requirement_contexts", []) if row.get("requirement_id") == rid]
        requirement = catalog["requirements"][rid]
        if len(contexts) != 1 or not _unchanged_stage_context(contexts[0], requirement):
            continue

        source_label = " ".join(requirement.text.split())
        shortened = source_label[:36] + ("…" if len(source_label) > 36 else "")
        raw_header = f"【{rid}｜{shortened}】"
        # Reproduce the URL scrubber's exact loss of the generated suffix.
        # A token in a user statement or a different redaction is ineligible.
        url = re.search(r"https?://", shortened)
        if url is None or redact_payload(raw_header + _STAGE_STATUS) != original:
            continue
        readable = shortened[:url.start()].rstrip(" （(") + "…"
        qid = catalog["requirement_quote_ids"][rid]
        display_header = f"【{qid}｜{readable}】"
        display = display_header + _STAGE_STATUS
        display_lines[number] = display + display_line[len(original):]
        quote_ids.append(qid)
        transformations.append({
            "field": "reason", "rule": "material_requirement_header_source_id_v1",
            "basis": "n3_material_stage_line_redaction_recovery_v1",
            "original_text": original, "display_text": display, "occurrences": 1,
            "source_line_index": number, "requirement_id": rid, "quote_id": qid,
            "source_refs": list(requirement.source_refs),
        })
    return "".join(display_lines), transformations, list(dict.fromkeys(quote_ids))
