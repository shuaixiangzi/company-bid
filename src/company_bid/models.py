"""Strict contracts for frozen facts, automatic plans and editable documents.

The application owns identities, references and adopted facts. Model outputs
are candidates; prepared M1 fixtures retain their explicit origin label.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
# Quoted requirement text may end in a TSV delimiter for an empty source cell.
# Reject blank-only input without trimming source bytes or changing column count.
SourceText = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
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
    text: SourceText
    category: Literal["qualification", "technical", "commercial", "fixed_table", "procedure"]
    source_refs: list[Text] = Field(min_length=1)
    mandatory: bool = True
    response_mode: Literal["writing", "fixed_text", "table", "attachment", "factual_response", "procedure"] = "writing"

    @model_validator(mode="after")
    def factual_category(self):
        if self.response_mode == "factual_response" and self.category not in {"qualification", "commercial"}:
            raise ValueError("factual responses require a qualification or commercial category")
        return self


class ReviewAction(Contract):
    """A source rule awaiting work or review; never a bidder fact or promise."""

    action_id: Text
    kind: Literal["procedure", "commercial_constraint"]
    status: Literal["pending_manual", "not_checked"]
    requirement_refs: list[Text] = Field(min_length=1)
    source_refs: list[Text] = Field(min_length=1)
    source_requirement: SourceText
    instruction: Text

    @model_validator(mode="after")
    def pending_status_matches_kind(self):
        expected = "not_checked" if self.kind == "commercial_constraint" else "pending_manual"
        if self.status != expected:
            raise ValueError("review action must retain its unfulfilled review status")
        if len(set(self.requirement_refs)) != len(self.requirement_refs) or len(set(self.source_refs)) != len(self.source_refs):
            raise ValueError("review action references must be unique")
        return self


class Issue(Contract):
    issue_id: Annotated[str, StringConstraints(pattern=r"^M-[0-9]{3,}$")]
    title: Text
    section_id: Text
    requirement_refs: list[Text] = Field(min_length=1)
    reason: Text
    action: Text
    source_requirement: SourceText
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
    # Original grids may have no separate header, or merge across their first
    # row. In that mode columns are internal labels; rows hold the entire grid.
    header_from_columns: bool = True
    repeat_header_rows: int = Field(default=1, ge=0, le=10)

    @model_validator(mode="after")
    def rectangular(self):
        if self.repeat_header_rows > len(self.rows) + int(self.header_from_columns):
            raise ValueError("repeated header exceeds the original grid")
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
    bundle_id: Text | None = None
    page_index: int = Field(default=1, ge=1)
    page_count: int = Field(default=1, ge=1, le=100)

    @model_validator(mode="after")
    def page_identity(self):
        if self.page_index > self.page_count or (not self.bundle_id and (self.page_index != 1 or self.page_count != 1)):
            raise ValueError("multi-page proof requires a bundle identity and valid page index")
        return self


class ReferenceExcerpt(Contract):
    """Frozen, scoped reference text. It is a method reference, not a company fact."""

    reference_id: Text
    source_id: Text
    locator: Text
    text: SourceText
    text_sha256: Sha256
    applicability: Text
    retained_points: list[Text] = Field(min_length=1)
    excluded_points: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def excerpt_hash(self):
        if hashlib.sha256(self.text.encode("utf-8")).hexdigest() != self.text_sha256:
            raise ValueError("reference excerpt text hash mismatch")
        return self


def validate_attachment_bundles(attachments: list[Attachment]) -> None:
    groups = {}
    for item in attachments:
        if item.bundle_id:
            groups.setdefault(item.bundle_id, []).append(item)
    for items in groups.values():
        expected = items[0].page_count
        if (len(items) != expected or {item.page_index for item in items} != set(range(1, expected + 1))
                or any(item.page_count != expected or item.title != items[0].title for item in items)):
            raise ValueError("every selected proof bundle must contain all ordered pages exactly once")


class WritingTask(Contract):
    """Application-owned writing unit, adapted from the reviewed task contract."""

    unit_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{1,100}$")]
    title: Text
    objective: Text
    must_cover: list[SourceText] = Field(min_length=1, max_length=30)
    requirement_refs: list[Text] = Field(min_length=1)
    reference_refs: list[Text] = Field(default_factory=list)
    forbidden_fields: list[Text] = Field(min_length=1)
    completion_criteria: list[Text] = Field(min_length=1)
    operation: Literal["reference_adaptation", "bounded_new_draft"]
    format_status: Literal["not_prescribed", "prescribed", "unread", "conflict"] = "not_prescribed"
    no_reference_reason: Text | None = None
    table_refs: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def explicit_reference_decision(self):
        if self.operation == "reference_adaptation" and not self.reference_refs:
            raise ValueError("reference adaptation requires frozen reference excerpts")
        if self.operation == "bounded_new_draft" and (self.reference_refs or not self.no_reference_reason):
            raise ValueError("bounded drafting requires an explicit no-reference reason and no claimed adaptation")
        if len(set(self.reference_refs)) != len(self.reference_refs) or len(set(self.table_refs)) != len(self.table_refs):
            raise ValueError("writing unit references must not repeat")
        return self


class DocumentStyle(Contract):
    profile_id: Text = "bid-a4-v1"
    reference_source_refs: list[Text] = Field(default_factory=list)
    page_size: Literal["A4", "Letter"] = "A4"
    margin_cm: float = Field(default=2.5, ge=1, le=4)
    font_family: Text = "宋体"
    body_pt: float = Field(default=12, ge=9, le=18)
    body_line_spacing: float = Field(default=1.5, ge=1, le=2.5)
    body_first_line_chars: float = Field(default=2, ge=0, le=4)
    heading_pt: tuple[float, float, float] = (16, 15, 14)
    table_pt: float = Field(default=10.5, ge=8, le=16)
    cover_title_pt: float = Field(default=48, ge=20, le=60)
    cover_meta_pt: float = Field(default=16, ge=10, le=22)
    header_cm: float = Field(default=1.5, ge=0.5, le=3)
    footer_cm: float = Field(default=1.75, ge=0.5, le=3)
    table_border_color: Annotated[str, StringConstraints(pattern=r"^[0-9A-Fa-f]{6}$")] = "000000"

    @model_validator(mode="after")
    def heading_sizes(self):
        if any(not 10 <= size <= 26 for size in self.heading_pt):
            raise ValueError("heading sizes must be between 10 and 26 points")
        if self.header_cm >= self.margin_cm or self.footer_cm >= self.margin_cm:
            raise ValueError("header/footer distances must fit within margins")
        return self


class PrescribedBlock(Contract):
    kind: Literal["paragraph", "table_ref"]
    text: str = ""
    table_ref: Text | None = None
    source_refs: list[Text] = Field(min_length=1)

    @model_validator(mode="after")
    def exactly_one_content(self):
        if self.kind == "paragraph" and (not self.text.strip() or self.table_ref is not None):
            raise ValueError("prescribed paragraph requires only source text")
        if self.kind == "table_ref" and (not self.table_ref or self.text):
            raise ValueError("prescribed table block requires only a table reference")
        return self


class SectionPlan(Contract):
    section_id: Text
    title: Text
    kind: Literal["facts", "technical", "commercial", "attachments"]
    requirement_refs: list[Text] = Field(min_length=1)
    fact_refs: list[Text] = Field(default_factory=list)
    table_refs: list[Text] = Field(default_factory=list)
    attachment_refs: list[Text] = Field(default_factory=list)
    writing_tasks: list[WritingTask] = Field(default_factory=list)
    level: int = Field(default=1, ge=1, le=3)
    start_on_new_page: bool = False
    source_refs: list[Text] = Field(default_factory=list)
    fixed_content: list[PrescribedBlock] = Field(default_factory=list)
    render_body: bool = True
    nonwriting_requirement_refs: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def tasks_belong_to_section(self):
        if not self.render_body and any((self.fact_refs, self.table_refs, self.attachment_refs, self.writing_tasks, self.fixed_content)):
            raise ValueError("procedure-only section cannot hide content")
        fixed_tables = [item.table_ref for item in self.fixed_content if item.kind == "table_ref"]
        if len(set(fixed_tables)) != len(fixed_tables) or set(fixed_tables) - set(self.table_refs):
            raise ValueError("prescribed tables must belong to their section exactly once")
        if self.writing_tasks and (self.kind != "technical" or self.level >= 3):
            raise ValueError("writing tasks require a technical section with room for a child heading")
        covered = set()
        if set(self.nonwriting_requirement_refs) - set(self.requirement_refs):
            raise ValueError("nonwriting requirements must belong to their section")
        assigned_tables = []
        for task in self.writing_tasks:
            if set(task.requirement_refs) - set(self.requirement_refs):
                raise ValueError("writing task requirements must belong to its section")
            if set(task.table_refs) - set(self.table_refs):
                raise ValueError("writing task tables must belong to its section")
            covered.update(task.requirement_refs)
            assigned_tables.extend(task.table_refs)
        if self.writing_tasks and covered | set(self.nonwriting_requirement_refs) != set(self.requirement_refs):
            raise ValueError("writing tasks must cover every requirement assigned to the technical section")
        if len(set(assigned_tables)) != len(assigned_tables):
            raise ValueError("a fixed table may be assigned to only one writing task")
        if set(assigned_tables) & set(fixed_tables):
            raise ValueError("a prescribed table cannot also be assigned to writing")
        return self


class CoverageClaim(Contract):
    item_index: int = Field(ge=0)
    paragraph_indices: list[int] = Field(default_factory=list)
    step_indices: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def has_locations(self):
        if not self.paragraph_indices and not self.step_indices:
            raise ValueError("coverage claim must point to content")
        if any(index < 0 for index in [*self.paragraph_indices, *self.step_indices]):
            raise ValueError("coverage indices must be nonnegative")
        return self


class ProposalContent(Contract):
    """Only proposed methods, never company facts, IDs, tables or attachments."""

    paragraphs: list[Text] = Field(min_length=1, max_length=30)
    steps: list[Text] = Field(default_factory=list, max_length=30)
    coverage: list[CoverageClaim] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def valid_coverage_locations(self):
        if len({item.item_index for item in self.coverage}) != len(self.coverage):
            raise ValueError("duplicate must-cover item")
        for item in self.coverage:
            if any(i >= len(self.paragraphs) for i in item.paragraph_indices) or any(i >= len(self.steps) for i in item.step_indices):
                raise ValueError("coverage location does not exist")
        return self


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
    text: SourceText
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
    blocks: list[Block] = Field(default_factory=list)
    level: int = Field(default=1, ge=1, le=3)
    start_on_new_page: bool = False
    render_body: bool = True

    @model_validator(mode="after")
    def visible_content(self):
        if self.render_body != bool(self.blocks):
            raise ValueError("visible sections need content; procedure-only sections must be empty")
        return self


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
    review_actions: list[ReviewAction] = Field(default_factory=list)
    reference_excerpts: list[ReferenceExcerpt] = Field(default_factory=list)
    style: DocumentStyle = Field(default_factory=DocumentStyle)

    @model_validator(mode="after")
    def shared_references(self):
        source_ids = _unique(self.sources, "source_id", "source")
        requirement_ids = _unique(self.requirements, "requirement_id", "requirement")
        _unique(self.facts, "fact_id", "fact")
        _unique(self.attachments, "attachment_id", "attachment")
        validate_attachment_bundles(self.attachments)
        _unique(self.issues, "issue_id", "issue")
        _unique(self.review_actions, "action_id", "review action")
        _unique(self.reference_excerpts, "reference_id", "reference excerpt")
        _known(self.style.reference_source_refs, source_ids, "style source")
        for reference in self.reference_excerpts:
            _known([reference.source_id], source_ids, "reference source")
        for item in [self.project, self.company, *self.facts, *self.requirements, *self.attachments]:
            _known(item.source_refs, source_ids, "source")
        for item in [*self.attachments, *self.issues]:
            _known(item.requirement_refs, requirement_ids, "requirement")
        requirements = {item.requirement_id: item for item in self.requirements}
        reviewed = set()
        for action in self.review_actions:
            _known(action.requirement_refs, requirement_ids, "review action requirement")
            _known(action.source_refs, source_ids, "review action source")
            selected = [requirements[ref] for ref in action.requirement_refs]
            category = "commercial" if action.kind == "commercial_constraint" else "procedure"
            if any(item.response_mode != "procedure" or item.category != category for item in selected):
                raise ValueError("review action must reference a compatible non-body requirement")
            if reviewed & set(action.requirement_refs):
                raise ValueError("each non-body requirement may have only one review action")
            reviewed.update(action.requirement_refs)
            if set(action.source_refs) != {ref for item in selected for ref in item.source_refs}:
                raise ValueError("review action sources must equal its requirement sources")
            if action.source_requirement != "\n".join(item.text for item in selected):
                raise ValueError("review action rule must equal its cited requirements")
        if self.synthetic and (any(not item.synthetic for item in self.sources) or any(not item.synthetic for item in self.attachments)):
            raise ValueError("synthetic sample must label every source and attachment synthetic")
        return self


class PreparedBid(Snapshot):
    input_kind: Literal["manual_prepared", "automatic"] = "manual_prepared"
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
        reference_ids = {item.reference_id for item in self.reference_excerpts}
        task_ids = _unique([task for section in self.sections for task in section.writing_tasks], "unit_id", "writing task")
        if task_ids & section_ids:
            raise ValueError("writing task identities must not collide with section checkpoint identities")
        if self.input_kind == "automatic":
            nonbody = {item.requirement_id for item in self.requirements if item.response_mode == "procedure"}
            if {ref for action in self.review_actions for ref in action.requirement_refs} != nonbody:
                raise ValueError("automatic non-body requirements need pending review actions")
        covered = set()
        for section in self.sections:
            _known(section.source_refs, source_ids, "section source")
            for block in section.fixed_content:
                _known(block.source_refs, source_ids, "prescribed source")
            if not section.render_body and any(item.response_mode != "procedure" for item in self.requirements if item.requirement_id in section.requirement_refs):
                raise ValueError("only procedural requirements may omit body content")
            _known(section.requirement_refs, requirement_ids, "requirement")
            _known(section.fact_refs, fact_ids, "fact")
            _known(section.table_refs, table_ids, "table")
            _known(section.attachment_refs, attachment_ids, "attachment")
            covered.update(section.requirement_refs)
            for task in section.writing_tasks:
                _known(task.reference_refs, reference_ids, "reference excerpt")
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
            if not section.render_body and any(item.response_mode != "procedure" for item in self.requirements if item.requirement_id in section.requirement_refs):
                raise ValueError("non-procedure requirement hidden from document")
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
