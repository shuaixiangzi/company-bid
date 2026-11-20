from pathlib import Path

import pytest

from company_bid.pipeline import build_executor


@pytest.mark.parametrize("keys", [
    ("contract_insurance_acceptance_source_resolution",),
    ("failed_contract_insurance_acceptance_replay",),
    ("contract_insurance_acceptance_source_resolution", "failed_contract_insurance_acceptance_replay"),
])
def test_insurance_acceptance_recovery_requires_proof_and_interested_parties_history(tmp_path, keys):
    from company_bid.pipeline import PipelineFailure

    with pytest.raises(PipelineFailure, match="FAILED_CONTRACT_INSURANCE_ACCEPTANCE_CONFIG_INVALID"):
        build_executor(tmp_path, {"pipeline": "automatic", "extraction": {key: {} for key in keys}})


@pytest.mark.parametrize("keys", [
    ("contract_interested_parties_source_resolution",),
    ("failed_contract_interested_parties_replay",),
    ("contract_interested_parties_source_resolution", "failed_contract_interested_parties_replay"),
])
def test_interested_parties_recovery_requires_proof_and_annex_history(tmp_path, keys):
    from company_bid.pipeline import PipelineFailure

    with pytest.raises(PipelineFailure, match="FAILED_CONTRACT_INTERESTED_PARTIES_CONFIG_INVALID"):
        build_executor(tmp_path, {"pipeline": "automatic", "extraction": {key: {} for key in keys}})


@pytest.mark.parametrize("keys", [
    ("contract_annex_source_resolution",),
    ("failed_contract_annex_replay",),
    ("contract_annex_source_resolution", "failed_contract_annex_replay"),
])
def test_contract_annex_recovery_requires_proof_and_illustration_history(tmp_path, keys):
    from company_bid.pipeline import PipelineFailure

    with pytest.raises(PipelineFailure, match="FAILED_CONTRACT_ANNEX_CONFIG_INVALID"):
        build_executor(tmp_path, {"pipeline": "automatic", "extraction": {key: {} for key in keys}})


@pytest.mark.parametrize("keys", [
    ("illustration_source_resolution",),
    ("failed_illustration_replay",),
    ("illustration_source_resolution", "failed_illustration_replay"),
])
def test_illustration_recovery_requires_its_proof_and_authenticated_predecessor(tmp_path, keys):
    from company_bid.pipeline import PipelineFailure

    with pytest.raises(PipelineFailure, match="FAILED_ILLUSTRATION_CONFIG_INVALID"):
        build_executor(tmp_path, {"pipeline": "automatic", "extraction": {key: {} for key in keys}})


def test_complete_illustration_history_config_requires_diagnostic_mode(tmp_path):
    from company_bid.pipeline import PipelineFailure

    keys = {"failed_cell_wrap_replay", "price_scoring_source_resolution", "failed_scoring_replay",
            "scoring_row_source_resolution", "failed_scoring_rows_replay",
            "response_cover_source_resolution", "failed_response_cover_replay",
            "response_review_table_source_resolution", "failed_response_review_table_replay",
            "response_letter_source_resolution", "failed_response_letter_replay",
            "response_cell_reflow_source_resolution", "failed_response_cell_reflow_replay",
            "illustration_source_resolution", "failed_illustration_replay"}
    with pytest.raises(PipelineFailure, match="FAILED_SCORING_REPLAY_MODE_REQUIRED"):
        build_executor(tmp_path, {"pipeline": "automatic", "extraction": dict.fromkeys(keys, {})})


@pytest.mark.parametrize("keys", [
    ("response_cell_reflow_source_resolution",),
    ("failed_response_cell_reflow_replay",),
    ("response_cell_reflow_source_resolution", "failed_response_cell_reflow_replay"),
])
def test_cell_reflow_recovery_requires_its_proof_and_authenticated_predecessor(tmp_path, keys):
    from company_bid.pipeline import PipelineFailure

    with pytest.raises(PipelineFailure, match="FAILED_RESPONSE_CELL_REFLOW_CONFIG_INVALID"):
        build_executor(tmp_path, {"pipeline": "automatic", "extraction": {key: {} for key in keys}})


@pytest.mark.parametrize("keys", [
    ("response_letter_source_resolution",),
    ("failed_response_letter_replay",),
    ("response_letter_source_resolution", "failed_response_letter_replay"),
])
def test_letter_recovery_requires_its_proof_and_prior_review_replay(tmp_path, keys):
    from company_bid.pipeline import PipelineFailure

    with pytest.raises(PipelineFailure, match="FAILED_RESPONSE_LETTER_CONFIG_INVALID"):
        build_executor(tmp_path, {"pipeline": "automatic", "extraction": {key: {} for key in keys}})


@pytest.mark.parametrize("keys", [
    ("response_review_table_source_resolution",),
    ("failed_response_review_table_replay",),
    ("response_review_table_source_resolution", "failed_response_review_table_replay"),
])
def test_review_table_recovery_requires_source_proof_and_prior_cover_replay(tmp_path, keys):
    from company_bid.pipeline import PipelineFailure

    with pytest.raises(PipelineFailure, match="FAILED_RESPONSE_REVIEW_TABLE_CONFIG_INVALID"):
        build_executor(tmp_path, {"pipeline": "automatic", "extraction": {key: {} for key in keys}})


@pytest.mark.parametrize("changed_file, affected", [
    ("unit_concurrency.py", {"N0", "N1", "N2", "N3", "N4", "N5", "N6"}),
    ("writing_prompt.py", {"N4"}),
    ("planning.py", {"N3"}),
    ("native_layout.py", {"N3"}),
    ("reading.py", {"N1", "N3"}),
    ("pdf_table_geometry.py", {"N1", "N3"}),
    ("pdf_text_lines.py", {"N1", "N3"}),
    ("provider.py", {"N1", "N2", "N3", "N4", "N5"}),
    ("source_format.py", {"N3"}),
    ("source_format_scope.py", {"N3"}),
    ("project_identity.py", {"N3"}),
    ("source_response_audit.py", {"N5"}),
    ("commitment_planning.py", {"N3"}),
    ("reviewed_source_actions.py", {"N3", "N4", "N5", "N6"}),
    ("source_format_contracts.py", {"N0", "N1", "N2", "N3", "N4", "N5", "N6"}),
    ("source_format_rendering.py", {"N6"}),
    ("rendering.py", {"N6"}),
    ("material_content_matching.py", {"N3"}),
    ("material_content_index.py", {"N3"}),
    ("review_document.py", {"N6"}),
    ("operator_summary.py", {"N6"}),
    ("project_questions.py", {"N4", "N5", "N6"}),
    ("review_source_context.py", {"N6"}),
    ("writing_candidate_import.py", {"N4", "N5"}),
    ("plan_component_controls.py", {"N3"}),
    ("project_constraints.py", {"N3", "N4", "N5", "N6"}),
    ("project_constraint_runtime.py", {"N3", "N4", "N5", "N6"}),
    # N4/N5 also authenticate historical writing via its shared origin reader.
    ("diagnostic_replay.py", {"N1", "N2", "N3", "N4", "N5", "N6"}),
    ("writing_replay.py", {"N4", "N5"}),
    ("material_heading_sources.py", {"N2", "N3"}),
    ("format_continuation_sources.py", {"N2", "N3"}),
    ("contract_blank_sources.py", {"N2", "N3"}),
    ("adjacent_owner_sources.py", {"N2", "N3"}),
    ("citation_gap_sources.py", {"N2", "N3"}),
    ("contract_pending_repair.py", {"N2", "N3"}),
    ("contract_review_slots.py", {"N2", "N3", "N4", "N5", "N6"}),
    ("contract_pending_plan.py", {"N3", "N4", "N5", "N6"}),
    ("pipeline.py", {"N4", "N5", "N6"}),
    ("slots.py", {"N0", "N1", "N2", "N3", "N4", "N5", "N6"}),
    ("extraction.py", {"N2", "N3", "N4", "N5", "N6"}),
    ("extraction_illustrations.py", {"N2", "N3", "N4", "N5", "N6"}),
    ("contract_annex_sources.py", {"N2", "N3", "N4", "N5", "N6"}),
    ("contract_interested_parties_sources.py", {"N2", "N3", "N4", "N5", "N6"}),
    ("contract_acceptance_sources.py", {"N2", "N3", "N4", "N5", "N6"}),
    ("contract_clause_index.py", {"N2", "N3", "N4", "N5", "N6"}),
    ("contract_clause_resolution.py", {"N2", "N3", "N4", "N5", "N6"}),
    ("contract_clause_packing.py", {"N2", "N3", "N4", "N5", "N6"}),
    ("extraction_replay_scope.py", {"N2", "N3", "N4", "N5", "N6"}),
    ("extraction_failed_scoring_replay.py", {"N2", "N3", "N4", "N5", "N6"}),
    ("extraction_numbered_clause_replay.py", {"N2", "N3", "N4", "N5", "N6"}),
    ("price_scoring_sources.py", {"N2", "N3", "N4", "N5", "N6"}),
    ("scoring_row_sources.py", {"N2", "N3", "N4", "N5", "N6"}),
    ("response_cover_sources.py", {"N2", "N3", "N4", "N5", "N6"}),
    ("response_review_table_sources.py", {"N2", "N3", "N4", "N5", "N6"}),
    ("response_letter_sources.py", {"N2", "N3", "N4", "N5", "N6"}),
    ("response_cell_reflow_sources.py", {"N2", "N3", "N4", "N5", "N6"}),
    ("response_authority_tail.py", {"N2", "N3", "N4", "N5", "N6"}),
    ("source_clauses.py", {"N2", "N3"}),
    ("table_continuations.py", {"N2", "N3", "N4", "N5", "N6"}),
    ("material_matching.py", {"N3"}),
    ("material_ocr_index.py", {"N3"}),
    ("tender_deepseek_ocr.py", {"N1"}),
    ("tender_ocr_replay.py", {"N1"}),
    ("reading_source_review.py", {"N1"}),
    ("reading_review_origin.py", {"N1"}),
    ("material_deepseek_ocr.py", {"N1", "N3"}),
    ("material_ocr_quality.py", {"N1", "N3"}),
    ("material_ocr_replay.py", {"N1", "N3"}),
    ("material_ocr_budget.py", {"N1", "N3"}),
    ("diagnostics.py", {"N4", "N5"}),
    ("material_ocr_diagnostics.py", {"N4", "N5"}),
    ("material_recall.py", {"N3", "N6"}),
    ("material_reviewed_pages.py", {"N3", "N6"}),
    ("material_source_selection.py", {"N3", "N6"}),
    ("semantic_materials.py", {"N3", "N6"}),
    ("semantic_replay.py", {"N3", "N6"}),
    ("response_organization.py", {"N3"}),
    ("preparation_rules.py", {"N3", "N4", "N5", "N6"}),
])
def test_local_code_change_invalidates_only_nodes_that_consume_it(tmp_path, monkeypatch, changed_file, affected):
    config = {"pipeline": "automatic", "provider": {"mode": "prepared"}}
    before = {node.key: node.version for node in build_executor(tmp_path, config).nodes}
    original = Path.read_text

    def changed(path, *args, **kwargs):
        text = original(path, *args, **kwargs)
        return text + "\n# different implementation\n" if path.name == changed_file else text

    monkeypatch.setattr(Path, "read_text", changed)
    after = {node.key: node.version for node in build_executor(tmp_path, config).nodes}
    assert {key for key in before if before[key] != after[key]} == affected


def test_explicit_extraction_and_mapping_configs_do_not_depend_on_writing_limits(tmp_path):
    from copy import deepcopy
    from company_bid.runtime import Executor, NodeSpec, ConfigurationChangedError
    config = {"pipeline": "automatic", "provider": {"mode": "prepared"},
              "structured_provider": {"mode": "recorded"}, "mapping_provider": {"mode": "recorded"}}
    # Deterministic bodies exercise real production version/config manifests,
    # without invoking a model to prove reuse of its already completed work.
    def runner(settings):
        nodes = [NodeSpec(node.key, node.version, lambda ctx, value: value, node.config_keys)
                 for node in build_executor(tmp_path, settings).nodes]
        return Executor(tmp_path, nodes, settings)
    parent = runner(config)
    rid = parent.create_run({})
    state = parent.execute(rid, stop_after="N3")
    newer = deepcopy(config); newer["provider"]["max_output_tokens"] = 3500
    child = runner(newer).fork(rid, "N4")
    reused = runner(newer).inspect(child)
    assert reused["nodes"][2]["output_ref"] == state["nodes"][2]["output_ref"]
    assert reused["nodes"][3]["output_ref"] == state["nodes"][3]["output_ref"]
    newer["structured_provider"]["model"] = "changed-extraction-model"
    with pytest.raises(ConfigurationChangedError):
        runner(newer).fork(rid, "N4")


@pytest.mark.parametrize("override, completed, too_late, proper", [
    ("structured_provider", "N2", "N3", "N2"),
    ("mapping_provider", "N3", "N4", "N3"),
])
@pytest.mark.parametrize("adding", [False, True])
def test_provider_override_selection_change_cannot_reuse_affected_prefix(tmp_path, override, completed, too_late, proper, adding):
    from copy import deepcopy
    from company_bid.runtime import Executor, NodeSpec, ConfigurationChangedError
    config = {"pipeline": "automatic", "provider": {"mode": "prepared", "model": "fallback"},
              "structured_provider": {"mode": "recorded", "model": "extraction"},
              "mapping_provider": {"mode": "recorded", "model": "mapping"}}
    alternate = deepcopy(config); alternate.pop(override)
    if adding:
        config, alternate = alternate, config
    def runner(settings):
        nodes = [NodeSpec(node.key, node.version, lambda ctx, value: value, node.config_keys)
                 for node in build_executor(tmp_path, settings).nodes]
        return Executor(tmp_path, nodes, settings)
    parent = runner(config)
    rid = parent.create_run({})
    assert parent.execute(rid, stop_after=completed)["status"] == "paused_debug"
    with pytest.raises(ConfigurationChangedError):
        runner(alternate).fork(rid, too_late)
    with pytest.raises(ConfigurationChangedError):
        parent.fork(rid, too_late, execution_config=alternate)
    assert runner(alternate).inspect(runner(alternate).fork(rid, proper))["cursor"] == int(proper[1:])
