"""Replay a split confidentiality clause through the native-N2 source and checkpoint gates."""

import copy
import hashlib
import json

import pdfplumber
import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from company_bid import contract_attachment_citation_split as split
from company_bid import extraction as ext
from company_bid.observability import redact_payload
from company_bid.pdf_text_lines import pdf_page_text_lines
from company_bid.storage import digest, json_bytes
from test_reading import pdf
from test_source_selected_citations import block, fixture, live_candidate


def _native_pdf(tmp_path, texts):
    # A ToUnicode map supplies CJK text while keeping the synthetic file
    # independent of system fonts and their rendering versions.
    alphabet = list(dict.fromkeys(" " + "".join(texts)))
    codes = {character: i for i, character in enumerate(alphabet, 32)}
    commands = []
    for i, text in enumerate(texts):
        raw = bytes(codes[character] for character in text)
        commands.append(f"BT /F1 8 Tf 25 {200 - 14*i} Td <{raw.hex()}> Tj ET")
    draft = pdf(tmp_path / "draft.pdf", ["\n".join(commands)])
    writer = PdfWriter(clone_from=PdfReader(draft))
    font = writer.pages[0]["/Resources"]["/Font"]["/F1"]
    font[NameObject("/BaseFont")] = NameObject("/SyntheticConfidentiality")
    font[NameObject("/FirstChar")] = NumberObject(32)
    font[NameObject("/LastChar")] = NumberObject(max(codes.values()))
    font[NameObject("/Widths")] = ArrayObject([NumberObject(500) for _ in alphabet])
    font[NameObject("/FontDescriptor")] = DictionaryObject({NameObject("/MissingWidth"): NumberObject(500)})
    cmap = DecodedStreamObject()
    mappings = "\n".join(f"<{code:02x}> <{ord(character):04x}>" for character, code in codes.items())
    cmap.set_data(("1 begincodespacerange\n<00> <ff>\nendcodespacerange\n"
                   f"{len(mappings.splitlines())} beginbfchar\n{mappings}\nendbfchar\n").encode("ascii"))
    font[NameObject("/ToUnicode")] = writer._add_object(cmap)
    path = tmp_path / "source.pdf"
    writer.write(path)
    return path


@pytest.fixture
def source(tmp_path):
    texts = ["附件9：保密条款", "第一条 保密信息定义", "1.1 本保密条款项下的保密信息指非公开信息。",
             "1.2 本保密条款将不适用：", "1.2.1 已公开的信息。", "1.3 本保密条款所称的接收方指获知方。",
             "1.4 其他：【/】", "第二条 保密义务", "2.1 接收方应保守秘密，", "并按约定用途使用。",
             "2.2 接收方应妥善保存信息。", "2.3 接收方须遵守本合同。"]
    path = _native_pdf(tmp_path, texts)
    manifest, parsed = fixture(tmp_path)
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest["sources"][0].update(path=str(path), sha256=source_hash)
    parsed["documents"][0].update(format="pdf", sha256=source_hash)
    with pdfplumber.open(path) as document:
        native = list(pdf_page_text_lines(document.pages[0], ()))
    assert [text for _, text, _, _, _ in native] == texts
    for i, text, box, _, _ in native:
        line = block(parsed, f"b{i}", text, origin="native", bbox=box)
        line["locator"] = f"pdf/page[1]/line[{i}]"
    parsed["documents"][0]["units"][0]["coverage"].update(origin="native", unread_objects=[], ignored_objects=[],
        unmapped_glyphs=[], error_code=None, warnings=[], table_count=0, image_count=0, transformations=[])
    blocks, _, documents = ext._index(parsed, manifest)
    bare = ext.extraction_window_config({"window_max_blocks": 10})
    windows, _ = ext._windows(documents, blocks, bare, manifest=manifest)
    raw = live_candidate({"window": windows[0]}, {"block_ids": ["b2", "b3", "b4", "b5", "b6", "b8", "b9"]})
    raw["requirements"][0].update(category="commercial", response_mode="procedure")
    config = {"window_id": windows[0]["window_id"], "source_id": "T", "source_sha256": source_hash,
              "raw_candidate_sha256": digest(raw), "candidate_index": 0, "physical_page": 1,
              "gap_heading_block_id": "b7", "rendered_rgb_sha256": split.rendered_page_rgb_sha256(path, 1)}
    return manifest, documents, blocks, bare, windows, raw, config


def _bind(source):
    manifest, documents, blocks, bare, _, _, config = source
    frozen = ext.extraction_window_config({**bare, split.SOURCE_CONFIG_KEY: config})
    return frozen, ext._windows(documents, blocks, frozen, manifest=manifest)[0]


def test_procedure_keeps_two_exact_evidence_groups_with_original_failure(source):
    _, _, _, bare, legacy, raw, _ = source
    frozen, windows = _bind(source)
    target = windows[0]
    assert windows[1] == legacy[1] and len(windows) == 2
    assert all(ext.extraction_window_payload(before, bare) == ext.extraction_window_payload(after, frozen)
               for before, after in zip(legacy, windows, strict=True))
    with pytest.raises(ext.PlanningError, match="CITATION_SPAN_NOT_CONTIGUOUS"):
        ext.validate_live_window(raw, legacy[0])
    result = ext.run_extraction_window(None, target, frozen, lambda *args: raw)
    checkpoint = json.loads(json_bytes(redact_payload(result)))
    assert checkpoint == result and checkpoint["raw_model_candidate"] == raw
    assert ext._validate_normalized_window_result(checkpoint, target) == result["validated"]
    evidence = checkpoint[split.RESOLUTION_KEY]
    assert evidence["context_citation"]["block_ids"] == ["b2", "b3", "b4", "b5", "b6"]
    assert evidence["operative_citation"]["block_ids"] == ["b8", "b9"]
    assert evidence["proof"]["omitted_heading"]["block_id"] == "b7"
    assert evidence["proof"]["following_clause_start"]["owner_window_id"] == windows[1]["window_id"]
    requirements, formats = ext._merge_results([checkpoint])
    assert not formats and len(requirements) == 1
    assert requirements[0]["text"] == "2.1 接收方应保守秘密，\n并按约定用途使用。"
    assert requirements[0]["contract_scope_citations"] == [evidence["context_citation"]]
    checkpoint[split.RESOLUTION_KEY]["context_citation"]["quote"] = "changed"
    with pytest.raises(ext.PlanningError, match="EXTRACTION_CONTRACT_ATTACHMENT_CITATION_SPLIT_MISMATCH"):
        ext._validate_normalized_window_result(checkpoint, target)


@pytest.mark.parametrize("drift", ["source_text", "rendered_hash", "ordinary_middle_block", "coverage", "next_clause_owner"])
def test_source_drift_cannot_authorize_the_selected_split(source, drift):
    _, documents, blocks, bare, _, _, config = source
    if drift == "source_text":
        blocks["b3"]["text"] += "未知来源"
    elif drift == "rendered_hash":
        config["rendered_rgb_sha256"] = "0" * 64
    elif drift == "ordinary_middle_block":
        config["gap_heading_block_id"] = "b6"
    elif drift == "coverage":
        documents[0]["units"][0]["coverage"]["unread_objects"] = ["unread"]
    else:
        bare["window_max_blocks"] += 1
    with pytest.raises(ext.PlanningError, match=split.SOURCE_ERROR):
        _bind(source)


@pytest.mark.parametrize("drift", ["raw_digest", "unselected_text", "out_of_order", "moved_label"])
def test_split_does_not_add_unselected_or_misgrounded_content(source, drift):
    raw, config = source[-2:]
    changed = copy.deepcopy(raw)
    candidate = changed["requirements"][0]
    if drift == "unselected_text":
        candidate["citation"]["block_ids"].remove("b4")
    elif drift == "out_of_order":
        candidate["citation"]["block_ids"].reverse()
    elif drift == "moved_label":
        candidate["required_fact_labels"] = ["接收方"]
    else:
        changed["requirements"][0]["confidence"] = "low"
    if drift != "raw_digest":
        config["raw_candidate_sha256"] = digest(changed)
    _, windows = _bind(source)
    with pytest.raises(ext.PlanningError, match=split.SOURCE_ERROR):
        ext.validate_live_window(changed, windows[0])


def test_review_list_keeps_the_same_single_window_proof(source):
    manifest, documents, blocks, bare, _, _, review = source
    frozen, singleton = _bind(source)
    multiple = ext.extraction_window_config({**bare, split.SOURCE_CONFIG_KEY: [review]})
    assert frozen[split.SOURCE_CONFIG_KEY] == review
    assert multiple[split.SOURCE_CONFIG_KEY] == [review]
    assert ext._windows(documents, blocks, multiple, manifest=manifest)[0] == singleton
    with pytest.raises(ext.PlanningError, match="CONTRACT_ATTACHMENT_CITATION_SPLIT_CONFIG_INVALID"):
        ext.extraction_window_config({**bare, split.SOURCE_CONFIG_KEY: [review, review]})
