"""Bounded, ordered assembly of historical source rows.

The fixed SQL metadata call returns at most the first 64 sources for each item
and a real-snapshot sentinel.  Only items whose sentinel says ``more`` are
followed with the source-page function.  Nothing is published by this module;
the caller retains the completed payload and only then replaces its row.
"""
from copy import deepcopy

from evolvmem.library_foundation import FoundationError

SOURCE_PAGE_SIZE = 64
MAX_SOURCES_PER_ITEM = 65536


def _fail(code):
    raise FoundationError(code)


def _source_id(source, item_id, library_id):
    if not isinstance(source, dict):
        _fail('invalid_qualification_shape')
    ident = source.get('id')
    if type(ident) is not int or ident < 1:
        _fail('invalid_qualification_shape')
    if source.get('item_id') != item_id or source.get('library_id') != library_id:
        _fail('qualification_scope_mismatch')
    proofs = source.get('event_proofs')
    if not isinstance(proofs, list) or len(proofs) > SOURCE_PAGE_SIZE:
        _fail('recall_dependency_limit')
    if any(not isinstance(proof, dict) for proof in proofs):
        _fail('invalid_qualification_shape')
    return ident


def validate_first_page(payload, item_id, library_id, source_after, source_more):
    """Validate the metadata cursor and return the ordered first-page sources."""
    sources = payload.get('sources')
    if not isinstance(sources, list) or len(sources) > SOURCE_PAGE_SIZE:
        _fail('invalid_qualification_shape')
    previous = None
    for source in sources:
        ident = _source_id(source, item_id, library_id)
        if previous is not None and ident <= previous:
            _fail('invalid_history_source_cursor')
        previous = ident
    if not sources:
        if source_after is not None or source_more is not False:
            _fail('invalid_history_source_cursor')
    else:
        if type(source_after) is not int or source_after != previous:
            _fail('invalid_history_source_cursor')
        if type(source_more) is not bool:
            _fail('invalid_history_source_cursor')
        # Match validate_page: a short non-empty page cannot claim more rows.
        if source_more and len(sources) != SOURCE_PAGE_SIZE:
            _fail('invalid_history_source_page')
    return sources


def validate_page(records, after, item_id, library_id):
    """Validate one SQL source page and return ``(sources, has_more)``."""
    if not isinstance(records, (list, tuple)) or not 1 <= len(records) <= SOURCE_PAGE_SIZE:
        _fail('invalid_history_source_page')
    sources = []
    markers = []
    previous = after
    for record in records:
        if not isinstance(record, (tuple, list)) or len(record) != 3:
            _fail('invalid_history_source_page')
        source_id, source, has_more = record
        if type(source_id) is not int or type(has_more) is not bool:
            _fail('invalid_history_source_page')
        ident = _source_id(source, item_id, library_id)
        if ident != source_id or ident <= previous:
            _fail('invalid_history_source_cursor')
        previous = ident
        sources.append(source)
        markers.append(has_more)
    if len(set(markers)) != 1:
        _fail('invalid_history_source_page')
    has_more = markers[0]
    # A non-sentinel page must contain the full 64 rows.  A short page that
    # claims more is an impossible sentinel and must never advance the cursor.
    if has_more and len(sources) != SOURCE_PAGE_SIZE:
        _fail('invalid_history_source_page')
    return sources, has_more


def assemble_sources(payload, item_id, library_id, source_after, source_more, fetch_page):
    """Append every page before returning the complete original payload.

    ``fetch_page(after)`` is expected to execute the fixed page SQL in the same
    read snapshot and return its raw rows.  It is deliberately not called when
    the metadata sentinel says there is no continuation.
    """
    first = validate_first_page(payload, item_id, library_id, source_after, source_more)
    if not source_more:
        return deepcopy(payload)
    sources = list(first)
    cursor = source_after
    while True:
        page, more = validate_page(fetch_page(cursor), cursor, item_id, library_id)
        if len(sources) + len(page) > MAX_SOURCES_PER_ITEM:
            _fail('recall_dependency_limit')
        sources.extend(page)
        cursor = page[-1]['id']
        if not more:
            break
    full = deepcopy(payload)
    full['sources'] = sources
    return full
