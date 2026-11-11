"""M1 slice: freeze prepared input -> write -> check/assemble -> render.

N1-N3 are explicitly supplied by a human-prepared snapshot in this milestone.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import platform
from pathlib import Path

from .models import (AttachmentBlock, DocumentPackage, IssueBlock, ListBlock,
                     ParagraphBlock, PreparedBid, ProposalContent, Section, TableBlock,
                     file_sha256)
from .provider import ChatConfig, ChatProposalProvider, PreparedProposalProvider, ProviderFailure
from .rendering import render_package
from .runtime import Executor, NodeSpec


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

    def freeze(ctx, value):
        prepared = PreparedBid.model_validate(value["prepared"])
        validate_sources(prepared)
        data = prepared.model_dump(mode="json")
        refs = []
        for group in ("sources", "attachments"):
            for item in data[group]:
                path = Path(item["path"])
                ref = ctx.store_bytes("source" + path.suffix.lower(), path.read_bytes())
                if ref["sha256"] != item["sha256"]:
                    raise PipelineFailure("SOURCE_CHANGED_DURING_FREEZE")
                # Pin real copied bytes rather than mutable outside input paths.
                item["path"] = str(root / ref["path"])
                refs.append(ref)
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
        sections = []
        for plan in prepared.sections:
            plan_data = plan.model_dump(mode="json")
            requirements = [item.model_dump(mode="json") for item in prepared.requirements if item.requirement_id in plan.requirement_refs]
            unit_input = {"plan": plan_data, "requirements": requirements,
                          "prepared_hash": hashlib.sha256(prepared.model_dump_json().encode()).hexdigest(),
                          "provider": provider_options, "proposals": value["proposals"].get(plan.section_id)}

            def write_section(plan=plan, plan_data=plan_data, requirements=requirements):
                blocks = []
                for fact_id in plan.fact_refs:
                    fact = facts[fact_id]
                    blocks.append(ParagraphBlock(text=f"{fact.label}：{fact.value}", content_role="fact",
                                                 fact_refs=[fact_id], source_refs=fact.source_refs,
                                                 requirement_refs=plan.requirement_refs))
                if plan.kind == "technical":
                    proposal = provider.generate(ctx, plan_data, requirements)
                    proposal = ProposalContent.model_validate(proposal.model_dump())
                    for paragraph in proposal.paragraphs:
                        blocks.append(ParagraphBlock(text=paragraph, content_role="proposal", requirement_refs=plan.requirement_refs))
                    if proposal.steps:
                        blocks.append(ListBlock(items=proposal.steps, content_role="proposal", requirement_refs=plan.requirement_refs))
                for table_id in plan.table_refs:
                    blocks.append(TableBlock(table=tables[table_id], requirement_refs=plan.requirement_refs))
                for attachment_id in plan.attachment_refs:
                    blocks.append(AttachmentBlock(attachment_id=attachment_id, requirement_refs=plan.requirement_refs))
                for issue in prepared.issues:
                    if issue.section_id == plan.section_id:
                        blocks.append(IssueBlock(issue_id=issue.issue_id, requirement_refs=issue.requirement_refs))
                if not blocks:
                    raise PipelineFailure("EMPTY_SECTION")
                return Section(section_id=plan.section_id, title=plan.title, requirement_refs=plan.requirement_refs, blocks=blocks).model_dump(mode="json")

            sections.append(ctx.run_unit(plan.section_id, unit_input, write_section))
        package = DocumentPackage(**prepared.model_dump(exclude={"sections", "tables", "input_kind"}),
                                  title=f"{prepared.project.name} 投标文件", sections=sections)
        return {"package": package.model_dump(mode="json"), "source_artifacts": value["source_artifacts"],
                "generation_mode": provider_options["mode"], "external_model_verified": provider_options["mode"] == "chat_completions"}

    def check(ctx, value):
        package = DocumentPackage.model_validate(value["package"])
        # Revalidate source bytes immediately before rendering. Source refs are not
        # semantic proof: the scoped human source review is recorded separately.
        for source in [*package.sources, *package.attachments]:
            if not source.path.is_file() or file_sha256(source.path) != source.sha256:
                raise PipelineFailure("SOURCE_HASH_MISMATCH")
        checks = {"schema": "passed", "source_hashes": "passed",
                  "semantic_fact_support": "requires_source_review",
                  "model_fact_invention": "requires_source_review",
                  "requirement_count": len(package.requirements), "section_count": len(package.sections),
                  "attachment_count": len(package.attachments), "issue_count": len(package.issues),
                  "requirement_coverage": []}
        for requirement in package.requirements:
            issues = [i.issue_id for i in package.issues if requirement.requirement_id in i.requirement_refs]
            checks["requirement_coverage"].append({"requirement_id": requirement.requirement_id,
                                                   "status": "pending_manual" if issues else "drafted_for_review",
                                                   "issue_ids": issues})
        ctx.capture("validation", checks)
        return {**value, "validation": checks}

    def render(ctx, value):
        package = DocumentPackage.model_validate(value["package"])
        rendered = render_package(package, ctx.artifact_dir / "documents")
        files = {}
        for key in ("bid_docx", "issues_docx"):
            path = Path(rendered[key]["path"])
            files[key] = ctx.store_bytes(key + ".docx", path.read_bytes(),
                                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        return {"files": files, "validation": value["validation"],
                "generation_mode": value["generation_mode"], "external_model_verified": value["external_model_verified"],
                "input_kind": "manual_prepared", "synthetic": package.synthetic,
                "warnings": rendered["warnings"], "issue_ids": rendered["issue_ids"],
                "source_artifacts": value["source_artifacts"]}

    # Actual source hashes prevent continuing a partially completed run after code
    # changes. A repair/fork is explicit and keeps the original record untouched.
    shared = ("pipeline.py", "models.py", "runtime.py", "storage.py", "observability.py")
    nodes = [NodeSpec("N0", _version(*shared), freeze, config_keys=()),
             NodeSpec("N4", _version(*shared, "provider.py"), draft, config_keys=("provider",)),
             NodeSpec("N5", _version(*shared), check, config_keys=()),
             NodeSpec("N6", _version(*shared, "rendering.py"), render, config_keys=())]
    return Executor(root, nodes, execution_config=config)
