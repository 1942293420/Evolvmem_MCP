"""Pure compatibility decisions, backed by the existing domain models."""
from evolvmem.context_migration import LegacyMemoryMigrator
from evolvmem.legacy_models import LegacyAddRequest, LegacyReplaceRequest
from evolvmem.project_models import ProjectResolutionRequest
from evolvmem.project_resolver import ProjectResolver, _CANONICAL_KEY_PATTERN


class UnsupportedBusiness(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def projection_payload(request, old=None):
    if not isinstance(request, (LegacyAddRequest, LegacyReplaceRequest)):
        raise TypeError('typed legacy write required')
    old = old or {}
    replacement = isinstance(request, LegacyReplaceRequest)
    def inherited(name, default):
        value = getattr(request, name)
        return old.get(name, default) if replacement and value is None else value
    tags = inherited('tags', '')
    if not isinstance(tags, str):
        tags = ','.join(tags)
    expires = inherited('expires_at', None)
    if expires and len(expires) == 10:
        expires += ' 00:00:00'
    return dict(key=request.key, value=request.new_value if replacement else request.value,
                attribute=inherited('attribute', ''), tags=tags,
                source_session=request.source_session, importance=inherited('importance', 5.0),
                tier=inherited('tier', 'normal'), expires_at=expires)


def resolved_draft(config, row, snapshot, request):
    if request.workspace_path:
        # No host filesystem/HMAC identity is inferred from a remote path.
        raise UnsupportedBusiness('unsupported_workspace_resolution')
    content_type = LegacyMemoryMigrator.content_type_for(row)
    scope = LegacyMemoryMigrator.scope_for(content_type)
    canonical = _CANONICAL_KEY_PATTERN.match(row['key'].strip().casefold())
    decision = ProjectResolver().resolve(ProjectResolutionRequest(
        content_type=content_type.value, scope=scope.value, key=row['key'],
        tags=LegacyMemoryMigrator.tags_for(row.get('tags')),
        source_session=row.get('source_session', ''), source_version='typed-write-v1',
        project_hint=request.project_hint or (canonical.group(1) if canonical else ''),
    ), snapshot)
    # This use invokes only the existing pure draft derivation, never SQLite.
    draft = LegacyMemoryMigrator(None, config).draft_from_projection_row(
        row, confidence=request.confidence, decision=decision)
    return draft, decision


def mutation_response(base, result):
    return dict(base, context_id=result.context_id, old_context_id=result.old_context_id,
                available_layers=[layer.value for layer in result.available_layers],
                index_state='degraded')
