"""Editable source-format geometry, distinct from narrative house style."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def format_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def negative_fill_instruction(text):
    return bool(re.search(r"(?:说明|注)[：:]|(?:不得|禁止|不应|无需).{0,8}(?:填写|替换|改写)|保持原字|保留原字", text))


class _FormatModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class NativeUnderline(_FormatModel):
    fixed_index: int = Field(ge=0)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    width_pt: float = Field(gt=0)
    locator: str
    basis: Literal["native_vector_rule", "native_underscore_glyphs"] = "native_vector_rule"
    bbox: Annotated[list[float], Field(min_length=4, max_length=4)] | None = None


class NativeSlotBinding(_FormatModel):
    fixed_index: int = Field(ge=0)
    source_block_id: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    original: str
    replacement: str
    label: Literal["company_name", "project_name", "project_number"]
    fact_id: str
    fact_source_refs: list[str] = Field(min_length=1)
    before_text: str
    after_text: str
    before_sha256: Sha
    after_sha256: Sha
    protected_text_sha256: Sha
    source_rule: NativeUnderline | None = None

    @model_validator(mode="after")
    def exact_change(self):
        if (self.end < self.start or self.start > len(self.before_text) or self.end > len(self.before_text) or
                self.before_text[self.start:self.end] != self.original or
                self.after_text != self.before_text[:self.start] + self.replacement + self.before_text[self.end:] or
                hashlib.sha256(self.before_text.encode()).hexdigest() != self.before_sha256 or
                hashlib.sha256(self.after_text.encode()).hexdigest() != self.after_sha256 or
                hashlib.sha256((self.before_text[:self.start] + "<slot>" + self.before_text[self.end:]).encode()).hexdigest()
                != self.protected_text_sha256):
            raise ValueError("native source fill may change only its exact protected slot")
        return self


class NativeParagraphLayout(_FormatModel):
    kind: Literal["paragraph"] = "paragraph"
    fixed_indices: list[int] = Field(min_length=1)
    source_block_ids: list[str] = Field(min_length=1)
    source_locators: list[str] = Field(min_length=1)
    source_texts: list[str] = Field(min_length=1)
    output_texts_sha256: Sha
    bbox: list[float] = Field(min_length=4, max_length=4)
    font_family: str
    font_size_pt: float = Field(gt=0)
    bold: bool
    alignment: Literal["left", "center", "right"]
    left_pt: float = Field(ge=0)
    right_pt: float = Field(ge=0)
    first_line_indent_pt: float
    line_spacing_pt: float = Field(gt=0)
    underlines: list[NativeUnderline] = Field(default_factory=list)
    grouping_rule: Literal["one_native_line", "continuous_native_sentence"]

    @model_validator(mode="after")
    def exact_members(self):
        if (len(self.fixed_indices) != len(set(self.fixed_indices)) or
                len(self.fixed_indices) != len(self.source_block_ids) or
                len(self.fixed_indices) != len(self.source_texts) or len(self.fixed_indices) != len(self.source_locators)):
            raise ValueError("native paragraph members must correspond exactly")
        texts = dict(zip(self.fixed_indices, self.source_texts))
        if any(rule.fixed_index not in texts or not 0 <= rule.start <= rule.end <= len(texts[rule.fixed_index])
               for rule in self.underlines):
            raise ValueError("native underline must stay inside its exact source member")
        return self


class NativeCellUnderline(_FormatModel):
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    width_pt: float = Field(gt=0)
    locator: str


class NativeCellLayout(_FormatModel):
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    font_family: str
    font_size_pt: float = Field(gt=0)
    bold: bool
    alignment: Literal["left", "center", "right"]
    vertical_alignment: Literal["top", "center", "bottom"]
    left_inset_pt: float = Field(default=0., ge=0)
    right_inset_pt: float = Field(default=0., ge=0)
    top_inset_pt: float = Field(default=0., ge=0)
    bottom_inset_pt: float = Field(default=0., ge=0)
    inset_basis: Literal["measured_glyph_bounds", "measured_glyph_bounds_with_same_table_right_inset",
                         "same_table_minimum_horizontal_inset", "no_measured_inset"] = "no_measured_inset"
    underlines: list[NativeCellUnderline] = Field(default_factory=list)


class NativeTableLayout(_FormatModel):
    kind: Literal["table"] = "table"
    fixed_index: int = Field(ge=0)
    source_block_id: str
    source_locator: str
    table_id: str
    bbox: list[float] = Field(min_length=4, max_length=4)
    column_widths_pt: list[float] = Field(min_length=1)
    row_heights_pt: list[float] = Field(min_length=1)
    cells: list[NativeCellLayout]
    source_rows: list[list[str]]
    source_merges: list[dict[str, int]]
    source_grid_sha256: Sha
    output_grid_sha256: Sha

    @model_validator(mode="after")
    def positive_geometry(self):
        if (not self.source_rows or any(len(row) != len(self.column_widths_pt) for row in self.source_rows) or
                len(self.row_heights_pt) != len(self.source_rows) or
                any(x <= 0 for x in [*self.column_widths_pt, *self.row_heights_pt]) or
                abs(sum(self.column_widths_pt) - (self.bbox[2] - self.bbox[0])) > .05 or
                abs(sum(self.row_heights_pt) - (self.bbox[3] - self.bbox[1])) > .05):
            raise ValueError("native table needs exact source absolute geometry")
        return self


NativeElement = Annotated[NativeParagraphLayout | NativeTableLayout, Field(discriminator="kind")]


def validate_native_format(spec, fixed, tables, sources, facts, *, title=None, identities=None):
    """Cross-check carried geometry/fills against actual document objects."""
    import re
    from .slots import whole_cell_slot, has_fill_marker
    source = sources.get(spec.source_id)
    if source is None or source.sha256 != spec.source_sha256 or source.kind != "tender":
        raise ValueError("native format must bind its original tender source")
    indices = [i for element in spec.elements for i in
               (element.fixed_indices if isinstance(element, NativeParagraphLayout) else [element.fixed_index])]
    if indices != list(range(len(fixed))):
        raise ValueError("native layout must cover every prescribed block without omission")
    members = {index: block for e in spec.elements if isinstance(e, NativeParagraphLayout)
               for index, block in zip(e.fixed_indices, e.source_block_ids)}
    member_rules = {index:[rule for rule in e.underlines if rule.fixed_index == index]
                    for e in spec.elements if isinstance(e, NativeParagraphLayout) for index in e.fixed_indices}
    is_v2 = spec.schema_version == "source-response-format-v2"
    bindings = {}
    for binding in spec.bindings:
        fact = facts.get(binding.fact_id)
        aliases = ({"公司名称", "投标人名称", "响应人名称", "供应商名称"} if binding.label == "company_name" else
                   {"项目编号"} if binding.label == "project_number" else {"项目名称"})
        if (binding.fixed_index not in members or members[binding.fixed_index] != binding.source_block_id or
                negative_fill_instruction(binding.before_text) or
                fact is None or fact.label not in aliases or fact.value != binding.replacement or
                identities is not None and binding.label != "project_number" and fact.value != identities[binding.label] or
                fact.source_refs != binding.fact_source_refs):
            raise ValueError("native fill must retain its exact permitted identity fact")
        if binding.label == "project_number" and (not is_v2 or not fact.value.strip() or
                len({item.value for item in facts.values() if item.label == "项目编号"}) != 1):
            raise ValueError("native project number needs one explicit fact value in v2")
        tail = binding.before_text[binding.end:]
        company_slot = (not binding.original.strip(" _＿\t") and
            (re.match(r"[（(](?:响应人|投标人|供应商)(?:名称|全称)[）)]", tail) or
             (re.fullmatch(r"(?:响应人|投标人|供应商)(?:名称)?[：:]", binding.before_text[:binding.start]) and
              re.match(r"[（(].*盖.*章[）)]", tail))))
        project_slot = binding.start == binding.end and tail.startswith(("（项目名称）", "(项目名称)"))
        rule = binding.source_rule
        if rule is not None and (rule not in member_rules.get(binding.fixed_index, []) or rule.bbox is None):
            raise ValueError("native fill rule differs from its frozen source member")
        project_rule_slot = (rule is not None and rule.start == binding.start and rule.end == binding.end and
            re.fullmatch(r"项目名称[：:]", binding.before_text[:binding.start]) and
            not binding.original.strip(" \t_＿") and binding.original and
            re.match(r"\s*(?:项目编号[：:]|$)", tail))
        company_rule_slot = (rule is not None and binding.start == binding.end == rule.start == 0 and
            re.match(r"[（(](?:响应人|投标人|供应商)[）)]\s+已(?:认真)?阅读", binding.before_text) is not None and
            rule.end >= len(re.match(r"[（(](?:响应人|投标人|供应商)[）)]", binding.before_text)[0]))
        company_slot = company_slot or company_rule_slot
        project_slot = project_slot or project_rule_slot
        allowed_slot = (company_slot if binding.label == "company_name" else
                        project_number_source_slot(binding) if binding.label == "project_number" else project_slot)
        if not allowed_slot:
            raise ValueError("native identity fill is not an explicit source slot")
        bindings.setdefault(binding.fixed_index, []).append(binding)
    def quoted(text, refs, locator, block_id, physical_page):
        wanted = hashlib.sha256(text.encode()).hexdigest()
        return any(ref in sources and sources[ref].sha256 == source.sha256 and
                   sources[ref].path == source.path and
                   sources[ref].locator.split(";", 1)[0] == locator and
                   locator.startswith(f"pdf/page[{physical_page}]/") and
                   re.findall(r"(?:^|;\s*)blocks=([^;]*)", sources[ref].locator) == [block_id] and
                   re.findall(r"(?:^|;\s*)quote_sha256=([^;]*)", sources[ref].locator) == [wanted]
                   for ref in refs)
    if (title is not None and spec.heading_text != title or
            not quoted(spec.heading_text, [spec.heading_source_ref], spec.heading_source_locator,
                       spec.heading_source_block_id, spec.physical_page)):
        raise ValueError("native heading must retain its exact original source")
    for element in spec.elements:
        physical_page = element.physical_page if is_v2 else spec.physical_page
        if isinstance(element, NativeParagraphLayout):
            texts = []
            for index, original, locator, block_id in zip(element.fixed_indices, element.source_texts,
                                                         element.source_locators, element.source_block_ids):
                item = fixed[index]
                if item.kind != "paragraph" or not quoted(original, item.source_refs, locator, block_id, physical_page):
                    raise ValueError("native paragraph must retain its exact scoped original quote")
                current = original
                for binding in bindings.get(index, []):
                    if binding.before_text != current or binding.fact_id not in item.fact_refs:
                        raise ValueError("native paragraph fill chain or fact binding changed")
                    current = binding.after_text
                if current != item.text:
                    raise ValueError("native paragraph changed protected printed words")
                texts.append(item.text)
                for rule in [r for r in element.underlines if r.fixed_index == index]:
                    if rule.basis == "native_underscore_glyphs" and (
                            not re.fullmatch(r"[_＿]{2,}", original[rule.start:rule.end]) or
                            rule.locator != f"{locator}; chars[{rule.start}:{rule.end}]" or rule.bbox is None):
                        raise ValueError("native glyph rule must match its exact printed underscores")
                    if rule.basis == "native_vector_rule" and not re.fullmatch(
                            re.escape(f"pdf/page[{physical_page}]/") + r"rule\[\d+\]", rule.locator):
                        raise ValueError("native vector rule must retain its source page")
            if format_digest(texts) != element.output_texts_sha256:
                raise ValueError("native paragraph output hash mismatch")
        else:
            table = tables.get(element.table_id)
            item = fixed[element.fixed_index]
            if table is None or (getattr(item, "table_ref", None) or getattr(getattr(item, "table", None), "table_id", None)) != element.table_id:
                raise ValueError("native table placement differs from its declared block")
            rows = [[cell.model_dump(mode="json") for cell in row] for row in table.rows]
            nr, nc = len(element.source_rows), len(element.column_widths_pt)
            anchors = {(r, c) for r in range(nr) for c in range(nc)}
            occupied = set()
            for merge in element.source_merges:
                if set(merge) != {"row", "column", "row_span", "col_span"}:
                    raise ValueError("native merge geometry fields changed")
                r, c, rs, cs = (merge[key] for key in ("row", "column", "row_span", "col_span"))
                area = {(y, x) for y in range(r, r+rs) for x in range(c, c+cs)}
                if r < 0 or c < 0 or rs < 1 or cs < 1 or rs*cs < 2 or not area <= anchors or occupied & area:
                    raise ValueError("native merge geometry exceeds source grid")
                occupied.update(area)
            covered = {pair for merge in element.source_merges for pair in
                       [(y, x) for y in range(merge["row"], merge["row"]+merge["row_span"])
                        for x in range(merge["column"], merge["column"]+merge["col_span"])]
                       if pair != (merge["row"], merge["column"])}
            cell_anchors = [(cell.row, cell.column) for cell in element.cells]
            if (len(rows) != nr or any(len(row) != nc for row in rows) or len(table.columns) != nc or
                    len(element.row_heights_pt) != nr or len(set(cell_anchors)) != len(cell_anchors) or
                    set(cell_anchors) != anchors - covered or
                    format_digest(rows) != element.output_grid_sha256 or
                    format_digest(element.source_rows) != element.source_grid_sha256 or
                    [m.model_dump() for m in table.merges] != element.source_merges or
                    not quoted("\n".join("\t".join(row) for row in element.source_rows), table.source_refs,
                               element.source_locator, element.source_block_id, physical_page)):
                raise ValueError("native table grid or source quote changed")
            for source_row, row in zip(element.source_rows, table.rows):
                for original, cell in zip(source_row, row):
                    if not (whole_cell_slot(original) or has_fill_marker(original)) and cell.text != original:
                        raise ValueError("native table protected source label changed")
                    if cell.fact_refs and not any(ref in facts and facts[ref].value == cell.text for ref in cell.fact_refs):
                        raise ValueError("native table fill differs from its known fact")
            for cell in element.cells:
                text = element.source_rows[cell.row][cell.column]
                merge = next((m for m in element.source_merges if (m["row"], m["column"]) == (cell.row, cell.column)), {})
                width = sum(element.column_widths_pt[cell.column:cell.column + merge.get("col_span", 1)])
                height = sum(element.row_heights_pt[cell.row:cell.row + merge.get("row_span", 1)])
                if cell.left_inset_pt + cell.right_inset_pt >= width or cell.top_inset_pt + cell.bottom_inset_pt >= height:
                    raise ValueError("native cell insets exceed its source geometry")
                if len({rule.locator for rule in cell.underlines}) != len(cell.underlines):
                    raise ValueError("native cell rule may appear only once")
                for rule in cell.underlines:
                    if (not 0 <= rule.start <= rule.end <= len(text) or
                            text[rule.start:rule.end].strip(" \t_＿") or
                            rule.width_pt > width or
                            not re.fullmatch(re.escape(f"pdf/page[{physical_page}]/") + r"rule\[\d+\]", rule.locator) or
                            table.rows[cell.row][cell.column].text != text):
                        raise ValueError("native cell underline must retain its exact blank source slot")


class NativeFormatLayout(_FormatModel):
    schema_version: Literal["source-response-format-v1"] = "source-response-format-v1"
    source_id: str
    source_sha256: Sha
    physical_page: int = Field(ge=1)
    page_width_pt: float = Field(gt=0)
    page_height_pt: float = Field(gt=0)
    heading_text: str
    heading_source_ref: str
    heading_source_block_id: str
    heading_source_locator: str
    heading_bbox: list[float] = Field(min_length=4, max_length=4)
    heading_font_family: str
    heading_font_size_pt: float = Field(gt=0)
    heading_bold: bool
    heading_alignment: Literal["left", "center", "right"]
    elements: list[NativeElement] = Field(min_length=1)
    bindings: list[NativeSlotBinding] = Field(default_factory=list)
    native_read_sha256: Sha
    frozen_blocks_sha256: Sha
    proof_sha256: Sha

    @model_validator(mode="after")
    def sealed_layout(self):
        if any(binding.label == "project_number" for binding in self.bindings):
            raise ValueError("native project number is not part of the v1 format contract")
        if format_digest(self.model_dump(mode="json", exclude={"proof_sha256"})) != self.proof_sha256:
            raise ValueError("native source-format proof hash mismatch")
        indices = [i for e in self.elements for i in
                   (e.fixed_indices if isinstance(e, NativeParagraphLayout) else [e.fixed_index])]
        if len(indices) != len(set(indices)) or indices != sorted(indices):
            raise ValueError("native format must preserve original block order exactly once")
        for box in [self.heading_bbox, *(element.bbox for element in self.elements)]:
            if not (0 <= box[0] < box[2] <= self.page_width_pt + .01 and
                    0 <= box[1] < box[3] <= self.page_height_pt + .01):
                raise ValueError("native format geometry exceeds its original page")
        for rule in [r for e in self.elements if isinstance(e, NativeParagraphLayout) for r in e.underlines]:
            if rule.bbox is not None:
                box=rule.bbox
                if (not 0 <= box[0] < box[2] <= self.page_width_pt + .01 or
                        not 0 <= box[1] <= box[3] <= self.page_height_pt + .01 or
                        abs(box[2]-box[0]-rule.width_pt) > .01):
                    raise ValueError("native source rule geometry changed")
        return self


def project_number_source_slot(binding):
    """An explicit numbered-project blank, never a name/date or existing value."""
    if (negative_fill_instruction(binding.before_text) or
            not re.search(r"(?:^|\s)项目编号[：:]$", binding.before_text[:binding.start]) or
            binding.original.strip(" \t_＿")):
        return False
    rule = binding.source_rule
    if rule is not None:
        return rule.start == binding.start and rule.end == binding.end
    return not binding.before_text[binding.end:].strip() and binding.end == len(binding.before_text)


def _inside_page(box, width, height, *, stroke=False):
    return (0 <= box[0] < box[2] <= width + .01 and
            0 <= box[1] <= box[3] <= height + .01 and (stroke or box[1] < box[3]))


def _same_box(left, right):
    return all(abs(a-b) <= .01 for a, b in zip(left, right))


def _union_boxes(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


class NativeScopeMember(_FormatModel):
    block_id: str = Field(min_length=1)
    locator: str
    ordinal: int = Field(ge=0)
    kind: Literal["paragraph", "table"]
    physical_page: int = Field(ge=1)
    text_sha256: Sha
    bbox: list[float] = Field(min_length=4, max_length=4)
    role: Literal["selected", "excluded", "page_furniture"]


class NativePageScope(_FormatModel):
    physical_page: int = Field(ge=1)
    page_width_pt: float = Field(gt=0)
    page_height_pt: float = Field(gt=0)
    scope_kind: Literal["whole_page", "partial_page"]
    scope_bbox: list[float] = Field(min_length=4, max_length=4)
    members: list[NativeScopeMember] = Field(min_length=1)
    fixed_indices: list[Annotated[int, Field(ge=0)]]
    native_page_sha256: Sha
    frozen_page_sha256: Sha

    @model_validator(mode="after")
    def exact_page_partition(self):
        ordinals = [member.ordinal for member in self.members]
        if (ordinals != sorted(set(ordinals)) or
                len({member.block_id for member in self.members}) != len(self.members) or
                len({member.locator for member in self.members}) != len(self.members) or
                self.fixed_indices != sorted(set(self.fixed_indices))):
            raise ValueError("native page scope must retain sorted unique members and indices")
        for member in self.members:
            expected_kind = "line" if member.kind == "paragraph" else "table"
            if (member.physical_page != self.physical_page or
                    not re.fullmatch(re.escape(f"pdf/page[{self.physical_page}]/") +
                                     expected_kind + r"\[\d+\]", member.locator) or
                    not _inside_page(member.bbox, self.page_width_pt, self.page_height_pt) or
                    member.role == "page_furniture" and member.kind != "paragraph"):
                raise ValueError("native scope member differs from its physical page or source type")
        body = [member for member in self.members if member.role != "page_furniture"]
        selected = [i for i, member in enumerate(body) if member.role == "selected"]
        if not selected or selected != list(range(selected[0], selected[-1]+1)):
            raise ValueError("native selected members must form one continuous source interval")
        if self.scope_kind == "whole_page" and any(member.role == "excluded" for member in self.members):
            raise ValueError("native whole page cannot exclude an original body member")
        union = _union_boxes([member.bbox for member in self.members if member.role == "selected"])
        if (not _inside_page(self.scope_bbox, self.page_width_pt, self.page_height_pt) or
                not _same_box(union, self.scope_bbox)):
            raise ValueError("native scope bbox must equal its selected source members")
        return self


class NativeParagraphLayoutV2(NativeParagraphLayout):
    physical_page: int = Field(ge=1)
    ink_color_rgb: Annotated[str, Field(pattern=r"^[0-9A-F]{6}$")] | None = Field(
        default=None, exclude_if=lambda value: value is None)


class NativeTableLayoutV2(NativeTableLayout):
    physical_page: int = Field(ge=1)


NativeElementV2 = Annotated[NativeParagraphLayoutV2 | NativeTableLayoutV2, Field(discriminator="kind")]


class NativeFormatLayoutV2(NativeFormatLayout):
    schema_version: Literal["source-response-format-v2"] = "source-response-format-v2"
    elements: list[NativeElementV2]
    page_scopes: list[NativePageScope] = Field(min_length=1)
    form_role: Literal["response", "prescribed_cover"]

    @model_validator(mode="after")
    def sealed_layout(self):
        # Override the v1 single-page validator. V1's schema and serialized
        # fields remain unchanged; each v2 page supplies its own source frame.
        if format_digest(self.model_dump(mode="json", exclude={"proof_sha256"})) != self.proof_sha256:
            raise ValueError("native source-format proof hash mismatch")
        numbers = [page.physical_page for page in self.page_scopes]
        if numbers != list(range(numbers[0], numbers[0]+len(numbers))):
            raise ValueError("native format pages must be ordered and consecutive")
        first = self.page_scopes[0]
        if (self.physical_page != first.physical_page or
                abs(self.page_width_pt-first.page_width_pt) > .01 or
                abs(self.page_height_pt-first.page_height_pt) > .01):
            raise ValueError("native heading frame must equal the first source page")
        pages = {page.physical_page: page for page in self.page_scopes}
        members = [member for page in self.page_scopes for member in page.members]
        ordinals = [member.ordinal for member in members]
        if (ordinals != sorted(set(ordinals)) or len({m.block_id for m in members}) != len(members) or
                len({m.locator for m in members}) != len(members)):
            raise ValueError("native format source members may occur once in original order")
        selected = [member for member in members if member.role == "selected"]
        by_id = {member.block_id: member for member in selected}

        def member(block_id, locator, text, kind, page):
            item = by_id.get(block_id)
            if (item is None or item.locator != locator or item.kind != kind or item.physical_page != page or
                    item.text_sha256 != hashlib.sha256(text.encode()).hexdigest()):
                raise ValueError("native selected member must match its exact source block")
            return item

        heading = member(self.heading_source_block_id, self.heading_source_locator,
                         self.heading_text, "paragraph", first.physical_page)
        if not _same_box(heading.bbox, self.heading_bbox):
            raise ValueError("native heading bbox differs from its selected source member")
        expected_ids = [heading.block_id]
        indices = []
        expected_indices = {page: [] for page in pages}
        for element in self.elements:
            page = pages.get(element.physical_page)
            if page is None or not _inside_page(element.bbox, page.page_width_pt, page.page_height_pt):
                raise ValueError("native element exceeds its own source page")
            if isinstance(element, NativeParagraphLayoutV2):
                original = [member(block_id, locator, text, "paragraph", element.physical_page)
                            for block_id, locator, text in zip(element.source_block_ids,
                                element.source_locators, element.source_texts)]
                current_indices = element.fixed_indices
                for rule in element.underlines:
                    if (rule.bbox is None or
                            not _inside_page(rule.bbox, page.page_width_pt, page.page_height_pt, stroke=True) or
                            abs(rule.bbox[2]-rule.bbox[0]-rule.width_pt) > .01):
                        raise ValueError("native source rule geometry changed")
            else:
                original = [member(element.source_block_id, element.source_locator,
                                   "\n".join("\t".join(row) for row in element.source_rows),
                                   "table", element.physical_page)]
                current_indices = [element.fixed_index]
            if not _same_box(_union_boxes([item.bbox for item in original]), element.bbox):
                raise ValueError("native element bbox differs from its selected source members")
            expected_ids.extend(item.block_id for item in original)
            indices.extend(current_indices)
            expected_indices[element.physical_page].extend(current_indices)
        if (indices != list(range(len(indices))) or
                expected_ids != [item.block_id for item in selected] or
                any(page.fixed_indices != expected_indices[page.physical_page] for page in self.page_scopes)):
            raise ValueError("native selected scope must cover heading and every fixed block exactly once")
        return self


NativeFormatSpec = Annotated[NativeFormatLayout | NativeFormatLayoutV2, Field(discriminator="schema_version")]
