"""Single-line citation repair proofs use synthetic frozen sources only."""
import copy
import hashlib

import pytest

from company_bid.citation_gap_sources import native_citation_gap_restore
from company_bid.extraction import (
    PlanningError, _index, _windows, extraction_window_payload, validate_live_window,
)
from company_bid.storage import digest


def source_data():
    lines = [('CH', '第六章 合同条款'), ('A8', '第八条 协作事项')]
    lines += [(f'R{i}', f'8.{i}甲方应按约定完成第{i}项合成协作。') for i in range(1, 7)]
    lines += [('A9', '第九条 合同变更'), ('P1', '9.1甲方应提交书面申请。'),
              ('P2', '9.2乙方应确认申请内容。'),
              ('G1', '9.3甲方如需变更服务范围，应当先提出书面申请，'),
              ('G2', '未经双方书面同意不得实施变更，且原约定继续执行，'),
              ('G3', '否则乙方应承担由此产生的损失。'),
              ('NEXT', '9.4乙方应保存书面变更记录。'), ('BLANK', '9.5其他：')]
    sha = hashlib.sha256('\n'.join(text for _, text in lines).encode()).hexdigest()
    manifest = {'sources': [{'source_id': 'T', 'sha256': sha}], 'source_roles': {'T': 'tender'}}
    parsed = {'coverage': {'required_complete': True}, 'documents': [{
        'source_id': 'T', 'sha256': sha, 'role': 'tender', 'format': 'pdf', 'expected_units': 1,
        'units': [{'unit_id': 'U1', 'source_id': 'T', 'locator': 'pdf/page[1]', 'page': 1,
                   'coverage': {'status': 'read', 'image_count': 0},
                   'blocks': [{'block_id': key, 'source_id': 'T', 'kind': 'paragraph', 'origin': 'native',
                               'locator': f'pdf/page[1]/line[{i}]', 'text': text}
                              for i, (key, text) in enumerate(lines)]}]}]}
    return parsed, manifest


def request_data(parsed, manifest, config=None):
    config = config or {'window_max_chars': 14000, 'window_max_blocks': 40}
    blocks, _, documents = _index(parsed, manifest)
    windows, _ = _windows(documents, blocks, config)
    window, = [w for w in windows if 'G1' in {b['block_id'] for b in w['blocks']}]
    return window, {**extraction_window_payload(window, config), 'task_type': 'extract_requirements'}


def candidate_data(window):
    def requirement(ids):
        return {'citation': {'block_ids': ids}, 'category': 'commercial', 'response_mode': 'procedure',
                'mandatory': True, 'confidence': 'low', 'format_status': 'not_prescribed',
                'format_title': None, 'required_fact_labels': [], 'attachment_keywords': [], 'reference_keywords': []}
    available = {b['block_id'] for b in [*window['blocks'], *window.get('adjacent_context', [])]}
    return {'window_id': window['window_id'], 'processing_declaration': 'processed_not_semantically_accepted',
            'unresolved_block_ids': ['BLANK'] if 'BLANK' in available else [],
            'format_status': 'none_verified', 'format_chapters': [],
            'requirements': [requirement([f'R{i}']) for i in range(1, 7) if f'R{i}' in available] +
                            [requirement(['G1', 'G3'])]}


def arrange():
    parsed, manifest = source_data()
    window, request = request_data(parsed, manifest)
    return candidate_data(window), request, parsed, manifest


@pytest.mark.parametrize('character', ['\ufffd', '\ue001', '\x01', '\x7f', '\u200b', '\ud800', '\u0378'])
def test_rebuilt_gap_source_never_restores_unknown_unicode(character):
    candidate, old_request, parsed, manifest = arrange()
    row = next(row for row in parsed['documents'][0]['units'][0]['blocks'] if row['block_id'] == 'G2')
    row['text'] += character
    try:
        _, request = request_data(parsed, manifest)
    except UnicodeEncodeError:
        assert native_citation_gap_restore(candidate, old_request, parsed, manifest) is None
        return
    before = copy.deepcopy((candidate, request, parsed, manifest))
    assert native_citation_gap_restore(candidate, request, parsed, manifest) is None
    assert (candidate, request, parsed, manifest) == before


@pytest.mark.parametrize('space', ['\u00a0', '\u3000'])
def test_known_unicode_space_is_preserved_in_rebuilt_gap_source(space):
    candidate, _, parsed, manifest = arrange()
    row = next(row for row in parsed['documents'][0]['units'][0]['blocks'] if row['block_id'] == 'G2')
    row['text'] += space
    _, request = request_data(parsed, manifest)
    result = native_citation_gap_restore(candidate, request, parsed, manifest)
    assert result and result['omitted_members'][0]['text'].endswith(space)


def test_proves_exactly_one_line_and_preserves_every_original_field():
    candidate, request, parsed, manifest = arrange()
    frozen = copy.deepcopy((candidate, request, parsed, manifest))
    proof = native_citation_gap_restore(candidate, request, parsed, manifest)
    assert proof and proof['candidate_index'] == 6
    assert proof['original_block_ids'] == ['G1', 'G3']
    assert proof['restored_block_ids'] == ['G1', 'G2', 'G3']
    assert (candidate, request, parsed, manifest) == frozen
    assert proof['source_proof_sha256'] == digest(proof['source_proof'])
    evidence = proof['source_proof']
    assert evidence['clause_number'] == '9.3'
    assert evidence['source_text_modified'] is evidence['business_fields_modified'] is evidence['candidate_mutation'] is False
    assert evidence['request_sha256'] == digest(request)
    assert evidence['parsed_sha256'] == digest(parsed) and evidence['manifest_sha256'] == digest(manifest)
    omitted, = proof['omitted_members']
    assert omitted['block_id'] == 'G2' and omitted['locator'] == 'pdf/page[1]/line[12]'
    assert omitted['text'].startswith('未经双方书面同意不得')
    assert omitted['text_sha256'] == hashlib.sha256(omitted['text'].encode()).hexdigest()
    assert evidence['contract_ancestor']['block_id'] == 'CH'
    assert evidence['article_heading']['block_id'] == 'A9' and evidence['next_boundary']['block_id'] == 'NEXT'
    restored = copy.deepcopy(candidate)
    restored['requirements'][6]['citation']['block_ids'] = proof['restored_block_ids']
    assert evidence['expected_candidate_sha256'] == digest(restored)
    window, _ = request_data(parsed, manifest)
    original_probe = copy.deepcopy(candidate)
    original_probe['unresolved_block_ids'] = []
    with pytest.raises(PlanningError, match='CITATION_SPAN_NOT_CONTIGUOUS'):
        validate_live_window(original_probe, window)
    with pytest.raises(PlanningError, match='LIVE_REQUIRED_BLOCK_UNRESOLVED'):
        validate_live_window(restored, window)
    # The helper does not clear this independent blank. When separately handled,
    # the original full source validator accepts the exact restored citation.
    restored['unresolved_block_ids'] = []
    validated = validate_live_window(restored, window)
    assert validated['requirements'][6]['citation']['quote'] == '\n'.join(b['text'] for b in evidence['members'])


@pytest.mark.parametrize('field,value', [
    ('category', 'procedure'), ('response_mode', 'factual_response'), ('mandatory', False),
    ('confidence', 'high'), ('format_status', 'prescribed'), ('format_title', '另一个题名'),
    ('required_fact_labels', ['合同金额']), ('attachment_keywords', ['证明']), ('reference_keywords', ['其他']),
])
def test_frozen_expected_candidate_hash_protects_business_fields(field, value):
    candidate, request, parsed, manifest = arrange()
    proof = native_citation_gap_restore(candidate, request, parsed, manifest)
    altered = copy.deepcopy(candidate)
    altered['requirements'][6]['citation']['block_ids'] = proof['restored_block_ids']
    altered['requirements'][6][field] = value
    assert digest(altered) != proof['source_proof']['expected_candidate_sha256']


@pytest.mark.parametrize('change', [
    'none', 'two_missing', 'two_gaps', 'two_items', 'reverse', 'duplicate', 'unknown',
    'clause_selector', 'row_selector', 'quote', 'body', 'bad_type', 'category', 'label',
    'other_item', 'wrong_window', 'bad_unresolved', 'duplicate_unresolved', 'root_unread',
    'item_unread', 'format_inconsistent', 'bad_coordinate',
])
def test_bad_candidate_never_earns_gap_proof(change):
    candidate, request, parsed, manifest = arrange()
    item = candidate['requirements'][6]
    citation = item['citation']
    if change == 'none': citation['block_ids'] = ['G1', 'G2', 'G3']
    elif change == 'two_missing': citation['block_ids'] = ['P2', 'G3']
    elif change == 'two_gaps': citation['block_ids'] = ['P1', 'G1', 'G3']
    elif change == 'two_items': candidate['requirements'][0]['citation']['block_ids'] = ['R1', 'R3']
    elif change == 'reverse': citation['block_ids'] = ['G3', 'G1']
    elif change == 'duplicate': citation['block_ids'] = ['G1', 'G1', 'G3']
    elif change == 'unknown': citation['block_ids'] = ['G1', 'UNKNOWN']
    elif change in {'clause_selector', 'row_selector'}: citation['clause_id' if change == 'clause_selector' else 'table_row_id'] = 'invented'
    elif change == 'quote': citation['quote'] = '伪造原文'
    elif change == 'body': item['text'] = '改写后的条款'
    elif change == 'bad_type': item['mandatory'] = 'true'
    elif change == 'category': item['category'] = 'qualification'
    elif change == 'label': item['attachment_keywords'] = ['未定位证明']
    elif change == 'other_item': candidate['requirements'][0]['category'] = 'technical'
    elif change == 'wrong_window': candidate['window_id'] = 'wrong'
    elif change == 'bad_unresolved': candidate['unresolved_block_ids'] = ['UNKNOWN']
    elif change == 'duplicate_unresolved': candidate['unresolved_block_ids'] *= 2
    elif change == 'root_unread': candidate['format_status'] = 'unread'
    elif change == 'item_unread': item['format_status'] = 'unread'
    elif change == 'format_inconsistent': candidate['format_status'] = 'specified'
    elif change == 'bad_coordinate': citation['source_id'] = 'OTHER'
    assert native_citation_gap_restore(candidate, request, parsed, manifest) is None


@pytest.mark.parametrize('change', [
    'ocr', 'table', 'image_block', 'image_count', 'unknown_images', 'unread', 'glyph', 'error',
    'replacement', 'newline', 'empty_line', 'line_gap', 'duplicate_locator', 'unit_locator',
    'chapter', 'article', 'number_gap', 'middle_heading', 'last_heading', 'short_tail',
    'partial_clause', 'source_end', 'zero_sha', 'unknown_sha', 'different_manifest_sha',
    'incomplete_pages', 'incomplete_global', 'ignored',
])
def test_source_faults_or_incomplete_clause_are_rejected_even_with_rebuilt_request(change):
    candidate, _, parsed, manifest = arrange()
    document = parsed['documents'][0]
    unit = document['units'][0]
    rows = {b['block_id']: b for b in unit['blocks']}
    if change == 'ocr': rows['G2']['origin'] = 'ocr'
    elif change == 'table': rows['G2'].update(kind='table', rows=[['不得遗漏的条件']])
    elif change == 'image_block': rows['G2']['kind'] = 'image'
    elif change == 'image_count': unit['coverage']['image_count'] = 1
    elif change == 'unknown_images': unit['coverage'].pop('image_count')
    elif change == 'unread': unit['coverage']['status'] = 'unread'
    elif change == 'glyph': unit['coverage']['unmapped_glyphs'] = 1
    elif change == 'error': unit['coverage']['error_code'] = 'READ_FAILED'
    elif change == 'replacement': rows['G2']['text'] = '不得\ufffd执行。'
    elif change == 'newline': rows['G2']['text'] += '\n另一行'
    elif change == 'empty_line': rows['G2']['text'] = ' '
    elif change == 'line_gap': rows['G2']['locator'] = 'pdf/page[1]/line[99]'
    elif change == 'duplicate_locator': rows['G2']['locator'] = rows['G1']['locator']
    elif change == 'unit_locator': unit['locator'] = 'pdf/page[2]'
    elif change == 'chapter': rows['CH']['text'] = '第六章 响应文件格式'
    elif change == 'article': rows['A9']['text'] = '第十条 合同变更'
    elif change == 'number_gap': rows['NEXT']['text'] = '9.6乙方应保存记录。'
    elif change == 'middle_heading': rows['G2']['text'] = '9.4甲方应另行确认。'
    elif change == 'last_heading': rows['G3']['text'] = '第十条 争议处理'
    elif change == 'short_tail': rows['G3']['text'] = '否则乙方应承担由此产生的'
    elif change == 'partial_clause': rows['G1']['text'] = '甲方如需变更服务范围，应先提出书面申请，'
    elif change == 'source_end': del unit['blocks'][-2:]
    elif change in {'zero_sha', 'unknown_sha'}:
        manifest['sources'][0]['sha256'] = document['sha256'] = '0' * 64 if change == 'zero_sha' else 'unknown'
    elif change == 'different_manifest_sha': manifest['sources'][0]['sha256'] = 'b' * 64
    elif change == 'incomplete_pages': document['expected_units'] = 2
    elif change == 'incomplete_global': parsed['coverage']['required_complete'] = False
    elif change == 'ignored': unit['coverage']['ignored_objects'] = [{'locator': rows['G2']['locator'], 'reason': 'ignore'}]
    try:
        window, request = request_data(parsed, manifest)
        candidate['window_id'] = window['window_id']
        if change == 'source_end': candidate['unresolved_block_ids'] = []
        proof = native_citation_gap_restore(candidate, request, parsed, manifest)
    except ValueError:
        proof = None
    assert proof is None


@pytest.mark.parametrize('change', ['source_word', 'instructions', 'config', 'extra', 'task', 'id', 'heading_catalog'])
def test_actual_request_must_match_complete_frozen_n1(change):
    candidate, request, parsed, manifest = arrange()
    if change == 'source_word': parsed['documents'][0]['units'][0]['blocks'][0]['text'] += '变更'
    elif change == 'instructions': request['task_instructions'] += '改写指令'
    elif change == 'config': request['window_config']['window_max_blocks'] = 1
    elif change == 'extra': request['unfrozen_extra'] = True
    elif change == 'task': request['task_type'] = 'write_section'
    elif change == 'id': request['window']['window_id'] = 'different'
    elif change == 'heading_catalog': request['window']['source_heading_catalog'] = []
    assert native_citation_gap_restore(candidate, request, parsed, manifest) is None


def test_one_omitted_adjacent_line_is_selectable_but_remote_context_is_not():
    parsed, manifest = source_data()
    config = {'window_max_chars': 14000, 'window_max_blocks': 12}
    window, request = request_data(parsed, manifest, config)
    assert 'G1' in {b['block_id'] for b in window['blocks']}
    assert 'G2' in {b['block_id'] for b in window['adjacent_context']}
    candidate = candidate_data(window)
    proof = native_citation_gap_restore(candidate, request, parsed, manifest)
    assert proof and proof['restored_block_ids'] == ['G1', 'G2', 'G3']


def test_cross_page_clause_is_not_restored():
    _, _, parsed, manifest = arrange()
    document = parsed['documents'][0]
    first = document['units'][0]
    second = copy.deepcopy(first)
    second.update(unit_id='U2', locator='pdf/page[2]', page=2, blocks=first['blocks'][12:])
    first['blocks'] = first['blocks'][:12]
    for i, block in enumerate(second['blocks']):
        block['locator'] = f'pdf/page[2]/line[{i}]'
    document['units'].append(second)
    document['expected_units'] = 2
    window, request = request_data(parsed, manifest)
    assert native_citation_gap_restore(candidate_data(window), request, parsed, manifest) is None


def test_next_article_can_bound_a_complete_final_clause():
    candidate, _, parsed, manifest = arrange()
    rows = parsed['documents'][0]['units'][0]['blocks']
    next(b for b in rows if b['block_id'] == 'NEXT')['text'] = '第十条 争议处理'
    window, request = request_data(parsed, manifest)
    proof = native_citation_gap_restore(candidate, request, parsed, manifest)
    assert proof and proof['source_proof']['next_boundary']['text'] == '第十条 争议处理'


def test_valid_existing_footer_skip_is_preserved_but_a_second_real_gap_is_rejected():
    parsed, manifest = source_data()
    document = parsed['documents'][0]
    first = document['units'][0]
    second = copy.deepcopy(first)
    second.update(unit_id='U2', locator='pdf/page[2]', page=2, blocks=[
        {'block_id': 'C2', 'source_id': 'T', 'kind': 'paragraph', 'origin': 'native',
         'text': '甲方应保存确认记录。'}, *copy.deepcopy(first['blocks'][8:])])
    first['blocks'] = first['blocks'][:8]
    third = copy.deepcopy(first)
    third.update(unit_id='U3', locator='pdf/page[3]', page=3, blocks=[
        {'block_id': 'END', 'source_id': 'T', 'kind': 'paragraph', 'origin': 'native', 'text': '第十条 争议处理'}])
    document.update(units=[first, second, third], expected_units=3)
    for page, unit in enumerate(document['units'], 1):
        unit['blocks'].append({'block_id': f'F{page}', 'source_id': 'T', 'kind': 'paragraph',
            'origin': 'native', 'text': str(page), 'bbox': [250, 700, 260, 710]})
        for index, row in enumerate(unit['blocks']):
            row['locator'] = f'pdf/page[{page}]/line[{index}]'
    window, request = request_data(parsed, manifest)
    assert {row['block_id'] for row in window['safe_skip_blocks']} == {'F1', 'F2', 'F3'}
    candidate = candidate_data(window)
    candidate['requirements'][5]['citation']['block_ids'] = ['R6', 'C2']
    frozen = copy.deepcopy(candidate)
    proof = native_citation_gap_restore(candidate, request, parsed, manifest)
    assert proof and proof['candidate_index'] == 6 and candidate == frozen
    assert candidate['requirements'][5]['citation']['block_ids'] == ['R6', 'C2']
    first['blocks'][-1]['text'] = '不得遗漏的实质条件'
    _, changed_request = request_data(parsed, manifest)
    assert native_citation_gap_restore(candidate, changed_request, parsed, manifest) is None


def test_unknown_source_identity_is_rejected_by_helper_itself():
    candidate, request, parsed, manifest = arrange()
    manifest['sources'][0]['sha256'] = parsed['documents'][0]['sha256'] = 'unknown'
    assert native_citation_gap_restore(candidate, request, parsed, manifest) is None
