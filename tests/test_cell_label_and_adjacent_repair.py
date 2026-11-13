"""A single exact native-cell LF label restore plus an existing adjacent group.

The source LF is preserved. No cell geometry, line-join permission or general
multi-error correction is inferred. HTTP replies are synthetic and offline.
"""
import copy
import hashlib
import json

import pytest

from company_bid.diagnostics import inspect_diagnostics
from company_bid.extraction import (
    _index, _model_window, _windows, extraction_window_payload,
    source_selected_output_contract, validate_live_window,
)
from company_bid.runtime import Executor
from company_bid.storage import digest
from company_bid.structured import _repair_coverage
from test_repair_semantic_inventory import run_repair
from test_source_selected_citations import block, fixture, live_candidate
from test_structured_validation_repair import artifacts, response, setup_run
from test_targeted_validation_repair import native_line, assert_two_distinct_settled_calls


def combined_case(tmp_path, *, cell="须提供营业执照、信誉资\n料和审计报告。", extra_cells=()):
    manifest, parsed = fixture(tmp_path)
    parsed['documents'][0]['format'] = 'pdf'
    table = block(parsed, 'TABLE', '', kind='table', origin='native',
                  rows=[['事项', '要求'], ['材料', cell], *[['补充', value] for value in extra_cells]],
                  merges=[], column_widths=[1, 3], header_rows=[0])
    table['locator'] = 'pdf/page[1]/table[0]'
    native_line(parsed, 'FEE', '响应人自行承担服务费用，不得另行增加。')
    native_line(parsed, 'HEAD', '附表2：详细评审表')
    native_line(parsed, 'NEXT', '价格部分详细评分表')
    native_line(parsed, 'AFTER', '该后续原始表格仍需在所属窗口独立处理。')
    blocks, _, documents = _index(parsed, manifest)
    window = _windows(documents, blocks, {'window_max_blocks': 3})[0][0]
    assert {block['block_id'] for block in window['blocks']} == {'TABLE', 'FEE', 'HEAD'}
    assert 'NEXT' in {block['block_id'] for block in window['adjacent_context']}
    first = live_candidate({'window': window}, {'block_ids': ['TABLE']})
    first['requirements'] = [
        {'citation': {'block_ids': ['TABLE']}, 'category': 'commercial', 'response_mode': 'factual_response',
         'format_status': 'not_prescribed', 'attachment_keywords': ['营业执照', '信誉资料', '审计报告']},
        {'citation': {'block_ids': ['FEE']}, 'category': 'commercial', 'response_mode': 'procedure',
         'format_status': 'not_prescribed'},
    ]
    first['unresolved_block_ids'] = ['HEAD', 'NEXT']
    second = copy.deepcopy(first)
    second['requirements'][0]['attachment_keywords'][1] = '信誉资\n料'
    second['unresolved_block_ids'] = []
    second['requirements'].append({'citation': {'block_ids': ['HEAD', 'NEXT']},
        'category': 'procedure', 'response_mode': 'procedure', 'format_status': 'not_prescribed',
        'mandatory': False, 'confidence': 'low'})
    return window, first, second


def test_combined_repair_has_one_exact_original_label_and_one_complete_appended_group(tmp_path, monkeypatch):
    window, first, second = combined_case(tmp_path)
    request = extraction_window_payload(window, {})
    assert request['producer_version'] == 'requirements-window-v21'
    frozen = copy.deepcopy((first, second, request))
    state, validated, relation, feedback = run_repair(tmp_path, monkeypatch, window, first, second)
    assert state['status'] == 'succeeded' and validated == [first, second]
    assert relation['schema_version'] == 'validation-repair-v7' and relation['status'] == 'completed'
    assert relation['first_candidate_hash'] == digest(first) and relation['second_candidate_hash'] == digest(second)
    assert_two_distinct_settled_calls(state, relation)
    coverage = feedback['required_coverage']
    edit = coverage['exact_cell_label_restore']
    assert (edit['candidate_index'], edit['field'], edit['label_index']) == (0, 'attachment_keywords', 1)
    assert edit['required_replacement'] == '信誉资\n料'
    proof = edit['source_proof']
    assert proof['physical_line_join_proven'] is False and proof['source_text_modified'] is False
    assert proof['source_sha256'] == window['source_sha256'] and proof['block_id'] == 'TABLE'
    assert proof['row'] == proof['column'] == 1
    table = next(block for block in window['blocks'] if block['block_id'] == 'TABLE')
    cell = table['rows'][1][1]
    assert cell[proof['cell_start']:proof['cell_end']] == edit['required_replacement']
    assert proof['cell_sha256'] == hashlib.sha256(cell.encode()).hexdigest()
    assert edit['source_proof_sha256'] == digest(proof)
    editable = coverage['entries']['requirements'][0]['editable_label']
    assert editable['prefix'] == ['营业执照'] and editable['suffix'] == ['审计报告']
    assert editable['required_replacement'] == edit['required_replacement']
    assert editable['source_proof_sha256'] == edit['source_proof_sha256']
    group, = coverage['resolution_groups']
    assert group['block_ids'] == ['HEAD', 'NEXT']
    final = validate_live_window(second, window)
    labels = final['requirements'][0]['citation']['label_grounding']
    assert next(label for label in labels if label['label'] == '信誉资\n料')['basis'] == 'exact_quote'
    assert final['requirements'][-1]['citation']['quote'] == '附表2：详细评审表\n价格部分详细评分表'
    assert (first, second, request) == frozen
    assert '不是物理断词证明' in feedback['instructions']


@pytest.mark.parametrize('damage', [
    'delete_label', 'keep_joined_label', 'different_exact_label', 'other_LF_position', 'change_prefix', 'change_suffix',
    'swap_labels', 'pad_label', 'drop_label_array', 'change_other_business', 'change_old_default', 'change_old_source',
    'drop_old_item', 'swap_old_items', 'insert_before_old', 'only_fix_label', 'only_clear_unresolved',
    'only_resolve_adjacent', 'omit_target', 'omit_adjacent', 'unknown_source', 'duplicate_append', 'keep_unresolved',
])
def test_combined_repair_cannot_trade_labels_or_old_business_for_unresolved_removal(tmp_path, monkeypatch, damage):
    window, first, second = combined_case(tmp_path)
    labels = second['requirements'][0]['attachment_keywords']
    if damage == 'delete_label':
        labels.pop(1)
    elif damage == 'keep_joined_label':
        labels[1] = '信誉资料'
    elif damage == 'different_exact_label':
        labels[1] = '营业执照'
    elif damage == 'other_LF_position':
        labels[1] = '信誉\n资料'
    elif damage == 'change_prefix':
        labels[0] = '审计报告'
    elif damage == 'change_suffix':
        labels[-1] = '营业执照'
    elif damage == 'swap_labels':
        labels.reverse()
    elif damage == 'pad_label':
        labels.append('营业执照')
    elif damage == 'drop_label_array':
        second['requirements'][0].pop('attachment_keywords')
    elif damage == 'change_other_business':
        second['requirements'][1]['category'] = 'procedure'
    elif damage == 'change_old_default':
        second['requirements'][0]['mandatory'] = False
    elif damage == 'change_old_source':
        second['requirements'][1]['citation']['block_ids'] = ['TABLE']
    elif damage == 'drop_old_item':
        second['requirements'].pop(1)
    elif damage == 'swap_old_items':
        second['requirements'][:2] = reversed(second['requirements'][:2])
    elif damage == 'insert_before_old':
        second['requirements'].insert(0, second['requirements'].pop())
    elif damage == 'only_fix_label':
        second['requirements'].pop()
    elif damage == 'only_clear_unresolved':
        second['requirements'].pop()
        labels[1] = '信誉资料'
    elif damage == 'only_resolve_adjacent':
        labels[1] = '信誉资料'
    elif damage in {'omit_target', 'omit_adjacent', 'unknown_source'}:
        second['requirements'][-1]['citation']['block_ids'] = {
            'omit_target': ['NEXT'], 'omit_adjacent': ['HEAD'], 'unknown_source': ['HEAD', 'NEXT', 'UNKNOWN']}[damage]
    elif damage == 'duplicate_append':
        second['requirements'].append(copy.deepcopy(second['requirements'][-1]))
    else:
        second['unresolved_block_ids'] = list(first['unresolved_block_ids'])
    state, _, relation, _ = run_repair(tmp_path, monkeypatch, window, first, second)
    assert state['status'] == 'failed' and state['error_code'] == 'VALIDATION_REPAIR_COVERAGE_LOST'
    assert relation['status'] == 'failed'
    assert_two_distinct_settled_calls(state, relation)


@pytest.mark.parametrize('variant', [
    'space_before_LF', 'space_after_LF', 'CRLF', 'double_LF', 'literal_backslash_n', 'punctuation',
    'across_cells', 'across_rows', 'duplicate_same_cell', 'duplicate_other_cell', 'duplicate_other_split',
    'ocr', 'missing_origin', 'docx', 'missing_hash', 'wrong_source', 'unread_cell',
    'second_label_error', 'second_same_label_error', 'second_bad_citation', 'second_bad_title', 'second_illegal_route',
    'other_label_field', 'unknown_label', 'no_adjacent_group', 'root_unread', 'existing_format_unread',
])
def test_combined_admission_rejects_ambiguity_other_whitespace_and_every_second_error(tmp_path, variant):
    cells = {
        'space_before_LF': '须提供营业执照、信誉资 \n料和审计报告。',
        'space_after_LF': '须提供营业执照、信誉资\n 料和审计报告。',
        'CRLF': '须提供营业执照、信誉资\r\n料和审计报告。',
        'double_LF': '须提供营业执照、信誉资\n\n料和审计报告。',
        'literal_backslash_n': '须提供营业执照、信誉资\\n料和审计报告。',
        'punctuation': '须提供营业执照、信誉资。\n料和审计报告。',
        'across_cells': '须提供营业执照、信誉资', 'across_rows': '须提供营业执照、信誉资',
        'duplicate_same_cell': '须提供营业执照、信誉资\n料和审计报告；另有信誉资\n料。',
    }
    extra = {'duplicate_other_cell': ('信誉资\n料',), 'duplicate_other_split': ('信誉\n资料',),
             'across_rows': ('料和审计报告',)}.get(variant, ())
    window, first, _ = combined_case(tmp_path, cell=cells.get(variant, '须提供营业执照、信誉资\n料和审计报告。'), extra_cells=extra)
    table = next(block for block in window['blocks'] if block['block_id'] == 'TABLE')
    if variant == 'across_cells':
        table['rows'][1].append('料和审计报告')
        table['text'] = '\n'.join('\t'.join(row) for row in table['rows'])
    elif variant in {'ocr', 'missing_origin'}:
        table['origin'] = 'ocr' if variant == 'ocr' else None
    elif variant == 'docx':
        window['source_format'] = 'docx'
    elif variant == 'missing_hash':
        window.pop('source_sha256')
    elif variant == 'wrong_source':
        table['source_id'] = 'FOREIGN'
    elif variant == 'unread_cell':
        table['read_disposition'] = {'status': 'unread'}
    elif variant == 'second_label_error':
        first['requirements'][0]['attachment_keywords'].append('另一不存在的材料')
    elif variant == 'second_same_label_error':
        first['requirements'][0]['attachment_keywords'].append('信誉资料')
    elif variant == 'second_bad_citation':
        first['requirements'][1]['citation']['block_ids'] = ['UNKNOWN']
    elif variant == 'second_bad_title':
        first['requirements'][1]['format_title'] = '不存在的原题'
    elif variant == 'second_illegal_route':
        first['requirements'][1]['category'] = 'technical'
    elif variant == 'other_label_field':
        first['requirements'][0]['required_fact_labels'] = ['信誉资料']
    elif variant == 'unknown_label':
        first['requirements'][0]['attachment_keywords'][1] = '不存在的资料'
    elif variant == 'no_adjacent_group':
        first['unresolved_block_ids'] = ['HEAD']
    elif variant == 'root_unread':
        first['format_status'] = 'unread'
    elif variant == 'existing_format_unread':
        first['requirements'][1]['format_status'] = 'unread'
    request = {'window': _model_window(window)}
    assert _repair_coverage(first, request, error_code='LIVE_REQUIRED_BLOCK_UNRESOLVED') is None


def test_combined_label_route_does_not_expand_prior_source_witness_contract(tmp_path):
    window, first, _ = combined_case(tmp_path)
    witness_group = {'block_ids': ['HEAD', 'NEXT'], 'required_target_ids': ['HEAD'],
        'basis': 'verified_prior_window_source_span_minimum_preservation_not_semantic_boundary', 'source_witnesses': []}
    assert _repair_coverage(first, {'window': _model_window(window)}, error_code='LIVE_REQUIRED_BLOCK_UNRESOLVED',
                           source_witnesses=[witness_group]) is None


@pytest.mark.parametrize('damage', ['second_label_error', 'ocr', 'duplicate_cell'])
def test_unapproved_combination_cannot_schedule_an_extra_request(tmp_path, monkeypatch, damage):
    extra = ('信誉资\n料',) if damage == 'duplicate_cell' else ()
    window, first, second = combined_case(tmp_path, extra_cells=extra)
    if damage == 'second_label_error':
        first['requirements'][0]['attachment_keywords'].append('不存在的材料')
    elif damage == 'ocr':
        next(block for block in window['blocks'] if block['block_id'] == 'TABLE')['origin'] = 'ocr'
    state, validated, relation, feedback = run_repair(tmp_path, monkeypatch, window, first, second, expected_calls=1)
    assert state['status'] == 'failed' and state['error_code'] == 'LIVE_REQUIRED_BLOCK_UNRESOLVED'
    assert validated == [first] and relation is feedback is None


def test_failed_combined_repair_keeps_sources_trace_and_exhausts_once_after_restart(tmp_path, monkeypatch):
    window, first, second = combined_case(tmp_path)
    second['requirements'][0]['attachment_keywords'].pop(1)
    executor, rid, _, payload, _, sent, _, _ = setup_run(
        tmp_path, monkeypatch, max_input_chars=50000, validate=lambda value, _: validate_live_window(value, window),
        transport=lambda _, count: response(first if count == 1 else second))
    payload.update(window=_model_window(window), output_contract=source_selected_output_contract())
    initial = executor.execute(rid)
    assert initial['error_code'] == 'VALIDATION_REPAIR_COVERAGE_LOST' and len(sent) == 2
    sealed = [(record['ref'], record['hash']) for record, _ in artifacts(executor, initial)]
    budget = copy.deepcopy(initial['call_budget']['reservations'])
    state = Executor(executor.store.root, executor.nodes, executor.execution_config).resume(rid)
    assert state['error_code'] == 'VALIDATION_REPAIR_ALREADY_ATTEMPTED' and len(sent) == 2
    assert state['call_budget']['reservations'] == budget
    assert all(hashlib.sha256((executor.store.root / ref['path']).read_bytes()).hexdigest() == sha for ref, sha in sealed)
    report = inspect_diagnostics(tmp_path, rid)
    assert report['actual_call_attempts'] == 2 and report['unknown_usage_attempts'] == 0 and not report['gaps']
