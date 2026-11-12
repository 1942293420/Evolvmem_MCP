"""Bounded source manifest primitives. No PostgreSQL completion authority."""
import hashlib
from . import organization_scan as S
PHYSICAL_CAP=2**31
INPUT_CAP=2**30
MAX_PAGES=4096

def summarize(pages,source,highwater):
 if type(source.get('physical_bytes')) is not int or not 0<=source['physical_bytes']<=PHYSICAL_CAP:raise ValueError('physical_source_cap_2GiB')
 counts=dict.fromkeys(S.TABLES,0);last={};ended=set();result=[];total=0;previous=-1;metadata=0
 for page in pages:
  t=page['table']
  if t not in counts or t in ended or S.TABLES.index(t)<previous:raise ValueError('manifest_table_order')
  previous=S.TABLES.index(t)
  if page['after']!=last.get(t):raise ValueError('manifest_cursor')
  raw=S.canonical(page['rows'])
  if hashlib.sha256(raw).hexdigest()!=page['sha256'] or len(raw)!=page['input_bytes']:raise ValueError('manifest_page_digest')
  if len(page['rows'])>128:raise ValueError('manifest_page_rows')
  if page['rows']:
   ids=[r['source_rowid'] for r in page['rows']]
   if ids!=sorted(set(ids)) or (page['after'] is not None and ids[0]<=page['after']) or ids[-1]!=page['end']:raise ValueError('manifest_range')
  elif not page['exhausted'] or page['end']!=page['after']:raise ValueError('manifest_empty_range')
  for r in page['rows']:S.M.map_row(t,r)
  last[t]=page['end'];counts[t]+=len(page['rows']);total+=len(raw)
  if total>INPUT_CAP:raise ValueError('serialized_domain_input_cap_1GiB')
  result.append({k:v for k,v in page.items() if k!='rows'}|{'row_count':len(page['rows'])})
  metadata+=len(S.canonical(result[-1]))+1
  if len(result)>MAX_PAGES or metadata>8*1024**2-32768 or sum(counts.values())>250000:raise ValueError('manifest_page_cap')
  if page['exhausted']:ended.add(t)
 if ended!=set(S.TABLES):raise ValueError('manifest_incomplete')
 if type(highwater) is not int or not max(0,last['organization_tasks'] or 0)<=highwater<2**63-1:raise ValueError('task_highwater')
 return dict(format='organization-seven-v1',source=dict(source),counts=counts,pages=result,input_bytes=total,task_highwater=highwater)

def checked_page(connection,manifest,index):
 expected=manifest['pages'][index]
 actual=S.page(connection,expected['table'],expected['after'])
 if {k:v for k,v in actual.items() if k!='rows'}|{'row_count':len(actual['rows'])}!=expected:raise ValueError('source_page_changed')
 return actual['rows']
