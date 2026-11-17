"""Partial source reading never changes full proof scope or silently recrops."""
import copy
import hashlib

from PIL import Image
import pytest

from company_bid.evidence import EvidenceSelection, _selected_png
from company_bid.material_ocr_regions import (
    MaterialOCRRegionFailure, derive_reading_region, validate_reading_crops,
)
from company_bid.storage import digest


@pytest.fixture
def case(tmp_path):
    parent = EvidenceSelection(selection_id="parent", source_id="S1", source_sha256="1" * 64,
        title="Proof", format="docx_images", package_parts=["word/media/image7.jpeg"], selection_reason="proof scope")
    image = Image.new("RGB", (100, 200), "white")
    image.putpixel((25, 30), (0, 0, 0))
    raw, _ = _selected_png(image, parent)
    path = tmp_path / "proof.png"
    path.write_bytes(raw)
    parent_hash = hashlib.sha256(raw).hexdigest()
    identity = {"source_id": parent.source_id, "source_sha256": parent.source_sha256, "format": parent.format,
        "target": parent.package_parts[0], "selection_sha256": digest(parent.model_dump(mode="json")),
        "crop_box_normalized": None, "rotate_clockwise": 0, "render_dpi": 160,
        "group_bindings": [{"group_id": "G1"}], "image_sha256": parent_hash}
    child = parent.model_copy(update={"selection_id": "child", "crop_box_normalized": (.1, .2, .8, .9)})
    child_data, dims = _selected_png(image, child)
    spec = {"selection": child.model_dump(mode="json"), "image_sha256": hashlib.sha256(child_data).hexdigest(),
            "review_method": "agent_source_image_review", "review_id": "review-1",
            "parent_selection_sha256": identity["selection_sha256"]}
    attachment = {"path": str(path), "sha256": parent_hash}
    return identity, attachment, parent, spec, child_data, dims


def derive(case):
    return derive_reading_region(*case[:4])


def test_crop_bytes_identity_and_partial_projection_keep_original_intact(case):
    identity, attachment, parent, spec, data, dims = case
    before = copy.deepcopy((identity, attachment, parent.model_dump(mode="json"), spec))
    from pathlib import Path
    original = Path(attachment["path"]).read_bytes()
    result = derive(case)
    assert result["image_bytes"] == data and result["dimensions"] == dims == [70, 140]
    expected = copy.deepcopy(identity)
    expected.update(crop_box_normalized=spec["selection"]["crop_box_normalized"],
                    selection_sha256=digest(spec["selection"]), image_sha256=spec["image_sha256"], group_bindings=[])
    assert result["child_identity"] == expected
    assert result["projection"]["parent_identity"] == identity
    assert result["projection"]["scope"] == "partial_selected_region"
    assert not result["projection"]["whole_image_read"]
    assert not result["projection"]["original_appendix_replaced"]
    assert not result["projection"]["automatic_fact_adoption"]
    assert result["projection"]["qualification_result"] == "not_determined"
    assert before == (identity, attachment, parent.model_dump(mode="json"), spec)
    assert Path(attachment["path"]).read_bytes() == original


@pytest.mark.parametrize("change", [None, [], {"bad": {}}, {"1" * 64: None}])
def test_invalid_crop_mapping_rejected_before_io(change):
    with pytest.raises(MaterialOCRRegionFailure):
        validate_reading_crops(change)


@pytest.mark.parametrize("field", ["selection", "image_sha256", "review_method", "review_id", "parent_selection_sha256"])
def test_all_frozen_spec_fields_required(case, field):
    del case[3][field]
    with pytest.raises(MaterialOCRRegionFailure):
        derive(case)


@pytest.mark.parametrize("review_method", [None, [], {}, 3, "model_guessed_from_ocr"])
def test_review_method_must_identify_independent_image_review(case, review_method):
    case[3]["review_method"] = review_method
    with pytest.raises(MaterialOCRRegionFailure, match="MATERIAL_OCR_REGION_REVIEW_REQUIRED"):
        derive(case)


@pytest.mark.parametrize("field,value", [("crop_box_normalized", None), ("crop_box_normalized", [0., 0., 1., 1.]),
    ("crop_box_normalized", [-.1, .1, .9, .9]), ("crop_box_normalized", [0., 0., float("nan"), 1.]),
    ("rotate_clockwise", 90), ("package_parts", ["word/media/image7.jpeg", "word/media/image8.jpeg"]),
    ("render_dpi", "160")])
def test_invalid_or_nonpartial_child_selection_rejected(case, field, value):
    case[3]["selection"][field] = value
    with pytest.raises(MaterialOCRRegionFailure):
        derive(case)


@pytest.mark.parametrize("field,value", [("source_id", "OTHER"), ("source_sha256", "2" * 64),
    ("package_parts", ["word/media/image8.jpeg"]), ("render_dpi", 200), ("selection_id", "parent")])
def test_child_cannot_escape_parent_source_target_or_dpi(case, field, value):
    case[3]["selection"][field] = value
    with pytest.raises(MaterialOCRRegionFailure, match="MATERIAL_OCR_REGION_OUTSIDE_PARENT_SCOPE"):
        derive(case)


@pytest.mark.parametrize("field,value", [("crop_box_normalized", [.1, .1, .9, .9]), ("rotate_clockwise", 90),
    ("source_id", "other"), ("target", "word/media/image8.jpeg"), ("format", "pdf_pages"), ("group_bindings", None)])
def test_parent_identity_must_match_complete_selection(case, field, value):
    case[0][field] = value
    with pytest.raises(MaterialOCRRegionFailure, match="MATERIAL_OCR_REGION_PARENT_INVALID"):
        derive(case)


def test_already_cropped_parent_selection_is_rejected(case):
    parent = case[2].model_copy(update={"crop_box_normalized": (.1, .1, .9, .9)})
    with pytest.raises(MaterialOCRRegionFailure, match="MATERIAL_OCR_REGION_PARENT_TRANSFORM_UNSUPPORTED"):
        derive_reading_region(case[0], case[1], parent, case[3])


def test_changed_parent_selection_hash_rejected(case):
    case[3]["parent_selection_sha256"] = "f" * 64
    with pytest.raises(MaterialOCRRegionFailure, match="MATERIAL_OCR_REGION_PARENT_SELECTION_CHANGED"):
        derive(case)


def test_missing_selection_defaults_are_not_invented(case):
    del case[3]["selection"]["pages"]
    with pytest.raises(MaterialOCRRegionFailure, match="MATERIAL_OCR_REGION_SELECTION_INVALID"):
        derive(case)


def test_changed_original_bytes_and_attachment_hash_are_rejected(case):
    from pathlib import Path
    Path(case[1]["path"]).write_bytes(b"changed")
    with pytest.raises(MaterialOCRRegionFailure, match="MATERIAL_OCR_REGION_PARENT_HASH_INVALID"):
        derive(case)


def test_wrong_frozen_child_hash_does_not_silently_adopt_new_image(case):
    case[3]["image_sha256"] = "f" * 64
    with pytest.raises(MaterialOCRRegionFailure, match="MATERIAL_OCR_REGION_FROZEN_IMAGE_HASH_MISMATCH"):
        derive(case)


def test_validated_mapping_is_independent_copy(case):
    mapping = {case[0]["image_sha256"]: case[3]}
    checked = validate_reading_crops(mapping)
    checked[case[0]["image_sha256"]]["selection"]["title"] = "changed"
    assert mapping[case[0]["image_sha256"]]["selection"]["title"] == "Proof"


def test_pdf_single_page_crop_retains_physical_target(case):
    identity, attachment, parent, spec, _, _ = case
    parent = parent.model_copy(update={"format": "pdf_pages", "pages": [4], "package_parts": []})
    identity.update(format="pdf_pages", target=4, selection_sha256=digest(parent.model_dump(mode="json")))
    spec["selection"].update(format="pdf_pages", pages=[4], package_parts=[])
    spec["parent_selection_sha256"] = identity["selection_sha256"]
    result = derive_reading_region(identity, attachment, parent, spec)
    assert result["child_identity"]["target"] == 4 and result["child_identity"]["format"] == "pdf_pages"
