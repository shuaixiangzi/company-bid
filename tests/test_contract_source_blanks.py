"""Empty contract templates are pending values, not unread requirements."""
import copy
import hashlib
import json
from pathlib import Path

import pytest
from docx import Document

from company_bid.contract_blank_sources import CONTRACT_BLANK_FIELDS, contract_blank_catalog
from company_bid.diagnostics import inspect_diagnostics
from company_bid.extraction import _index, _windows, extraction_window_payload, validate_live_window, PlanningError
from company_bid.pipeline import build_executor
from company_bid.provider import ChatProposalProvider, PreparedProposalProvider
from company_bid.runtime import Executor, NodeSpec
from company_bid.storage import digest
from company_bid.structured import JsonTaskGateway, _repair_coverage, _covers_rejected_candidate
from test_source_selected_citations import fixture, block
from test_structured_validation_repair import response
from test_targeted_validation_repair import assert_two_distinct_settled_calls


def source_data(tmp_path):
    manifest, parsed = fixture(tmp_path)
    document = parsed['documents'][0]
    document['format'] = 'pdf'
    lines = [('CH', '第六章 合同条款'), ('A1', '第一条 技术服务范围'),
        ('R', '1.1 技术服务内容：按明确范围实施合成检查。'), ('O1', '1.2 其他：'),
        ('A2', '第二条 进度要求及质量要求'), ('S1', '2.1乙方应当按照下列进度要求向甲方提供技术服务：'),
        ('S2', '本技术服务项目的履行期限为： 至 。具体进度要求为：'), ('S3', '。'),
        ('Q1', '2.2乙方将按照下列质量要求向甲方提供技术服务：'), ('Q2', '。'),
        ('O2', '2.3其他：'), ('A3', '第三条 各方权利义务'), ('R3', '3.1甲方应提供已确认的合成资料。')]
    for index, (identity, text) in enumerate(lines):
        row = block(parsed, identity, text, origin='native')
        row['locator'] = f'pdf/page[1]/line[{index}]'
    document['units'][0]['coverage']['image_count'] = 0
    document['expected_units'] = len(document['units'])
    path = Path(manifest['sources'][0]['path'])
    path.write_text('\n'.join(text for _, text in lines), encoding='utf8')
    manifest['sources'][0]['sha256'] = document['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {'manifest': manifest, 'parsed': parsed, 'source_artifacts': [], 'input_kind': 'automatic'}


def window_data(value):
    blocks, _, documents = _index(value['parsed'], value['manifest'])
    config = {'window_max_chars': 14000, 'window_max_blocks': 40}
    windows, _ = _windows(documents, blocks, config)
    assert len(windows) == 1
    return windows[0], {**extraction_window_payload(windows[0], config), 'task_type': 'extract_requirements'}


def candidates(window):
    first = {'window_id': window['window_id'], 'processing_declaration': 'processed_not_semantically_accepted',
        'unresolved_block_ids': ['S1', 'S2', 'S3', 'Q1', 'Q2', 'O1', 'O2'], 'format_status': 'none_verified',
        'requirements': [{'citation': {'block_ids': ['R']}, 'category': 'commercial', 'response_mode': 'procedure',
                          'format_status': 'not_prescribed', 'mandatory': True, 'confidence': 'high'}], 'format_chapters': []}
    second = copy.deepcopy(first)
    second['unresolved_block_ids'] = []
    second['requirements'].extend({'citation': {'block_ids': ids}, **copy.deepcopy(CONTRACT_BLANK_FIELDS)}
                                 for ids in (['O1'], ['S1', 'S2', 'S3'], ['Q1', 'Q2'], ['O2']))
    return first, second


def coverage(value):
    window, request = window_data(value)
    first, second = candidates(window)
    cov = _repair_coverage(first, request, error_code='LIVE_REQUIRED_BLOCK_UNRESOLVED', source_data=value)
    return window, request, first, second, cov


def test_source_proof_keeps_empty_slots_and_original_requirements(tmp_path):
    value = source_data(tmp_path)
    original = copy.deepcopy(value)
    window, task, first, second, cov = coverage(value)
    assert cov and len(cov['resolution_groups']) == 4
    assert [g['source_proof']['blank_kind'] for g in cov['resolution_groups']] == ['other_terms', 'schedule', 'quality', 'other_terms']
    assert all(g['source_proof']['value_status'] == 'blank_in_source' and g['source_proof']['reading_status'] == 'read'
               and g['source_proof_sha256'] == digest(g['source_proof']) for g in cov['resolution_groups'])
    assert _covers_rejected_candidate(second, cov, task)
    validate_live_window(copy.deepcopy(second), window)
    with pytest.raises(PlanningError, match='LIVE_REQUIRED_BLOCK_UNRESOLVED'):
        validate_live_window(copy.deepcopy(first), window)
    assert value == original


@pytest.mark.parametrize('change', ['unread', 'glyph', 'error', 'image', 'unknown_images', 'ocr', 'line_gap',
    'unit_locator', 'chapter', 'article', 'substantive_schedule', 'substantive_quality', 'other_text', 'missing_period',
    'next_number', 'ambiguous_source', 'incomplete_pages'])
def test_real_source_faults_and_nonempty_content_do_not_earn_blank_repair(tmp_path, change):
    value = source_data(tmp_path)
    document = value['parsed']['documents'][0]
    unit = document['units'][0]
    rows = {b['block_id']: b for b in unit['blocks']}
    if change == 'unread': unit['coverage']['status'] = 'unread'
    elif change == 'glyph': unit['coverage']['unmapped_glyphs'] = 1
    elif change == 'error': unit['coverage']['error_code'] = 'READ_FAILED'
    elif change == 'image': unit['coverage']['image_count'] = 1
    elif change == 'unknown_images': unit['coverage'].pop('image_count')
    elif change == 'ocr': rows['S2']['origin'] = 'ocr'
    elif change == 'line_gap': rows['S2']['locator'] = 'pdf/page[1]/line[60]'
    elif change == 'unit_locator': unit['locator'] = 'pdf/page[2]'
    elif change == 'chapter': rows['CH']['text'] = '第六章 响应文件格式'
    elif change == 'article': rows['A2']['text'] = '第四条 其他条款'
    elif change == 'substantive_schedule': rows['S2']['text'] = '本技术服务项目的履行期限为：一日至十日。具体进度要求为：'
    elif change == 'substantive_quality': rows['Q2']['text'] = '按合格标准执行。'
    elif change == 'other_text': rows['O1']['text'] = '1.2 其他：另有实质限制。'
    elif change == 'missing_period': unit['blocks'].remove(rows['S3'])
    elif change == 'next_number': rows['Q1']['text'] = rows['Q1']['text'].replace('2.2', '2.4')
    elif change == 'ambiguous_source': document['sha256'] = '0' * 64
    elif change == 'incomplete_pages': document['expected_units'] = 2
    try:
        *_, cov = coverage(value)
    except PlanningError:
        cov = None
    assert cov is None


@pytest.mark.parametrize('change', ['drop_old', 'change_old', 'clear_only', 'omit_group', 'swap', 'procedure',
    'writing', 'label', 'quote_subset', 'extra_requirement', 'source_proof', 'format'])
def test_correction_cannot_clear_or_guess_values(tmp_path, change):
    window, task, first, second, cov = coverage(source_data(tmp_path))
    assert cov
    if change == 'drop_old': second['requirements'].pop(0)
    elif change == 'change_old': second['requirements'][0]['mandatory'] = False
    elif change == 'clear_only': second['requirements'] = first['requirements']
    elif change == 'omit_group': second['requirements'].pop()
    elif change == 'swap': second['requirements'][1:3] = second['requirements'][2:0:-1]
    elif change in {'procedure', 'writing'}: second['requirements'][2]['response_mode'] = change
    elif change == 'label': second['requirements'][2]['required_fact_labels'] = ['履行期限']
    elif change == 'quote_subset': second['requirements'][2]['citation']['block_ids'] = ['S1']
    elif change == 'extra_requirement': second['requirements'].append(copy.deepcopy(second['requirements'][-1]))
    elif change == 'source_proof': cov['resolution_groups'][0]['source_proof']['value_status'] = 'filled'
    elif change == 'format': second['format_status'] = 'specified'
    assert not _covers_rejected_candidate(second, cov, task)


def setup_runtime(tmp_path, monkeypatch, *, bad_second=False, capture=True, damage_n1=False):
    value = source_data(tmp_path)
    sent, holder = [], {}
    options = {'mode': 'chat_completions', 'endpoint': 'https://example.invalid/chat', 'model': 'synthetic',
        'auth_env': 'TEST_CONTRACT_BLANK_KEY', 'max_attempts': 1, 'validation_repair_attempts': 1,
        'max_output_tokens': 512, 'max_input_chars': 100000, 'max_run_calls': 3, 'max_run_usd': 1,
        'input_usd_per_million': .30, 'output_usd_per_million': 1.20}
    monkeypatch.setenv(options['auth_env'], 'synthetic-credential')
    def transport(endpoint, wire, secret, timeout):
        sent.append(copy.deepcopy(wire))
        task = json.loads(wire['messages'][-1]['content'])
        first, second = candidates(task['window'])
        if bad_second: second['requirements'][2]['response_mode'] = 'procedure'
        if damage_n1 and len(sent) == 1:
            s = holder['executor'].inspect(holder['run'])
            ref = s['nodes'][1]['output_ref']
            (holder['executor'].store.root / ref['path']).write_text('{}', encoding='utf8')
        return response(first if len(sent) == 1 else second)
    gateway = JsonTaskGateway(options, transport=transport)
    # Preserve the real gateway before replacing its entry for production N2.
    original_call = JsonTaskGateway.__call__
    def dispatch(self, ctx, task_type, unit_id, payload, validate):
        assert task_type == 'extract_requirements'
        return original_call(gateway, ctx, task_type, unit_id, payload, validate)
    monkeypatch.setattr(JsonTaskGateway, '__call__', dispatch)
    def no_model(*args, **kwargs): pytest.fail('A source blank cannot purchase writing or guessed fact mapping')
    monkeypatch.setattr(ChatProposalProvider, 'generate', no_model)
    monkeypatch.setattr(PreparedProposalProvider, 'generate', no_model)
    config = {'pipeline': 'automatic', 'structured_provider': options, 'debug_capture': capture,
              'extraction': {'window_max_chars': 14000, 'window_max_blocks': 40},
              'planning': {'map_table_fields': False}, 'provider': {'mode': 'prepared'}}
    root = tmp_path / 'runtime'
    production = build_executor(root, config)
    def freeze(ctx, data):
        data = copy.deepcopy(data)
        source = data['manifest']['sources'][0]
        ref = ctx.store_bytes('synthetic-native-source.txt', Path(source['path']).read_bytes())
        source['path'] = str(ctx.executor.store.root / ref['path'])
        data['source_artifacts'] = [ref]
        return data
    executor = Executor(root, [NodeSpec('N0', 'synthetic-freeze', freeze, ()),
        NodeSpec('N1', 'synthetic-native-read', lambda ctx, data: data, ()), *production.nodes[2:]], config)
    run = executor.create_run(value)
    holder.update(executor=executor, run=run)
    return executor, run, sent


def test_production_n2_n6_correction_keeps_pending_word_and_trace(tmp_path, monkeypatch):
    executor, run, sent = setup_runtime(tmp_path, monkeypatch)
    paused = executor.execute(run, stop_after='N2')
    assert paused['status'] == 'paused_debug', paused.get('error_code')
    assert len(sent) == 2
    node = paused['nodes'][2]
    relation, = [executor.store.read_json(c['ref']) for a in node['attempts'] for c in a['captures']
                 if c['name'].startswith('validation-repair-result-')]
    assert relation['schema_version'] == 'validation-repair-v10'
    assert_two_distinct_settled_calls(paused, relation)
    n2_ref = node['output_ref']
    n2 = copy.deepcopy(executor.output(run, 'N2'))
    done = executor.resume(run, stop_after='N6')
    assert done['status'] == 'succeeded', done.get('error_code')
    assert len(sent) == 2 and done['nodes'][2]['output_ref'] == n2_ref and executor.output(run, 'N2') == n2
    prepared = executor.output(run, 'N3')['prepared']
    issues = [i for i in prepared['issues'] if '合同第' in i['title']]
    assert len(issues) == 4 and all('已读原文未给出' in i['reason'] and '物理留白未判定' in i['reason'] for i in issues)
    assert all(not s['writing_tasks'] and not s['fact_refs'] for s in prepared['sections'])
    n5 = executor.output(run, 'N5')
    pending_ids = {rid for i in issues for rid in i['requirement_refs']}
    assert all(r['status'] == 'pending_manual' for r in n5['validation']['requirement_coverage'] if r['requirement_id'] in pending_ids)
    assert n5['validation']['writing_units'] == []
    files = executor.output(run)['files']
    texts = ['\n'.join(p.text for p in Document(executor.store.root / files[k]['path']).paragraphs)
             for k in ('bid_docx', 'issues_docx')]
    for issue in issues:
        assert all(issue['issue_id'] in text for text in texts)
    assert '合同待约定原文（已读文本未给值）' in texts[0] and '期限为： 至 。' in texts[0]
    diagnostic = inspect_diagnostics(executor.store.root, run)
    assert diagnostic['actual_call_attempts'] == 2 and diagnostic['gaps'] == []


@pytest.mark.parametrize('options', [{'bad_second': True}, {'capture': False}, {'damage_n1': True}])
def test_failed_correction_or_missing_diagnostics_never_buys_third_candidate(tmp_path, monkeypatch, options):
    executor, run, sent = setup_runtime(tmp_path, monkeypatch, **options)
    result = executor.execute(run, stop_after='N2')
    assert result['status'] == 'failed'
    assert len(sent) == (2 if options.get('bad_second') else 1)
    if options.get('bad_second'):
        executor.resume(run, stop_after='N2')
        assert len(sent) == 2


@pytest.mark.parametrize('change', ['none', 'container', 'reading_fault'])
def test_v10_history_replay_is_free_and_rechecks_current_reading(tmp_path, monkeypatch, change):
    from test_diagnostic_replay import target_run, snapshot
    original_call = JsonTaskGateway.__call__
    executor, run, sent = setup_runtime(tmp_path, monkeypatch)
    assert executor.execute(run, stop_after='N2')['status'] == 'paused_debug'
    monkeypatch.setattr(JsonTaskGateway, '__call__', original_call)
    value = executor.output(run, 'N1')
    window, _ = window_data(value)
    state = executor.inspect(run)
    source = {'executor': executor, 'run': run, 'value': value, 'unit_id': window['window_id'], 'tmp_path': tmp_path,
              'options': state['execution_config']['structured_provider'], 'extraction': state['execution_config']['extraction']}
    before = snapshot(executor.store.root)
    def mutate(value):
        if change == 'container': value['parsed']['container_note'] = 'new read container, same source'
        elif change == 'reading_fault': value['parsed']['documents'][0]['units'][0]['coverage']['image_count'] = 1
    target, target_id, done, new_sent, _ = target_run(source, value_change=mutate,
        validator=lambda raw: validate_live_window(raw, window))
    assert new_sent == [] and 'call_budget' not in done
    assert done['status'] == ('failed' if change == 'reading_fault' else 'succeeded'), done.get('error_code')
    if change == 'reading_fault': assert done['error_code'] == 'DIAGNOSTIC_REPLAY_SOURCE_CONTEXT_MISMATCH'
    assert snapshot(executor.store.root) == before
    assert len(sent) == 2 and inspect_diagnostics(target.store.root, target_id)['actual_call_attempts'] == 0
