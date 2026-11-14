"""Raw tender and a maintained company-library snapshot, before extraction."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .models import Attachment, Company, Contract, DocumentStyle, Fact, Project, SourceRef, Text, Sha256, file_sha256, validate_attachment_bundles
from .evidence import EvidenceSelection
from .material_records import MaterialRecord


class ProofParentLocator(Contract):
    text: Text
    status: Literal["declared_unverified_parent_page_range"]


class ProofVisualReview(Contract):
    method: Literal["agent_visual_review_of_local_render"]
    status: Literal["candidate_identity_and_boundary_reviewed"]
    project_title: Text
    physical_pages: list[int] = Field(min_length=1, max_length=100)
    observations: list[Text] = Field(min_length=1, max_length=30)


class ProofDerivation(Contract):
    derivation_id: Text
    relation: Literal["user_authorized_bid_split"]
    selection_id: Text
    child_source_id: Text
    child_source_sha256: Sha256
    parent_source_id: Text
    parent_source_sha256: Sha256
    record_id: Text
    physical_pages: list[int] = Field(min_length=1, max_length=100)
    parent_locator: ProofParentLocator
    visual_review: ProofVisualReview
    qualification_result: Literal["not_determined"]
    project_assignment: Literal["not_made"]

    @model_validator(mode="after")
    def complete_declared_scope(self):
        if self.physical_pages != list(range(1, len(self.physical_pages) + 1)):
            raise ValueError("derived proof keeps the whole ordered physical source")
        if (len(set(self.visual_review.physical_pages)) != len(self.visual_review.physical_pages) or
                set(self.visual_review.physical_pages) - set(self.physical_pages)):
            raise ValueError("visual review pages must belong to the selected source")
        return self


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
    proof_derivations: list[ProofDerivation] = Field(default_factory=list)
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
        if len({d.derivation_id for d in self.proof_derivations}) != len(self.proof_derivations):
            raise ValueError("duplicate proof derivation")
        for derivation in self.proof_derivations:
            child, parent = source_map.get(derivation.child_source_id), source_map.get(derivation.parent_source_id)
            selection = next((x for x in self.evidence_selections if x.selection_id == derivation.selection_id), None)
            record = next((r for r in self.material_records if r.record_id == derivation.record_id), None)
            if (child is None or parent is None or child.provenance != "derived_from_bid" or
                    parent.provenance != "derived_from_bid" or child.kind == "tender" or parent.kind == "tender" or
                    child.sha256 != derivation.child_source_sha256 or parent.sha256 != derivation.parent_source_sha256 or
                    selection is None or selection.source_id != child.source_id or selection.format != "pdf_pages" or
                    selection.pages != derivation.physical_pages or selection.crop_box_normalized is not None or
                    selection.rotate_clockwise != 0 or record is None or record.kind != "historical_performance"):
                raise ValueError("derived proof must bind its exact sources, record and complete selection")
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


def verify_derived_proof_scopes(ctx, manifest):
    """Check actual frozen PDF page counts before rendering any declared full pack."""
    from .storage import digest
    from .evidence import EvidenceFailure
    for derivation in manifest.get("proof_derivations", []):
        source = next(s for s in manifest["sources"] if s["source_id"] == derivation["child_source_id"])
        def verify(source=source, derivation=derivation):
            from pathlib import Path
            import pypdfium2 as pdfium
            if file_sha256(Path(source["path"])) != derivation["child_source_sha256"]:
                raise EvidenceFailure("DERIVED_PROOF_SOURCE_CHANGED")
            with pdfium.PdfDocument(source["path"]) as doc:
                count = len(doc)
            if derivation["physical_pages"] != list(range(1, count + 1)):
                raise EvidenceFailure("DERIVED_PROOF_PHYSICAL_SCOPE_INCOMPLETE")
            return {"source_id": source["source_id"], "source_sha256": source["sha256"],
                    "physical_page_count": count, "complete_physical_source": True,
                    "complete_contract_text": "not_determined", "qualification_result": "not_determined"}
        ctx.run_unit("verify-proof-scope-" + digest(derivation)[:24],
                     {"derivation": derivation, "source": source}, verify)
