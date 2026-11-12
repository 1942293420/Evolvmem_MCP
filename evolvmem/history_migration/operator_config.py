"""Trusted local Linux operator configuration. No HTTP/DSN input surface."""
from dataclasses import dataclass, field
from pathlib import Path
import hashlib, json, os, re, stat
from .history_core.mapping import check, uuid_text, digest
from .static_wal import SealedWalSource


def private_read(path, limit=65536):
    check(os.name=='posix','operator_posix_required')
    p=Path(path);check(p.is_absolute() and p.resolve()==p,'operator_absolute_private_path_required')
    for parent in p.parents:
        s=parent.stat()
        check(s.st_uid in (0,os.geteuid()) and not s.st_mode & 0o022,'operator_parent_not_private')
    fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        s=os.fstat(fd)
        check(stat.S_ISREG(s.st_mode) and s.st_uid==os.geteuid() and s.st_nlink==1 and not s.st_mode & 0o077,'operator_file_not_private')
        check(s.st_size<=limit,'operator_file_budget')
        data=os.read(fd,limit+1);check(len(data)==s.st_size,'operator_file_budget')
        fields=('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns','st_mode','st_nlink')
        after=os.fstat(fd)
        check(all(getattr(after,k)==getattr(s,k) for k in fields),'operator_file_changed')
        return data
    finally:os.close(fd)


def _pairs(entries):
    result={}
    for key,value in entries:
        check(key not in result,'operator_duplicate_key');result[key]=value
    return result


@dataclass(frozen=True,repr=False)
class OperatorConfig:
    binding_hash:str
    work_dir:Path
    source:SealedWalSource=field(repr=False)
    migration_id:str
    project_mapping:tuple
    _raw:bytes=field(repr=False)
    @property
    def document(self):return json.loads(self._raw)
    @property
    def command_seconds(self):return self.document['command_seconds']

_LOADED={}
def trusted(config):
    check(_LOADED.get(id(config)) is config,'trusted_operator_config_required')


def load_config(path):
    raw=private_read(path)
    try:d=json.loads(raw,object_pairs_hook=_pairs)
    except (ValueError,TypeError):raise ValueError('operator_config_invalid') from None
    check(type(d) is dict and set(d)==set('version migration_id work_dir source_seal_file source_seal_sha256 project_mapping target control content capacity_path peak_reserve_bytes command_seconds'.split()),'operator_config_fields')
    check(type(d['version']) is int and d['version']==1,'operator_config_version');uuid_text(d['migration_id'])
    t=d['target'];c=d['control'];n=d['content']
    check(type(t) is dict and set(t)==set('database owner_role login_role library_id route_epoch policy_epoch owner_principal_id connection_ref'.split()),'operator_target_fields')
    check(type(c) is dict and set(c)==set('host port database owner_role login_role password_file ca_file'.split()),'operator_control_fields')
    check(type(n) is dict and set(n)==set('host port password_file ca_file'.split()),'operator_content_fields')
    for v in (t['database'],t['owner_role'],t['login_role'],c['database'],c['owner_role'],c['login_role']):
        check(type(v) is str and re.fullmatch('[a-z][a-z0-9_]{0,62}',v),'operator_name_invalid')
    check(t['database']!=c['database'] and t['owner_role']!=t['login_role'] and c['owner_role']!=c['login_role'],'operator_identity_collision')
    for k in ('library_id','owner_principal_id'):uuid_text(t[k])
    check(type(t['connection_ref']) is str and re.fullmatch('[A-Za-z][A-Za-z0-9_.-]{0,127}',t['connection_ref']),'operator_reference_invalid')
    check(all(type(t[k]) is int and t[k]>0 for k in ('route_epoch','policy_epoch')),'operator_epochs_invalid')
    for endpoint in (c,n):
        check(type(endpoint['host']) is str and re.fullmatch('[A-Za-z0-9.:-]{1,253}',endpoint['host']),'operator_host_invalid')
        check(type(endpoint['port']) is int and 1<=endpoint['port']<=65535,'operator_port_invalid')
        for k in ('password_file','ca_file'):private_read(endpoint[k])
    for k in ('work_dir','capacity_path','source_seal_file'):
        check(type(d[k]) is str and Path(d[k]).is_absolute() and Path(d[k]).resolve()==Path(d[k]),'operator_path_invalid')
    w=Path(d['work_dir']);s=w.stat()
    check(stat.S_ISDIR(s.st_mode) and s.st_uid==os.geteuid() and not s.st_mode & 0o077,'operator_work_dir_not_private')
    for parent in w.parents:
        info=parent.stat()
        check(info.st_uid in (0,os.geteuid()) and not info.st_mode & 0o022,'operator_parent_not_private')
    check(type(d['peak_reserve_bytes']) is int and d['peak_reserve_bytes']>0,'operator_peak_reserve_required')
    check(type(d['command_seconds']) is int and 1<=d['command_seconds']<=300,'operator_command_budget')
    projects=d['project_mapping'];check(type(projects) is dict,'operator_project_mapping')
    for name,value in projects.items():
        check(type(name) is str and name and '\x00' not in name,'operator_project_name');uuid_text(value)
    check(len(set(projects.values()))==len(projects),'operator_project_collision')
    source=SealedWalSource.from_receipt(private_read(d['source_seal_file']),d['source_seal_sha256'])
    config=OperatorConfig(digest(d),w,source,d['migration_id'],tuple(sorted(projects.items())),raw)
    _LOADED[id(config)]=config
    return config
