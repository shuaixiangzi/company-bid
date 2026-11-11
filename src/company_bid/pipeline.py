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
from .runtime import Executor, NodeSpec
from .storage import digest


class PipelineFailure(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _version(*names: str) -> str:
    digest = hashlib.sha256()
    for name in names:
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
                context = {"section_id": task.unit_id, "title": task.title,
                           "writing_task": task.model_dump(mode="json"), "reference_excerpts": references}
                source_ids = {ref["source_id"] for ref in references}
                source_ids.update(ref for requirement in task_requirements for ref in requirement["source_refs"])
                source_ids.update(ref for table_id in task.table_refs for ref in tables[table_id].source_refs)
                task_input = {"context": context, "requirements": task_requirements,
                              "sources": [{"source_id": item.source_id, "sha256": item.sha256} for item in prepared.sources if item.source_id in source_ids],
                              "tables": [tables[ref].model_dump(mode="json") for ref in task.table_refs],
                              "provider": provider_options, "proposal": value["proposals"].get(task.unit_id)}

                def write_task(task=task, context=context, task_requirements=task_requirements,
                               plan=plan, references=references):
                    proposal = provider.generate(ctx, context, task_requirements)
                    source_refs = list(dict.fromkeys(item["source_id"] for item in references))
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
                "generation_mode": provider_options["mode"], "external_model_verified": provider_options["mode"] == "chat_completions"}

    def check(ctx, value):
        package = DocumentPackage.model_validate(value["package"])
        # Revalidate source bytes immediately before rendering. Source refs are not
        # semantic proof: the scoped human source review is recorded separately.
        for source in [*package.sources, *package.attachments]:
            if not source.path.is_file() or file_sha256(source.path) != source.sha256:
                raise PipelineFailure("SOURCE_HASH_MISMATCH")
        table_ids = [block.table.table_id for section in package.sections for block in section.blocks if isinstance(block, TableBlock)]
        if len(set(table_ids)) != len(table_ids) or set(table_ids) != set(value.get("expected_table_ids", table_ids)):
            raise PipelineFailure("TABLE_ASSEMBLY_INCOMPLETE")
        checks = {"schema": "passed", "source_hashes": "passed",
                  "semantic_fact_support": "requires_source_review",
                  "model_fact_invention": "requires_source_review",
                  "requirement_count": len(package.requirements), "section_count": len(package.sections),
                  "attachment_count": len(package.attachments), "issue_count": len(package.issues),
                  "table_count": len(table_ids), "writing_units": value.get("writing_review", []),
                  "requirement_coverage": []}
        for requirement in package.requirements:
            issues = [i.issue_id for i in package.issues if requirement.requirement_id in i.requirement_refs]
            checks["requirement_coverage"].append({"requirement_id": requirement.requirement_id,
                                                   "status": "pending_manual" if issues else "no_body_required" if requirement.response_mode == "procedure" else "drafted_for_review",
                                                   "issue_ids": issues})
        ctx.capture("validation", checks)
        return {**value, "validation": checks}

    def render(ctx, value):
        package_data = dict(value["package"])
        if "document_style" in ctx.execution_config:
            package_data["style"] = ctx.execution_config["document_style"]
        package = DocumentPackage.model_validate(package_data)
        rendered = render_package(package, ctx.artifact_dir / "documents")
        files = {}
        for key in ("bid_docx", "issues_docx"):
            path = Path(rendered[key]["path"])
            files[key] = ctx.store_bytes(key + ".docx", path.read_bytes(),
                                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        return {"files": files, "validation": value["validation"],
                "generation_mode": value["generation_mode"], "external_model_verified": value["external_model_verified"],
                "input_kind": value.get("input_kind", "manual_prepared"), "synthetic": package.synthetic,
                "chain_diagnostics": value.get("chain_diagnostics", {}),
                "warnings": rendered["warnings"], "issue_ids": rendered["issue_ids"],
                "source_artifacts": value["source_artifacts"]}

    # Actual source hashes prevent continuing a partially completed run after code
    # changes. A repair/fork is explicit and keeps the original record untouched.
    shared = ("pipeline.py", "models.py", "runtime.py", "storage.py", "observability.py")
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
