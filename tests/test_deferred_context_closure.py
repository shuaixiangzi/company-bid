"""Window handoffs close against frozen target owners, never semantic claims."""
import copy

import pytest

from company_bid.extraction import (
    PlanningError, _close_deferred_context, _index, _windows,
    extract_requirements, validate_live_window,
)
from company_bid.planning import plan_bid
from test_source_selected_citations import Context, fixture, block, selected, live_candidate


def prepared_windows(tmp_path, *, deferrals=None):
    manifest, parsed = fixture(tmp_path)
    source = block(parsed, "A", "应说明许可、测点编号与巡检方法。")
    block(parsed, "B", "应说明异常复测和记录交付方法。")
    block(parsed, "C", "应说明质量检查与复核方法。")
    deferrals = deferrals or {}
    def call(ctx, task, unit, payload, validate):
        first = payload["window"]["blocks"][0]
        raw = live_candidate(payload, selected(first))
        raw["unresolved_block_ids"] = deferrals.get(first["block_id"], [])
        return validate(raw)
    output = extract_requirements(Context(), parsed, manifest, call, {"window_max_blocks": 1})
    blocks, _, documents = _index(parsed, manifest)
    windows = _windows(documents, blocks, {"window_max_blocks": 1})[0]
    return manifest, parsed, windows, output


def no_call(*args):
    pytest.fail("Deferred closure validation must not make model calls")


@pytest.mark.parametrize("deferrals", [{"A": ["B"]}, {"C": ["B"]}, {"A": ["B"], "C": ["B"]}])
def test_forward_and_backward_context_close_only_when_owner_target_processed(tmp_path, deferrals):
    manifest, parsed, windows, output = prepared_windows(tmp_path, deferrals=deferrals)
    closure = output["deferred_context_closure"]
    assert closure["schema_version"] == "deferred-context-closure-v1"
    assert closure["status"] == "structurally_closed"
    assert closure["semantic_review"] == "requires_source_review"
    owner = next(w["window_id"] for w in windows if w["blocks"][0]["block_id"] == "B")
    assert len(closure["items"]) == sum(len(ids) for ids in deferrals.values())
    assert all(item["owner_window_id"] == owner for item in closure["items"])
    assert all(item["resolution"] == "owner_target_structurally_processed" for item in closure["items"])
    assert all(item["semantic_review"] == "requires_source_review" for item in closure["items"])
    for result in output["window_results"]:
        assert all(row["status"] == "deferred_context" for row in result["processing"]["deferred_context"])
        assert "owner_window_id" not in result["processing"]
    assert plan_bid(Context(), output, parsed, manifest, no_call, {})["requirements"]


def test_no_deferred_context_has_versioned_empty_closure_without_semantic_acceptance(tmp_path):
    _, _, windows, output = prepared_windows(tmp_path)
    closure = output["deferred_context_closure"]
    assert closure == {"schema_version": "deferred-context-closure-v1", "status": "structurally_closed",
        "basis": "all_frozen_target_windows_processed_v1", "semantic_review": "requires_source_review",
        "target_window_count": len(windows), "completed_target_window_count": len(windows), "items": []}


@pytest.mark.parametrize("case", ["owner_missing", "owner_running", "result_duplicate", "owner_target_unresolved", "cyclic_targets"])
def test_incomplete_owners_and_own_target_deferral_cannot_close(tmp_path, case):
    _, _, windows, output = prepared_windows(tmp_path, deferrals={"A": ["B"]})
    results = copy.deepcopy(output["window_results"])
    owner = next(r for r in results if r["candidate"]["reviewed_block_ids"] == ["B"])
    if case == "owner_missing": results.remove(owner)
    elif case == "owner_running": owner["status"] = "running"
    elif case == "result_duplicate": results.append(copy.deepcopy(owner))
    elif case == "owner_target_unresolved": owner["raw_model_candidate"]["unresolved_block_ids"] = ["B"]
    else:
        for result in results[:2]:
            result["raw_model_candidate"]["unresolved_block_ids"] = ["A", "B"]
    with pytest.raises(PlanningError):
        _close_deferred_context(windows, results)


def test_pending_unit_cannot_create_closed_output_before_owner_finishes(tmp_path):
    manifest, parsed = fixture(tmp_path)
    block(parsed, "A", "应说明巡检方法。")
    block(parsed, "B", "应说明异常复测方法。")
    ctx = Context()
    def call(ctx, task, unit, payload, validate):
        source = payload["window"]["blocks"][0]
        if source["block_id"] == "B":
            raise PlanningError("SYNTHETIC_OWNER_NOT_FINISHED")
        raw = live_candidate(payload, selected(source)); raw["unresolved_block_ids"] = ["B"]
        return validate(raw)
    with pytest.raises(PlanningError, match="SYNTHETIC_OWNER_NOT_FINISHED"):
        extract_requirements(ctx, parsed, manifest, call, {"window_max_blocks": 1})
    assert len(ctx.units) == 1
    pending = next(iter(ctx.units.values()))[1]
    assert pending["processing"]["deferred_context"][0]["status"] == "deferred_context"
    assert "deferred_context_closure" not in pending
    assert "n2-coverage-report" not in ctx.captures


@pytest.mark.parametrize("case", ["duplicate_owner", "cross_source_owner", "missing_frozen_owner"])
def test_ownership_is_unique_same_source_and_program_reconstructed(tmp_path, case):
    _, _, windows, output = prepared_windows(tmp_path, deferrals={"A": ["B"]})
    frozen = copy.deepcopy(windows)
    if case == "duplicate_owner": frozen[2]["blocks"].append(copy.deepcopy(frozen[1]["blocks"][0]))
    elif case == "cross_source_owner": frozen[1]["blocks"][0]["source_id"] = "OTHER-SOURCE"
    else:
        # Valid caller receipt for A's adjacent B must not close without B's
        # frozen target owner, even if a saved closure claims it was complete.
        frozen[1]["blocks"] = []
    with pytest.raises(PlanningError):
        _close_deferred_context(frozen, output["window_results"])


@pytest.mark.parametrize("case", ["wrong_owner", "wrong_source", "wrong_locator", "wrong_source_hash", "premature_count", "duplicate_item", "semantic_accepted", "unit_claim_closed", "missing_closure"])
def test_n3_does_not_trust_forged_saved_ownership_or_closure(tmp_path, case):
    manifest, parsed, windows, output = prepared_windows(tmp_path, deferrals={"A": ["B"]})
    closure = output["deferred_context_closure"]
    item = closure["items"][0]
    if case == "wrong_owner": item["owner_window_id"] = windows[2]["window_id"]
    elif case == "wrong_source": item["source_id"] = "OTHER"
    elif case == "wrong_locator": item["locator"] = "pdf/page[999]"
    elif case == "wrong_source_hash": item["source_sha256"] = "0" * 64
    elif case == "premature_count": closure["completed_target_window_count"] = 1
    elif case == "duplicate_item": closure["items"].append(copy.deepcopy(item))
    elif case == "semantic_accepted": closure["semantic_review"] = "accepted"
    elif case == "unit_claim_closed": output["window_results"][0]["processing"]["deferred_context"][0]["status"] = "closed"
    else: output.pop("deferred_context_closure")
    with pytest.raises(PlanningError):
        plan_bid(Context(), output, parsed, manifest, no_call, {})


@pytest.mark.parametrize("unresolved,code", [(["UNKNOWN"], "LIVE_UNRESOLVED_BLOCK_IDS_INVALID"),
    (["B", "B"], "LIVE_UNRESOLVED_BLOCK_IDS_INVALID"), (["A"], "LIVE_REQUIRED_BLOCK_UNRESOLVED")])
def test_unknown_duplicate_and_current_target_remain_strict(tmp_path, unresolved, code):
    _, _, windows, output = prepared_windows(tmp_path)
    raw = copy.deepcopy(output["window_results"][0]["raw_model_candidate"])
    raw["unresolved_block_ids"] = unresolved
    with pytest.raises(PlanningError, match=code): validate_live_window(raw, windows[0])


def test_catalog_only_id_cannot_be_deferred(tmp_path):
    manifest, parsed = fixture(tmp_path)
    first = block(parsed, "A", "应说明巡检方法。")
    for index in range(9): block(parsed, f"B{index}", "本段为背景资料。")
    later = block(parsed, "FORM", "第二章 投标文件格式")
    blocks, _, documents = _index(parsed, manifest)
    window = _windows(documents, blocks, {"window_max_blocks": 1})[0][0]
    assert later["block_id"] in {r["block_id"] for r in window["source_heading_catalog"]}
    raw = live_candidate({"window": window}, selected(first)); raw["unresolved_block_ids"] = [later["block_id"]]
    with pytest.raises(PlanningError, match="LIVE_UNRESOLVED_BLOCK_IDS_INVALID"): validate_live_window(raw, window)


@pytest.mark.parametrize("case", ["missing_owner_result", "running_owner", "missing_all_results", "duplicate_owner", "forged_assignment"])
def test_closure_errors_locate_frozen_window_block_owner_and_counts_without_text(tmp_path, case):
    _, _, windows, output = prepared_windows(tmp_path, deferrals={"A": ["B"]})
    results = copy.deepcopy(output["window_results"])
    origin, owner, other = [w["window_id"] for w in windows]
    if case == "missing_owner_result": results.pop(1)
    elif case == "running_owner": results[1]["status"] = "running"
    elif case == "missing_all_results": results = []
    elif case == "duplicate_owner": windows[2]["blocks"].append(copy.deepcopy(windows[1]["blocks"][0]))
    else: results[0]["processing"]["deferred_context"][0]["owner_window_id"] = other
    with pytest.raises(PlanningError) as failure:
        _close_deferred_context(windows, results)
    error = failure.value
    context = error.diagnostic_context
    assert context["source_id"] == "T"
    assert str(error) == error.code
    if case == "missing_all_results":
        assert context["window_id"] == origin and context["owner_window_id"] == origin
        assert context["expected_count"] == 3 and context["actual_count"] == 0
    else:
        assert context["window_id"] == (other if case == "duplicate_owner" else origin)
        assert context["block_id"] == "B" and context["owner_window_id"] == owner
    if case in {"missing_owner_result", "running_owner"}:
        assert context["expected_count"] == 3
        assert context["actual_count"] == (2 if case == "missing_owner_result" else 3)
    assert set(context) <= {"window_id", "block_id", "owner_window_id", "source_id", "expected_count", "actual_count"}


def test_closure_diagnostics_are_bounded_ids_and_do_not_include_source_text_or_paths(tmp_path):
    _, _, windows, output = prepared_windows(tmp_path)
    long_id = "B" * 500
    windows[0]["blocks"][0].update(block_id=long_id, text="PRIVATE-TEXT-CANARY", locator="C:/private/path/CANARY")
    windows[1]["blocks"].append(copy.deepcopy(windows[0]["blocks"][0]))
    with pytest.raises(PlanningError, match="DEFERRED_CONTEXT_OWNER_NOT_UNIQUE") as failure:
        _close_deferred_context(windows, output["window_results"])
    context = failure.value.diagnostic_context
    assert context["block_id"] == "B" * 120
    assert all(not isinstance(value, str) or len(value) <= 120 for value in context.values())
    assert "CANARY" not in str(context) and "private" not in str(context)


def test_complete_closure_failure_report_is_not_partial_and_still_blocks_n2(tmp_path):
    manifest, parsed = fixture(tmp_path)
    block(parsed, "A", "应说明巡检方法。")
    block(parsed, "B", "应说明异常复测方法。")
    class IncompleteOwnerContext(Context):
        def __init__(self):
            super().__init__()
            self.capture_flags = {}

        def capture(self, name, payload, **kwargs):
            self.capture_flags[name] = kwargs
            super().capture(name, payload, **kwargs)

        def run_unit(self, unit_id, data, fn):
            result = super().run_unit(unit_id, data, fn)
            if data["window"]["blocks"][0]["block_id"] == "B":
                result["status"] = "running"
            return result
    ctx = IncompleteOwnerContext()
    def call(ctx, task, unit, payload, validate):
        source = payload["window"]["blocks"][0]
        raw = live_candidate(payload, selected(source))
        raw["unresolved_block_ids"] = ["B"] if source["block_id"] == "A" else []
        return validate(raw)
    with pytest.raises(PlanningError, match="DEFERRED_CONTEXT_OWNER_INCOMPLETE"):
        extract_requirements(ctx, parsed, manifest, call, {"window_max_blocks": 1})
    name = "n2-deferred-context-unresolved"
    assert ctx.capture_flags[name] == {"partial": False}
    assert ctx.captures[name]["status"] == "not_closed"
    assert ctx.captures[name]["code"] == "DEFERRED_CONTEXT_OWNER_INCOMPLETE"
    assert ctx.captures[name]["diagnostic_context"]["block_id"] == "B"
    assert "n2-coverage-report" not in ctx.captures
