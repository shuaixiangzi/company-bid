"""Strict M1 contracts for explicitly prepared input and editable documents.

The input is a human prepared snapshot, not a parser result. Providers only
return ProposalContent; the application owns identities, references and facts.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class SourceRef(Contract):
    source_id: Text
    title: Text
    path: Path
    sha256: Sha256
    locator: Text
    kind: Literal["tender", "manual_input", "company_material", "attachment"]
    provenance: Literal["original", "manual_input", "synthetic", "user_authorized_fact", "derived_from_bid"] = "original"
    synthetic: bool = False


class Project(Contract):
    project_id: Text
    name: Text
    source_refs: list[Text] = Field(min_length=1)


class Company(Contract):
    company_id: Text
    name: Text
    source_refs: list[Text] = Field(min_length=1)


class Fact(Contract):
    fact_id: Text
    label: Text
    value: Text
    source_refs: list[Text] = Field(min_length=1)


class Requirement(Contract):
    requirement_id: Text
    text: Text
    category: Literal["qualification", "technical", "commercial", "fixed_table", "procedure"]
    source_refs: list[Text] = Field(min_length=1)
    mandatory: bool = True


class Issue(Contract):
    issue_id: Annotated[str, StringConstraints(pattern=r"^M-[0-9]{3,}$")]
    title: Text
    section_id: Text
    requirement_refs: list[Text] = Field(min_length=1)
    reason: Text
    action: Text
    source_requirement: Text
    severity: Literal["missing", "warning"] = "missing"

    @property
    def marker(self) -> str:
        return f"【待补 {self.issue_id}：{self.title}】"


class TableCell(Contract):
    text: str = ""
    fact_refs: list[Text] = Field(default_factory=list)
    issue_refs: list[Text] = Field(default_factory=list)


class TableMerge(Contract):
    """Zero-based body-row coordinates; the separate header is not counted."""

    row: int = Field(ge=0)
    column: int = Field(ge=0)
    row_span: int = Field(default=1, ge=1)
    col_span: int = Field(default=1, ge=1)


class TableSpec(Contract):
    table_id: Text
    section_id: Text
    title: Text
    columns: list[Text] = Field(min_length=1)
    rows: list[list[TableCell]] = Field(min_length=1)
    source_refs: list[Text] = Field(min_length=1)
    requirement_refs: list[Text] = Field(min_length=1)
    column_widths: list[float] = Field(default_factory=list)
    merges: list[TableMerge] = Field(default_factory=list)

    @model_validator(mode="after")
    def rectangular(self):
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("fixed table rows must match the original column count")
        if self.column_widths and (
            len(self.column_widths) != len(self.columns)
            or any(width <= 0 for width in self.column_widths)
        ):
            raise ValueError("column_widths must contain one positive weight per column")
        occupied = set()
        for merge in self.merges:
            if merge.row_span * merge.col_span == 1:
                raise ValueError("table merge must span at least two cells")
            if merge.row + merge.row_span > len(self.rows) or merge.column + merge.col_span > len(self.columns):
                raise ValueError("table merge exceeds original table boundaries")
            for row_index in range(merge.row, merge.row + merge.row_span):
                for column_index in range(merge.column, merge.column + merge.col_span):
                    coordinate = (row_index, column_index)
                    if coordinate in occupied:
                        raise ValueError("table merges must not overlap")
                    occupied.add(coordinate)
                    if coordinate != (merge.row, merge.column):
                        covered = self.rows[row_index][column_index]
                        if covered.text or covered.fact_refs or covered.issue_refs:
                            raise ValueError("covered merged cells must be empty; content belongs in the anchor cell")
        return self


class Attachment(Contract):
    attachment_id: Text
    section_id: Text
    title: Text
    path: Path
    sha256: Sha256
    source_refs: list[Text] = Field(min_length=1)
    requirement_refs: list[Text] = Field(min_length=1)
    synthetic: bool = False
    description: Text


class SectionPlan(Contract):
    section_id: Text
    title: Text
    kind: Literal["facts", "technical", "commercial", "attachments"]
    requirement_refs: list[Text] = Field(min_length=1)
    fact_refs: list[Text] = Field(default_factory=list)
    table_refs: list[Text] = Field(default_factory=list)
    attachment_refs: list[Text] = Field(default_factory=list)


class ProposalContent(Contract):
    """Only proposed methods, never company facts, IDs, tables or attachments."""

    paragraphs: list[Text] = Field(min_length=1, max_length=30)
    steps: list[Text] = Field(default_factory=list, max_length=30)


class BlockReferences(Contract):
    requirement_refs: list[Text] = Field(default_factory=list)
    fact_refs: list[Text] = Field(default_factory=list)
    source_refs: list[Text] = Field(default_factory=list)
    issue_refs: list[Text] = Field(default_factory=list)


class HeadingBlock(BlockReferences):
    kind: Literal["heading"] = "heading"
    text: Text
    level: int = Field(default=2, ge=1, le=3)


class ParagraphBlock(BlockReferences):
    kind: Literal["paragraph"] = "paragraph"
    text: Text
    content_role: Literal["proposal", "fact", "neutral"] = "neutral"


class ListBlock(BlockReferences):
    kind: Literal["list"] = "list"
    items: list[Text] = Field(min_length=1)
    ordered: bool = True
    content_role: Literal["proposal", "fact", "neutral"] = "proposal"


class TableBlock(BlockReferences):
    kind: Literal["table"] = "table"
    table: TableSpec


class AttachmentBlock(BlockReferences):
    kind: Literal["attachment"] = "attachment"
    attachment_id: Text


class IssueBlock(BlockReferences):
    kind: Literal["issue"] = "issue"
    issue_id: Text


Block = Annotated[
    HeadingBlock | ParagraphBlock | ListBlock | TableBlock | AttachmentBlock | IssueBlock,
    Field(discriminator="kind"),
]


class Section(Contract):
    section_id: Text
    title: Text
    requirement_refs: list[Text] = Field(min_length=1)
    blocks: list[Block] = Field(min_length=1)


def _unique(items, key: str, label: str) -> set[str]:
    identities = [getattr(item, key) for item in items]
    if len(set(identities)) != len(identities):
        raise ValueError(f"duplicate {label} identity")
    return set(identities)


def _known(refs, identities: set[str], label: str) -> None:
    if set(refs) - identities:
        raise ValueError(f"unknown {label} reference: {sorted(set(refs) - identities)}")


class Snapshot(Contract):
    schema_version: Literal["1.0"] = "1.0"
    synthetic: bool = False
    project: Project
    company: Company
    sources: list[SourceRef] = Field(min_length=1)
    facts: list[Fact] = Field(default_factory=list)
    requirements: list[Requirement] = Field(min_length=1)
    attachments: list[Attachment] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)

    @model_validator(mode="after")
    def shared_references(self):
        source_ids = _unique(self.sources, "source_id", "source")
        requirement_ids = _unique(self.requirements, "requirement_id", "requirement")
        _unique(self.facts, "fact_id", "fact")
        _unique(self.attachments, "attachment_id", "attachment")
        _unique(self.issues, "issue_id", "issue")
        for item in [self.project, self.company, *self.facts, *self.requirements, *self.attachments]:
            _known(item.source_refs, source_ids, "source")
        for item in [*self.attachments, *self.issues]:
            _known(item.requirement_refs, requirement_ids, "requirement")
        if self.synthetic and (any(not item.synthetic for item in self.sources) or any(not item.synthetic for item in self.attachments)):
            raise ValueError("synthetic sample must label every source and attachment synthetic")
        return self


class PreparedBid(Snapshot):
    input_kind: Literal["manual_prepared"] = "manual_prepared"
    preparation_note: Text
    sections: list[SectionPlan] = Field(min_length=1)
    tables: list[TableSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def prepared_references(self):
        section_ids = _unique(self.sections, "section_id", "section")
        table_ids = _unique(self.tables, "table_id", "table")
        fact_ids = {fact.fact_id for fact in self.facts}
        source_ids = {source.source_id for source in self.sources}
        requirement_ids = {item.requirement_id for item in self.requirements}
        attachment_ids = {item.attachment_id for item in self.attachments}
        issue_ids = {item.issue_id for item in self.issues}
        covered = set()
        for section in self.sections:
            _known(section.requirement_refs, requirement_ids, "requirement")
            _known(section.fact_refs, fact_ids, "fact")
            _known(section.table_refs, table_ids, "table")
            _known(section.attachment_refs, attachment_ids, "attachment")
            covered.update(section.requirement_refs)
        if requirement_ids - covered:
            raise ValueError("every requirement must have a planned section")
        for item in [*self.tables, *self.attachments, *self.issues]:
            _known([item.section_id], section_ids, "section")
        for table in self.tables:
            _known(table.source_refs, source_ids, "source")
            _known(table.requirement_refs, requirement_ids, "requirement")
            for row in table.rows:
                for cell in row:
                    _known(cell.fact_refs, fact_ids, "fact")
                    _known(cell.issue_refs, issue_ids, "issue")
        for table in self.tables:
            target = next(section for section in self.sections if section.section_id == table.section_id)
            if table.table_id not in target.table_refs:
                raise ValueError("table must be assigned to its declared section")
        for attachment in self.attachments:
            target = next(section for section in self.sections if section.section_id == attachment.section_id)
            if attachment.attachment_id not in target.attachment_refs:
                raise ValueError("attachment must be assigned to its declared section")
        return self


class DocumentPackage(Snapshot):
    title: Text
    preparation_note: Text
    sections: list[Section] = Field(min_length=1)

    @model_validator(mode="after")
    def package_references(self):
        section_ids = _unique(self.sections, "section_id", "section")
        requirement_ids = {item.requirement_id for item in self.requirements}
        source_ids = {item.source_id for item in self.sources}
        fact_ids = {item.fact_id for item in self.facts}
        attachment_ids = {item.attachment_id for item in self.attachments}
        issue_ids = {item.issue_id for item in self.issues}
        attachments_seen = []
        issues_seen = set()
        requirements_seen = set()
        for section in self.sections:
            _known(section.requirement_refs, requirement_ids, "requirement")
            requirements_seen.update(section.requirement_refs)
            for block in section.blocks:
                _known(block.requirement_refs, requirement_ids, "requirement")
                _known(block.fact_refs, fact_ids, "fact")
                _known(block.source_refs, source_ids, "source")
                _known(block.issue_refs, issue_ids, "issue")
                issues_seen.update(block.issue_refs)
                if isinstance(block, (ParagraphBlock, ListBlock)) and block.content_role == "fact" and not block.fact_refs:
                    raise ValueError("fact body requires known fact references")
                if isinstance(block, AttachmentBlock):
                    _known([block.attachment_id], attachment_ids, "attachment")
                    attachment = next(item for item in self.attachments if item.attachment_id == block.attachment_id)
                    if attachment.section_id != section.section_id:
                        raise ValueError("attachment must occur in its declared section")
                    attachments_seen.append(block.attachment_id)
                if isinstance(block, IssueBlock):
                    _known([block.issue_id], issue_ids, "issue")
                    issues_seen.add(block.issue_id)
                if isinstance(block, TableBlock):
                    if block.table.section_id != section.section_id:
                        raise ValueError("table must occur in its declared section")
                    _known(block.table.requirement_refs, requirement_ids, "requirement")
                    _known(block.table.source_refs, source_ids, "source")
                    for row in block.table.rows:
                        for cell in row:
                            _known(cell.fact_refs, fact_ids, "fact")
                            _known(cell.issue_refs, issue_ids, "issue")
                            issues_seen.update(cell.issue_refs)
        if requirements_seen != requirement_ids:
            raise ValueError("every requirement must have a document section")
        if set(attachments_seen) != attachment_ids or len(attachments_seen) != len(attachment_ids):
            raise ValueError("each attachment must be embedded exactly once")
        if issues_seen != issue_ids:
            raise ValueError("each issue must have a matching body marker")
        for issue in self.issues:
            _known([issue.section_id], section_ids, "section")
            target = next(section for section in self.sections if section.section_id == issue.section_id)
            present = any(issue.issue_id in block.issue_refs or (isinstance(block, IssueBlock) and block.issue_id == issue.issue_id) or (isinstance(block, TableBlock) and any(issue.issue_id in cell.issue_refs for row in block.table.rows for cell in row)) for block in target.blocks)
            if not present:
                raise ValueError("issue marker must occur in its declared section")
        return self
