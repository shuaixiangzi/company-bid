"""The normal node handlers preserve diagnostic identity across N2/N3."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from company_bid import chain, extraction, planning
from company_bid.pipeline import PipelineFailure, build_executor
from company_bid.provider import ProviderFailure
from company_bid.sample import create_synthetic_sample


def config():
    return {"pipeline": "automatic", "run_purpose": "diagnostic_replay",
            "provider": {"mode": "prepared"}, "structured_provider": {"mode": "diagnostic_mixed"},
            "mapping_provider": {"mode": "recorded"},
            "diagnostic_replay": {"schema_version": "diagnostic-replay-v1", "sources": {}, "bindings": {}}}


@pytest.mark.parametrize("override", [
    {"structured_provider": {"mode": "chat_completions"}},
    {"structured_provider": {"mode": "recorded"}},
    {"pipeline": "library"},
    {"run_purpose": "production"},
])
def test_declared_failed_response_replay_requires_mixed_n2(tmp_path, override):
    settings = {**config(), "extraction": {"failed_cell_wrap_replay": {}}, **override}
    with pytest.raises(PipelineFailure, match="FAILED_CELL_WRAP_REPLAY_MODE_REQUIRED"):
        build_executor(tmp_path / "runtime", settings)
    assert not (tmp_path / "runtime").exists()


@pytest.mark.parametrize("mode", ["chat_completions", "recorded"])
def test_price_scoring_review_cannot_dispatch_standalone(tmp_path, mode):
    settings = {**config(), "structured_provider": {"mode": mode}, "extraction": {
        "price_scoring_source_resolution": {}, "failed_scoring_replay": {}, "failed_cell_wrap_replay": {}}}
    with pytest.raises(PipelineFailure, match="FAILED_SCORING_REPLAY_MODE_REQUIRED"):
        build_executor(tmp_path / "runtime", settings)
    assert not (tmp_path / "runtime").exists()


@pytest.mark.parametrize("missing", ["price_scoring_source_resolution", "failed_scoring_replay", "failed_cell_wrap_replay"])
def test_incomplete_price_scoring_recovery_is_rejected(tmp_path, missing):
    options = {"price_scoring_source_resolution": {}, "failed_scoring_replay": {}, "failed_cell_wrap_replay": {}}
    del options[missing]
    with pytest.raises(PipelineFailure, match="FAILED_SCORING_CONFIG_INVALID"):
        build_executor(tmp_path / "runtime", {**config(), "extraction": options})
    assert not (tmp_path / "runtime").exists()


@pytest.mark.parametrize("change", ["missing_purpose", "ordinary_purpose", "missing_mapping", "mixed_mapping", "mixed_writer"])
def test_mixed_mode_cannot_accidentally_apply_to_writing_or_inherited_mapping(tmp_path, change):
    settings = config()
    if change == "missing_purpose":
        del settings["run_purpose"]
    elif change == "ordinary_purpose":
        settings["run_purpose"] = "production"
    elif change == "missing_mapping":
        del settings["mapping_provider"]
    elif change == "mixed_mapping":
        settings["mapping_provider"]["mode"] = "diagnostic_mixed"
    else:
        settings["provider"]["mode"] = "diagnostic_mixed"
    with pytest.raises(ProviderFailure, match="DIAGNOSTIC_PROVIDER_SCOPE_INVALID"):
        build_executor(tmp_path / "runtime", settings)


def test_failed_replay_preflight_precedes_any_extraction_work(tmp_path, monkeypatch):
    settings = config()
    events = []

    class BrokenGateway:
        def __init__(self, options, recorded, **kwargs):
            self.options = options

        def preflight(self, ctx, value):
            events.append("preflight")
            raise ProviderFailure("DIAGNOSTIC_REPLAY_SOURCE_INVALID")

    def forbidden(*args, **kwargs):
        events.append("extraction")
        raise AssertionError("No extraction/provider work after invalid declared source")

    monkeypatch.setattr(chain, "JsonTaskGateway", BrokenGateway)
    monkeypatch.setattr(extraction, "extract_requirements", forbidden)
    node = next(n for n in build_executor(tmp_path / "runtime", settings).nodes if n.key == "N2")
    with pytest.raises(ProviderFailure, match="DIAGNOSTIC_REPLAY_SOURCE_INVALID"):
        node.handler(SimpleNamespace(execution_config=settings), {"parsed": {}, "manifest": {}})
    assert events == ["preflight"]


def test_diagnostic_provenance_survives_planning_with_normal_mapping_provider(tmp_path, monkeypatch):
    settings = config()
    prepared = create_synthetic_sample(tmp_path / "sample").model_dump(mode="json")
    prepared["input_kind"] = "automatic"
    events = []
    provenance = {"schema_version": "diagnostic-replay-summary-v1", "diagnostic_only": True,
                  "binding_count": 1, "verified_binding_count": 1, "revalidated_count": 1,
                  "live_miss_count": 1, "origins": [{"run_id": "historical-test-run", "origin_kind": "synthetic"}]}

    class Gateway:
        def __init__(self, options, recorded, **kwargs):
            self.options = options
            if options["mode"] == "diagnostic_mixed":
                assert kwargs["diagnostic_replay"] == settings["diagnostic_replay"]

        def preflight(self, ctx, value):
            events.append("preflight")
            return {**provenance, "revalidated_count": 0, "live_miss_count": 0}

        def summary(self):
            return deepcopy(provenance)

    def extracted(ctx, parsed, manifest, gateway, options):
        assert events == ["preflight"]
        events.append("extraction")
        return {"coverage": {"scope": "synthetic-only"}}

    monkeypatch.setattr(chain, "JsonTaskGateway", Gateway)
    monkeypatch.setattr(extraction, "extract_requirements", extracted)
    monkeypatch.setattr(planning, "plan_bid", lambda *args, **kwargs: deepcopy(prepared))
    nodes = {n.key: n for n in build_executor(tmp_path / "runtime", settings).nodes}
    ctx = SimpleNamespace(execution_config=settings)
    value = {"parsed": {}, "manifest": {"preparation_note": "synthetic diagnostic propagation only",
                                       "sources": deepcopy(prepared["sources"])},
             "source_artifacts": [], "proposals": {}}
    n2 = nodes["N2"].handler(ctx, value)
    assert "diagnostic_replay" not in value
    n3 = nodes["N3"].handler(ctx, n2)
    assert n3["chain_diagnostics"]["diagnostic_replay"] == n2["diagnostic_replay"]
    assert n3["chain_diagnostics"]["publication_status"] == "diagnostic_only"
    assert n3["chain_diagnostics"]["diagnostic_replay"]["revalidated_count"] == 1
    assert n3["chain_diagnostics"]["generation_mode"] == "recorded"
