"""Use native numbered contract clauses to verify interrupted N2 selections."""

import re
from collections import defaultdict

from .storage import digest


INDEX_OUTPUT_KEY = "contract_clause_indexes"
WINDOW_CONTEXT_KEY = "contract_clause_boundary_context"
RESOLUTION_KEY = "contract_clause_source_resolution"


def bind_contract_clause_windows(windows, indexes, blocks, footer_skips):
    """Project complete, two-owner clauses privately, without changing the wire."""
    owners = {block["block_id"]: index for index, window in enumerate(windows) for block in window["blocks"]}
    by_ordinal = {block["ordinal"]: block for block in blocks.values()}
    for index in indexes:
        for clause in index["entries"]:
            if not clause["complete"] or len(clause["block_ids"]) > 24:
                continue
            ids = clause["block_ids"]
            owner_indices = list(dict.fromkeys(owners.get(identity) for identity in ids))
            if (len(owner_indices) != 2 or None in owner_indices or owner_indices[1] != owner_indices[0] + 1 or
                    any(span["kind"] != "paragraph" or span["text"] != blocks[span["block_id"]]["text"] for span in clause["spans"])):
                continue
            left, right = (windows[position] for position in owner_indices)
            if any(window["source_id"] != index["source_id"] or window["source_sha256"] != index["source_sha256"]
                   for window in (left, right)):
                continue
            start, end = (blocks[identity]["ordinal"] for identity in (ids[0], ids[-1]))
            skipped = [by_ordinal[position] for position in range(start + 1, end) if by_ordinal[position]["block_id"] not in ids]
            if any(block["block_id"] not in footer_skips for block in skipped):
                continue
            members = {identity: blocks[identity] for identity in ids}
            if any(block.get("read_disposition") or block.get("illustration_source_use") for block in members.values()):
                continue
            view = {"index_sha256": index["index_sha256"], "clause": clause, "members": members,
                    "skipped": {block["block_id"]: block for block in skipped},
                    "skips": {block["block_id"]: footer_skips[block["block_id"]] for block in skipped},
                    "opening_window_id": left["window_id"], "closing_window_id": right["window_id"],
                    "opening_ids": [identity for identity in ids if owners[identity] == owner_indices[0]],
                    "closing_ids": [identity for identity in ids if owners[identity] == owner_indices[1]]}
            for window in (left, right):
                window.setdefault(WINDOW_CONTEXT_KEY, []).append(view)


_NUMBER = r"(?:\d+|[零〇一二两三四五六七八九十百]+)(?:[（(]\d+[）)])?"
_AMOUNT = rf"(?:【{_NUMBER}】|\[{_NUMBER}\]|{_NUMBER})"
_DEADLINE = r"(?:立即|及时|(?:不超过|最迟在|于|在)[^。！？；]{0,36}" + _AMOUNT + r"(?:个工作日|工作日|小时|日|天)(?:之)?内)"
_COUNTERPART = r"(?:另一方|对方|甲方|乙方)"
_TIMED_NOTICE = re.compile(_DEADLINE + r"(?:以[^。，,！？；]{1,14}(?:方式|形式)|书面|口头|电话)?(?:通知|告知|报告)" + _COUNTERPART)
_TIMED_EVIDENCE = re.compile(_DEADLINE + r"[^。！？；]{0,18}(?:向|交)" + _COUNTERPART + r"[^。！？；]{0,12}(?:提供|提交|送交)[^。！？；]{0,32}(?:文件|报告|凭证|证明|材料|记录)")
_CONSEQUENCE = re.compile(r"(?:未|没有|不)[^。！？；]{0,45}(?:应|须)[^。！？；]{0,42}(?:承担|赔偿|支付)[^。！？；]{0,32}[。！？]$")
_BIDDING_OR_FORM = re.compile(
    r"资格|投标|响应文件|报价|评分|废标|模板|按[《〈]|附件[^。；]{0,8}(?:格式|提供|提交|填)|"
    r"[【\[]\s*(?:/|／|_+)?\s*[】\]]|[☑□☒]|[（(][一二三四五六七八九1-9][）)](?![】\]])"
)
_OPTIONAL_DIRECTION = re.compile(r"无需|不必|不须|无须|无义务|不得|不应|可不|可以|有权|自愿|另有约定|除外")
_PRECEDING_ACTION = re.compile(
    r"(?:应当|应|须)(?:(?:记录|登记|保存|保留|留存)[^。！？；，,]{1,20}[，,]|"
    r"采取[^。！？；，,]{0,20}措施(?:[，,](?:避免|减少|降低|尽量|将)[^。！？；，,]{1,24})*[，,])?$"
)
_OTHER_DUTY = re.compile(r"必须|须|不得|(?<![相供响反适])应(?![急用答对])")


def _contract_administration_basis(clause):
    """Admit explicit contractual notice, evidence and consequence, or abstain."""
    boundary = clause.get("next_boundary") or {}
    if clause.get("items") or (boundary.get("clause_number") or "").startswith(clause["clause_number"] + "."):
        return None  # The unindexed effect of parent text on children is ambiguous here.
    text = "".join(clause["quote"].split())
    if _BIDDING_OR_FORM.search(text) or not re.search(r"合同[^。！？；]{0,12}(?:义务|履行)", text):
        return None
    sentences = [match.group() for match in re.finditer(r"[^。！？]+[。！？]", text)]
    if len(sentences) != 2 or "".join(sentences) != text:
        return None
    direction, consequence = sentences
    if _OPTIONAL_DIRECTION.search(direction):
        return None
    notice = _TIMED_NOTICE.search(direction)
    if not notice or not _CONSEQUENCE.search(consequence):
        return None
    action = _PRECEDING_ACTION.search(direction[:notice.start()])
    evidence = _TIMED_EVIDENCE.search(direction, notice.end())
    if (not action or _OTHER_DUTY.search(direction[:action.start()]) or not evidence or
            not re.fullmatch(r"[，,](?:并|并且|同时|且)?", direction[notice.end():evidence.start()]) or
            direction[evidence.end():] not in {"。", "！", "？"}):
        return None
    return "contract_performance_notice_evidence_and_consequence_v1"


PERFORMANCE_EVIDENCE_POLICY = "indexed_performance_evidence_v1"


def performance_evidence_scopes(indexes):
    """Identify what must be furnished to the counterparty under a complete contract clause."""
    scopes = {}
    for index in indexes:
        for clause in index["entries"]:
            if not clause["complete"] or any(span["kind"] != "paragraph" for span in clause["spans"]):
                continue
            basis = _contract_administration_basis(clause)
            if not basis:
                continue
            text = "".join(clause["quote"].split())
            notice = _TIMED_NOTICE.search(text)
            evidence = _TIMED_EVIDENCE.search(text, notice.end())
            document = re.split(r"提供|提交|送交", evidence.group())[-1]
            key = (clause["source_id"], tuple(clause["block_ids"]), clause["quote_sha256"])
            scopes[key] = {"stage": "contract_performance", "basis": basis, "document": document,
                           "clause_id": clause["clause_id"], "clause_proof_sha256": clause["proof_sha256"],
                           "index_sha256": index["index_sha256"]}
    return scopes


def performance_evidence_scope(candidate, scopes):
    if (not scopes or candidate["category"] != "commercial" or candidate["response_mode"] != "procedure"
            or candidate["format_status"] != "not_prescribed" or candidate["required_fact_labels"]
            or candidate["reference_keywords"]):
        return None
    citation = candidate["citation"]
    key = (citation["source_id"], tuple(citation.get("block_ids") or [citation["block_id"]]),
           citation.get("quote_sha256"))
    scope = scopes.get(key)
    if scope and all("".join(label.split()) in scope["document"] for label in candidate["attachment_keywords"]):
        return scope
    return None


def _exact_clause_citation(entry, window):
    # Import locally to reuse the ordinary contiguous source-selection guard.
    from .extraction import PlanningError, _citation

    clause = entry["clause"]
    if (not clause["complete"] or clause["source_id"] != window["source_id"] or clause["source_sha256"] != window["source_sha256"] or
            clause["proof_sha256"] != digest({key: value for key, value in clause.items() if key != "proof_sha256"})):
        return None
    target = entry["opening_ids"] if entry["opening_window_id"] == window["window_id"] else entry["closing_ids"]
    local = {block["block_id"]: block for block in window["blocks"]}
    if any(identity not in local or any(local[identity].get(key) != entry["members"][identity].get(key)
                                       for key in ("source_id", "locator", "unit_id", "ordinal", "text", "kind"))
           for identity in target):
        return None
    ids = clause["block_ids"]
    if ids != entry["opening_ids"] + entry["closing_ids"]:
        return None
    anchor = entry["members"][ids[0]]
    try:
        citation = _citation({"source_id": anchor["source_id"], "block_id": ids[0], "locator": anchor["locator"],
                              "block_ids": ids, "clause_id": None, "table_row_id": None, "quote": None},
                             entry["members"] | entry["skipped"], entry["skips"])
    except PlanningError:
        return None
    return citation if citation["quote"] == clause["quote"] and citation["quote_sha256"] == clause["quote_sha256"] else None


def _matches_clause(requirement, citation):
    actual = requirement.get("citation", {})
    return all(actual.get(key) == citation[key] for key in (
        "source_id", "block_id", "locator", "block_ids", "end_locator", "quote", "quote_sha256", "start", "end", "skipped_block_ids"
    )) and actual.get("clause_id") is None and actual.get("table_row_id") is None


def _prior_witness(predecessor, expected_id, citation, clause_id):
    if not isinstance(predecessor, dict) or predecessor.get("status") != "complete" or predecessor.get("window_id") != expected_id:
        return None
    raw = predecessor.get("raw_model_candidate") or {}
    raw_hash = digest(raw)
    if (raw.get("window_id") != expected_id or
            (predecessor.get("live_confirmation") or {}).get("raw_structured_candidate_sha256") != raw_hash):
        return None
    actual = predecessor.get("validated", {}).get("requirements", [])
    originals = raw.get("requirements", [])
    matches = []
    for index, candidate in enumerate(actual):
        if index >= len(originals) or not _matches_clause(candidate, citation):
            continue
        original = originals[index]
        selector = original.get("citation", {})
        if (selector.get("block_ids") == citation["block_ids"] and
                all(key not in selector or selector[key] == citation[key] for key in ("source_id", "block_id", "locator")) and
                all(original.get(field, candidate[field]) == candidate[field] for field in
                    ("category", "response_mode", "mandatory", "format_status", "format_title", "required_fact_labels",
                     "attachment_keywords"))):
            matches.append(("validated_model_requirement", candidate))
    prior_resolution = predecessor.get(RESOLUTION_KEY) or {}
    if prior_resolution.get("raw_candidate_sha256") == raw_hash:
        for item in prior_resolution.get("items", []):
            requirement = item.get("requirement")
            if (item.get("clause_id") == clause_id and item.get("resolution") == "complete_clause_from_frozen_owners" and
                    requirement and _matches_clause(requirement, citation)):
                matches.append(("validated_source_requirement", requirement))
    if len(matches) != 1:
        return None
    (basis, requirement), = matches
    if not requirement.get("mandatory") or requirement.get("format_status") in {"unread", "conflict"}:
        return None
    return {"basis": basis, "window_id": expected_id, "raw_candidate_sha256": raw_hash,
            "requirement_sha256": digest(requirement), "category": requirement["category"],
            "response_mode": requirement["response_mode"]}


def resolve_contract_clause_boundaries(raw, data, window, critical, predecessor_result=None):
    """Prove every critical fragment and preserve the unmodified model inventory."""
    if not critical or not window.get(WINDOW_CONTEXT_KEY):
        return None
    current_id = window["window_id"]
    reported = set(critical)
    by_block = defaultdict(list)
    for entry in window[WINDOW_CONTEXT_KEY]:
        current_ids = entry["opening_ids"] if entry["opening_window_id"] == current_id else entry["closing_ids"]
        if set(current_ids) <= reported:
            for identity in current_ids:
                by_block[identity].append(entry)
    if any(len(by_block[identity]) != 1 for identity in critical):
        return None
    selected = {identity for requirement in [*data["requirements"], *data["format_chapters"]]
                for citation in [requirement["citation"]]
                for identity in (citation["block_ids"] or [citation["block_id"]])}
    selected.update(identity for chapter in data["format_chapters"] for identity in chapter["content_block_ids"])
    items = []
    seen = set()
    for identity in critical:
        entry, = by_block[identity]
        clause = entry["clause"]
        clause_id = clause["clause_id"]
        if clause_id in seen:
            continue
        seen.add(clause_id)
        if selected.intersection(clause["block_ids"]):
            return None
        citation = _exact_clause_citation(entry, window)
        if citation is None:
            return None
        item = {"clause_id": clause_id, "clause_proof_sha256": clause["proof_sha256"],
                "index_sha256": entry["index_sha256"], "opening_window_id": entry["opening_window_id"],
                "closing_window_id": entry["closing_window_id"], "citation": citation}
        if entry["closing_window_id"] == current_id:
            if callable(predecessor_result):
                predecessor_result = predecessor_result()
            witness = _prior_witness(predecessor_result, entry["opening_window_id"], citation, clause_id)
            if witness is None:
                return None
            item.update(resolution="covered_by_preceding_checkpoint", witness=witness, resolved_block_ids=entry["closing_ids"])
        else:
            basis = _contract_administration_basis(clause)
            if basis is None:
                return None
            requirement = {"citation": {**citation, "label_grounding": []}, "category": "commercial",
                           "response_mode": "procedure", "mandatory": True, "confidence": "high",
                           "format_status": "not_prescribed", "format_title": None,
                           "required_fact_labels": [], "attachment_keywords": [], "reference_keywords": []}
            item.update(resolution="complete_clause_from_frozen_owners", route_basis=basis,
                        requirement=requirement, resolved_block_ids=entry["opening_ids"])
        items.append(item)
    return {"rule": "native_numbered_contract_window_boundary_v1", "raw_candidate_sha256": digest(raw),
            "window_id": current_id, "resolved_block_ids": list(critical), "items": items,
            "semantic_review": "requires_source_review"}


def verify_contract_clause_predecessor(predecessor, resolution):
    """Recheck the merge witness when consuming a successful result."""
    for item in resolution["items"]:
        if item["resolution"] == "covered_by_preceding_checkpoint":
            witness = _prior_witness(predecessor, item["opening_window_id"], item["citation"], item["clause_id"])
            if witness is None or item["witness"] != witness:
                return False
    return True
