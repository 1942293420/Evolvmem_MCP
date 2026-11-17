"""Whole-table catch-up planning; only authenticated keys/hashes cross phases."""
from contextlib import ExitStack
import hashlib
import hmac
import json
import os
from pathlib import Path
import time
import uuid
from . import catchup_catalog as C
from . import catchup_plan as B
from . import resume_planning as P
from . import catchup_source as S
from . import static_wal as W

CAP=64*1024**2

LEGACY={'epoch_id': '7c5b591d-148f-498e-ae6b-e88a694936b2', 'draft_sha256': '9ab27c70c078efddc283801df7d492c8651b3864d215720b5d27d6d4321cb40f', 'state_sha256': '3a108af25391314b385f65208c238136bfc4e2dad2a2d11da09966576922511c', 'binding_sha256': '72777993e572dd439b380cd4dba874666391a246180f5c01491ebbe95cfb299b', 'revision': 93, 'table_index': 93, 'code_sha256': {'cli': '5016df5d406239ea636e4d10a6e19f6a5cf69ecb5e45608a8d138816afcb5c87', 'evolvmem.history_migration.capability_mapping': 'ed89c722f4ef291ebf2dfb75ef1599e2c5ee9c6e5a33fe07a865d81e853b2a5e', 'evolvmem.history_migration.capture_mapping': '2c28e24781a39ba6df8c051acafb441ee77403af1c425761e6b94242bd3e15a9', 'evolvmem.history_migration.catchup_catalog': 'ab108c144dad6b2758cf24d2b5c2597ae47f696a4476cd4a0a95cb2ee891fc05', 'evolvmem.history_migration.catchup_plan': '3c160ba2b6586468079038c5e0d65ffaefd60334b93c29a556932c9ca421dad7', 'evolvmem.history_migration.catchup_source': '98be42445e830c782cb2f1e1c6f500845af60544eec01d4ed5c893a41c0adc8b', 'evolvmem.history_migration.continuity_mapping': 'f9e1f84d85ce0e56b970ab1c0722037adabb7bcc8ab20db71c1bbbe1f52b723e', 'evolvmem.history_migration.governance_large_row': '805d9177bf1e4fd5695982fe27bc93c670bd6afcfe21b78e756a987de30eca48', 'evolvmem.history_migration.governance_mapping': 'a6143d1ef5f0469f2d857c7dc897bd20b1dc675fb0a35930ae8ea40197dbae50', 'evolvmem.history_migration.history_core.mapping': '8b1de58545ef7812493a3a7c3c514e53738c6a619888dd8700f3e2b73043f92b', 'evolvmem.history_migration.knowledge_mapping': 'cd300e57aacd3b11e79f5a9856b9331f8779daac441a7d8f49589c593c0b7e7c', 'evolvmem.history_migration.operations_mapping': 'b9077e1079f171ceb0964a472a68e6048645f8c829ffb0f938fd3985d2f58e12', 'evolvmem.history_migration.organization_mapping': '376fde5c41fdc79e776e61152c468214988158f3c23e0e9148bb4355ba21f7d1', 'evolvmem.history_migration.resume_planning': '19546fec84f839a61232a38066a07898a598871f0142a56f7e83713f27631525', 'evolvmem.history_migration.source_mapping': 'd14736226981739ed5d6f1b9ca5daca727f5957b6b142f96e3fba649edc6081a', 'evolvmem.history_migration.static_wal': 'f1893673189ae3814b13e0cdc7da39b6810c79e44472345d8ab37f75b967e076', 'planner': '9e2bd082292280469c417384f73b6a32e3a75bdf0ced4445d5d2f96f235937e3'}, 'failure_attempt': 'a0b407ce-6b49-4fde-a14d-eb489db2989c', 'failure_sha256': '2b85dbd9c61e4d58369c8466372dfa03bde347bb5f7ec4b10d8ec4473df4c7fa'}

def code_binding():
    from . import catchup_importer as I
    code={m.__name__:hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest()
          for m in (B,C,P,S,W,I,C.L,*C.MODULES.values())}
    code['planner']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    code['cli']=hashlib.sha256(Path(__file__).with_name('catchup.py').read_bytes()).hexdigest()
    return code

def _exists(path):return path.exists() or path.is_symlink()

def legacy_document(stem):
    raw=P._read(Path(str(stem)+'.draft.json'),CAP)
    C.check(hashlib.sha256(raw).hexdigest()==LEGACY['draft_sha256'],'planning_legacy_draft_changed')
    envelope=json.loads(raw);auth=envelope.pop('auth');key=P._read(Path(str(stem)+'.key'),32)
    C.check(len(key)==32 and type(auth) is str and hmac.compare_digest(auth,hmac.new(key,C.canonical(envelope),hashlib.sha256).hexdigest()),'planning_draft_authentication')
    binding=envelope['binding'];state=envelope['state']
    C.check(binding['epoch_id']==LEGACY['epoch_id'] and binding['code_sha256']==LEGACY['code_sha256']
        and C.digest(binding)==LEGACY['binding_sha256'] and C.digest(state)==LEGACY['state_sha256']
        and envelope['revision']==LEGACY['revision'] and state['table_index']==LEGACY['table_index'],'planning_legacy_profile_changed')
    tables=C.tables('owner');_validate(state,tables,binding['max_changes'],binding['max_input_bytes'])
    C.check(C.binary_table(tables[93]) and all(not C.binary_table(t) for t in tables[:93]),'planning_legacy_prefix_changed')
    for t in tables[:93]:
        specs=t.module.SPECS[t.name]
        kinds=[v['type'] for v in specs.values()] if t.domain=='organization' else [v[1] for v in specs]
        C.check('BLOB' not in kinds,'planning_legacy_prefix_not_equivalent')
    failure=stem.parent/'attempts'/(LEGACY['failure_attempt']+'.failure.json')
    C.check(hashlib.sha256(P._read(failure,65536)).hexdigest()==LEGACY['failure_sha256'],'planning_legacy_failure_changed')
    return envelope

def adopted_binding(old,binding):
    expected=dict(old['binding'],code_sha256=binding['code_sha256'])
    C.check(expected==binding,'planning_adoption_binding_changed')
    return dict(binding,adopted_from=dict(draft_sha256=LEGACY['draft_sha256'],state_sha256=LEGACY['state_sha256'],
        failure_sha256=LEGACY['failure_sha256'],revision=93,table_index=93))

def adoption_receipt(old,binding,key):
    initial=dict(revision=93,binding=binding,state=old['state'])
    initial['auth']=hmac.new(key,C.canonical(initial),hashlib.sha256).hexdigest()
    return dict(format='catchup-binary-draft-adoption-v1',adopted_from=binding['adopted_from'],
        new_code_sha256=binding['code_sha256'],new_binding_sha256=C.digest(binding),
        initial_draft_sha256=hashlib.sha256(C.canonical(initial)).hexdigest())

def _verify_adoption(stem,oldstem,binding):
    receipt_path=Path(str(stem)+'.adoption.json')
    if not _exists(Path(str(oldstem)+'.draft.json')):
        C.check(not _exists(receipt_path),'planning_legacy_draft_missing');return binding
    C.check(_exists(receipt_path),'planning_explicit_adoption_required')
    C.check(_exists(Path(str(stem)+'.draft.json')),'planning_adopted_draft_missing')
    old=legacy_document(oldstem);binding=adopted_binding(old,binding)
    key=P._read(Path(str(stem)+'.key'),32);C.check(len(key)==32,'planning_key_invalid')
    receipt=json.loads(P._read(receipt_path,65536));auth=receipt.pop('auth')
    C.check(type(auth) is str and hmac.compare_digest(auth,hmac.new(key,C.canonical(receipt),hashlib.sha256).hexdigest()),'planning_adoption_authentication')
    C.check(receipt==adoption_receipt(old,binding,key),'planning_adoption_changed')
    return binding


RELEASE_LEGACY={'epoch_id': '7c5b591d-148f-498e-ae6b-e88a694936b2', 'draft_sha256': '2a05800af4b52f1fee5c818a0113b1bd3423c055f0a21661c26238c5c5fb895f', 'state_sha256': '7699e1232f90ab3ff5f0173974f0e5528b2366ed3c260aa897930d7fdfd359e3', 'binding_sha256': '89c6c5007bf9f7a742c57f65f9a72a2d11dda738e6b6a1edda4f369231c9140c', 'code_sha256': {'cli': '5016df5d406239ea636e4d10a6e19f6a5cf69ecb5e45608a8d138816afcb5c87', 'evolvmem.history_migration.capability_mapping': 'ed89c722f4ef291ebf2dfb75ef1599e2c5ee9c6e5a33fe07a865d81e853b2a5e', 'evolvmem.history_migration.capture_mapping': '2c28e24781a39ba6df8c051acafb441ee77403af1c425761e6b94242bd3e15a9', 'evolvmem.history_migration.catchup_catalog': 'dd29650e4747b8dfb5bce3756f6a98427ab64eef662525e6ae5d62af01753c0a', 'evolvmem.history_migration.catchup_importer': 'acaf7b2e529a9ae481fbf52bf2b091f75fbf07e84c2f5faa3c4008cd995dc4ad', 'evolvmem.history_migration.catchup_plan': '2eed9d3430f82ad6f78fb2cac8b88d413f5cef96580ab631a60b9ba25f6c2f5e', 'evolvmem.history_migration.catchup_source': 'a78844be0354eb0a5b68b8bf9ccf7e2141c5fa09f72d616b8152b3949c8dd783', 'evolvmem.history_migration.continuity_mapping': 'f9e1f84d85ce0e56b970ab1c0722037adabb7bcc8ab20db71c1bbbe1f52b723e', 'evolvmem.history_migration.governance_large_row': '805d9177bf1e4fd5695982fe27bc93c670bd6afcfe21b78e756a987de30eca48', 'evolvmem.history_migration.governance_mapping': 'a6143d1ef5f0469f2d857c7dc897bd20b1dc675fb0a35930ae8ea40197dbae50', 'evolvmem.history_migration.history_core.mapping': '8b1de58545ef7812493a3a7c3c514e53738c6a619888dd8700f3e2b73043f92b', 'evolvmem.history_migration.knowledge_mapping': 'cd300e57aacd3b11e79f5a9856b9331f8779daac441a7d8f49589c593c0b7e7c', 'evolvmem.history_migration.operations_mapping': 'b9077e1079f171ceb0964a472a68e6048645f8c829ffb0f938fd3985d2f58e12', 'evolvmem.history_migration.organization_mapping': '376fde5c41fdc79e776e61152c468214988158f3c23e0e9148bb4355ba21f7d1', 'evolvmem.history_migration.resume_planning': '19546fec84f839a61232a38066a07898a598871f0142a56f7e83713f27631525', 'evolvmem.history_migration.source_mapping': 'd14736226981739ed5d6f1b9ca5daca727f5957b6b142f96e3fba649edc6081a', 'evolvmem.history_migration.static_wal': 'f1893673189ae3814b13e0cdc7da39b6810c79e44472345d8ab37f75b967e076', 'planner': 'd81bfeae4fb5c093f30c828ed1531228ec516daf26b28aaaa3c628af11d39389'}, 'revision': 95, 'table_index': 95, 'failure_attempt': '92ac3b25-c88e-4333-aebb-50c45ef193c4', 'failure_sha256': '75489dc3016799314cb54f9c73b473de2da10baca6972080b2c369a25210ab76', 'prior_adoption_sha256': '333b34597746d97c406926f237f19bb8374366c0102739e6980b5b671145a5d4'}

def release_legacy_document(stem):
    raw=P._read(Path(str(stem)+'.draft.json'),CAP)
    C.check(hashlib.sha256(raw).hexdigest()==RELEASE_LEGACY['draft_sha256'],'planning_release_legacy_draft_changed')
    old=json.loads(raw);auth=old.pop('auth');key=P._read(Path(str(stem)+'.key'),32)
    C.check(len(key)==32 and type(auth) is str and hmac.compare_digest(auth,hmac.new(key,C.canonical(old),hashlib.sha256).hexdigest()),'planning_draft_authentication')
    binding=old['binding'];state=old['state']
    C.check(binding['epoch_id']==RELEASE_LEGACY['epoch_id'] and binding['code_sha256']==RELEASE_LEGACY['code_sha256']
        and C.digest(binding)==RELEASE_LEGACY['binding_sha256'] and C.digest(state)==RELEASE_LEGACY['state_sha256']
        and old['revision']==95 and state['table_index']==95,'planning_release_legacy_profile_changed')
    initial=stem.parent/('.catchup-planner-'+binding['epoch_id'])
    prior_binding={k:v for k,v in binding.items() if k!='adopted_from'}
    C.check(hashlib.sha256(P._read(Path(str(stem)+'.adoption.json'),65536)).hexdigest()==RELEASE_LEGACY['prior_adoption_sha256']
        and _verify_adoption(stem,initial,prior_binding)==binding,'planning_prior_adoption_changed')
    _validate(state,C.tables('owner'),binding['max_changes'],binding['max_input_bytes'])
    failure=stem.parent/'attempts'/(RELEASE_LEGACY['failure_attempt']+'.failure.json')
    C.check(hashlib.sha256(P._read(failure,65536)).hexdigest()==RELEASE_LEGACY['failure_sha256'],'planning_release_legacy_failure_changed')
    return old

def release_adopted_binding(old,binding):
    expected={k:v for k,v in old['binding'].items() if k!='adopted_from'}
    expected['code_sha256']=binding['code_sha256']
    C.check(expected==binding,'planning_release_adoption_binding_changed')
    return dict(binding,adopted_from=old['binding']['adopted_from'],resumed_from=dict(
        draft_sha256=RELEASE_LEGACY['draft_sha256'],state_sha256=RELEASE_LEGACY['state_sha256'],
        failure_sha256=RELEASE_LEGACY['failure_sha256'],prior_adoption_sha256=RELEASE_LEGACY['prior_adoption_sha256'],revision=95,table_index=95))

def release_adoption_receipt(old,binding,key):
    initial=dict(revision=95,binding=binding,state=old['state'])
    initial['auth']=hmac.new(key,C.canonical(initial),hashlib.sha256).hexdigest()
    return dict(format='catchup-release-only-draft-adoption-v1',resumed_from=binding['resumed_from'],
        new_code_sha256=binding['code_sha256'],new_binding_sha256=C.digest(binding),
        initial_draft_sha256=hashlib.sha256(C.canonical(initial)).hexdigest())

def _verify_release_adoption(stem,oldstem,binding):
    receipt_path=Path(str(stem)+'.adoption.json')
    if not _exists(Path(str(oldstem)+'.draft.json')):
        initial=oldstem.parent/('.catchup-planner-'+binding['epoch_id']+'.draft.json')
        C.check(not _exists(initial) and not _exists(receipt_path),'planning_release_explicit_adoption_required');return binding
    C.check(_exists(receipt_path),'planning_release_explicit_adoption_required')
    C.check(_exists(Path(str(stem)+'.draft.json')),'planning_adopted_draft_missing')
    old=release_legacy_document(oldstem);binding=release_adopted_binding(old,binding)
    key=P._read(Path(str(stem)+'.key'),32);C.check(len(key)==32,'planning_key_invalid')
    receipt=json.loads(P._read(receipt_path,65536));auth=receipt.pop('auth')
    C.check(type(auth) is str and hmac.compare_digest(auth,hmac.new(key,C.canonical(receipt),hashlib.sha256).hexdigest()),'planning_adoption_authentication')
    C.check(receipt==release_adoption_receipt(old,binding,key),'planning_adoption_changed')
    return binding

def _validate(state,tables,max_changes,max_input_bytes):
    C.check(type(state) is dict and set(state)=={'table_index','old_summary','new_summary','ops','source_bytes'},'catchup_planning_shape')
    index=state['table_index']
    C.check(type(index) is int and 0<=index<=len(tables),'catchup_planning_frontier')
    names={t.name for t in tables[:index]}
    for side in ('old_summary','new_summary'):
        summaries=state[side]
        C.check(type(summaries) is dict and set(summaries)==names,'catchup_planning_summaries')
        for summary in summaries.values():
            C.check(type(summary) is dict and set(summary)=={'count','typed_sha256','keys_sha256','input_bytes'},'catchup_planning_summary')
            C.check(all(type(summary[k]) is int and summary[k]>=0 for k in ('count','input_bytes')),'catchup_planning_summary')
            C.check(all(type(summary[k]) is str and P.R.HASH.fullmatch(summary[k]) for k in ('typed_sha256','keys_sha256')),'catchup_planning_digest')
    C.check(type(state['source_bytes']) is list and len(state['source_bytes'])==2 and all(type(v) is int and 0<=v<=max_input_bytes for v in state['source_bytes']),'catchup_source_byte_budget')
    ops=state['ops'];C.check(type(ops) is list and len(ops)<=max_changes,'catchup_change_budget')
    seen=set()
    for o in ops:
        C.check(type(o) is dict and set(o)=={'table','key','op','before_hash','after_hash','input_bytes'} and o['table'] in names,'catchup_planning_metadata_only')
        t=C.table(o['table']);C.check(type(o['key']) is list and len(o['key'])==len(t.keys),'catchup_planning_key')
        C.check(o['op'] in ('insert','delete','update','delete_insert'),'catchup_planning_operation')
        for k in ('before_hash','after_hash'):
            C.check(o[k] is None or type(o[k]) is str and P.R.HASH.fullmatch(o[k]),'catchup_planning_digest')
        C.check((o['before_hash'] is None)==(o['op']=='insert') and (o['after_hash'] is None)==(o['op']=='delete'),'catchup_planning_operation')
        C.check(type(o['input_bytes']) is int and o['input_bytes']>0,'catchup_planning_operation')
        C.changes_budget([o]);key=(o['table'],C.canonical(o['key']))
        C.check(key not in seen,'catchup_planning_duplicate_key');seen.add(key)

def build_plan_step(old_source,new_source,path,*,epoch_config_sha256,base_binding,previous_proof,target,catalog,
        projects,caught_up_to,epoch_id,seconds=60,max_changes=100000,max_input_bytes=4*1024**3):
    """One explicit invocation completes one table, or the final dependencies.

    A single table and the final dependency pass retain the original deadline.
    A failed phase never publishes its summaries, operations or revision.
    """
    C.check(old_source.namespace==new_source.namespace,'catchup_namespace_changed')
    C.core.uuid_text(epoch_id)
    C.check(type(seconds) in (int,float) and 0<seconds<=60,'phase_deadline_invalid')
    C.check(type(max_changes) is int and 1<=max_changes<=100000,'catchup_change_budget')
    C.check(type(max_input_bytes) is int and 0<max_input_bytes<=4*1024**3,'catchup_input_budget')
    tables=tuple(C.tables(old_source.namespace))
    code=code_binding()
    binding=json.loads(C.canonical(dict(code_sha256=code,format='catchup-table-planner-v1',epoch_id=epoch_id,
        epoch_config_sha256=epoch_config_sha256,base_binding=base_binding,previous_proof=previous_proof,
        target=target,catalog=catalog,project_mapping=list(projects),caught_up_to=caught_up_to,
        old_seal=old_source.receipt_sha256,new_seal=new_source.receipt_sha256,
        old_source=old_source.document,new_source=new_source.document,
        tables=[t.name for t in tables],seconds=seconds,max_changes=max_changes,max_input_bytes=max_input_bytes)))
    initialstem=Path(path).parent/('.catchup-planner-'+epoch_id)
    oldstem=Path(str(initialstem)+'-binary-v1')
    stem=Path(str(initialstem)+'-release-v1')
    draft=Path(str(stem)+'.draft.json');keypath=Path(str(stem)+'.key')
    with P._lock(Path(str(stem)+'.lock')):
        binding=_verify_release_adoption(stem,oldstem,binding)
        if not keypath.exists():
            C.check(not (draft.exists() or draft.is_symlink()),'planning_key_missing')
            fd=os.open(keypath,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as out:out.write(os.urandom(32));out.flush();os.fsync(out.fileno())
            P._sync(keypath.parent)
        key=P._read(keypath,32);C.check(len(key)==32,'planning_key_invalid')
        previous=P._read(draft,CAP) if draft.exists() or draft.is_symlink() else None;revision=0
        if previous is None:
            state=dict(table_index=0,old_summary={},new_summary={},ops=[],source_bytes=[0,0])
        else:
            envelope=json.loads(previous)
            C.check(type(envelope) is dict and set(envelope)=={'revision','binding','state','auth'},'catchup_planning_shape')
            auth=envelope.pop('auth')
            C.check(type(auth) is str and hmac.compare_digest(auth,hmac.new(key,C.canonical(envelope),hashlib.sha256).hexdigest()),'planning_draft_authentication')
            C.check(envelope['binding']==binding,'planning_binding_changed')
            revision=envelope['revision'];C.check(type(revision) is int and revision>0,'planning_revision_invalid')
            state=envelope['state']
        _validate(state,tables,max_changes,max_input_bytes)
        if 'resumed_from' in binding:
            C.check(revision==95 and state['table_index']==95 and C.digest(state)==RELEASE_LEGACY['state_sha256'],'planning_adopted95_state_changed')
        elif 'adopted_from' in binding:
            prior=legacy_document(initialstem)['state']
            C.check(revision==state['table_index'] and revision>=93
                and state['ops'][:len(prior['ops'])]==prior['ops']
                and all(state[side].get(name)==value for side in ('old_summary','new_summary') for name,value in prior[side].items())
                and all(a>=b for a,b in zip(state['source_bytes'],prior['source_bytes'])),'planning_adopted_prefix_changed')
        index=state['table_index'];deadline=time.monotonic()+seconds
        with ExitStack() as stack:
            old=stack.enter_context(B.Snapshot(old_source,seconds=seconds,max_input_bytes=max(1,max_input_bytes-state['source_bytes'][0])))
            new=stack.enter_context(B.Snapshot(new_source,seconds=seconds,max_input_bytes=max(1,max_input_bytes-state['source_bytes'][1])))
            if index<len(tables):
                t=tables[index];a={};b={}
                for o in B.merge_rows(t,old.rows(t),new.rows(t),old_summary=a,new_summary=b):
                    state['ops'].append(o);C.check(len(state['ops'])<=max_changes,'catchup_change_budget')
                    C.check(time.monotonic()<deadline,'catchup_plan_deadline')
                state['old_summary'][t.name]=a;state['new_summary'][t.name]=b
                state['table_index']+=1
            else:
                pages=B.dependency_pages(old,new,state['ops'],catalog,dict(projects),deadline=deadline,max_changes=max_changes)
                old_sequence=old.sequences();new_sequence=new.sequences()
            source_bytes=[state['source_bytes'][0]+old.input_bytes,state['source_bytes'][1]+new.input_bytes]
            C.check(all(v<=max_input_bytes for v in source_bytes),'catchup_source_byte_budget')
        C.check(old.phase.proven_complete and new.phase.proven_complete,'catchup_source_phase_incomplete')
        C.check((P._read(draft,CAP) if draft.exists() or draft.is_symlink() else None)==previous,'planning_draft_cas_changed')
        if index==len(tables):
            return B.Plan.from_document(dict(format='history-catchup-v1',epoch_id=epoch_id,
                namespace=old_source.namespace,base_binding=base_binding,previous_proof=previous_proof,target=target,
                old_seal=old_source.receipt_sha256,new_seal=new_source.receipt_sha256,
                old_summary=state['old_summary'],new_summary=state['new_summary'],old_sequence=old_sequence,new_sequence=new_sequence,
                catalog=catalog,project_mapping=list(projects),caught_up_to=caught_up_to,
                pages=[dict(ordinal=i,sha256=C.digest(p),changes=p) for i,p in enumerate(pages)],complete=True))
        state['source_bytes']=source_bytes;_validate(state,tables,max_changes,max_input_bytes)
        envelope=dict(revision=revision+1,binding=binding,state=state)
        envelope['auth']=hmac.new(key,C.canonical(envelope),hashlib.sha256).hexdigest()
        payload=C.canonical(envelope)
        C.check(len(payload)<=CAP,'catchup_planning_metadata_budget')
        temporary=Path(str(draft)+'.'+uuid.uuid4().hex+'.tmp')
        try:
            fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as out:out.write(payload);out.flush();os.fsync(out.fileno())
            os.replace(temporary,draft);P._sync(draft.parent)
        finally:
            if temporary.exists():temporary.unlink()
        return dict(state='planning',table_index=state['table_index'],tables=len(tables),revision=revision+1,
                    changes=len(state['ops']),next_phase='dependencies' if state['table_index']==len(tables) else 'table')
