"""Shared run caps and local purchase admission for official OCR."""
from decimal import Decimal, InvalidOperation
import json
import re

from .provider import ChatConfig, ProviderFailure
from .runtime import ProviderBudgetLedgerError, call_budget_summary
from .storage import RunLockedError, StorageError, atomic_json, process_lock


def _caps(ocr):
    return Decimal(ocr['max_run_usd']), ocr['max_run_calls']


def validate_pipeline_ocr_budget(config):
    recall = config.get('planning', {}).get('semantic_material_recall', {})
    material_ocr = recall.get('original_ocr')
    tender_ocr = config.get('ocr')
    official_tender = isinstance(tender_ocr, dict) and tender_ocr.get('engine') == 'deepseek_ocr'
    caps = []
    if isinstance(material_ocr, dict) and material_ocr.get('engine') == 'deepseek_ocr':
        from .material_deepseek_ocr import _options
        from .material_ocr_index import _settings

        caps.append(_caps(_options(_settings(material_ocr)[0])))
    if official_tender:
        from .ocr import official_tender_options

        caps.append(_caps(official_tender_options(tender_ocr)))
    if not caps:
        return  # Historical configurations remain inspectable.
    if len(set(caps)) != 1:
        raise ProviderFailure('MATERIAL_OCR_SHARED_RUN_BUDGET_MISMATCH')

    expected = caps[0]
    provider = config.get('provider', {})
    structured = config.get('structured_provider', provider)
    providers = [provider, structured, config.get('mapping_provider') or structured]
    if recall.get('enabled') and recall.get('recorded_source') is None:
        providers.append({'mode': 'chat_completions', **recall.get('provider', {})})
    if tender_ocr and not official_tender:
        providers.append({'mode': 'chat_completions', **tender_ocr.get('provider', provider)})
    if provider.get('mode') == 'diagnostic_writing_commitment_partition':
        providers.append(config.get('commitment_drafting', {}).get('direct_provider', {}))
    for provider in providers:
        if provider.get('mode') not in {'chat_completions', 'diagnostic_mixed', 'diagnostic_writing_mixed'}:
            continue
        try:
            cap = Decimal(str(provider.get('max_run_usd', ChatConfig.max_run_usd)))
            count = provider.get('max_run_calls', ChatConfig.max_run_calls)
            valid = cap.is_finite() and cap > 0 and type(count) is int and count > 0
        except (InvalidOperation, TypeError, ValueError):
            valid = False
        if not valid or (cap, count) != expected:
            raise ProviderFailure('MATERIAL_OCR_SHARED_RUN_BUDGET_MISMATCH')


def _prior_tender_reservation(store, pointer, purchase_key):
    if (not isinstance(pointer, dict) or pointer.get('schema_version') != 'tender.ocr.purchase-intent.v1' or
            pointer.get('purchase_key') != purchase_key or
            not isinstance(pointer.get('call_id'), str) or not re.fullmatch(r'[0-9a-f]{32}', pointer['call_id']) or
            not isinstance(pointer.get('input_hash'), str) or not re.fullmatch(r'[0-9a-f]{64}', pointer['input_hash'])):
        raise ValueError('Invalid purchase intent.')
    state = store.read_state(pointer['run_id'])
    budget = state.get('call_budget', {})
    call_budget_summary(budget)
    call_id = pointer['call_id']
    records = [row for row in budget.get('reservations', []) if row['call_id'] == call_id]
    if not records:
        return False  # A transport request cannot start without the durable reservation.
    if len(records) != 1:
        raise ValueError('Ambiguous purchase reservation.')
    record = records[0]
    if (record['node_key'] != 'N1' or record['call_attempt'] != 1 or record.get('input_hash') != pointer['input_hash'] or
            record.get('input_usd_per_million') is not None or record.get('output_usd_per_million') is not None):
        raise ValueError('Purchase intent conflicts with the durable reservation.')
    captures = [capture for node in state['nodes'] if node['key'] == 'N1'
                for attempt in node['attempts'] if attempt['attempt_id'] == record['node_attempt_id']
                for capture in attempt.get('captures', []) if capture['name'] == 'request-' + call_id]
    if (len(captures) != 1 or captures[0]['status'] != 'captured' or
            any(captures[0].get(key) != record.get(key) for key in ('unit_id', 'unit_attempt_id'))):
        raise ValueError('Purchase reservation request capture is missing or conflicting.')
    request = store.read_json(captures[0]['ref'])
    if (not isinstance(request, dict) or request.get('call_id') != call_id or
            request.get('purchase_key') != purchase_key or request.get('body_sha256') != pointer['input_hash']):
        raise ValueError('Purchase request does not match its intent and reservation.')
    return True


def reserve_tender_ocr(ctx, purchase_key, call_id, config, *, input_hash):
    """Reserve once per physical source scope within the same local storage root.

    The pointer is written first. A crash before ledger admission can leave an
    unreserved intent. A reserved attempt blocks dispatch even if its outcome
    or trace could not be saved. The keyed OS lock covers sibling runs too.
    """
    if (ctx.node_key != 'N1' or not isinstance(purchase_key, str) or not re.fullmatch(r'[0-9a-f]{64}', purchase_key) or
            not isinstance(call_id, str) or not re.fullmatch(r'[0-9a-f]{32}', call_id) or
            not isinstance(input_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', input_hash)):
        raise ProviderFailure('OCR_PURCHASE_IDENTITY_INVALID')
    store = ctx.executor.store
    try:
        pointer_path = store.resolve('ocr-purchases/' + purchase_key + '.json')
        with process_lock(pointer_path.with_suffix('.lock')):
            try:
                prior = json.loads(pointer_path.read_bytes())
            except FileNotFoundError:
                pass
            else:
                if _prior_tender_reservation(store, prior, purchase_key):
                    raise ProviderFailure('OCR_PURCHASE_ALREADY_RESERVED')
            atomic_json(pointer_path, {'schema_version': 'tender.ocr.purchase-intent.v1',
                                       'purchase_key': purchase_key, 'run_id': ctx.run_id,
                                       'call_id': call_id, 'input_hash': input_hash})
            ctx.reserve_call(call_id, 1, config['reserve_usd_per_page'], config['max_run_usd'],
                             config['max_run_calls'], input_hash=input_hash)
    except RunLockedError:
        raise ProviderFailure('OCR_PURCHASE_ADMISSION_BUSY') from None
    except (ProviderBudgetLedgerError, StorageError, OSError, ValueError, TypeError, KeyError):
        raise ProviderFailure('OCR_PURCHASE_LEDGER_UNAVAILABLE') from None
