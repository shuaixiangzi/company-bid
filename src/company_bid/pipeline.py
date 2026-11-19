"""One executor for automatic N0-N6 and the retained prepared M1 fixture mode."""
from __future__ import annotations

import hashlib
import importlib.metadata
import platform
from pathlib import Path

from .models import (AttachmentBlock, DocumentPackage, HeadingBlock, IssueBlock, ListBlock,
                     ParagraphBlock, PreparedBid, ProposalContent, Section, TableBlock,
                     file_sha256)
from .provider import ChatConfig, ChatProposalProvider, PreparedProposalProvider, ProviderFailure
from .rendering import render_package
from .runtime import Executor, NodeSpec, diagnostic_artifact
from .storage import digest


class PipelineFailure(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _version(*names: str) -> str:
    # Node-owned helpers are explicit transitive inputs. Extending an N3 helper
    # must invalidate N3 without relabeling unchanged N0/N1/N2 implementations.
    dependencies = {"runtime.py": ("unit_concurrency.py",),
                    "source_format_contracts.py": ("slots.py",),
                    "native_layout.py": ("reading.py",),
                    "reading.py": ("pdf_table_geometry.py", "pdf_text_lines.py"),
                    "ocr.py": ("tender_deepseek_ocr.py", "reading_source_review.py"),
                    "tender_deepseek_ocr.py": ("material_deepseek_ocr.py", "material_ocr_quality.py", "material_ocr_budget.py", "tender_ocr_replay.py"),
                    "tender_ocr_replay.py": ("material_ocr_replay.py",),
                    "reading_source_review.py": ("reading_review_origin.py",),
                    "reading_review_origin.py": ("tender_ocr_replay.py", "material_ocr_quality.py"),
                    "extraction.py": ("extraction_illustrations.py", "extraction_replay_scope.py", "price_scoring_sources.py",
                                      "scoring_row_sources.py", "response_cover_sources.py", "response_review_table_sources.py",
                                      "response_letter_sources.py", "response_cell_reflow_sources.py", "response_authority_tail.py"),
                    "extraction_replay_scope.py": ("extraction_failed_scoring_replay.py",),
                    "source_format.py": ("source_format_scope.py",),
                    "source_response_audit.py": ("extraction.py", "table_continuations.py"),
                    "planning.py": ("plan_component_controls.py", "project_constraint_runtime.py", "format_continuation_sources.py", "contract_pending_plan.py", "contract_review_slots.py", "writing_plan.py", "response_organization.py", "project_identity.py", "commitment_planning.py", "reviewed_source_actions.py", "material_recall.py"),
                    "material_recall.py": ("semantic_materials.py", "material_source_selection.py", "semantic_replay.py", "evidence.py", "material_reviewed_pages.py"),
                    "material_matching.py": ("material_content_matching.py", "material_content_index.py", "material_ocr_index.py", "material_deepseek_ocr.py", "material_ocr_quality.py", "material_ocr_replay.py", "material_ocr_budget.py"),
                    "material_ocr_replay.py": ("diagnostic_replay.py",),
                    "material_ocr_index.py": ("material_ocr_regions.py", "material_ocr_disposition.py", "material_ocr_failure.py", "material_source_review.py"),
                    "semantic_replay.py": ("diagnostic_replay.py",),
                    "diagnostics.py": ("material_ocr_diagnostics.py",),
                    "pipeline.py": ("project_constraint_runtime.py",),
                    "project_constraint_runtime.py": ("project_constraints.py", "contract_pending_plan.py", "reviewed_source_actions.py"),
                    "contract_pending_plan.py": ("contract_review_slots.py", "extraction.py"),
                    "project_constraints.py": ("table_continuations.py",),
                    "structured.py": ("material_heading_sources.py", "format_continuation_sources.py", "contract_blank_sources.py",
                                      "contract_pending_repair.py", "adjacent_owner_sources.py", "citation_gap_sources.py"),
                    "contract_pending_repair.py": ("contract_review_slots.py",)}
    expanded = list(dict.fromkeys(names))
    for name in expanded:
        for dependency in dependencies.get(name, ()):
            if dependency not in expanded:
                expanded.append(dependency)
    digest = hashlib.sha256()
    for name in expanded:
        digest.update(name.encode() + b"\0")
        digest.update((Path(__file__).parent / name).read_text(encoding="utf-8").encode("utf-8"))
    digest.update(platform.python_version().encode())
    for dependency in ("pydantic", "python-docx", "Pillow", "opentelemetry-sdk"):
        digest.update((dependency + "=" + importlib.metadata.version(dependency)).encode())
    return digest.hexdigest()


def validate_sources(prepared: PreparedBid) -> None:
    for source in [*prepared.sources, *prepared.attachments]:
        if not source.path.is_file():
            raise PipelineFailure("SOURCE_FILE_MISSING")
        if file_sha256(source.path) != source.sha256:
            raise PipelineFailure("SOURCE_HASH_MISMATCH")


def build_executor(root: Path, config: dict) -> Executor:
    root = Path(root).resolve()
    extraction = config.get("extraction")
    scoring_keys = {"price_scoring_source_resolution", "failed_scoring_replay"}
    row_keys = {"scoring_row_source_resolution", "failed_scoring_rows_replay"}
    cover_keys = {"response_cover_source_resolution", "failed_response_cover_replay"}
    review_keys = {"response_review_table_source_resolution", "failed_response_review_table_replay"}
    letter_keys = {"response_letter_source_resolution", "failed_response_letter_replay"}
    reflow_keys = {"response_cell_reflow_source_resolution", "failed_response_cell_reflow_replay"}
    illustration_keys = {"illustration_source_resolution", "failed_illustration_replay"}
    if isinstance(extraction, dict) and illustration_keys.intersection(extraction):
        if not (illustration_keys | reflow_keys).issubset(extraction):
            raise PipelineFailure("FAILED_ILLUSTRATION_CONFIG_INVALID")
    if isinstance(extraction, dict) and reflow_keys.intersection(extraction):
        if not (reflow_keys | letter_keys).issubset(extraction):
            raise PipelineFailure("FAILED_RESPONSE_CELL_REFLOW_CONFIG_INVALID")
    if isinstance(extraction, dict) and letter_keys.intersection(extraction):
        if not (letter_keys | review_keys).issubset(extraction):
            raise PipelineFailure("FAILED_RESPONSE_LETTER_CONFIG_INVALID")
    if isinstance(extraction, dict) and review_keys.intersection(extraction):
        if not (review_keys | cover_keys).issubset(extraction):
            raise PipelineFailure("FAILED_RESPONSE_REVIEW_TABLE_CONFIG_INVALID")
    if isinstance(extraction, dict) and cover_keys.intersection(extraction):
        if not (cover_keys | row_keys).issubset(extraction):
            raise PipelineFailure("FAILED_RESPONSE_COVER_CONFIG_INVALID")
    if isinstance(extraction, dict) and row_keys.intersection(extraction):
        if not (row_keys | scoring_keys).issubset(extraction):
            raise PipelineFailure("FAILED_SCORING_ROWS_CONFIG_INVALID")
    if isinstance(extraction, dict) and scoring_keys.intersection(extraction):
        if not scoring_keys.issubset(extraction) or "failed_cell_wrap_replay" not in extraction:
            raise PipelineFailure("FAILED_SCORING_CONFIG_INVALID")
    if isinstance(extraction, dict) and "failed_cell_wrap_replay" in extraction:
        provider = config.get("structured_provider", config.get("provider"))
        if (config.get("pipeline") != "automatic" or config.get("run_purpose") != "diagnostic_replay"
                or not isinstance(provider, dict) or provider.get("mode") != "diagnostic_mixed"):
            raise PipelineFailure("FAILED_SCORING_REPLAY_MODE_REQUIRED" if "failed_scoring_replay" in extraction
                                  else "FAILED_CELL_WRAP_REPLAY_MODE_REQUIRED")
    reading = config.get("reading")
    if isinstance(reading, dict) and reading.get("pdf_region_reviews"):
        ocr = config.get("ocr")
        if (config.get("pipeline") != "automatic" or config.get("run_purpose") != "diagnostic_replay"
                or (ocr is not None and not (isinstance(ocr, dict) and ocr.get("engine") == "deepseek_ocr"
                    and isinstance(ocr.get("recorded_targets"), list) and "live_targets" not in ocr))):
            raise PipelineFailure("READING_SOURCE_REVIEW_OFFLINE_REQUIRED")
    if isinstance(reading, dict) and reading.get("diagnostic_pdf_range") is not None and (
            config.get("pipeline") != "automatic" or config.get("run_purpose") != "diagnostic_replay"):
        raise PipelineFailure("DIAGNOSTIC_ORIGINAL_PDF_SCOPE_INVALID")
    if ("review_before_writing" in config and type(config["review_before_writing"]) is not bool
            or (config.get("review_before_writing") or "project_answer_snapshot" in config)
            and config.get("pipeline") != "automatic"):
        raise PipelineFailure("PROJECT_REVIEW_REQUIRES_AUTOMATIC_PIPELINE")
    from .material_ocr_budget import validate_pipeline_ocr_budget
    budget_config = config
    tender_ocr = config.get("ocr")
    if (config.get("run_purpose") == "diagnostic_replay" and isinstance(tender_ocr, dict)
            and tender_ocr.get("engine") == "deepseek_ocr" and isinstance(tender_ocr.get("recorded_targets"), list)):
        from .ocr import official_tender_options

        # N1 rejects every unbound image in recorded-only mode. Keep its
        # original configuration for authentication, including its historic cap.
        official_tender_options(tender_ocr)
        budget_config = {**config, "ocr": None}
    validate_pipeline_ocr_budget(budget_config)
    if config.get("pipeline") == "library":
        from .library_pipeline import build_library_executor
        return build_library_executor(root, config, _version)

    def freeze(ctx, value):
        prepared = PreparedBid.model_validate(value["prepared"])
        validate_sources(prepared)
        data = prepared.model_dump(mode="json")
        refs = []
        copied = {}
        for group in ("sources", "attachments"):
            for item in data[group]:
                path = Path(item["path"])
                ref = copied.get(item["sha256"])
                if ref is None:
                    ref = ctx.store_bytes("source" + path.suffix.lower(), path.read_bytes())
                    if ref["sha256"] != item["sha256"]:
                        raise PipelineFailure("SOURCE_CHANGED_DURING_FREEZE")
                    copied[item["sha256"]] = ref
                    refs.append(ref)
                # Pin real copied bytes rather than mutable outside input paths.
                item["path"] = str(root / ref["path"])
        return {"prepared": data, "proposals": value.get("proposals", {}),
                "source_artifacts": refs, "input_kind": "manual_prepared",
                "unimplemented_automatic_nodes": ["N1", "N2", "N3"]}

    def draft(ctx, value):
        from .project_review import project_review_input
        frozen_prepared = value["prepared"]
        value = project_review_input(ctx, value)
        prepared = PreparedBid.model_validate(value["prepared"])
        from .project_constraint_runtime import (load_project_constraints, validate_project_constraint_context,
                                                project_constraint_trace_bindings)
        # N3 constraint provenance is immutable. Per-unit inputs below come
        # from the effective plan and must still equal the certified originals.
        project_constraints = load_project_constraints(ctx, PreparedBid.model_validate(frozen_prepared).model_dump(mode="json"))
        provider_options = ctx.execution_config.get("provider", {"mode": "prepared"})
        if provider_options["mode"] == "prepared":
            provider = PreparedProposalProvider(value["proposals"])
        elif provider_options["mode"] == "chat_completions":
            options = {k: v for k, v in provider_options.items() if k != "mode"}
            provider = ChatProposalProvider(ChatConfig(**options))
        elif provider_options["mode"] == "diagnostic_writing_mixed":
            from .writing_replay import DiagnosticWritingProvider
            provider = DiagnosticWritingProvider(provider_options, ctx.execution_config.get("writing_replay"))
        elif provider_options["mode"] == "diagnostic_writing_candidate_import":
            from .writing_candidate_import import DiagnosticWritingCandidateImport
            provider = DiagnosticWritingCandidateImport(provider_options, ctx.execution_config.get("writing_candidate_import"))
        elif provider_options["mode"] == "diagnostic_writing_commitment_partition":
            from .commitment_drafting import CommitmentPartitionProvider
            provider = CommitmentPartitionProvider(provider_options, ctx.execution_config.get("writing_candidate_import"),
                                                   ctx.execution_config.get("commitment_drafting"))
        else:
            raise PipelineFailure("PROVIDER_MODE_INVALID")
        facts = {item.fact_id: item for item in prepared.facts}
        tables = {item.table_id: item for item in prepared.tables}
        excerpts = {item.reference_id: item for item in prepared.reference_excerpts}
        writing_partition = provider_options["mode"] == "diagnostic_writing_commitment_partition"
        writing_import = writing_partition or provider_options["mode"] == "diagnostic_writing_candidate_import"
        writing_diagnostic = writing_import or provider_options["mode"] == "diagnostic_writing_mixed"
        if writing_diagnostic:
            if writing_partition:
                from .commitment_drafting import build_partition_unit_input
                def build_writing_unit_input(prepared, plan, task, options, constraints, proposals):
                    return build_partition_unit_input(prepared, plan, task, options, constraints, proposals,
                                                      ctx.execution_config["commitment_drafting"])
            elif writing_import:
                from .writing_candidate_import import build_import_unit_input as build_writing_unit_input
            else:
                from .writing_replay import build_writing_unit_input
            base_inputs = {task.unit_id: build_writing_unit_input(prepared, plan, task, provider_options,
                           project_constraints, value["proposals"])
                           for plan in prepared.sections for task in plan.writing_tasks}
            provider.preflight(ctx, PreparedBid.model_validate(frozen_prepared).model_dump(mode="json"), base_inputs)
        sections = []
        writing_review = []
        for plan in prepared.sections:
            plan_data = plan.model_dump(mode="json")
            requirements = [item.model_dump(mode="json") for item in prepared.requirements if item.requirement_id in plan.requirement_refs]
            task_outputs = []
            # Each response unit commits before the section assembly. A later
            # unit failure does not discard or recall successfully written text.
            for task in plan.writing_tasks:
                task_requirements = [item for item in requirements if item["requirement_id"] in task.requirement_refs]
                references = [excerpts[ref].model_dump(mode="json") for ref in task.reference_refs]
                source_contexts = [context for requirement in task_requirements
                                   for context in requirement.get("source_contexts", [])]
                context_proof_hashes = list(dict.fromkeys(item["proof_sha256"] for item in source_contexts))
                context = {"section_id": task.unit_id, "title": task.title,
                           "writing_task": task.model_dump(mode="json"), "reference_excerpts": references,
                           "source_contexts": source_contexts}
                if project_constraints is not None:
                    import copy
                    context["project_constraints"] = copy.deepcopy(project_constraints["plan"]["tasks"][task.unit_id])
                    validate_project_constraint_context(task.model_dump(mode="json"), context, project_constraints)
                project_source_refs = list(dict.fromkeys(item["source_id"] for item in context.get("project_constraints", [])))
                source_ids = {ref["source_id"] for ref in references}
                source_ids.update(project_source_refs)
                source_ids.update(ref for requirement in task_requirements for ref in requirement["source_refs"])
                source_ids.update(ref for item in source_contexts for ref in item["source_refs"])
                source_ids.update(ref for table_id in task.table_refs for ref in tables[table_id].source_refs)
                task_input = {"context": context, "requirements": task_requirements,
                              "sources": [{"source_id": item.source_id, "sha256": item.sha256} for item in prepared.sources if item.source_id in source_ids],
                              "tables": [tables[ref].model_dump(mode="json") for ref in task.table_refs],
                              "source_context_proof_hashes": context_proof_hashes,
                              "provider": provider_options, "proposal": value["proposals"].get(task.unit_id)}
                if project_constraints is not None:
                    task_input["project_constraint_artifact_refs"] = project_constraints["artifact_refs"]
                    task_input["project_constraint_plan_sha256"] = project_constraints["plan_sha256"]
                if writing_diagnostic:
                    if writing_partition:
                        task_input = base_inputs[task.unit_id]
                    task_input = provider.unit_input(task.unit_id, task_input)
                correction_scope = task_input.get("writing_correction", {}).get("source_scope")
                commitment_base = task_input if "commitment_drafting" in task_input else None

                def write_task(task=task, context=context, task_requirements=task_requirements,
                               plan=plan, references=references, source_contexts=source_contexts,
                               context_proof_hashes=context_proof_hashes, project_source_refs=project_source_refs,
                               correction_scope=correction_scope, commitment_base=commitment_base):
                    project_constraint_checks = validate_project_constraint_context(task.model_dump(mode="json"), context,
                                                                                   project_constraints)
                    proposal = provider.generate(ctx, context, task_requirements)
                    source_refs = list(dict.fromkeys([*(item["source_id"] for item in references),
                        *(ref for requirement in task_requirements for ref in requirement["source_refs"]),
                        *(ref for item in source_contexts for ref in item["source_refs"]), *project_source_refs]))
                    correction_refs = [item["source_id"] for item in correction_scope["sources"]] if correction_scope else []
                    source_refs = list(dict.fromkeys([*source_refs, *correction_refs]))
                    if commitment_base is not None:
                        from .commitment_drafting import commitment_content_source_refs
                        source_refs = list(dict.fromkeys([*source_refs, *commitment_content_source_refs(commitment_base)]))
                    blocks = [HeadingBlock(text=task.title, level=plan.level + 1,
                                           requirement_refs=task.requirement_refs, source_refs=source_refs)]
                    for paragraph in proposal.paragraphs:
                        blocks.append(ParagraphBlock(text=paragraph, content_role="proposal",
                                                     requirement_refs=task.requirement_refs, source_refs=source_refs))
                    if proposal.steps:
                        blocks.append(ListBlock(items=proposal.steps, content_role="proposal",
                                                requirement_refs=task.requirement_refs, source_refs=source_refs))
                    for table_id in task.table_refs:
                        blocks.append(TableBlock(table=tables[table_id], requirement_refs=task.requirement_refs,
                                                 source_refs=correction_refs))
                    result = {"blocks": [block.model_dump(mode="json") for block in blocks],
                            "review": {"unit_id": task.unit_id, "section_id": plan.section_id,
                                       "document_role": plan.document_role,
                                       "title": task.title, "operation": task.operation,
                                       "requirement_refs": task.requirement_refs, "reference_refs": task.reference_refs,
                                       "must_cover": task.must_cover, "completion_criteria": task.completion_criteria,
                                       "coverage": [item.model_dump(mode="json") for item in proposal.coverage],
                                       "source_context_refs": task.source_context_refs,
                                       "source_context_proof_hashes": context_proof_hashes,
                                       "project_constraint_bindings": project_constraint_checks,
                                       "project_constraint_source_refs": project_source_refs,
                                       "context_review_status": "not_checked" if source_contexts else "not_applicable",
                                       "semantic_status": "requires_source_review"}}
                    if correction_scope is not None:
                        result["review"].update(correction_context_requirement_refs=[item["requirement_id"]
                            for item in correction_scope["requirements"]],
                            correction_source_scope_sha256=digest(correction_scope), correction_source_refs=correction_refs)
                    if commitment_base is not None:
                        from .commitment_drafting import commitment_review
                        result["review"]["commitment_drafting"] = commitment_review(commitment_base)
                    return result

                task_output = ctx.run_unit(task.unit_id, task_input, write_task)
                task_outputs.append(task_output)
                writing_review.append(task_output["review"])
            unit_input = {"plan": plan_data, "requirements": requirements,
                          "prepared_hash": hashlib.sha256(prepared.model_dump_json().encode()).hexdigest(),
                          "provider": provider_options, "proposals": value["proposals"].get(plan.section_id),
                          "writing_output_hashes": [digest(output) for output in task_outputs]}

            def write_section(plan=plan, plan_data=plan_data, requirements=requirements, task_outputs=task_outputs):
                blocks = []
                for prescribed in plan.fixed_content:
                    if prescribed.kind == "paragraph":
                        blocks.append(ParagraphBlock(text=prescribed.text, content_role="neutral",
                                                     fact_refs=prescribed.fact_refs,
                                                     source_refs=prescribed.source_refs, requirement_refs=plan.requirement_refs))
                    else:
                        blocks.append(TableBlock(table=tables[prescribed.table_ref], requirement_refs=plan.requirement_refs))
                for fact_id in plan.fact_refs:
                    fact = facts[fact_id]
                    blocks.append(ParagraphBlock(text=f"{fact.label}：{fact.value}", content_role="fact",
                                                 fact_refs=[fact_id], source_refs=fact.source_refs,
                                                 requirement_refs=plan.requirement_refs))
                for task_output in task_outputs:
                    blocks.extend(task_output["blocks"])
                if plan.kind == "technical" and not plan.writing_tasks and not plan.fixed_content and plan.render_body:
                    proposal = provider.generate(ctx, plan_data, requirements)
                    proposal = ProposalContent.model_validate(proposal.model_dump())
                    for paragraph in proposal.paragraphs:
                        blocks.append(ParagraphBlock(text=paragraph, content_role="proposal", requirement_refs=plan.requirement_refs))
                    if proposal.steps:
                        blocks.append(ListBlock(items=proposal.steps, content_role="proposal", requirement_refs=plan.requirement_refs))
                assigned_tables = {ref for task in plan.writing_tasks for ref in task.table_refs}
                assigned_tables.update(item.table_ref for item in plan.fixed_content if item.kind == "table_ref")
                for table_id in plan.table_refs:
                    if table_id in assigned_tables:
                        continue
                    blocks.append(TableBlock(table=tables[table_id], requirement_refs=plan.requirement_refs))
                for attachment_id in plan.attachment_refs:
                    blocks.append(AttachmentBlock(attachment_id=attachment_id, requirement_refs=plan.requirement_refs))
                for issue in prepared.issues:
                    if issue.section_id == plan.section_id:
                        blocks.append(IssueBlock(issue_id=issue.issue_id, requirement_refs=issue.requirement_refs))
                if not blocks and plan.render_body and plan.native_format is None:
                    raise PipelineFailure("EMPTY_SECTION")
                return Section(section_id=plan.section_id, title=plan.title, requirement_refs=plan.requirement_refs,
                               document_role=plan.document_role,
                               native_format=plan.native_format,
                               level=plan.level, start_on_new_page=plan.start_on_new_page,
                               render_body=plan.render_body, blocks=blocks).model_dump(mode="json")

            sections.append(ctx.run_unit(plan.section_id, unit_input, write_section))
        package = DocumentPackage(**prepared.model_dump(exclude={"sections", "tables", "input_kind"}),
                                  title=f"{prepared.project.name} 投标文件", sections=sections)
        diagnostics = dict(value.get("chain_diagnostics", {}))
        if writing_diagnostic:
            diagnostics["commitment_partition" if writing_partition else
                        "writing_candidate_import" if writing_import else "writing_replay"] = provider.summary()
            diagnostics["publication_status"] = "diagnostic_only"
        return {"package": package.model_dump(mode="json"), "source_artifacts": value["source_artifacts"],
                "input_kind": prepared.input_kind,
                "chain_diagnostics": diagnostics,
                "writing_review": writing_review, "expected_table_ids": list(tables),
                "expected_section_document_roles": {item.section_id: item.document_role for item in prepared.sections},
                "expected_project_constraint_bindings": project_constraint_trace_bindings(project_constraints),
                "expected_table_links": {table_id: {"section_id": table.section_id,
                    "requirement_refs": list(table.requirement_refs)} for table_id, table in tables.items()},
                "generation_mode": provider_options["mode"], "external_model_verified": provider_options["mode"] == "chat_completions"}

    def check(ctx, value):
        package = DocumentPackage.model_validate(value["package"])
        # Layout is an N3 source-derived contract. Re-sealing a modified N4
        # layout must not make it authoritative over the frozen plan.
        plan_node = "N3" if any(node["key"] == "N3" for node in ctx._state["nodes"]) else "N0"
        plan_record = next(node for node in ctx._state["nodes"] if node["key"] == plan_node)
        from .project_review import SNAPSHOT_KEY, project_review_input
        expected_input = project_review_input(ctx, ctx.executor._checkpoint_data(plan_record))
        frozen_plan = expected_input["prepared"]
        if SNAPSHOT_KEY in ctx.execution_config:
            encoded = package.model_dump(mode="json")
            if (any(encoded[key] != frozen_plan[key] for key in
                    ("project", "sources", "requirements", "facts", "attachments", "issues", "review_actions"))
                    or value["source_artifacts"] != expected_input["source_artifacts"]):
                raise PipelineFailure("PROJECT_REVIEW_ASSEMBLY_MISMATCH")
            expected_tables = {item["table_id"]: item for item in frozen_plan["tables"]}
            if any(block.table.model_dump(mode="json") != expected_tables.get(block.table.table_id)
                   for section in package.sections for block in section.blocks if isinstance(block, TableBlock)):
                raise PipelineFailure("PROJECT_REVIEW_TABLE_MISMATCH")
            by_section = {section.section_id: section for section in package.sections}
            reviewed_facts = {fact["fact_id"]: fact for fact in frozen_plan["facts"]
                              if fact["fact_id"].startswith("PROJECT-ANSWER-")}
            for plan in frozen_plan["sections"]:
                blocks = by_section[plan["section_id"]].blocks
                for index, item in enumerate(plan["fixed_content"]):
                    if item["kind"] == "paragraph" and set(item["fact_refs"]) & reviewed_facts.keys():
                        block = blocks[index]
                        if (not isinstance(block, ParagraphBlock) or block.text != item["text"]
                                or block.fact_refs != item["fact_refs"] or block.source_refs != item["source_refs"]):
                            raise PipelineFailure("PROJECT_REVIEW_FIXED_PARAGRAPH_CHANGED")
                for fact_ref in plan["fact_refs"]:
                    if fact_ref in reviewed_facts:
                        fact = reviewed_facts[fact_ref]
                        if sum(isinstance(b, ParagraphBlock) and b.fact_refs == [fact_ref]
                               and b.source_refs == fact["source_refs"] and b.text == f'{fact["label"]}：{fact["value"]}'
                               for b in blocks) != 1:
                            raise PipelineFailure("PROJECT_REVIEW_DECISION_PARAGRAPH_CHANGED")
        expected_native = {section["section_id"]: section["native_format"]
                           for section in frozen_plan["sections"] if section.get("native_format") is not None}
        actual_native = {section.section_id: section.native_format.model_dump(mode="json")
                         for section in package.sections if section.native_format is not None}
        if actual_native != expected_native:
            raise PipelineFailure("NATIVE_FORMAT_PLAN_MISMATCH")
        from .writing_replay import validate_correction_links
        correction_links = validate_correction_links(ctx, value, package)
        from .writing_candidate_import import validate_candidate_import_links
        correction_links.extend(validate_candidate_import_links(ctx, value, package))
        from .commitment_drafting import validate_commitment_partition_links
        commitment_links = validate_commitment_partition_links(ctx, value, package)
        if commitment_links is not None:
            correction_links.extend(commitment_links["historical_correction_links"])
        section_roles = {item.section_id: item.document_role for item in package.sections}
        expected_roles = value.get("expected_section_document_roles")
        if ((expected_roles is not None and expected_roles != section_roles) or
                expected_roles is None and "review" in section_roles.values()):
            raise PipelineFailure("SECTION_DOCUMENT_ROLE_MISMATCH")
        from .project_constraint_runtime import load_project_constraints, project_constraint_trace_bindings
        project_constraints = load_project_constraints(ctx)
        expected_project_constraints = project_constraint_trace_bindings(project_constraints)
        if expected_project_constraints is not None:
            reviews = value.get("writing_review", [])
            actual_project_constraints = {item["unit_id"]: item.get("project_constraint_bindings") for item in reviews}
            if (len(actual_project_constraints) != len(reviews) or
                    value.get("expected_project_constraint_bindings") != expected_project_constraints or
                    actual_project_constraints != expected_project_constraints):
                raise PipelineFailure("PROJECT_CONSTRAINT_REVIEW_BINDING_MISSING_OR_CHANGED")
        # Revalidate source bytes immediately before rendering. Source refs are not
        # semantic proof: the scoped human source review is recorded separately.
        for source in [*package.sources, *package.attachments]:
            if not source.path.is_file() or file_sha256(source.path) != source.sha256:
                raise PipelineFailure("SOURCE_HASH_MISMATCH")
        table_ids = [block.table.table_id for section in package.sections for block in section.blocks if isinstance(block, TableBlock)]
        if len(set(table_ids)) != len(table_ids) or set(table_ids) != set(value.get("expected_table_ids", table_ids)):
            raise PipelineFailure("TABLE_ASSEMBLY_INCOMPLETE")
        # Preserve every N3 table-to-requirement edge, including one requirement
        # answered by two distinct forms. Aggregate table/RID counts miss losing
        # just one edge. This checks assembly, not substantive compliance.
        expected_links = value.get("expected_table_links")
        if expected_links is None and value.get("input_kind") == "automatic":
            raise PipelineFailure("TABLE_REQUIREMENT_LINKS_MISSING")
        table_links = []
        if expected_links is not None:
            if not isinstance(expected_links, dict) or set(expected_links) != set(table_ids):
                raise PipelineFailure("TABLE_REQUIREMENT_LINK_MISMATCH")
            for section in package.sections:
                for block in section.blocks:
                    if not isinstance(block, TableBlock):
                        continue
                    table = block.table
                    actual = {"section_id": table.section_id, "requirement_refs": list(table.requirement_refs)}
                    if (expected_links[table.table_id] != actual or table.section_id != section.section_id or
                            set(table.requirement_refs) - set(section.requirement_refs)):
                        raise PipelineFailure("TABLE_REQUIREMENT_LINK_MISMATCH")
                    table_links.append({"table_id": table.table_id, **actual,
                                        "document_role": section.document_role,
                                        "assembly_status": "checked", "satisfaction_status": "not_determined"})
        checks = {"schema": "passed", "source_hashes": "passed",
                  "semantic_fact_support": "requires_source_review",
                  "writing_correction_contexts": correction_links,
                  "model_fact_invention": "requires_source_review",
                  "requirement_count": len(package.requirements), "section_count": len(package.sections),
                  "response_section_count": sum(item.document_role == "response" for item in package.sections),
                  "review_section_count": sum(item.document_role == "review" for item in package.sections),
                  "attachment_count": len(package.attachments), "issue_count": len(package.issues),
                  "response_attachment_count": sum(section_roles[item.section_id] == "response" for item in package.attachments),
                  "review_attachment_count": sum(section_roles[item.section_id] == "review" for item in package.attachments),
                  "response_issue_count": sum(section_roles[item.section_id] == "response" for item in package.issues),
                  "review_issue_count": sum(section_roles[item.section_id] == "review" for item in package.issues),
                  "table_count": len(table_ids), "writing_units": value.get("writing_review", []),
                  "response_table_count": sum(isinstance(block, TableBlock) for section in package.sections
                      if section.document_role == "response" for block in section.blocks),
                  "review_table_count": sum(isinstance(block, TableBlock) for section in package.sections
                      if section.document_role == "review" for block in section.blocks),
                  "table_requirement_links": table_links,
                  "table_requirement_links_status": "checked" if expected_links is not None else "not_recorded",
                  "review_actions": [item.model_dump(mode="json") for item in package.review_actions],
                  "pending_review_count": len(package.review_actions),
                  "source_context_review": [], "pending_context_review_count": 0,
                  "project_constraint_bindings": expected_project_constraints,
                  "project_constraint_compliance": "requires_source_review" if project_constraints is not None else "not_recorded",
                  "requirement_coverage": []}
        if commitment_links is not None:
            checks["commitment_partition"] = commitment_links
        actions_by_requirement = {ref: action for action in package.review_actions for ref in action.requirement_refs}
        nonbody_requirements = {item.requirement_id for item in package.requirements if item.response_mode == "procedure"}
        if value.get("input_kind") == "automatic" and set(actions_by_requirement) != nonbody_requirements:
            raise PipelineFailure("NON_BODY_REVIEW_ACTION_MISSING")
        by_requirement = {item.requirement_id: item for item in package.requirements}
        reviews = value.get("writing_review", [])
        for review in reviews:
            refs = review["requirement_refs"]
            if (set(refs) - set(by_requirement) or review.get("section_id") not in section_roles or
                    review.get("document_role", "response") != section_roles[review["section_id"]]):
                raise PipelineFailure("SOURCE_CONTEXT_WRITING_REVIEW_MISMATCH")
            expected_contexts = [context for requirement in package.requirements if requirement.requirement_id in refs
                                 for context in requirement.source_contexts]
            if (review.get("source_context_refs", []) != [item.context_id for item in expected_contexts] or
                    review.get("source_context_proof_hashes", []) != list(dict.fromkeys(
                        item.proof_sha256 for item in expected_contexts)) or
                    expected_contexts and review.get("context_review_status") != "not_checked"):
                raise PipelineFailure("SOURCE_CONTEXT_WRITING_REVIEW_MISMATCH")
        for requirement in package.requirements:
            response_sections = [section.section_id for section in package.sections
                if section.document_role == "response" and section.render_body and
                requirement.requirement_id in section.requirement_refs]
            review_sections = [section.section_id for section in package.sections
                if section.document_role == "review" and section.render_body and
                requirement.requirement_id in section.requirement_refs]
            issues = [i.issue_id for i in package.issues if requirement.requirement_id in i.requirement_refs]
            response_issues = [i.issue_id for i in package.issues
                if i.section_id in response_sections and requirement.requirement_id in i.requirement_refs]
            coverage = {"requirement_id": requirement.requirement_id,
                        "status": "pending_manual" if issues else "no_body_required" if requirement.response_mode == "procedure" else "drafted_for_review",
                        "issue_ids": issues, "response_issue_ids": response_issues,
                        "response_section_ids": response_sections, "review_section_ids": review_sections,
                        "satisfaction_status": "not_determined"}
            if requirement.response_mode == "procedure":
                action = actions_by_requirement.get(requirement.requirement_id)
                coverage.update(body_status="not_required", review_status=action.status if action else "not_recorded",
                                review_action_ids=[action.action_id] if action else [], satisfaction_status="not_determined")
                if action:
                    coverage.update(source_scope_status=action.source_scope_status,
                                    source_scope_refs=[item.source_ref for item in action.source_scope_contexts])
            if requirement.source_contexts:
                if requirement.response_mode == "writing" and not any(
                        requirement.requirement_id in review["requirement_refs"] and
                        section_roles.get(review.get("section_id")) == "response" for review in reviews):
                    raise PipelineFailure("SOURCE_CONTEXT_WRITING_REVIEW_MISSING")
                coverage.update(body_status="drafted_for_review" if requirement.response_mode == "writing" else
                    "pending_manual" if issues else "assembled_for_review", applicability_status="not_checked" if any(
                    context.operator == "service_applicability" for context in requirement.source_contexts) else "not_applicable",
                    satisfaction_status="not_determined", source_context_refs=[context.context_id for context in requirement.source_contexts])
                for context in requirement.source_contexts:
                    checks["source_context_review"].append({"requirement_id": requirement.requirement_id,
                        "context_id": context.context_id, "operator": context.operator, "proof_sha256": context.proof_sha256,
                        "provenance_status": "checked", "review_status": "not_checked",
                        "assembly_placement_status": "checked_for_known_components" if context.operator == "exclude_commercial_quotation" else "not_applicable",
                        "semantic_compliance": "requires_source_review", "satisfaction_status": "not_determined"})
            if review_sections and requirement.response_mode != "procedure":
                # The quoted rule and its review table are not a drafted answer.
                # The response-side issue is independently required by the model.
                coverage.update(status="pending_manual", body_status="pending_manual",
                                review_status="not_checked", review_content_status="retained_for_review")
            checks["requirement_coverage"].append(coverage)
        checks["pending_context_review_count"] = len(checks["source_context_review"])
        if ctx.execution_config.get("source_response_audit", False):
            if value.get("input_kind") != "automatic":
                raise PipelineFailure("SOURCE_AUDIT_AUTOMATIC_INPUT_REQUIRED")
            from .source_response_audit import build_source_response_audit
            n1 = next(node for node in ctx._state["nodes"] if node["key"] == "N1")
            frozen_reading = ctx.executor._checkpoint_data(n1)
            audit = build_source_response_audit(frozen_reading["parsed"], frozen_reading["manifest"],
                                                package.model_dump(mode="json"))
            capture = ctx.capture("source-response-audit", audit)
            checks["source_response_audit"] = {
                key: audit[key] for key in ("version", "status", "semantic_completeness", "source_unit_count",
                    "quotation_counts", "requirement_count", "without_destination_count", "audit_sha256",
                    "reading_unit_count", "reading_counts", "reading_denominator_status")}
            checks["source_response_audit"].update(capture_status=capture["status"], capture_ref=capture.get("ref"))
        ctx.capture("validation", checks)
        return {**value, "validation": checks}

    def render(ctx, value):
        package_data = dict(value["package"])
        if "document_style" in ctx.execution_config:
            package_data["style"] = ctx.execution_config["document_style"]
        package = DocumentPackage.model_validate(package_data)
        diagnostic = ctx.artifact_scope == "diagnostic" or diagnostic_artifact(value)
        review_mode = ctx.execution_config.get("review_document_mode", "full_trace")
        review_inputs = {}
        if review_mode == "task_list":
            from .review_document import material_review_report
            review_inputs = {
                "material_review_report": material_review_report(ctx),
                "glyph_maps": ctx.execution_config.get("reading", {}).get("glyph_maps"),
            }
        rendered = render_package(package, ctx.artifact_dir / "documents", diagnostic=diagnostic,
                                  review_document_mode=review_mode, **review_inputs)
        review_index = rendered.pop("review_document_index", None)
        review_capture = review_index_ref = None
        if review_index is not None:
            import json
            # This is the generated checklist's business index, not a prompt or
            # trace dump. Keep the short-ID mapping even when debug capture is off.
            review_index_ref = ctx.store_bytes("review-document-index.json",
                json.dumps(review_index, ensure_ascii=False, sort_keys=True).encode("utf-8"), "application/json")
            review_capture = ctx.capture("review-document-index", {"index_ref": review_index_ref,
                                                                   "mode": review_mode})
        files = {}
        for key in ("bid_docx", "issues_docx"):
            path = Path(rendered[key]["path"])
            files[key] = ctx.store_bytes(("diagnostic-" if diagnostic else "") + key + ".docx", path.read_bytes(),
                                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        return {**({"diagnostic_files": files, "publication_status": "diagnostic_only", "artifact_scope": "diagnostic"}
                    if diagnostic else {"files": files}), "validation": value["validation"],
                "generation_mode": value["generation_mode"], "external_model_verified": value["external_model_verified"],
                "input_kind": value.get("input_kind", "manual_prepared"), "synthetic": package.synthetic,
                "chain_diagnostics": value.get("chain_diagnostics", {}),
                "warnings": rendered["warnings"], "issue_ids": rendered["issue_ids"],
                "review_document_mode": review_mode,
                "review_document_index": ({"capture_status": review_capture["status"], "ref": review_index_ref}
                                          if review_capture is not None else None),
                "review_action_ids": [item.action_id for item in package.review_actions],
                "source_artifacts": value["source_artifacts"]}

    # Actual source hashes prevent continuing a partially completed run after code
    # changes. A repair/fork is explicit and keeps the original record untouched.
    shared = ("pipeline.py", "models.py", "source_format_contracts.py", "preparation_rules.py", "runtime.py", "storage.py", "observability.py")
    writing_replay_keys = (("writing_replay", "run_purpose", "debug_capture")
                           if config.get("provider", {}).get("mode") == "diagnostic_writing_mixed" else ())
    if config.get("provider", {}).get("mode") == "diagnostic_writing_candidate_import":
        writing_replay_keys = ("writing_candidate_import", "run_purpose", "debug_capture")
    if config.get("provider", {}).get("mode") == "diagnostic_writing_commitment_partition":
        writing_replay_keys = ("writing_candidate_import", "commitment_drafting", "run_purpose", "debug_capture")
    commitment_versions = (("commitment_drafting.py", "commitment_prompt.py", "commitment_planning.py")
        if config.get("provider", {}).get("mode") == "diagnostic_writing_commitment_partition" else ())
    review_versions = ("project_review.py", "project_questions.py", "project_answers.py")
    nodes = [NodeSpec("N0", _version(*shared), freeze, config_keys=()),
             NodeSpec("N4", _version(*shared, *review_versions, "provider.py", "writing_prompt.py", "writing_replay.py", "writing_candidate_import.py", "diagnostic_replay.py", "diagnostics.py", *commitment_versions),
                      draft, config_keys=("provider", "project_answer_snapshot") + writing_replay_keys),
             NodeSpec("N5", _version(*shared, *review_versions, "provider.py", "writing_replay.py", "writing_candidate_import.py", "diagnostic_replay.py", "diagnostics.py", "source_response_audit.py", *commitment_versions), check,
                      config_keys=((("provider",) + writing_replay_keys) if writing_replay_keys else ()) + ("source_response_audit", "project_answer_snapshot")),
             NodeSpec("N6", _version(*shared, "rendering.py", "source_format_rendering.py", "project_answers.py", "project_questions.py", "review_document.py",
                                     "operator_summary.py", "review_source_context.py", "material_source_review.py", "material_recall.py"), render,
                      config_keys=("document_style", "review_document_mode", "reading"))]
    if config.get("pipeline", "prepared") == "automatic":
        from .chain import intake_nodes
        nodes = intake_nodes(root, _version, config) + nodes[1:]
    elif config.get("pipeline", "prepared") != "prepared":
        raise PipelineFailure("PIPELINE_MODE_INVALID")
    if config.get("review_before_writing"):
        from .project_review import ProjectReviewExecutor
        return ProjectReviewExecutor(root, nodes, execution_config=config)
    return Executor(root, nodes, execution_config=config)
