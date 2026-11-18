from pathlib import Path

import pytest

from company_bid.pipeline import build_executor


@pytest.mark.parametrize("changed_file, affected", [
    ("unit_concurrency.py", {"N0", "N1", "N2", "N3", "N4", "N5", "N6"}),
    ("writing_prompt.py", {"N4"}),
    ("planning.py", {"N3"}),
    ("native_layout.py", {"N3"}),
    ("reading.py", {"N1", "N3"}),
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
    ("review_source_context.py", {"N6"}),
    ("writing_candidate_import.py", {"N4", "N5"}),
    ("plan_component_controls.py", {"N3"}),
    ("project_constraints.py", {"N3", "N4", "N5", "N6"}),
    ("project_constraint_runtime.py", {"N3", "N4", "N5", "N6"}),
    # N4/N5 also authenticate historical writing via its shared origin reader.
    ("diagnostic_replay.py", {"N2", "N3", "N4", "N5", "N6"}),
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
    ("source_clauses.py", {"N2", "N3"}),
    ("table_continuations.py", {"N2", "N3", "N4", "N5", "N6"}),
    ("material_matching.py", {"N3"}),
    ("material_ocr_index.py", {"N3"}),
    ("material_deepseek_ocr.py", {"N3"}),
    ("material_ocr_quality.py", {"N3"}),
    ("material_ocr_replay.py", {"N3"}),
    ("material_ocr_budget.py", {"N3"}),
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
