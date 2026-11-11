from pathlib import Path

import pytest

from company_bid.pipeline import build_executor


@pytest.mark.parametrize("changed_file, affected", [
    ("writing_prompt.py", {"N4"}),
    ("planning.py", {"N3"}),
    ("slots.py", {"N3"}),
    ("extraction.py", {"N2", "N3"}),
    ("source_clauses.py", {"N2", "N3"}),
    ("table_continuations.py", {"N2", "N3"}),
    ("material_matching.py", {"N3"}),
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
