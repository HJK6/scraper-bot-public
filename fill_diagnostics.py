"""Content-free diagnostics for every confidential fill, without caller opt-in.

Values remain available to browser operations and comparisons only. Diagnostic
fields are allowlisted; replacing the submitted string cannot protect unrelated,
partial or transformed readbacks, nested exceptions or old-server responses.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import logging

MESSAGES = {
    'bad_request': 'invalid confidential fill request; check target, mode, item_index, verify_scope_css, verify_value_css and verify_text; automatic capture must be disabled',
    'backend_unsupported': 'confidential fill requires a supported input backend',
    'element_not_found': 'fill target was not found',
    'element_not_visible': 'fill target is not visible',
    'out_of_viewport': 'fill target is outside the viewport',
    'covered_by_overlay': 'an overlay blocks the confidential fill',
    'effect_not_observed': 'confidential fill effect was not observed',
    'not_editable': 'fill target is not editable',
    'list_empty': 'selection list did not render',
    'item_not_found': 'selection item was not found',
    'error': 'confidential fill failed',
}
PRIMITIVES = frozenset({'trusted.type', 'legacy.type', 'trusted.select_option',
                       'trusted.select_option.type', 'trusted.select_option.open',
                       'trusted.select_option.wait', 'trusted.select_option.pick',
                       'trusted.select_option.mask'})
MODES = frozenset({'keystroke', 'send_keys', 'insert'})
_confidential = ContextVar('confidential_fill_diagnostics', default=False)


def error_detail(reason='error', primitive='trusted.type'):
    reason = reason if isinstance(reason, str) and reason in MESSAGES else 'error'
    primitive = primitive if isinstance(primitive, str) and primitive in PRIMITIVES else 'trusted.type'
    return {'ok': False, 'reason': reason, 'message': MESSAGES[reason], 'primitive': primitive}


def success_detail(result, primitive):
    """Keep only non-content flags and fixed enums, including for older servers."""
    result = result if isinstance(result, dict) else {}
    out = {'ok': True, 'primitive': error_detail(primitive=primitive)['primitive']}
    for name in ('verified', 'opened'):
        if type(result.get(name)) is bool or (name in result and result[name] is None):
            out[name] = result[name]
    if isinstance(result.get('mode'), str) and result['mode'] in MODES:
        out['mode'] = result['mode']
    if result.get('status') == 'typed':
        out['status'] = 'typed'
    if 'focus_warning' in result:
        out['focus_warning'] = bool(result['focus_warning'])
    return out


@contextmanager
def confidential_logs():
    """Suppress request/readback/exception bodies even when library DEBUG is on."""
    token = _confidential.set(True)
    try:
        yield
    finally:
        _confidential.reset(token)


# Filtering at record creation also covers handlers on child/library loggers;
# a filter on the root logger alone would miss propagated records. Other threads
# and non-fill operations retain the existing logging policy.
_previous_factory = logging.getLogRecordFactory()


def _record_factory(*args, **kwargs):
    record = _previous_factory(*args, **kwargs)
    if _confidential.get():
        record.msg = 'confidential fill diagnostic content omitted'
        record.args = ()
        record.exc_info = record.exc_text = record.stack_info = None
    return record


logging.setLogRecordFactory(_record_factory)
