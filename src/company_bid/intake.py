"""Raw tender and a maintained company-library snapshot, before extraction."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .models import Attachment, Company, Contract, DocumentStyle, Fact, Project, SourceRef, Text, validate_attachment_bundles
from .evidence import EvidenceSelection
from .material_records import MaterialRecord


class IntakeManifest(Contract):
    schema_version: Literal["chain.intake.v1"] = "chain.intake.v1"
    project: Project
    company: Company
    sources: list[SourceRef] = Field(min_length=1)
    source_roles: dict[str, Literal["tender", "company_material", "reference_bid", "project_supplement", "provenance_only"]]
    facts: list[Fact] = Field(default_factory=list)
    material_records: list[MaterialRecord] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)
    evidence_selections: list[EvidenceSelection] = Field(default_factory=list)
    style: DocumentStyle = Field(default_factory=DocumentStyle)
    synthetic: bool = False
    preparation_note: Text

    @model_validator(mode="after")
    def references_and_roles(self):
        source_ids = {source.source_id for source in self.sources}
        if len(source_ids) != len(self.sources) or set(self.source_roles) != source_ids:
            raise ValueError("every unique source needs an explicit ingestion role")
        if "tender" not in self.source_roles.values():
            raise ValueError("a complete chain requires a tender document")
        if len({record.record_id for record in self.material_records}) != len(self.material_records):
            raise ValueError("duplicate material declaration identity")
        source_map = {source.source_id: source for source in self.sources}
        def validate_declaration_source(value):
            if isinstance(value, dict):
                if "source_id" in value and "source_sha256" in value:
                    source = source_map.get(value["source_id"])
                    if (source is None or source.sha256 != value["source_sha256"] or source.kind == "tender"
                            or source.provenance != "derived_from_bid"):
                        raise ValueError("declaration citation must bind its derived company source")
                for child in value.values():
                    validate_declaration_source(child)
            elif isinstance(value, list):
                for child in value:
                    validate_declaration_source(child)
        for record in self.material_records:
            validate_declaration_source(record.model_dump(mode="json"))
        if len({item.selection_id for item in self.evidence_selections}) != len(self.evidence_selections):
            raise ValueError("duplicate proof selection")
        for item in self.evidence_selections:
            source = next((s for s in self.sources if s.source_id == item.source_id), None)
            if source is None or source.sha256 != item.source_sha256 or source.kind == "tender":
                raise ValueError("proof selection must bind an existing company source hash")
        for group, key in [(self.facts, "fact_id"), (self.attachments, "attachment_id")]:
            if len({getattr(item, key) for item in group}) != len(group):
                raise ValueError("library snapshot contains duplicate identities")
        validate_attachment_bundles(self.attachments)
        for item in [self.project, self.company, *self.facts, *self.attachments]:
            if set(item.source_refs) - source_ids:
                raise ValueError("library snapshot contains an unknown source")
        if set(self.style.reference_source_refs) - source_ids:
            raise ValueError("style references an unknown source")
        if self.synthetic and any(not item.synthetic for item in [*self.sources, *self.attachments]):
            raise ValueError("synthetic input must label every original and attachment")
        return self
