"""Explicit privileged legacy activation CLI; never starts a Host or model."""
import argparse
import json

from evolvmem.library_foundation import FoundationError,Authenticator
from evolvmem.library_catalog import ControlProvider,ControlCatalog
from evolvmem.library_catalog.adapter import _uuid
from evolvmem.library_deadline.guard import SyncRequestDeadlineGuard
from .coordinator import AuthorityIntent,AuthorityCoordinator,AuthorityOperation,VerifiedAuthorityReceipt,require
from .worker import AuthorityProjector,decode

INTENT_KEYS=set('version action library_id expected_route_epoch expected_policy_epoch connection_ref operation_id request_id'.split())

def intent_from_document(document,identity):
    require(type(document) is dict and set(document)==INTENT_KEYS,'invalid_authority_intent')
    require(type(document['version']) is int and document['version']==1,'invalid_authority_intent')
    return AuthorityIntent(identity,document['library_id'],document['expected_route_epoch'],document['expected_policy_epoch'],
        document['connection_ref'],document['operation_id'],document['request_id'],document['action'])

def parser():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('config','owner-token-file','device-id','intent-file'):p.add_argument('--'+name,required=True)
    p.add_argument('command',choices=('begin','status','project','recover','finalize'))
    return p

def execute(args):
    # Import the existing composition helpers, without calling build_library_host.
    from evolvmem.library_startup import (load_server_configuration,FileSecretResolver,
        read_private_secret,_read_bounded_file)
    _uuid(args.device_id)
    startup=load_server_configuration(args.config);settings=startup.library
    require(settings is not None and settings.acl_control_binding is not None and bool(settings.acl_projection_targets),
        'authority_configuration_required')
    try:
        import psycopg
        require(psycopg.capabilities.has_cancel_safe(check=True),'unsupported_pg_deadline_driver')
    except FoundationError:raise
    except Exception:raise FoundationError('library_driver_unavailable') from None
    guard=SyncRequestDeadlineGuard(settings.timeout_seconds)
    with guard.enforce():
        resolver=FileSecretResolver(settings.secret_files);resolver.validate()
        auth_provider=ControlProvider(settings.control_binding,psycopg.connect,resolver,deadline_guard=guard)
        token=read_private_secret(args.owner_token_file)
        try:identity=Authenticator(ControlCatalog(auth_provider)).authenticate(token,claimed_device_id=args.device_id)
        finally:token=None
        raw,_=_read_bounded_file(args.intent_file,65536,private=True)
        intent=intent_from_document(decode(raw.decode('utf-8')),identity)
        provider=ControlProvider(settings.acl_control_binding,psycopg.connect,resolver,deadline_guard=guard)
        projector=AuthorityProjector(provider,settings.acl_projection_targets,psycopg.connect,resolver,deadline_guard=guard)
        projector.validate_target(intent)
        coordinator=AuthorityCoordinator(provider,projector)
        result=getattr(coordinator,args.command)(intent)
        if isinstance(result,AuthorityOperation):value=dict(state=result.document['operation_state'],operation=dict(result.document))
        elif isinstance(result,VerifiedAuthorityReceipt):value=result.summary
        else:
            require(result is None and args.command=='status','authority_result_invalid');value=dict(state='absent')
        guard.check()
    return dict(value,phase=args.command,automatic_retry=False,production_cutover=False,current_readiness_asserted=False)

def main(argv=None):
    args=parser().parse_args(argv)
    try:
        result=execute(args);print(json.dumps(result,sort_keys=True,ensure_ascii=False,allow_nan=False));return 0
    except BaseException as exc:
        code=exc.code if isinstance(exc,FoundationError) else 'authority_cli_failed'
        print(json.dumps(dict(state='stopped_inspect_before_retry',code=code,automatic_retry=False,
            production_cutover=False,commit_outcome='inspect_durable_operation_and_content_receipt')))
        return 2

if __name__=='__main__':raise SystemExit(main())
