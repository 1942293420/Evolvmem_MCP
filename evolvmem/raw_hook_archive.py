"""Persist hook input independently of any AI result or model availability."""
import json


def archive_hook_messages(*, config, project, adapter, source_session, messages):
    from evolvmem.context_models import ContextMode, parse_context_mode
    from evolvmem.context_service import ContextService
    from evolvmem.conversation import clean_messages
    from evolvmem.session_archive import SessionArchiver
    service = None
    try:
        service = ContextService(config)
        service.initialize(mode=parse_context_mode(config.context_mode) or ContextMode.LEGACY,
                           adapter=adapter)
        payload = json.dumps({'messages': messages, 'conversation': clean_messages(messages)}, ensure_ascii=False)
        record = SessionArchiver(config, service.store).archive_session(project, adapter, source_session, payload)
        return record.id if record else None
    except Exception:
        # No contents, credential configuration or exception message escapes.
        return None
    finally:
        if service is not None:
            service.close()
