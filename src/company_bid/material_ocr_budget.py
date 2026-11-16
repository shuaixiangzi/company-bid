"""Reject incompatible frozen run-wide caps before any pipeline call."""
from decimal import Decimal, InvalidOperation

from .provider import ChatConfig, ProviderFailure


def validate_pipeline_ocr_budget(config):
    recall = config.get('planning', {}).get('semantic_material_recall', {})
    ocr = recall.get('original_ocr')
    if not isinstance(ocr, dict) or ocr.get('engine') != 'deepseek_ocr':
        return  # Historical configurations remain inspectable.
    from .material_deepseek_ocr import _options
    from .material_ocr_index import _settings

    normalized = _options(_settings(ocr)[0])
    expected = (Decimal(normalized['max_run_usd']), normalized['max_run_calls'])
    providers = [config.get('provider', {}),
                 config.get('structured_provider', config.get('provider', {})),
                 config.get('mapping_provider') or config.get('structured_provider', config.get('provider', {}))]
    if recall.get('enabled') and recall.get('recorded_source') is None:
        providers.append({'mode': 'chat_completions', **recall.get('provider', {})})
    if config.get('ocr'):
        providers.append({'mode': 'chat_completions',
                          **config['ocr'].get('provider', config.get('provider', {}))})
    if config.get('provider', {}).get('mode') == 'diagnostic_writing_commitment_partition':
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
