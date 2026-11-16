"""Selected native content, honest scan coverage, and per-page recovery."""
import copy

from docx import Document
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
import pytest

from company_bid import material_content_index as indexer
from company_bid.material_matching import material_candidate_report
from company_bid.models import file_sha256
from company_bid.runtime import Executor, NodeSpec


class Context:
    def __init__(self):
        self.units = {}
        self.captures = {}

    def run_unit(self, key, inputs, fn):
        value = fn()
        self.units[key] = (copy.deepcopy(inputs), copy.deepcopy(value))
        return value

    def capture(self, name, value):
        self.captures[name] = copy.deepcopy(value)


def pdf_file(path, texts):
    writer = PdfWriter()
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    for text in texts:
        page = writer.add_blank_page(width=500, height=700)
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
        stream = DecodedStreamObject()
        stream.set_data(f'BT /F1 14 Tf 30 640 Td ({text}) Tj ET'.encode('ascii'))
        page[NameObject('/Contents')] = writer._add_object(stream)
    writer.write(path)


def manifest(path, *, pages=None, parts=None, title="合成扫描件", crop=None):
    targets = parts if parts is not None else pages or [1]
    source = {"source_id": "S", "sha256": file_sha256(path), "kind": "company_material", "path": str(path), "title": title}
    selection = {"selection_id": "G", "title": title, "source_id": "S", "source_sha256": source['sha256'],
                 "format": "docx_images" if parts is not None else "pdf_pages", "pages": [] if parts is not None else targets,
                 "package_parts": targets if parts is not None else [], "selection_reason": "Synthetic scope test."}
    if crop:
        selection['crop_box_normalized'] = crop
    return {"sources": [source], "attachments": [
        {"attachment_id": f"A-{n}", "bundle_id": "G", "title": title, "source_refs": ["S"],
         "path": "synthetic-page.png", "sha256": "a" * 64, "page_count": len(targets), "page_index": n}
        for n, _ in enumerate(targets, 1)], "evidence_selections": [selection], "material_records": []}


def test_only_selected_native_pdf_page_is_indexed_without_mutating_inputs(tmp_path):
    path = tmp_path / 'proof.pdf'
    pdf_file(path, ['UNSELECTED ISO9001', 'SELECTED ISO14001'])
    value = manifest(path, pages=[2]); parsed = {'documents': []}
    before = copy.deepcopy((value, parsed)); ctx = Context()
    result = indexer.build_material_content_index(ctx, value, parsed)
    assert (value, parsed) == before
    assert result['coverage']['selected_pdf_pages'] == 1
    assert result['coverage']['status_counts']['read'] == 1
    unit, = result['parsed']['documents'][0]['units']
    assert unit['page'] == 2
    assert 'ISO14001' in indexer._text(unit) and 'ISO9001' not in indexer._text(unit)
    report = material_candidate_report([{'requirement_id': 'R', 'response_mode': 'attachment', 'text': '须提供ISO9001认证证书。'}], [],
        attachments=value['attachments'], sources=value['sources'], evidence_selections=value['evidence_selections'],
        parsed=result['parsed'])
    assert report['proof_queries'][0]['candidate_group_ids'] == []
    assert len(ctx.units) == 1
    assert result['summary']['qualification_result'] == 'not_determined'


def test_scanned_selected_page_stays_needs_ocr_without_external_call(tmp_path):
    path = tmp_path / 'scan.pdf'
    Image.new('RGB', (100, 150), 'white').save(path, 'PDF')
    result = indexer.build_material_content_index(Context(), manifest(path), {'documents': []})
    assert result['coverage']['status_counts']['needs_ocr'] == 1
    assert result['coverage']['units_with_text'] == 0
    assert result['coverage']['ocr_calls'] == result['coverage']['model_calls'] == 0


def test_native_text_on_partly_unread_page_remains_candidate_with_unread_scope(tmp_path, monkeypatch):
    path = tmp_path / 'partial.pdf'; pdf_file(path, ['ISO9001'])
    read_page = indexer._read_pdf_page
    def partial(*args):
        unit = read_page(*args)
        unit.coverage.status = 'needs_ocr'
        unit.coverage.unread_objects = [unit.locator + '/image[0]']
        return unit
    monkeypatch.setattr(indexer, '_read_pdf_page', partial)
    value = manifest(path)
    result = indexer.build_material_content_index(Context(), value, {'documents': []})
    report = material_candidate_report([{'requirement_id': 'R', 'response_mode': 'attachment', 'text': '须提供ISO9001认证证书。'}], [],
        attachments=value['attachments'], sources=value['sources'], evidence_selections=value['evidence_selections'],
        parsed=result['parsed'])
    group, = report['proof_groups']
    assert group['read_scope_status'] == 'unread_or_incomplete'
    assert group['read_scope_unread_count'] == 1
    assert group['read_text_basis'][0]['content_coverage'] == 'partial_native_page_needs_ocr'
    assert report['proof_queries'][0]['candidate_group_ids'] == ['G']
    assert not report['assembly_candidates']


def test_selected_docx_image_does_not_borrow_parent_or_other_image_text(tmp_path):
    png = tmp_path / 'image.png'; Image.new('RGB', (20, 30), 'blue').save(png)
    path = tmp_path / 'parent.docx'; doc = Document()
    doc.add_paragraph('营业执照 ISO9001 unselected parent prose')
    doc.add_picture(str(png)); doc.save(path)
    value = manifest(path, parts=['word/media/image1.png'])
    result = indexer.build_material_content_index(Context(), value, {'documents': []})
    assert result['coverage']['selected_docx_images'] == 1
    assert result['coverage']['status_counts']['needs_ocr'] == 1
    assert result['coverage']['units_with_text'] == 0
    assert result['summary']['parent_docx_text_read'] is False
    assert all(b['kind'] == 'image' for b in result['parsed']['documents'][0]['units'][0]['blocks'])


def test_repeated_selected_docx_image_is_one_asset_and_ocr_stays_unread(tmp_path):
    png = tmp_path / 'image.png'; Image.new('RGB', (20, 30), 'blue').save(png)
    path = tmp_path / 'parent.docx'; doc = Document(); doc.add_picture(str(png)); doc.save(path)
    value = manifest(path, parts=['word/media/image1.png'])
    image_sha = file_sha256(png)
    parsed = {'documents': [{'source_id': 'S', 'sha256': value['sources'][0]['sha256'], 'role': 'reference_bid',
        'units': [{'unit_id': f'U-{i}', 'source_id': 'S', 'coverage': {'status': 'needs_ocr'}, 'blocks': [
            {'block_id': f'I-{i}', 'source_id': 'S', 'kind': 'image', 'locator': f'body/p[{i}]/image[0]',
             'image_ref': {'package_part': 'word/media/image1.png', 'sha256': image_sha}}]} for i in range(2)]}]}
    result = indexer.build_material_content_index(Context(), value, parsed)
    assert result['coverage']['selected_docx_images'] == 1
    assert result['coverage']['status_counts']['needs_ocr'] == 1
    assert result['coverage']['units_with_text'] == 0
    unit, = result['parsed']['documents'][0]['units']
    assert 'REPEATED_SELECTED_IMAGE_REFERENCE_OCR_NOT_ADOPTED' in unit['coverage']['warnings']


def test_source_hash_change_and_invalid_selected_page_fail_closed(tmp_path):
    path = tmp_path / 'proof.pdf'; pdf_file(path, ['Selected'])
    value = manifest(path); pdf_file(path, ['Changed'])
    with pytest.raises(indexer.MaterialContentFailure, match='HASH_MISMATCH'):
        indexer.build_material_content_index(Context(), value, {'documents': []})
    value = manifest(path, pages=[2])
    with pytest.raises(indexer.MaterialContentFailure, match='PAGE_OUT_OF_RANGE'):
        indexer.build_material_content_index(Context(), value, {'documents': []})


def test_crop_does_not_read_unselected_full_page_text(tmp_path):
    path = tmp_path / 'proof.pdf'; pdf_file(path, ['ISO9001'])
    result = indexer.build_material_content_index(Context(), manifest(path, crop=(0.1, 0.1, 0.5, 0.5)), {'documents': []})
    assert result['coverage']['status_counts']['unsupported'] == 1
    assert result['coverage']['units_with_text'] == 0


def test_group_binding_cannot_be_borrowed_by_different_group(tmp_path):
    path = tmp_path / 'proof.pdf'; pdf_file(path, ['ISO9001'])
    value = manifest(path)
    result = indexer.build_material_content_index(Context(), value, {'documents': []})
    result['parsed']['group_bindings'][0]['attachment_ids'] = ['OTHER']
    report = material_candidate_report([{'requirement_id': 'R', 'response_mode': 'attachment', 'text': '须提供ISO9001认证证书。'}], [],
        attachments=value['attachments'], sources=value['sources'], evidence_selections=value['evidence_selections'],
        parsed=result['parsed'])
    assert report['proof_queries'][0]['candidate_group_ids'] == []


def test_page_failure_resumes_without_reextracting_successful_page(tmp_path, monkeypatch):
    path = tmp_path / 'proof.pdf'; pdf_file(path, ['First', 'Second'])
    value = {'manifest': manifest(path, pages=[1, 2]), 'parsed': {'documents': []}}
    calls = []
    read_page = indexer._read_pdf_page
    def flaky(source, page, target, config, ocr):
        calls.append(target)
        if calls == [1, 2]:
            raise RuntimeError('Synthetic interrupted page')
        return read_page(source, page, target, config, ocr)
    monkeypatch.setattr(indexer, '_read_pdf_page', flaky)
    executor = Executor(tmp_path / 'runtime', [NodeSpec('N3', 'test-content-v1',
        lambda ctx, inputs: indexer.build_material_content_index(ctx, inputs['manifest'], inputs['parsed']))], {})
    run_id = executor.create_run(value)
    failed = executor.execute(run_id)
    assert failed['status'] == 'failed'
    complete = executor.resume(run_id)
    assert complete['status'] == 'succeeded', complete.get('error_code')
    assert calls == [1, 2, 2]
    result = executor.output(run_id)
    assert result['coverage']['selected_unit_count'] == 2
    assert result['coverage']['status_counts']['read'] == 2
