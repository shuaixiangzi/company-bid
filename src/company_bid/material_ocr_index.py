"""Bounded local transcription of selected proof images, never verified facts."""
from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import io
from pathlib import Path
import zipfile

from PIL import Image

from .evidence import EvidenceSelection, _selected_png
from .material_content_index import MaterialContentFailure, _require, _verify_file
from .models import file_sha256
from .storage import digest

MATERIAL_OCR_INDEX_VERSION = 'material-ocr-index.v1'


def _render(source, selection, target):
    """Rebuild exactly the selected image, including crop and rotation."""
    if selection.format == 'pdf_pages':
        import pypdfium2 as pdfium
        with pdfium.PdfDocument(source['path']) as document:
            _require(type(target) is int and 1 <= target <= len(document), 'MATERIAL_OCR_PAGE_INVALID')
            page = document[target - 1]
            try:
                width, height = page.get_size()
                scale = selection.render_dpi / 72
                _require(max(width, height) * scale <= 6000 and width * height * scale * scale <= 18_000_000,
                         'MATERIAL_OCR_IMAGE_LIMIT')
                bitmap = page.render(scale=scale)
                try:
                    return _selected_png(bitmap.to_pil(), selection)
                finally:
                    bitmap.close()
            finally:
                page.close()
    with zipfile.ZipFile(source['path']) as archive:
        parts = [p for p in archive.infolist() if p.filename == target]
        _require(len(parts) == 1 and parts[0].file_size <= 20_000_000, 'MATERIAL_OCR_IMAGE_PART_INVALID')
        data = archive.read(parts[0])
    with Image.open(io.BytesIO(data)) as image:
        return _selected_png(image, selection)


def transcribe_selected_index(ctx, manifest, index, options):
    from .material_local_ocr import engine_fingerprint, recognize

    fingerprint = engine_fingerprint(options)
    expected = options.get('expected_engine_fingerprint')
    _require(expected == fingerprint or expected == digest(fingerprint), 'MATERIAL_OCR_ENGINE_NOT_FROZEN')
    output = copy.deepcopy(index)
    sources = {s['source_id']: s for s in manifest['sources']}
    selections = {s['selection_id']: EvidenceSelection.model_validate(s) for s in manifest['evidence_selections']}
    attachments = {a['attachment_id']: a for a in manifest['attachments']}
    groups = {g['group_id']: g for g in output['groups']}
    renderer = {name: importlib.metadata.version(name) for name in ('pypdfium2', 'Pillow')}
    documents = {d['source_id']: d for d in output['parsed']['documents']}
    counters = {'ocr_transcribed_unverified_count': 0, 'ocr_no_text_count': 0,
                'ocr_transcribed_crop_count': 0, 'ocr_text_char_count': 0,
                'ocr_processed_unit_count': 0, 'ocr_failed_count': 0}
    for meta in output['units']:
        if meta['status'] in {'read', 'empty'}:
            continue
        source = sources[meta['source_id']]
        _verify_file(source)
        selected_groups = [groups[gid] for gid in meta['group_ids']]
        candidates = []
        for group in selected_groups:
            origin = group['origin']
            selection = selections[origin['selection_id']]
            targets = selection.pages if selection.format == 'pdf_pages' else selection.package_parts
            _require(selection.source_id == source['source_id'] and selection.source_sha256 == source['sha256']
                     and meta['target'] in targets, 'MATERIAL_OCR_SELECTION_SOURCE_INVALID')
            locator = (f"pdf/page[{meta['target']}]" if selection.format == 'pdf_pages'
                       else 'docx/' + meta['target'])
            aid = 'PROOF-' + digest([selection.selection_id, locator])[:24]
            _require(aid in group['attachment_ids'] and aid in attachments, 'MATERIAL_OCR_ATTACHMENT_SCOPE_INVALID')
            attachment = attachments[aid]
            _require(attachment['page_index'] == targets.index(meta['target']) + 1 and
                     attachment['page_count'] == len(targets), 'MATERIAL_OCR_ATTACHMENT_PAGE_INVALID')
            path = Path(attachment['path'])
            _require(path.is_file() and file_sha256(path) == attachment['sha256'], 'MATERIAL_OCR_ATTACHMENT_HASH_INVALID')
            candidates.append((selection, attachment))
        _require(bool(candidates), 'MATERIAL_OCR_SCOPE_EMPTY')
        selection, attachment = candidates[0]
        transform = lambda s: [s.crop_box_normalized, s.rotate_clockwise, s.render_dpi, s.format]
        _require(all(transform(s) == transform(selection) and a['sha256'] == attachment['sha256']
                     for s, a in candidates), 'MATERIAL_OCR_TRANSFORM_AMBIGUOUS')
        identity = {'source_id': source['source_id'], 'source_sha256': source['sha256'],
                    'format': meta['format'], 'target': meta['target'],
                    'selection_sha256': digest(selection.model_dump(mode='json')),
                    'crop_box_normalized': list(selection.crop_box_normalized) if selection.crop_box_normalized else None,
                    'rotate_clockwise': selection.rotate_clockwise, 'render_dpi': selection.render_dpi,
                    'group_bindings': [{k: g[k] for k in ('group_id', 'origin', 'attachment_ids', 'source_refs')}
                                       for g in selected_groups], 'image_sha256': attachment['sha256']}
        inputs = {'version': MATERIAL_OCR_INDEX_VERSION, 'identity': identity, 'renderer': renderer,
                  'engine_fingerprint': fingerprint, 'options': options}
        unit_id = 'material-ocr-' + digest(identity)[:24]

        def transcribe(source=source, selection=selection, target=meta['target'], identity=identity,
                       attachment=attachment, inputs=inputs):
            try:
                data, dimensions = _render(source, selection, target)
            except MaterialContentFailure:
                raise
            except Exception:
                raise MaterialContentFailure('MATERIAL_OCR_IMAGE_REBUILD_FAILED') from None
            _require(hashlib.sha256(data).hexdigest() == attachment['sha256'],
                     'MATERIAL_OCR_RENDERED_SCOPE_HASH_MISMATCH')
            # Use the authenticated original attachment bytes, never a whole page for a crop.
            response = recognize(ctx, image_path=attachment['path'], image_sha256=attachment['sha256'],
                                 identity=identity, options=options)
            _verify_file(source)
            _require(file_sha256(Path(attachment['path'])) == attachment['sha256'],
                     'MATERIAL_OCR_IMAGE_CHANGED_DURING_CALL')
            result = {'input_sha256': digest(inputs), 'identity': identity, 'dimensions': dimensions,
                      'response': response, 'quality_status': 'machine_transcription_unverified',
                      'reading_completeness': 'not_established', 'layout_status': 'lines_boxes_only_unverified',
                      'qualification_result': 'not_determined', 'automatic_assembly_allowed': False}
            result['output_sha256'] = digest(result)
            return result

        result = ctx.run_unit(unit_id, inputs, transcribe)
        _require(result.get('input_sha256') == digest(inputs) and result.get('identity') == identity and
                 result.get('output_sha256') == digest({k: v for k, v in result.items() if k != 'output_sha256'}),
                 'MATERIAL_OCR_CHECKPOINT_INVALID')
        response = result['response']
        _require(response['image_sha256'] == attachment['sha256'], 'MATERIAL_OCR_RESPONSE_IMAGE_INVALID')
        units = [u for u in documents[source['source_id']]['units'] if u['unit_id'] == meta['unit_id']
                 and set(u['material_content_group_ids']) == set(meta['group_ids'])]
        _require(len(units) == 1, 'MATERIAL_OCR_PROJECTED_UNIT_AMBIGUOUS')
        unit = units[0]
        text = response['text']
        transcription = 'ocr_transcribed_unverified' if text.strip() else 'ocr_no_text_unresolved'
        prefix = unit['locator']
        if meta['format'] == 'pdf_pages':
            prefix += '/selected-image[' + attachment['sha256'] + ']'
        if meta['format'] == 'docx_images':
            images = [b for b in unit['blocks'] if b['kind'] == 'image']
            _require(len(images) == 1, 'MATERIAL_OCR_PROJECTED_IMAGE_AMBIGUOUS')
            prefix = images[0]['locator']
        for number, line in enumerate(response['lines']):
            if line['text'].strip():
                boxes = [word['box'] for word in line.get('words', [])]
                bbox = ([min(b[0] for b in boxes), min(b[1] for b in boxes),
                         max(b[0] + b[2] for b in boxes), max(b[1] + b[3] for b in boxes)] if boxes else None)
                unit['blocks'].append({'block_id': unit_id + '-line-' + str(number), 'source_id': source['source_id'],
                                      'kind': 'paragraph', 'origin': 'ocr', 'locator': prefix + '/ocr/line[' + str(number) + ']',
                                      'text': line['text'], 'rows': [], 'bbox': bbox})
        unit['material_content_ocr_candidate_readable'] = bool(text.strip())
        unit['transcription_status'] = transcription
        unit['coverage'].update(status='needs_ocr', origin='mixed' if unit['coverage']['native_text_chars'] else 'ocr',
                                error_code=None, warnings=[*unit['coverage'].get('warnings', []),
                                    'OCR_TEXT_AND_TABLE_RELATIONSHIPS_NOT_HUMAN_VERIFIED'])
        meta.update(transcription_status=transcription, transcription_scope='selected_crop' if selection.crop_box_normalized else 'selected_image',
                    ocr_char_count=len(text), ocr_text_sha256=hashlib.sha256(text.encode('utf-8')).hexdigest(),
                    ocr_unit_id=unit_id, ocr_input_sha256=digest(inputs), ocr_output_sha256=result['output_sha256'],
                    ocr_image_sha256=attachment['sha256'], coordinate_space='selected_image_pixels',
                    local_ocr_call_id=response['call_id'], reading_completeness='not_established',
                    layout_status='lines_boxes_only_unverified', status='needs_ocr')
        for group in selected_groups:
            for row in group['units']:
                if row['unit_id'] == meta['unit_id']:
                    row.update(copy.deepcopy(meta))
        counters['ocr_processed_unit_count'] += 1
        counters['ocr_text_char_count'] += len(text)
        counters['ocr_transcribed_unverified_count'] += bool(text.strip())
        counters['ocr_no_text_count'] += not text.strip()
        counters['ocr_transcribed_crop_count'] += bool(text.strip() and selection.crop_box_normalized)
    coverage = output['coverage']
    coverage.update(counters)
    coverage['status_counts'] = {status: sum(u['status'] == status for u in output['units'])
                                 for status in ('read', 'empty', 'needs_ocr', 'unsupported')}
    coverage['pending_ocr_unit_count'] = sum(u['status'] not in {'read', 'empty'} and
        u.get('transcription_status') != 'ocr_transcribed_unverified' for u in output['units'])
    coverage['local_ocr_engine'] = fingerprint
    coverage['ocr_processed_count_includes_reused_checkpoints'] = True
    coverage['api_ocr_calls'] = 0
    # Per-attempt events are authoritative for actual calls, not this checkpoint total.
    coverage.pop('ocr_calls', None)
    output['parsed']['coverage'] = copy.deepcopy(coverage)
    output['summary']['local_ocr'] = 'candidate_text_only_no_fact_or_qualification_adoption'
    output['native_index_sha256'] = index['output_sha256']
    output['ocr_index_version'] = MATERIAL_OCR_INDEX_VERSION
    output['output_sha256'] = digest({k: v for k, v in output.items() if k != 'output_sha256'})
    captured = ctx.capture('material-local-ocr-index', output)
    _require(captured and captured.get('status') == 'captured', 'MATERIAL_OCR_INDEX_CAPTURE_UNAVAILABLE')
    return output
