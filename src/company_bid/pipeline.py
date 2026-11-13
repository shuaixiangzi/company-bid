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
                    "planning.py": ("plan_component_controls.py", "project_constraint_runtime.py", "format_continuation_sources.py", "contract_pending_plan.py", "contract_review_slots.py"),
                    "pipeline.py": ("project_constraint_runtime.py",),
                    "project_constraint_runtime.py": ("project_constraints.py", "contract_pending_plan.py"),
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
        prepared = PreparedBid.model_validate(value["prepared"])
        from .project_constraint_runtime import (load_project_constraints, validate_project_constraint_context,
                                                project_constraint_trace_bindings)
        project_constraints = load_project_constraints(ctx, prepared.model_dump(mode="json"))
        provider_options = ctx.execution_config.get("provider", {"mode": "prepared"})
        if provider_options["mode"] == "prepared":
            provider = PreparedProposalProvider(value["proposals"])
        elif provider_options["mode"] == "chat_completions":
            options = {k: v for k, v in provider_options.items() if k != "mode"}
            provider = ChatProposalProvider(ChatConfig(**options))
        else:
            raise PipelineFailure("PROVIDER_MODE_INVALID")
        facts = {item.fact_id: item for item in prepared.facts}
        tables = {item.table_id: item for item in prepared.tables}
        excerpts = {item.reference_id: item for item in prepared.reference_excerpts}
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

                def write_task(task=task, context=context, task_requirements=task_requirements,
                               plan=plan, references=references, source_contexts=source_contexts,
                               context_proof_hashes=context_proof_hashes, project_source_refs=project_source_refs):
                    project_constraint_checks = validate_project_constraint_context(task.model_dump(mode="json"), context,
                                                                                   project_constraints)
                    proposal = provider.generate(ctx, context, task_requirements)
                    source_refs = list(dict.fromkeys([*(item["source_id"] for item in references),
                        *(ref for requirement in task_requirements for ref in requirement["source_refs"]),
                        *(ref for item in source_contexts for ref in item["source_refs"]), *project_source_refs]))
                    blocks = [HeadingBlock(text=task.title, level=plan.level + 1,
                                           requirement_refs=task.requirement_refs, source_refs=source_refs)]
                    for paragraph in proposal.paragraphs:
                        blocks.append(ParagraphBlock(text=paragraph, content_role="proposal",
                                                     requirement_refs=task.requirement_refs, source_refs=source_refs))
                    if proposal.steps:
                        blocks.append(ListBlock(items=proposal.steps, content_role="proposal",
                                                requirement_refs=task.requirement_refs, source_refs=source_refs))
                    for table_id in task.table_refs:
                        blocks.append(TableBlock(table=tables[table_id], requirement_refs=task.requirement_refs))
                    return {"blocks": [block.model_dump(mode="json") for block in blocks],
                            "review": {"unit_id": task.unit_id, "section_id": plan.section_id,
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
                if not blocks and plan.render_body:
                    raise PipelineFailure("EMPTY_SECTION")
                return Section(section_id=plan.section_id, title=plan.title, requirement_refs=plan.requirement_refs,
                               level=plan.level, start_on_new_page=plan.start_on_new_page,
                               render_body=plan.render_body, blocks=blocks).model_dump(mode="json")

            sections.append(ctx.run_unit(plan.section_id, unit_input, write_section))
        package = DocumentPackage(**prepared.model_dump(exclude={"sections", "tables", "input_kind"}),
                                  title=f"{prepared.project.name} 投标文件", sections=sections)
        return {"package": package.model_dump(mode="json"), "source_artifacts": value["source_artifacts"],
                "input_kind": prepared.input_kind,
                "chain_diagnostics": value.get("chain_diagnostics", {}),
                "writing_review": writing_review, "expected_table_ids": list(tables),
                "expected_project_constraint_bindings": project_constraint_trace_bindings(project_constraints),
                "expected_table_links": {table_id: {"section_id": table.section_id,
                    "requirement_refs": list(table.requirement_refs)} for table_id, table in tables.items()},
                "generation_mode": provider_options["mode"], "external_model_verified": provider_options["mode"] == "chat_completions"}

    def check(ctx, value):
        package = DocumentPackage.model_validate(value["package"])
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
                                        "assembly_status": "checked", "satisfaction_status": "not_determined"})
        checks = {"schema": "passed", "source_hashes": "passed",
                  "semantic_fact_support": "requires_source_review",
                  "model_fact_invention": "requires_source_review",
                  "requirement_count": len(package.requirements), "section_count": len(package.sections),
                  "attachment_count": len(package.attachments), "issue_count": len(package.issues),
                  "table_count": len(table_ids), "writing_units": value.get("writing_review", []),
                  "table_requirement_links": table_links,
                  "table_requirement_links_status": "checked" if expected_links is not None else "not_recorded",
                  "review_actions": [item.model_dump(mode="json") for item in package.review_actions],
                  "pending_review_count": len(package.review_actions),
                  "source_context_review": [], "pending_context_review_count": 0,
                  "project_constraint_bindings": expected_project_constraints,
                  "project_constraint_compliance": "requires_source_review" if project_constraints is not None else "not_recorded",
                  "requirement_coverage": []}
        actions_by_requirement = {ref: action for action in package.review_actions for ref in action.requirement_refs}
        nonbody_requirements = {item.requirement_id for item in package.requirements if item.response_mode == "procedure"}
        if value.get("input_kind") == "automatic" and set(actions_by_requirement) != nonbody_requirements:
            raise PipelineFailure("NON_BODY_REVIEW_ACTION_MISSING")
        by_requirement = {item.requirement_id: item for item in package.requirements}
        reviews = value.get("writing_review", [])
        for review in reviews:
            refs = review["requirement_refs"]
            if set(refs) - set(by_requirement):
                raise PipelineFailure("SOURCE_CONTEXT_WRITING_REVIEW_MISMATCH")
            expected_contexts = [context for requirement in package.requirements if requirement.requirement_id in refs
                                 for context in requirement.source_contexts]
            if (review.get("source_context_refs", []) != [item.context_id for item in expected_contexts] or
                    review.get("source_context_proof_hashes", []) != list(dict.fromkeys(
                        item.proof_sha256 for item in expected_contexts)) or
                    expected_contexts and review.get("context_review_status") != "not_checked"):
                raise PipelineFailure("SOURCE_CONTEXT_WRITING_REVIEW_MISMATCH")
        for requirement in package.requirements:
            issues = [i.issue_id for i in package.issues if requirement.requirement_id in i.requirement_refs]
            coverage = {"requirement_id": requirement.requirement_id,
                        "status": "pending_manual" if issues else "no_body_required" if requirement.response_mode == "procedure" else "drafted_for_review",
                        "issue_ids": issues}
            if requirement.response_mode == "procedure":
                action = actions_by_requirement.get(requirement.requirement_id)
                coverage.update(body_status="not_required", review_status=action.status if action else "not_recorded",
                                review_action_ids=[action.action_id] if action else [], satisfaction_status="not_determined")
                if action:
                    coverage.update(source_scope_status=action.source_scope_status,
                                    source_scope_refs=[item.source_ref for item in action.source_scope_contexts])
            if requirement.source_contexts:
                if requirement.response_mode == "writing" and not any(
                        requirement.requirement_id in review["requirement_refs"] for review in reviews):
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
            checks["requirement_coverage"].append(coverage)
        checks["pending_context_review_count"] = len(checks["source_context_review"])
        ctx.capture("validation", checks)
        return {**value, "validation": checks}

    def render(ctx, value):
        package_data = dict(value["package"])
        if "document_style" in ctx.execution_config:
            package_data["style"] = ctx.execution_config["document_style"]
        package = DocumentPackage.model_validate(package_data)
        diagnostic = ctx.artifact_scope == "diagnostic" or diagnostic_artifact(value)
        rendered = render_package(package, ctx.artifact_dir / "documents", diagnostic=diagnostic)
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
                "review_action_ids": [item.action_id for item in package.review_actions],
                "source_artifacts": value["source_artifacts"]}

    # Actual source hashes prevent continuing a partially completed run after code
    # changes. A repair/fork is explicit and keeps the original record untouched.
    shared = ("pipeline.py", "models.py", "preparation_rules.py", "runtime.py", "storage.py", "observability.py")
    nodes = [NodeSpec("N0", _version(*shared), freeze, config_keys=()),
             NodeSpec("N4", _version(*shared, "provider.py", "writing_prompt.py"), draft, config_keys=("provider",)),
             NodeSpec("N5", _version(*shared), check, config_keys=()),
             NodeSpec("N6", _version(*shared, "rendering.py"), render, config_keys=("document_style",))]
    if config.get("pipeline", "prepared") == "automatic":
        from .chain import intake_nodes
        nodes = intake_nodes(root, _version, config) + nodes[1:]
    elif config.get("pipeline", "prepared") != "prepared":
        raise PipelineFailure("PIPELINE_MODE_INVALID")
    return Executor(root, nodes, execution_config=config)
