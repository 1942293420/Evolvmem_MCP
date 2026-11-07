"""Trusted console construction and LAN enabled-account gate.

No Web server import, source database access, directory creation, credentials,
bootstrap, or cached account state. Call the LAN gate only after bearer auth.
"""
import os
from pathlib import Path

from evolvmem.knowledge_console import ConsoleService
from evolvmem.product_policy import load_policy


def configured_console(*, owner_data_dir=None, lan_data_dir=None, history_environ=None):
    """Build from trusted server configuration; deliberately no installation."""
    registry=Path(os.environ.get('EVOLVMEM_CONSOLE_REGISTRY') or
                  Path.home()/'.config/evolvmem-console/users.db').expanduser()
    owner=os.environ.get('EVOLVMEM_CONSOLE_OWNER_ID','jiangli')
    lan=Path(os.environ.get('EVOLVMEM_LAN_DATA_DIR') or lan_data_dir or
             Path.home()/'.local/share/evolvmem-lan').expanduser()
    personal=Path(owner_data_dir or Path.home()/'.claude/evolvmem').expanduser()
    return ConsoleService(registry,owner,personal,lan,history_environ or {})


def console_user_gate(user_id, settings):
    """None permits; otherwise return bounded HTTP status and error payload.

    Input user_id must be the existing token authenticator's result, never a
    request-body, namespace or expected-user header value. Existing identities
    remain fixed; this gate does not provision additional LAN credentials.
    """
    try:
        policy=load_policy()
        if not policy.valid:
            return 503,{'error':'product_policy_unavailable'}
        if not policy.console_enabled:
            return None
        service=configured_console(owner_data_dir=settings.owner_data_dir,
                                   lan_data_dir=settings.data_dir)
        if not service.registry_ready():
            return 503,{'error':'console_registry_unavailable'}
        if not service.user_enabled(user_id):
            return 403,{'error':'account_disabled'}
        return None
    except Exception:
        return 503,{'error':'console_registry_unavailable'}
