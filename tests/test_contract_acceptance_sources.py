"""A reviewed acceptance list crosses windows without replacing their records."""
from copy import deepcopy

import pytest

from company_bid.contract_acceptance_sources import (
    CONTRACT_INSURANCE_ACCEPTANCE_SOURCE_CONFIG_KEY as KEY,
    build_contract_insurance_acceptance_proof,
)
from company_bid.extraction import (
    PlanningError, _index, _validate_normalized_window_result, _windows, extract_requirements,
    extraction_window_payload, validate_live_window,
)
from company_bid.storage import digest
from test_source_selected_citations import Context, block, fixture


def require(*ids, **fields):
    return {"citation": {"block_ids": list(ids)}, "category": "commercial", "response_mode": "procedure",
            "mandatory": True, "format_status": "not_prescribed", **fields}


def reply(window, requirements, unresolved=()):
    return {"window_id": window["window_id"], "processing_declaration": "processed_not_semantically_accepted",
            "unresolved_block_ids": list(unresolved), "format_status": "none_verified", "format_chapters": [],
            "requirements": requirements}


@pytest.fixture
def acceptance_case(tmp_path):
    manifest, parsed = fixture(tmp_path)
    document = parsed["documents"][0]
    document["format"] = "pdf"
    pages = [
        [(f"Z{i}", f"普通段落 {i} 应归档。", 70) for i in range(10)] + [
            ("I0", "乙方应按时交接。乙方负责保障进场人员，", 70),
            ("I1", "购买适当的保险应对受伤后", 70),
            ("I2", "产生的索赔和费用。甲方不对受伤人员承担任何责任和费", 70),
            ("I3", "用。", 70),
            ("I4", "7.6 双方另行核对进场记录。", 95),
            ("F0", "甲方应审核入场手续。", 70),
            ("F1", "乙方应登记临时通行证。", 70),
        ],
        [(f"F{i}", f"补充记录 {i} 应签收。", 70) for i in range(2, 7)] + [
            ("A0", "办理手续的服务费用。", 70),
            ("A1", "(3) 技术服务工作成果的验收标准：【Q_GGW 112.1—2001", 95),
            ("A2", "实验设备管理 第 1 部分：巡检 Q_GGW", 70),
            ("A3", "112.2—2001 实验设备管理 第 2 部分：交接", 70),
            ("A4", "SY/T 1111-2001《状态记录》 SY/T 1112-2001《过程", 70),
            ("A5", "校验办法》 GB/T 1113-2001《设备", 70),
            ("A6", "运行技术规范》 SY/T 1114.1-2001《设备维护", 70),
            ("A7", "第 1 部分：开展巡检》 GB 1115-2001《交接记", 70),
            ("A8", "录》 GB/T 1116-2001《作业区域管理》 GB/T 1117-2001", 70),
            ("A9", "《报告记录方式》】。", 70),
            ("A10", "(4) 技术服务工作成果的验收方法：现场核查。", 95),
        ],
    ]
    for page, rows in enumerate(pages, 1):
        for i, (identity, text, left) in enumerate(rows):
            row = block(parsed, identity, text, bbox=[left, i * 18 + 15, 420, i * 18 + 27], origin="native", page=page)
            row["locator"] = f"pdf/page[{page}]/line[{i}]"
    for unit in document["units"]:
        unit["coverage"]["origin"] = "native"
    base = {"window_max_blocks": 12}
    blocks, _, docs = _index(parsed, manifest)
    (prior, current, future), _ = _windows(docs, blocks, base, manifest=manifest)
    prior_raw = reply(prior, [require("I0", "I1", "I2", "I3")], ["I2"])
    raw = reply(current, [require("I4"), *[require(f"F{i}") for i in range(7)], require("A0")],
                ["I2", "I3", "A1", "A7"])
    insurance = [f"I{i}" for i in range(5)]
    acceptance = [f"A{i}" for i in range(11)]
    confirmed, = validate_live_window(prior_raw, prior)["requirements"]
    scope = {"source_id": "T", "source_sha256": document["sha256"], "window_id": current["window_id"],
             "predecessor_window_id": prior["window_id"], "successor_window_id": future["window_id"],
             "insurance_page": 1, "acceptance_page": 2, "insurance_block_ids": insurance,
             "acceptance_block_ids": acceptance, "adjacent_unresolved_block_id": "A7",
             "raw_candidate_sha256": digest(raw), "predecessor_requirement_sha256": digest(confirmed)}
    scope["source_proof_sha256"] = build_contract_insurance_acceptance_proof(
        scope, [blocks[key] for key in insurance], [blocks[key] for key in acceptance])["proof_sha256"]
    list_ids = acceptance[1:-1]
    future_raw = reply(future, [
        require(*list_ids), require("A3", "A4", "A5"), require("A10"),
        require("A3", "A4", "A5", mandatory=False),
        require("A3", "A4", "A5", category="technical", response_mode="writing"),
        require("A9", "A10"),
    ])
    return manifest, parsed, base, scope, (prior_raw, raw, future_raw), (prior, current, future)


def execute(manifest, parsed, base, scope, replies):
    by_id = {raw["window_id"]: raw for raw in replies}

    def recorded(_ctx, _task, identity, payload, validate):
        assert KEY not in payload["window"] and KEY not in payload["window_config"]
        return validate(by_id[identity])

    return extract_requirements(Context(), parsed, manifest, recorded, {**base, KEY: scope})


def test_acceptance_supplement_keeps_native_quote_and_compatible_overlap_only(acceptance_case):
    manifest, parsed, base, scope, replies, original_windows = acceptance_case
    with pytest.raises(PlanningError, match="LIVE_REQUIRED_BLOCK_UNRESOLVED") as failure:
        validate_live_window(replies[1], original_windows[1])
    assert failure.value.diagnostic_context["block_ids"] == ["I2", "I3", "A1"]
    extracted = execute(manifest, parsed, base, scope, replies)
    prior, current, future = extracted["window_results"]
    assert current["raw_model_candidate"] == replies[1] and future["raw_model_candidate"] == replies[2]
    assert len(current["validated"]["requirements"]) == 9
    assert current["processing"]["unresolved_block_ids"] == ["I2", "I3", "A1", "A7"]
    assert current[KEY]["resolved_block_ids"] == ["I2", "I3", "A1"]
    assert current[KEY]["original_error"]["diagnostic_context"] == failure.value.diagnostic_context

    proof = current[KEY]["proof"]
    exact = current[KEY]["requirement"]["citation"]
    blocks, _, documents = _index(parsed, manifest)
    windows, _ = _windows(documents, blocks, {**base, KEY: scope}, manifest=manifest)
    assert [extraction_window_payload(w, {**base, KEY: scope}) for w in windows] == [
        extraction_window_payload(w, base) for w in original_windows]
    assert exact["quote"] == "\n".join(blocks[identity]["text"] for identity in scope["acceptance_block_ids"][1:-1])
    codes = [row["identifier"] for row in proof["acceptance"]["standards"]]
    assert codes == ["Q_GGW 112.1—2001", "Q_GGW\n112.2—2001", "SY/T 1111-2001", "SY/T 1112-2001",
                     "GB/T 1113-2001", "SY/T 1114.1-2001", "GB 1115-2001", "GB/T 1116-2001", "GB/T 1117-2001"]
    requirements = extracted["requirements"]
    canonical, = [item for item in requirements if item["citations"] == [exact]]
    assert canonical["window_ids"] == [current["window_id"], future["window_id"]]
    assert [item["citations"][0]["block_ids"] for item in requirements[-4:]] == [
        ["A10"], ["A3", "A4", "A5"], ["A3", "A4", "A5"], ["A9", "A10"]]
    assert requirements[-3]["mandatory"] is False and requirements[-2]["response_mode"] == "writing"
    assert requirements[0]["citations"] == [prior["validated"]["requirements"][0]["citation"]]
    assert [row["resolution"] for row in extracted["deferred_context_closure"]["items"]] == [
        "owner_target_source_proven_contract_insurance_acceptance",
        "owner_target_processed_with_complete_preceding_acceptance_clause"]

    current[KEY]["requirement"]["citation"]["quote"] += "改写"
    with pytest.raises(PlanningError, match="EXTRACTION_CONTRACT_INSURANCE_ACCEPTANCE_RESOLUTION_MISMATCH"):
        _validate_normalized_window_result(current, windows[1])


@pytest.mark.parametrize(("alteration", "expected"), [
    ("source", "CONTRACT_INSURANCE_ACCEPTANCE_SOURCE_PROOF_MISMATCH"),
    ("adjacent", "CONTRACT_INSURANCE_ACCEPTANCE_RAW_RESPONSE_MISMATCH"),
    ("prior", "CONTRACT_INSURANCE_ACCEPTANCE_PREDECESSOR_MISMATCH"),
    ("future", "LIVE_REQUIRED_BLOCK_UNRESOLVED"),
    ("candidate", "PROCEDURE_CLASSIFICATION_INVALID"),
])
def test_reviewed_scope_cannot_mask_changed_source_other_candidates_or_owner_failure(acceptance_case, alteration, expected):
    manifest, parsed, base, scope, replies, _ = deepcopy(acceptance_case)
    if alteration == "source":
        line = next(row for row in parsed["documents"][0]["units"][1]["blocks"] if row["block_id"] == "A8")
        line["text"] = line["text"].replace("2001", "2003")
    elif alteration == "adjacent":
        replies[1]["unresolved_block_ids"].pop()
        scope["raw_candidate_sha256"] = digest(replies[1])
    elif alteration == "prior":
        replies[0]["requirements"][0]["citation"]["block_ids"].pop()
    elif alteration == "future":
        replies[2]["unresolved_block_ids"] = ["A7"]
    elif alteration == "candidate":
        replies[1]["requirements"][0]["category"] = "technical"
        scope["raw_candidate_sha256"] = digest(replies[1])
    with pytest.raises(PlanningError, match=expected):
        execute(manifest, parsed, base, scope, replies)
