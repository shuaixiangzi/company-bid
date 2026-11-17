"""DeepSeek-only selected proof transcription; failures never fall back."""
from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import io
import re
from pathlib import Path
import zipfile

from PIL import Image

from .evidence import EvidenceSelection, _selected_png
from .material_content_index import MaterialContentFailure, _require, _verify_file
from .models import file_sha256
from .storage import digest

MATERIAL_OCR_INDEX_VERSION = 'material-ocr-index.v3'


def _settings(options):
    _require(isinstance(options, dict) and options.get('engine') == 'deepseek_ocr',
             'MATERIAL_OCR_DEEPSEEK_REQUIRED_NO_LOCAL_FALLBACK')
    provider = {k: v for k, v in options.items() if k not in {'recorded_sources', 'source_checks', 'reading_crops', 'recorded_only'}}
    _require(type(options.get('recorded_only', False)) is bool, 'MATERIAL_OCR_RECORDED_ONLY_INVALID')
    bindings, checks = options.get('recorded_sources', {}), options.get('source_checks', {})
    for mapping in (bindings, checks):
        _require(isinstance(mapping, dict) and len(mapping) <= 2000 and
                 all(isinstance(k, str) and re.fullmatch(r'[0-9a-f]{64}', k) for k in mapping),
                 'MATERIAL_OCR_REVIEW_OR_REPLAY_CONFIG_INVALID')
        _require(all(isinstance(value, dict) and bool(value) for value in mapping.values()),
                 'MATERIAL_OCR_REVIEW_OR_REPLAY_CONFIG_INVALID')
    for binding in bindings.values():
        _require(set(binding) == {'run_id', 'node_key', 'unit_id'} and
                 all(isinstance(value, str) and value for value in binding.values()) and
                 re.fullmatch(r'[0-9a-f]{32}', binding['run_id']) and
                 (binding['node_key'] == 'OCR_COMPARISON' or binding['node_key'] == 'N3' and
                  re.fullmatch(r'material-ocr-[0-9a-f]{24}', binding['unit_id'])),
                 'MATERIAL_OCR_REVIEW_OR_REPLAY_CONFIG_INVALID')
    return provider, bindings, checks


def _reviewed_reference(review, identity):
    if review is None:
        return None
    _require(isinstance(review, dict) and set(review) == {
        'source_id', 'source_sha256', 'selection_sha256', 'image_sha256',
        'review_method', 'review_id', 'reference', 'reference_sha256'}, 'MATERIAL_OCR_REVIEW_SCHEMA_INVALID')
    _require(all(review[k] == identity[k] for k in
                 ('source_id', 'source_sha256', 'selection_sha256', 'image_sha256')),
             'MATERIAL_OCR_REVIEW_SOURCE_CHANGED')
    _require(review['review_method'] in {'operator_source_image_review', 'agent_source_image_review'} and
             isinstance(review['review_id'], str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,120}', review['review_id']),
             'MATERIAL_OCR_REVIEW_PROVENANCE_REQUIRED')
    reference = review['reference']
    _require(isinstance(reference, dict) and reference.get('image_sha256') == identity['image_sha256'] and
             digest(reference) == review['reference_sha256'], 'MATERIAL_OCR_REVIEW_HASH_INVALID')
    return copy.deepcopy(reference)


def _recorded(ctx, binding, identity, dimensions, provider, fingerprint, reference):
    from .material_ocr_replay import replay_material_ocr
    from .material_ocr_quality import assess_ocr_response

    metadata = {'operation': 'material_ocr_replay_validation', 'input_hash': digest(identity)}
    provenance = None
    with ctx._observer.span('material.ocr_replay_validation', {**metadata, **ctx._metadata()}):
        try:
            raw, provenance = replay_material_ocr(ctx, binding=binding, image_sha256=identity['image_sha256'],
                identity=identity, purpose=provider.get('purpose', 'rich_layout_recognition'))
            result = assess_ocr_response(raw, width=dimensions[0], height=dimensions[1],
                                         identity=identity, numeric_reference=reference)
            result.update(width=dimensions[0], height=dimensions[1], image_sha256=identity['image_sha256'],
                          engine_fingerprint=fingerprint, call_id=provenance['origin_call_id'],
                          recorded_response=provenance, actual_new_api_calls=0,
                          qualification_result='not_determined', project_assignment='not_made',
                          quality_status='machine_transcription_unverified')
            captured = ctx.capture('material-ocr-replay-validation', {
                'status': 'passed', 'provenance': provenance, 'validated_output': result})
            _require(captured and captured['status'] == 'captured', 'MATERIAL_OCR_REPLAY_CAPTURE_REQUIRED')
            ctx.call_event('material.ocr_replay.validated', {**metadata, 'output_hash': digest(result),
                                                           'validation_status': 'passed'})
            return result
        except Exception as error:
            code = getattr(error, 'code', 'MATERIAL_OCR_REPLAY_VALIDATION_FAILED')
            captured = ctx.capture('material-ocr-replay-validation-error', {'status': 'failed',
                'provenance': provenance, 'error_code': code, 'details': getattr(error, 'details', {})})
            _require(captured and captured['status'] == 'captured', 'MATERIAL_OCR_REPLAY_CAPTURE_REQUIRED')
            ctx.call_event('material.ocr_replay.rejected', {**metadata, 'error_code': code,
                                                          'validation_status': 'failed'})
            raise


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


def _image_identity(source, meta, selection, selected_groups, attachment):
    return {'source_id': source['source_id'], 'source_sha256': source['sha256'],
            'format': meta['format'], 'target': meta['target'],
            'selection_sha256': digest(selection.model_dump(mode='json')),
            'crop_box_normalized': list(selection.crop_box_normalized) if selection.crop_box_normalized else None,
            'rotate_clockwise': selection.rotate_clockwise, 'render_dpi': selection.render_dpi,
            'group_bindings': [{k: g[k] for k in ('group_id', 'origin', 'attachment_ids', 'source_refs')}
                               for g in selected_groups], 'image_sha256': attachment['sha256']}


def transcribe_selected_index(ctx, manifest, index, options):
    from .material_deepseek_ocr import engine_fingerprint, recognize
    from .material_ocr_regions import validate_reading_crops, derive_reading_region

    provider, bindings, reviews = _settings(options)
    crops = validate_reading_crops(options.get('reading_crops', {}))
    fingerprint = engine_fingerprint(provider)
    expected = provider.get('expected_engine_fingerprint')
    _require(expected == fingerprint or expected == digest(fingerprint), 'MATERIAL_OCR_ENGINE_NOT_FROZEN')
    output = copy.deepcopy(index)
    sources = {s['source_id']: s for s in manifest['sources']}
    selections = {s['selection_id']: EvidenceSelection.model_validate(s) for s in manifest['evidence_selections']}
    attachments = {a['attachment_id']: a for a in manifest['attachments']}
    groups = {g['group_id']: g for g in output['groups']}
    renderer = {name: importlib.metadata.version(name) for name in ('pypdfium2', 'Pillow')}
    documents = {d['source_id']: d for d in output['parsed']['documents']}
    planned_images, planned_regions = set(), {}
    for meta in output['units']:
        if meta['status'] in {'read', 'empty'}:
            continue
        for gid in meta['group_ids']:
            group = groups[gid]
            selection = selections[group['origin']['selection_id']]
            targets = selection.pages if selection.format == 'pdf_pages' else selection.package_parts
            source = sources[meta['source_id']]
            _require(selection.source_id == source['source_id'] and selection.source_sha256 == source['sha256']
                     and meta['target'] in targets, 'MATERIAL_OCR_SELECTION_SOURCE_INVALID')
            locator = (f"pdf/page[{meta['target']}]" if selection.format == 'pdf_pages'
                       else 'docx/' + meta['target'])
            aid = 'PROOF-' + digest([selection.selection_id, locator])[:24]
            _require(aid in attachments and aid in group['attachment_ids'], 'MATERIAL_OCR_ATTACHMENT_SCOPE_INVALID')
            planned_images.add(attachments[aid]['sha256'])
            attachment = attachments[aid]
            if attachment['sha256'] in crops:
                parent_identity = _image_identity(source, meta, selection,
                    [groups[g] for g in meta['group_ids']], attachment)
                region = derive_reading_region(parent_identity, attachment, selection, crops[attachment['sha256']])
                previous = planned_regions.get(attachment['sha256'])
                _require(previous is None or previous['projection'] == region['projection'],
                         'MATERIAL_OCR_TRANSFORM_AMBIGUOUS')
                planned_regions[attachment['sha256']] = region
    _require(set(crops) <= planned_images, 'MATERIAL_OCR_UNUSED_READING_CROP_SCOPE')
    reading_images = (planned_images - set(crops)) | {r['child_identity']['image_sha256'] for r in planned_regions.values()}
    _require(set(bindings) <= reading_images and set(reviews) <= reading_images,
             'MATERIAL_OCR_UNUSED_REPLAY_OR_REVIEW_SCOPE')
    _require(not options.get('recorded_only') or set(bindings) == reading_images,
             'MATERIAL_OCR_RECORDED_ONLY_COVERAGE_MISSING')
    counters = {'ocr_transcribed_unverified_count': 0, 'ocr_no_text_count': 0,
                'ocr_transcribed_crop_count': 0, 'ocr_text_char_count': 0,
                'ocr_processed_unit_count': 0, 'ocr_failed_count': 0,
                'ocr_recorded_response_count': 0, 'ocr_numeric_pending_unit_count': 0,
                'ocr_numeric_issue_count': 0, 'ocr_numeric_issue_codes': {}}
    seen_images = set()
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
        parent_identity = _image_identity(source, meta, selection, selected_groups, attachment)
        region = planned_regions.get(attachment['sha256'])
        identity = region['child_identity'] if region else parent_identity
        seen_images.add(identity['image_sha256'])
        binding = bindings.get(identity['image_sha256'])
        review = reviews.get(identity['image_sha256'])
        reference = _reviewed_reference(review, identity)
        inputs = {'version': MATERIAL_OCR_INDEX_VERSION, 'identity': identity, 'renderer': renderer,
                  'engine_fingerprint': fingerprint, 'options': provider,
                  'recorded_source': binding, 'reviewed_numeric_reference': review}
        if region:
            inputs['reading_region_projection'] = region['projection']
        unit_id = 'material-ocr-' + digest(identity)[:24]

        def transcribe(source=source, selection=selection, target=meta['target'], identity=identity,
                       attachment=attachment, inputs=inputs, binding=binding, reference=reference,
                       region=region, parent_identity=parent_identity):
            try:
                data, dimensions = _render(source, selection, target)
            except MaterialContentFailure:
                raise
            except Exception:
                raise MaterialContentFailure('MATERIAL_OCR_IMAGE_REBUILD_FAILED') from None
            _require(hashlib.sha256(data).hexdigest() == attachment['sha256'],
                     'MATERIAL_OCR_RENDERED_SCOPE_HASH_MISMATCH')
            image_path = attachment['path']
            if region:
                rebuilt = derive_reading_region(parent_identity, attachment, selection, crops[attachment['sha256']])
                _require(rebuilt == region, 'MATERIAL_OCR_READING_REGION_CHANGED')
                dimensions = region['dimensions']
                ref = ctx.store_bytes('ocr-reading-region.png', region['image_bytes'], 'image/png')
                image_path = ctx.executor.store.root / ref['path']
            # Use the authenticated original attachment bytes, never a whole page for a crop.
            response = (_recorded(ctx, binding, identity, dimensions, provider, fingerprint, reference)
                if binding is not None else recognize(ctx, image_path=image_path,
                    image_sha256=identity['image_sha256'], identity=identity, options=provider,
                    numeric_reference=reference))
            _verify_file(source)
            _require(file_sha256(Path(attachment['path'])) == attachment['sha256'],
                     'MATERIAL_OCR_IMAGE_CHANGED_DURING_CALL')
            result = {'input_sha256': digest(inputs), 'identity': identity, 'dimensions': dimensions,
                      'response': response, 'quality_status': 'machine_transcription_unverified',
                      'reading_completeness': 'not_established', 'layout_status': 'regions_and_tables_unverified',
                      'qualification_result': 'not_determined', 'automatic_assembly_allowed': False}
            result['output_sha256'] = digest(result)
            return result

        result = ctx.run_unit(unit_id, inputs, transcribe)
        _require(result.get('input_sha256') == digest(inputs) and result.get('identity') == identity and
                 result.get('output_sha256') == digest({k: v for k, v in result.items() if k != 'output_sha256'}),
                 'MATERIAL_OCR_CHECKPOINT_INVALID')
        response = result['response']
        _require(response['image_sha256'] == identity['image_sha256'], 'MATERIAL_OCR_RESPONSE_IMAGE_INVALID')
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
        line_prefix = prefix + '/ocr/'
        if region:
            line_prefix += 'reading-region[' + identity['image_sha256'] + ']/'
            unit['ocr_reading_region_projection'] = copy.deepcopy(region['projection'])
        for number, line in enumerate(response['lines']):
            if line['text'].strip():
                boxes = [word['box'] for word in line.get('words', [])]
                bbox = line.get('bounding_box') or ([min(b[0] for b in boxes), min(b[1] for b in boxes),
                         max(b[0] + b[2] for b in boxes), max(b[1] + b[3] for b in boxes)] if boxes else None)
                unit['blocks'].append({'block_id': unit_id + '-line-' + str(number), 'source_id': source['source_id'],
                                      'kind': 'paragraph', 'origin': 'ocr', 'locator': line_prefix + 'line[' + str(number) + ']',
                                      'text': line['text'], 'rows': [], 'bbox': bbox})
        unit['material_content_ocr_candidate_readable'] = bool(text.strip())
        unit['transcription_status'] = transcription
        numeric = copy.deepcopy(response.get('numeric_validation', {'status': 'pending_source_check'}))
        unit['ocr_numeric_validation'] = numeric
        unit['ocr_tables_unverified'] = copy.deepcopy(response.get('tables', []))
        unit['coverage'].update(status='needs_ocr', origin='mixed' if unit['coverage']['native_text_chars'] else 'ocr',
                                error_code=None, warnings=[*unit['coverage'].get('warnings', []),
                                    'OCR_TEXT_AND_TABLE_RELATIONSHIPS_NOT_HUMAN_VERIFIED'])
        meta.update(transcription_status=transcription, transcription_scope='partial_selected_region' if region else 'selected_crop' if selection.crop_box_normalized else 'selected_image',
                    ocr_char_count=len(text), ocr_text_sha256=hashlib.sha256(text.encode('utf-8')).hexdigest(),
                    ocr_unit_id=unit_id, ocr_input_sha256=digest(inputs), ocr_output_sha256=result['output_sha256'],
                    ocr_image_sha256=identity['image_sha256'], coordinate_space='reading_region_pixels' if region else 'selected_image_pixels',
                    ocr_call_id=response['call_id'], ocr_provider='deepseek_official_ocr',
                    ocr_recorded_response=copy.deepcopy(response.get('recorded_response')),
                    numeric_validation=numeric, reading_completeness='not_established',
                    layout_status='regions_and_tables_unverified', status='needs_ocr')
        if region:
            meta['reading_region_projection'] = copy.deepcopy(region['projection'])
        for group in selected_groups:
            for row in group['units']:
                if row['unit_id'] == meta['unit_id']:
                    row.update(copy.deepcopy(meta))
        counters['ocr_processed_unit_count'] += 1
        counters['ocr_text_char_count'] += len(text)
        counters['ocr_transcribed_unverified_count'] += bool(text.strip())
        counters['ocr_no_text_count'] += not text.strip()
        counters['ocr_transcribed_crop_count'] += bool(text.strip() and (selection.crop_box_normalized or region))
        counters['ocr_recorded_response_count'] += response.get('recorded_response') is not None
        counters['ocr_numeric_pending_unit_count'] += numeric.get('status') not in {'no_numeric_tokens', 'no_numeric_content'}
        counters['ocr_numeric_issue_count'] += len(numeric.get('findings', []))
        for finding in numeric.get('findings', []):
            code = finding['code']
            counters['ocr_numeric_issue_codes'][code] = counters['ocr_numeric_issue_codes'].get(code, 0) + 1
    _require(set(bindings) <= seen_images and set(reviews) <= seen_images,
             'MATERIAL_OCR_UNUSED_REPLAY_OR_REVIEW_SCOPE')
    coverage = output['coverage']
    coverage.update(counters)
    coverage['status_counts'] = {status: sum(u['status'] == status for u in output['units'])
                                 for status in ('read', 'empty', 'needs_ocr', 'unsupported')}
    coverage['pending_ocr_unit_count'] = sum(u['status'] not in {'read', 'empty'} and
        u.get('transcription_status') != 'ocr_transcribed_unverified' for u in output['units'])
    coverage['ocr_engine'] = fingerprint
    coverage['fallback_enabled'] = False
    coverage['ocr_processed_count_includes_reused_checkpoints'] = True
    coverage['actual_api_call_count_source'] = 'diagnostics_calls_and_attempts_not_checkpoint_count'
    # Per-attempt events are authoritative for actual calls, not this checkpoint total.
    coverage.pop('ocr_calls', None)
    output['parsed']['coverage'] = copy.deepcopy(coverage)
    output['summary']['deepseek_ocr'] = 'candidate_text_only_no_fact_or_qualification_adoption'
    output['native_index_sha256'] = index['output_sha256']
    output['ocr_index_version'] = MATERIAL_OCR_INDEX_VERSION
    output['output_sha256'] = digest({k: v for k, v in output.items() if k != 'output_sha256'})
    captured = ctx.capture('material-deepseek-ocr-index', output)
    _require(captured and captured.get('status') == 'captured', 'MATERIAL_OCR_INDEX_CAPTURE_UNAVAILABLE')
    return output
