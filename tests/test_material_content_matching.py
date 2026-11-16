"""Catalog-response provenance and current source matching remain separate."""
import copy

from company_bid import material_content_matching as module
from company_bid.material_matching import material_candidate_report
from company_bid.storage import digest
from test_material_recall_mainline import source_case, executor, settings, install_transport, matching_report


class Context:
    def __init__(self):
        self.units = {}

    def run_unit(self, identity, value, handler):
        key = digest(value)
        if identity in self.units:
            assert self.units[identity][0] == key
            return copy.deepcopy(self.units[identity][1])
        output = handler()
        self.units[identity] = (key, copy.deepcopy(output))
        return output


def test_catalog_replay_wire_scope_unchanged_by_later_original_index(tmp_path, monkeypatch):
    data = source_case(tmp_path, ["请附证明企业依法登记存续的文件。"])
    install_transport(monkeypatch)
    runs = []
    for enabled in (False, True):
        ex = executor(tmp_path / str(enabled), settings(read_selected_originals=enabled))
        rid = ex.create_run(data)
        assert ex.execute(rid)["status"] == "succeeded"
        report = matching_report(ex, rid)
        unit = ex.inspect(rid)["nodes"][0]["units"]["semantic-catalog"]
        catalog = ex.store.read_json(unit["output_ref"])["data"]
        runs.append((report, catalog))
    old, new = runs
    assert old[1] == new[1]
    assert new[0]["original_content_index"]
    assert new[0]["semantic_recall"]["catalog_stage_report_sha256"]
    row, = new[0]["semantic_recall"]["queries"]
    assert row["catalog_stage_status"] == old[0]["semantic_recall"]["queries"][0]["status"]
    assert row["response_sha256"] == old[0]["semantic_recall"]["queries"][0]["response_sha256"]
    assert row["automatic_assembly_allowed"] is False
    assert new[0]["assembly_candidates"] == []


def test_current_source_recheck_cannot_revive_historically_blocked_candidate(tmp_path, monkeypatch):
    import company_bid.material_content_index as indexer
    data = source_case(tmp_path, ["须提供营业执照。"])
    manifest = data["manifest"]
    reqs = [{"requirement_id": "R", "text": "须提供营业执照。", "response_mode": "attachment"}]
    report = material_candidate_report(reqs, [], attachments=manifest["attachments"],
        sources=manifest["sources"], parsed=data["parsed"], evidence_selections=[])
    candidate = {"group_id": "BUS", "decision": "blocked_candidate", "blockers": ["previous_original_conflict"],
                 "automatic_assembly_allowed": False}
    report["semantic_recall"] = {"queries": [{"requirement_id": "R", "candidates": [candidate],
        "status": "all_candidates_blocked", "condition_findings": [], "response_sha256": "a" * 64}],
        "counts": {"candidate_queries": 0, "blocked_candidates": 1}, "new_model_query_count": 0}
    original = copy.deepcopy(report)
    monkeypatch.setattr(indexer, "build_material_content_index", lambda *_: {
        "parsed": data["parsed"], "summary": {"status": "needs_ocr"}, "coverage": {}, "groups": []})
    ctx = Context()
    result = module.integrate_original_content(ctx, reqs, manifest, data["parsed"], report)
    row, = result["semantic_recall"]["queries"]
    checked, = row["candidates"]
    assert checked["decision"] == "blocked_candidate"
    assert "previous_original_conflict" in checked["blockers"]
    assert row["response_sha256"] == "a" * 64
    assert report == original
    # A successful query checkpoint is read again without re-evaluating it.
    unit_count = len(ctx.units)
    assert result == module.integrate_original_content(ctx, reqs, manifest, data["parsed"], report)
    assert len(ctx.units) == unit_count
