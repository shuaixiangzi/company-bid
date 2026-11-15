"""Strict contracts for frozen facts, automatic plans and editable documents.

The application owns identities, references and adopted facts. Model outputs
are candidates; prepared M1 fixtures retain their explicit origin label.
"""

from __future__ import annotations

import hashlib
import json
import re
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


def context_digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class SourceBlockIdentity(Contract):
    block_id: Text
    locator: Text
    ordinal: int = Field(ge=0)
    page: int = Field(ge=1)
    unit_id: Text
    text_sha256: Sha256


class SourceContextProof(Contract):
    proof_id: Text
    rule: Literal["native_numbered_child_context_v1"]
    source_id: Text
    source_sha256: Sha256
    requirement_id: Text
    requirement_quote_sha256: Sha256
    child_source_refs: list[Text] = Field(min_length=1)
    scope_members: list[SourceBlockIdentity] = Field(min_length=1, max_length=2)
    container_members: list[SourceBlockIdentity] = Field(min_length=3, max_length=100)
    child_members: list[SourceBlockIdentity] = Field(min_length=1, max_length=24)
    selected_members: list[SourceBlockIdentity] = Field(min_length=1, max_length=26)
    proof_sha256: Sha256

    @model_validator(mode="after")
    def proof_integrity(self):
        payload = self.model_dump(mode="json", exclude={"proof_id", "proof_sha256"})
        if self.proof_sha256 != context_digest(payload) or self.proof_id != "CTXP-" + self.proof_sha256[:24]:
            raise ValueError("source context proof identity mismatch")
        members = self.container_members
        first = members[0]
        if (len({item.block_id for item in members}) != len(members) or
                [item.ordinal for item in members] != list(range(first.ordinal, first.ordinal + len(members))) or
                any(item.unit_id != first.unit_id or item.page != first.page for item in members)):
            raise ValueError("source context container must be one complete native interval")
        locators = [re.fullmatch(r"pdf/page\[(\d+)\]/line\[(\d+)\]", item.locator) for item in members]
        if (not all(locators) or any(int(match[1]) != item.page for match, item in zip(locators, members)) or
                [int(match[2]) for match in locators] != list(range(int(locators[0][2]), int(locators[0][2]) + len(members)))):
            raise ValueError("source context proof requires consecutive native line locators")
        positions = {item.block_id: item for item in members}
        if any(positions.get(item.block_id) != item for item in [*self.scope_members, *self.child_members, *self.selected_members]):
            raise ValueError("source context members must retain their container identities")
        if (len({item.block_id for item in self.scope_members}) != len(self.scope_members) or
                len({item.block_id for item in self.child_members}) != len(self.child_members) or
                max(item.ordinal for item in self.scope_members) >= min(item.ordinal for item in self.child_members)):
            raise ValueError("source context parents must precede their own child")
        selected = self.selected_members
        if ([item.ordinal for item in selected] != list(range(selected[0].ordinal, selected[0].ordinal + len(selected))) or
                {item.block_id for item in self.child_members} - {item.block_id for item in selected} or
                {item.block_id for item in selected} - {item.block_id for item in [*self.scope_members, *self.child_members]}):
            raise ValueError("source context selected span must bind its complete child and only its parents")
        return self


def source_context_operator(text: str) -> str | None:
    if re.fullmatch(r"[（(]\d+[）)](?:合成|测试)?服务(?:类)?适用", text.strip()):
        return "service_applicability"
    if re.fullmatch(r"[A-Z][.．、]\s*(?:(?:不带|不含)商务报价的技术部分|"
                    r"技术(?:部分|材料|文件|册).{0,10}(?:不得|禁止|不带|不含).{0,8}(?:商务|商业)报价)"
                    r"[^。；;]{0,12}(?:包括|包含)[：:]", text.strip()):
        return "exclude_commercial_quotation"
    return None


class RequirementSourceContext(Contract):
    context_id: Text
    kind: Literal["applicability_condition", "assembly_constraint"]
    operator: Literal["service_applicability", "exclude_commercial_quotation"]
    text: SourceText
    source_refs: list[Text] = Field(min_length=1, max_length=1)
    parent_block_id: Text
    proof_ref: Text
    proof_sha256: Sha256
    review_status: Literal["not_checked"] = "not_checked"

    @model_validator(mode="after")
    def context_integrity(self):
        expected = "applicability_condition" if self.operator == "service_applicability" else "assembly_constraint"
        if self.kind != expected or self.operator != source_context_operator(self.text):
            raise ValueError("source context operator and kind differ")
        payload = self.model_dump(mode="json", exclude={"context_id"})
        if self.context_id != "CTX-" + context_digest(payload)[:24]:
            raise ValueError("source context identity mismatch")
        return self


class Requirement(Contract):
    requirement_id: Text
    text: SourceText
    category: Literal["qualification", "technical", "commercial", "fixed_table", "procedure"]
    source_refs: list[Text] = Field(min_length=1)
    source_contexts: list[RequirementSourceContext] = Field(default_factory=list, max_length=2)
    mandatory: bool = True
    response_mode: Literal["writing", "fixed_text", "table", "attachment", "factual_response", "procedure"] = "writing"

    @model_validator(mode="after")
    def factual_category(self):
        if self.response_mode == "factual_response" and self.category not in {"qualification", "commercial"}:
            raise ValueError("factual responses require a qualification or commercial category")
        return self


class PendingScopeContext(Contract):
    text: SourceText
    source_ref: Text


class ReviewAction(Contract):
    """A source rule awaiting work or review; never a bidder fact or promise."""

    action_id: Text
    kind: Literal["procedure", "commercial_constraint"]
    status: Literal["pending_manual", "not_checked"]
    requirement_refs: list[Text] = Field(min_length=1)
    source_refs: list[Text] = Field(min_length=1)
    source_requirement: SourceText
    instruction: Text
    source_scope_status: Literal["not_applicable", "not_checked", "unresolved"] = "not_applicable"
    source_scope_contexts: list[PendingScopeContext] = Field(default_factory=list, max_length=2)

    @model_validator(mode="after")
    def pending_status_matches_kind(self):
        expected = "not_checked" if self.kind == "commercial_constraint" else "pending_manual"
        if self.status != expected:
            raise ValueError("review action must retain its unfulfilled review status")
        if len(set(self.requirement_refs)) != len(self.requirement_refs) or len(set(self.source_refs)) != len(self.source_refs):
            raise ValueError("review action references must be unique")
        from .preparation_rules import manual_signing_submission, preparation_rule
        if (self.source_scope_status == "not_applicable") != (not self.source_scope_contexts):
            raise ValueError("pending scope status and source contexts must agree")
        if self.source_scope_status == "unresolved" and not (
                self.kind == "procedure" and manual_signing_submission(self.source_requirement)):
            raise ValueError("unresolved scope is limited to a manual signing and submission action")
        if self.source_scope_status == "not_checked" and preparation_rule(self.source_requirement) != "additional_materials_review":
            raise ValueError("pending scope review cannot replace a substantive response")
        if len({item.source_ref for item in self.source_scope_contexts}) != len(self.source_scope_contexts):
            raise ValueError("pending scope sources must be unique")
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
    source_context_refs: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def explicit_reference_decision(self):
        if self.operation == "reference_adaptation" and not self.reference_refs:
            raise ValueError("reference adaptation requires frozen reference excerpts")
        if self.operation == "bounded_new_draft" and (self.reference_refs or not self.no_reference_reason):
            raise ValueError("bounded drafting requires an explicit no-reference reason and no claimed adaptation")
        if (len(set(self.reference_refs)) != len(self.reference_refs) or len(set(self.table_refs)) != len(self.table_refs) or
                len(set(self.source_context_refs)) != len(self.source_context_refs)):
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


from .source_format_contracts import NativeFormatSpec


class PrescribedBlock(Contract):
    kind: Literal["paragraph", "table_ref"]
    text: str = ""
    table_ref: Text | None = None
    source_refs: list[Text] = Field(min_length=1)
    fact_refs: list[Text] = Field(default_factory=list)

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
    document_role: Literal["response", "review"] = "response"
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
    native_format: NativeFormatSpec | None = None

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
    document_role: Literal["response", "review"] = "response"
    requirement_refs: list[Text] = Field(min_length=1)
    blocks: list[Block] = Field(default_factory=list)
    level: int = Field(default=1, ge=1, le=3)
    start_on_new_page: bool = False
    render_body: bool = True
    native_format: NativeFormatSpec | None = None

    @model_validator(mode="after")
    def visible_content(self):
        native_heading = (self.native_format is not None and
                          self.native_format.schema_version == "source-response-format-v2" and bool(self.native_format.heading_text))
        if self.render_body != bool(self.blocks or native_heading):
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


def _cover_source(source: SourceRef, text: str) -> tuple[Path, str]:
    """Bind prescribed text to the frozen, scoped tender quote, not a file ID alone."""
    quotes = re.findall(r"(?:^|;\s*)quote_sha256=([^;]*)(?=;|$)", source.locator)
    if (source.kind != "tender" or source.provenance not in {"original", "synthetic"}
            or quotes != [hashlib.sha256(text.encode("utf-8")).hexdigest()]):
        raise ValueError("prescribed cover text requires its exact scoped tender source")
    return source.path, source.sha256


def _prescribed_cover(snapshot, section, paragraphs) -> set[str]:
    """Check the whole source template as well as each original body paragraph."""
    if section.document_role != "response" or not section.render_body or not paragraphs:
        raise ValueError("prescribed cover must be visible and contain original paragraphs")
    requirements = {item.requirement_id: item for item in snapshot.requirements}
    sources = {item.source_id: item for item in snapshot.sources}
    native = section.native_format
    native_cover = native is not None and native.schema_version == "source-response-format-v2" and native.form_role == "prescribed_cover"
    if native_cover:
        members = {i:text for element in native.elements if element.kind == "paragraph"
                   for i,text in zip(element.fixed_indices,element.source_texts)}
        if sorted(members) != list(range(len(paragraphs))):
            raise ValueError("native cover must retain every original paragraph")
        original_texts = [members[i] for i in range(len(paragraphs))]
    else:
        original_texts = [item.text for item in paragraphs]
    text = "\n".join([section.title, *original_texts])
    matches = [requirements[ref] for ref in section.requirement_refs
               if requirements[ref].response_mode == "fixed_text" and requirements[ref].text == text]
    if not matches:
        raise ValueError("prescribed cover title and paragraphs must equal a complete fixed-text requirement")
    originals = {_cover_source(sources[ref], text) for requirement in matches for ref in requirement.source_refs}
    if len(originals) != 1:
        raise ValueError("prescribed cover must have one original source identity")
    for index, (paragraph, original) in enumerate(zip(paragraphs,original_texts)):
        bound = [b for b in native.bindings if b.fixed_index == index] if native_cover else []
        fact_sources = {ref for b in bound for ref in b.fact_source_refs}
        original_refs = [ref for ref in paragraph.source_refs if ref not in fact_sources]
        if (not original_refs or any(_cover_source(sources[ref], original) not in originals for ref in original_refs) or
                native_cover and set(paragraph.fact_refs) != {b.fact_id for b in bound}):
            raise ValueError("prescribed cover paragraph must retain its scoped original source")
    for issue in snapshot.issues:
        if issue.section_id == section.section_id:
            if (set(issue.requirement_refs) - set(section.requirement_refs)
                    or issue.source_requirement != "；".join(requirements[ref].text for ref in issue.requirement_refs)):
                raise ValueError("prescribed cover issue must retain its source requirements")
    return {requirement.requirement_id for requirement in matches}


def _validate_document_roles(snapshot) -> None:
    """Review content keeps its sources and cannot replace a required response.

    A review section is visible in the companion document. A non-procedural
    requirement assigned there still needs its own visible pending marker in
    the response, rather than obtaining response coverage from quoted rules.
    """
    response_sections = {item.section_id: item for item in snapshot.sections
                         if item.document_role == "response" and item.render_body}
    response_refs = {ref for item in response_sections.values() for ref in item.requirement_refs}
    required_refs = {item.requirement_id for item in snapshot.requirements
                     if item.response_mode != "procedure"}
    if required_refs - response_refs:
        raise ValueError("review content cannot replace a required response section")
    review_refs = {ref for item in snapshot.sections if item.document_role == "review"
                   for ref in item.requirement_refs} & required_refs
    pending_refs = {ref for issue in snapshot.issues if issue.section_id in response_sections
                    for ref in issue.requirement_refs if ref in response_sections[issue.section_id].requirement_refs}
    if review_refs - pending_refs:
        raise ValueError("review requirements need their own pending issue in the response")


class Snapshot(Contract):
    schema_version: Literal["1.0"] = "1.0"
    synthetic: bool = False
    project: Project
    company: Company
    sources: list[SourceRef] = Field(min_length=1)
    facts: list[Fact] = Field(default_factory=list)
    requirements: list[Requirement] = Field(min_length=1)
    source_context_proofs: list[SourceContextProof] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    review_actions: list[ReviewAction] = Field(default_factory=list)
    reference_excerpts: list[ReferenceExcerpt] = Field(default_factory=list)
    prescribed_cover_section_id: Text | None = None
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
        proof_ids = _unique(self.source_context_proofs, "proof_id", "source context proof")
        proofs = {item.proof_id: item for item in self.source_context_proofs}
        sources = {item.source_id: item for item in self.sources}
        used_proofs = set()
        context_ids = set()
        for requirement in self.requirements:
            local_ids = set()
            for context in requirement.source_contexts:
                if (context.context_id in context_ids or context.context_id in local_ids or
                        context.proof_ref not in proof_ids or requirement.category != "technical" or
                        requirement.response_mode == "procedure"):
                    raise ValueError("source context must bind one real technical child")
                context_ids.add(context.context_id)
                local_ids.add(context.context_id)
                proof = proofs[context.proof_ref]
                _known([proof.source_id, *proof.child_source_refs, *context.source_refs], source_ids, "context source")
                original = sources[proof.source_id]
                if (proof.requirement_id != requirement.requirement_id or proof.proof_sha256 != context.proof_sha256 or
                        proof.requirement_quote_sha256 != hashlib.sha256(requirement.text.encode()).hexdigest() or
                        set(proof.child_source_refs) != set(requirement.source_refs) or original.kind != "tender" or
                        original.provenance not in {"original", "synthetic"} or
                        original.sha256 != proof.source_sha256):
                    raise ValueError("source context proof does not bind its child quote and original source")
                parents = {item.block_id: item for item in proof.scope_members}
                if (context.parent_block_id not in parents or
                        parents[context.parent_block_id].text_sha256 != hashlib.sha256(context.text.encode()).hexdigest()):
                    raise ValueError("source context parent text differs from its proof")
                for ref, text, members in [*((ref, requirement.text, proof.selected_members) for ref in proof.child_source_refs),
                                          *((ref, context.text, [parents[context.parent_block_id]]) for ref in context.source_refs)]:
                    source = sources[ref]
                    quotes = re.findall(r"(?:^|;\s*)quote_sha256=([^;]*)(?=;|$)", source.locator)
                    ends = re.findall(r"(?:^|;\s*)through=([^;]*)(?=;|$)", source.locator)
                    selected_ids = re.findall(r"(?:^|;\s*)blocks=([^;]*)(?=;|$)", source.locator)
                    offsets = re.findall(r"(?:^|;\s*)chars\[(\d+):(\d+)\](?=;|$)", source.locator)
                    if (source.kind != "tender" or source.provenance not in {"original", "synthetic"} or
                            source.path != original.path or source.sha256 != original.sha256 or
                            quotes != [hashlib.sha256(text.encode()).hexdigest()] or
                            source.locator.split(";", 1)[0] != members[0].locator or offsets != [("0", str(len(text)))] or
                            (len(members) > 1 and (ends != [members[-1].locator] or selected_ids != [",".join(item.block_id for item in members)])) or
                            (ends and ends != [members[-1].locator]) or
                            (selected_ids and selected_ids != [",".join(item.block_id for item in members)])):
                        raise ValueError("source context needs exact scoped tender quotes")
                if not any(issue.severity == "warning" and requirement.requirement_id in issue.requirement_refs and
                           issue.source_requirement == context.text for issue in self.issues):
                    raise ValueError("unchecked source context needs a visible child warning")
                used_proofs.add(proof.proof_id)
            for proof in self.source_context_proofs:
                if proof.requirement_id == requirement.requirement_id and {
                        context.parent_block_id for context in requirement.source_contexts if context.proof_ref == proof.proof_id
                        } != {item.block_id for item in proof.scope_members}:
                    raise ValueError("source context parents cannot disappear from their child")
        if used_proofs != proof_ids:
            raise ValueError("source context proof registry must match actual child contexts")
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
            for context in action.source_scope_contexts:
                _known([context.source_ref], source_ids, "pending scope source")
                source = sources[context.source_ref]
                child_sources = [sources[ref] for ref in action.source_refs]
                quotes = re.findall(r"(?:^|;\s*)quote_sha256=([^;]*)(?=;|$)", source.locator)
                if (source.kind != "tender" or source.provenance not in {"original", "synthetic"} or
                        not all(child.kind == "tender" and child.path == source.path and child.sha256 == source.sha256
                                for child in child_sources) or
                        quotes != [hashlib.sha256(context.text.encode()).hexdigest()]):
                    raise ValueError("pending scope context must retain an exact same-source quote")
        if self.synthetic and (any(not item.synthetic for item in self.sources) or any(not item.synthetic for item in self.attachments)):
            raise ValueError("synthetic sample must label every source and attachment synthetic")
        return self


def validate_context_quote_placement(snapshot, tables) -> None:
    requirements = {item.requirement_id: item for item in snapshot.requirements}
    quote_form = re.compile(r"(?:报价|响应价|投标价)(?:一览|明细|汇总|统计|分项|构成)?表|(?:分项|综合|总价)报价(?:表|清单)?")
    quote_refs = {item.requirement_id for item in snapshot.requirements if item.category in {"commercial", "fixed_table"} and
                  item.response_mode in {"table", "fixed_text"} and quote_form.search(item.text)}
    for section in snapshot.sections:
        constrained = any(context.operator == "exclude_commercial_quotation"
                          for rid in section.requirement_refs for context in requirements[rid].source_contexts)
        if not constrained:
            continue
        attached = [table for table in tables if table.section_id == section.section_id]
        if set(section.requirement_refs) & quote_refs or any(set(table.requirement_refs) & quote_refs for table in attached):
            raise ValueError("source context forbids commercial quotation placement in technical content")
        if hasattr(section, "writing_tasks"):
            for task in section.writing_tasks:
                if any(table.table_id in task.table_refs and set(table.requirement_refs) & quote_refs for table in tables):
                    raise ValueError("source context forbids quotation tables in a technical writing task")


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
                _known(block.fact_refs, fact_ids, "prescribed fact")
            if not section.render_body and any(item.response_mode != "procedure" for item in self.requirements if item.requirement_id in section.requirement_refs):
                raise ValueError("only procedural requirements may omit body content")
            _known(section.requirement_refs, requirement_ids, "requirement")
            _known(section.fact_refs, fact_ids, "fact")
            _known(section.table_refs, table_ids, "table")
            _known(section.attachment_refs, attachment_ids, "attachment")
            covered.update(section.requirement_refs)
            for task in section.writing_tasks:
                _known(task.reference_refs, reference_ids, "reference excerpt")
                _known(task.requirement_refs, set(section.requirement_refs), "writing task requirement")
                expected_contexts = list(dict.fromkeys(context.context_id for item in self.requirements
                    if item.requirement_id in task.requirement_refs for context in item.source_contexts))
                if task.source_context_refs != expected_contexts:
                    raise ValueError("writing task must retain exactly its child source contexts")
            for item in self.requirements:
                if (section.document_role == "response" and item.requirement_id in section.requirement_refs
                        and item.source_contexts and item.response_mode == "writing"
                        and not any(item.requirement_id in task.requirement_refs for task in section.writing_tasks)):
                    raise ValueError("context-bound technical writing requires an actual writing task")
        if requirement_ids - covered:
            raise ValueError("every requirement must have a planned section")
        _validate_document_roles(self)
        for item in self.requirements:
            if item.source_contexts and item.response_mode == "writing" and not any(
                    section.document_role == "response" and item.requirement_id in task.requirement_refs
                    for section in self.sections for task in section.writing_tasks):
                raise ValueError("source context technical writing requires a response writing task")
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
        if self.prescribed_cover_section_id is not None:
            _known([self.prescribed_cover_section_id], section_ids, "prescribed cover section")
            cover = next(item for item in self.sections if item.section_id == self.prescribed_cover_section_id)
            if (any((cover.fact_refs, cover.writing_tasks, cover.table_refs, cover.attachment_refs))
                    or any(item.kind != "paragraph" for item in cover.fixed_content)):
                raise ValueError("prescribed cover permits only original fixed paragraphs and issues")
            requirement_ids = _prescribed_cover(self, cover, cover.fixed_content)
            if not any(set(cover.source_refs) == set(item.source_refs) for item in self.requirements
                       if item.requirement_id in requirement_ids):
                raise ValueError("prescribed cover section must retain its complete template sources")
        validate_context_quote_placement(self, self.tables)
        from .source_format_contracts import validate_native_format
        for section in self.sections:
            if section.native_format is not None:
                validate_native_format(section.native_format, section.fixed_content,
                    {table.table_id: table for table in self.tables}, {source.source_id: source for source in self.sources},
                    {fact.fact_id: fact for fact in self.facts}, title=section.title,
                    identities={"company_name": self.company.name, "project_name": self.project.name})
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
                if isinstance(block, (ParagraphBlock, ListBlock)) and block.content_role == "proposal":
                    needed = {ref for requirement in self.requirements if requirement.requirement_id in block.requirement_refs
                              and requirement.source_contexts for ref in [*requirement.source_refs,
                                  *(ref for context in requirement.source_contexts for ref in context.source_refs)]}
                    if needed - set(block.source_refs):
                        raise ValueError("proposal must retain child and parent context source provenance")
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
        _validate_document_roles(self)
        for requirement in self.requirements:
            if requirement.source_contexts and requirement.response_mode == "writing" and not any(
                    isinstance(block, (ParagraphBlock, ListBlock)) and block.content_role == "proposal" and
                    requirement.requirement_id in block.requirement_refs for section in self.sections
                    if section.document_role == "response" for block in section.blocks):
                raise ValueError("source context warning cannot replace technical proposal content")
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
        if self.prescribed_cover_section_id is not None:
            _known([self.prescribed_cover_section_id], section_ids, "prescribed cover section")
            cover = next(item for item in self.sections if item.section_id == self.prescribed_cover_section_id)
            native_cover = cover.native_format is not None and cover.native_format.schema_version == "source-response-format-v2" and cover.native_format.form_role == "prescribed_cover"
            if any(not isinstance(block, (ParagraphBlock, IssueBlock)) or (block.fact_refs and not native_cover)
                   or (isinstance(block, ParagraphBlock) and block.content_role != "neutral")
                   for block in cover.blocks):
                raise ValueError("prescribed cover permits only original neutral paragraphs and issues")
            paragraphs = [block for block in cover.blocks if isinstance(block, ParagraphBlock)]
            requirement_ids = _prescribed_cover(self, cover, paragraphs)
            issues = {item.issue_id: item for item in self.issues}
            for block in cover.blocks:
                if (not block.requirement_refs or set(block.requirement_refs) - set(cover.requirement_refs)
                        or (isinstance(block, ParagraphBlock) and not requirement_ids.intersection(block.requirement_refs))):
                    raise ValueError("prescribed cover blocks must retain their template requirements")
                refs = [*block.issue_refs, *([block.issue_id] if isinstance(block, IssueBlock) else [])]
                if any(issues[ref].section_id != cover.section_id for ref in refs):
                    raise ValueError("prescribed cover cannot display another section's issue")
                if isinstance(block, IssueBlock) and block.requirement_refs != issues[block.issue_id].requirement_refs:
                    raise ValueError("prescribed cover issue block must retain its issue requirements")
        validate_context_quote_placement(self, [block.table for section in self.sections for block in section.blocks
                                              if isinstance(block, TableBlock)])
        from .source_format_contracts import validate_native_format
        for section in self.sections:
            if section.native_format is not None:
                fixed_count = sum(len(element.fixed_indices) if element.kind == "paragraph" else 1
                                  for element in section.native_format.elements)
                validate_native_format(section.native_format, section.blocks[:fixed_count],
                    {block.table.table_id: block.table for block in section.blocks if isinstance(block, TableBlock)},
                    {source.source_id: source for source in self.sources}, {fact.fact_id: fact for fact in self.facts},
                    title=section.title, identities={"company_name": self.company.name, "project_name": self.project.name})
        return self
