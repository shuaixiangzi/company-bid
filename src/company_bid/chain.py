"""N0-N3 adapters feeding the existing N4-N6 and the same Executor."""
from __future__ import annotations

import hashlib
import importlib.metadata
from pathlib import Path

from .intake import IntakeManifest
from .models import PreparedBid, file_sha256
from .provider import ProviderFailure
from .runtime import NodeSpec
from .structured import JsonTaskGateway


def intake_nodes(root: Path, version, settings: dict):
    extraction_options = settings.get("structured_provider", settings.get("provider", {}))
    if (settings.get("provider", {}).get("mode") == "diagnostic_mixed" or
            settings.get("mapping_provider", {}).get("mode") == "diagnostic_mixed"):
        raise ProviderFailure("DIAGNOSTIC_PROVIDER_SCOPE_INVALID")
    if extraction_options.get("mode") == "diagnostic_mixed":
        if (settings.get("run_purpose") != "diagnostic_replay" or
                settings.get("mapping_provider", {}).get("mode") not in {"recorded", "chat_completions", "prepared"}):
            raise ProviderFailure("DIAGNOSTIC_PROVIDER_SCOPE_INVALID")

    def freeze(ctx, value):
        manifest = IntakeManifest.model_validate(value["manifest"])
        data = manifest.model_dump(mode="json")
        artifacts, copied = [], {}
        for group in ("sources", "attachments"):
            for item in data[group]:
                source = Path(item["path"])
                ref = copied.get(item["sha256"])
                if ref is None:
                    def freeze_source(source=source, expected=item["sha256"]):
                        if not source.is_file() or file_sha256(source) != expected:
                            raise ProviderFailure("SOURCE_HASH_MISMATCH")
                        reference = ctx.store_bytes("source" + source.suffix.lower(), source.read_bytes())
                        if reference["sha256"] != expected:
                            raise ProviderFailure("SOURCE_CHANGED_DURING_FREEZE")
                        return {"artifact": reference}
                    ref = ctx.run_unit("freeze-source-" + item["sha256"],
                        {"source_path": str(source), "source_sha256": item["sha256"]}, freeze_source)["artifact"]
                    copied[item["sha256"]] = ref
                    artifacts.append(ref)
                item["path"] = str(root / ref["path"])
        if data["evidence_selections"]:
            from .evidence import prepare_evidence
            from .intake import verify_derived_proof_scopes
            verify_derived_proof_scopes(ctx, data)
            evidence = prepare_evidence(ctx, data["sources"], data["evidence_selections"], root)
            existing_sources = {item["source_id"] for item in data["sources"]}
            for source in evidence["sources"]:
                if source["source_id"] in existing_sources:
                    raise ProviderFailure("EVIDENCE_SOURCE_ID_COLLISION")
                data["sources"].append(source)
                data["source_roles"][source["source_id"]] = "provenance_only"
                existing_sources.add(source["source_id"])
            data["attachments"].extend(evidence["attachments"])
            artifacts.extend(evidence["artifacts"])
            data = IntakeManifest.model_validate(data).model_dump(mode="json")
        return {"manifest": data, "source_artifacts": artifacts,
                "recorded_candidates": value.get("recorded_candidates", {}),
                "proposals": value.get("proposals", {}), "input_kind": "automatic"}

    def read(ctx, value):
        from .reading import read_documents
        manifest = value["manifest"]
        documents = [{**source, "role": manifest["source_roles"][source["source_id"]]}
                     for source in manifest["sources"] if manifest["source_roles"][source["source_id"]] != "provenance_only"]
        ocr = None
        ocr_options = ctx.execution_config.get("ocr")
        if ocr_options is not None:
            from .ocr import create_ocr
            ocr = create_ocr(ctx, {**ocr_options, "provider": ocr_options.get("provider", ctx.execution_config.get("provider", {}))})
        parsed = read_documents(ctx, documents, ctx.execution_config.get("reading", {}), ocr)
        return {**value, "parsed": parsed}

    def gateway(ctx, value, *, mapping=False):
        options = ctx.execution_config.get("mapping_provider", {}) if mapping else {}
        options = options or ctx.execution_config.get("structured_provider", ctx.execution_config.get("provider", {}))
        if options.get("mode") == "prepared":
            options = {"mode": "recorded"}
        if options.get("mode") == "diagnostic_mixed":
            return JsonTaskGateway(options, value.get("recorded_candidates", {}),
                                   diagnostic_replay=ctx.execution_config.get("diagnostic_replay"))
        return JsonTaskGateway(options, value.get("recorded_candidates", {}))

    def extract(ctx, value):
        from .extraction import extract_requirements
        caller = gateway(ctx, value)
        diagnostic = caller.options.get("mode") == "diagnostic_mixed"
        if diagnostic:
            caller.preflight(ctx, value)
        extracted = extract_requirements(ctx, value["parsed"], value["manifest"], caller,
                                         ctx.execution_config.get("extraction", {}))
        result = {**value, "extracted": extracted}
        if diagnostic:
            result["diagnostic_replay"] = {**caller.summary(), "publication_status": "diagnostic_only"}
        return result

    def plan(ctx, value):
        from .planning import plan_bid
        from .material_matching import material_candidate_report
        result = plan_bid(ctx, value["extracted"], value["parsed"], value["manifest"], gateway(ctx, value, mapping=True),
                          ctx.execution_config.get("planning", {}))
        prepared = PreparedBid.model_validate(result)
        if prepared.input_kind != "automatic":
            raise ProviderFailure("AUTOMATIC_PLAN_ORIGIN_INVALID")
        # Full intermediate content remains in the preceding node checkpoints;
        # downstream diagnostics carry references instead of duplicating it.
        output = {"prepared": prepared.model_dump(mode="json"), "proposals": value.get("proposals", {}),
                "source_artifacts": value["source_artifacts"], "input_kind": "automatic",
                "chain_diagnostics": {"parser_coverage": value["parsed"].get("coverage", {}),
                                      "library_preparation_note": value["manifest"]["preparation_note"],
                                      "material_candidates": material_candidate_report(
                                          result["requirements"], value["manifest"].get("material_records", []),
                                          attachments=value["manifest"].get("attachments", []),
                                          sources=value["manifest"]["sources"], parsed=value["parsed"],
                                          evidence_selections=value["manifest"].get("evidence_selections", []),
                                          proof_derivations=value["manifest"].get("proof_derivations", [])),
                                      "extraction_coverage": value["extracted"].get("coverage", {}),
                                      "generation_mode": "recorded" if gateway(ctx, value, mapping=True).options.get("mode") == "recorded" else "model_candidates",
                                      "semantic_review": "requires_source_review"}}
        if "diagnostic_replay" in value:
            output["chain_diagnostics"]["diagnostic_replay"] = value["diagnostic_replay"]
            output["chain_diagnostics"]["publication_status"] = "diagnostic_only"
        return output

    core = ("chain.py", "intake.py", "material_records.py", "models.py", "source_format_contracts.py", "runtime.py", "storage.py", "observability.py")
    freeze_version = hashlib.sha256((version(*core, "evidence.py") + "|" + importlib.metadata.version("pypdfium2")).encode()).hexdigest()
    parser_version = hashlib.sha256((version(*core, "reading.py", "ocr.py", "provider.py") + "|" + "|".join(
        importlib.metadata.version(name) for name in ("pdfplumber", "pypdf", "pypdfium2", "python-docx"))).encode()).hexdigest()
    # Only actual dependencies invalidate a successful prefix. For example an
    # explicit extraction provider is independent of later writing parameters.
    extraction_provider_key = "structured_provider" if "structured_provider" in settings else "provider"
    mapping_provider_key = "mapping_provider" if settings.get("mapping_provider") else extraction_provider_key
    # Include selection switches even when absent: adding an override changes
    # what the handler consumes, including fork(execution_config=...) calls.
    extraction_keys = tuple(dict.fromkeys(("structured_provider", extraction_provider_key, "extraction",
                                          "run_purpose", "diagnostic_replay")))
    mapping_keys = tuple(dict.fromkeys(("mapping_provider", mapping_provider_key, "planning") +
                         (() if settings.get("mapping_provider") else ("structured_provider",))))
    reading_keys = ("reading", "ocr") + (("provider",) if settings.get("ocr") is not None and "provider" not in settings["ocr"] else ())
    planning_source = version(*core, "extraction.py", "source_clauses.py", "table_continuations.py", "planning.py",
                              "preparation_rules.py", "material_matching.py", "slots.py", "provider.py",
                              "structured.py", "native_layout.py", "source_format.py", "source_format_contracts.py")
    layout_dependencies = [importlib.metadata.version(name) for name in ("pdfplumber", "pypdfium2")]
    planning_version = hashlib.sha256("|".join([planning_source, *layout_dependencies]).encode()).hexdigest()
    return [NodeSpec("N0", freeze_version, freeze, config_keys=()),
            NodeSpec("N1", parser_version, read, config_keys=reading_keys),
            NodeSpec("N2", version(*core, "extraction.py", "source_clauses.py", "table_continuations.py", "provider.py", "structured.py", "diagnostic_replay.py"), extract,
                     config_keys=extraction_keys),
            NodeSpec("N3", planning_version, plan,
                     config_keys=mapping_keys)]
