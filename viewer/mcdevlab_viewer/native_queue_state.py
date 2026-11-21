"""Local dispatcher queue metadata, never native liveness or machine authority."""
from .harness_state import _read
PATH='/root/atlas-beta-program/state/native-dispatch/queue-status.json'

def observe(host,path=PATH):
 try:
  o,mtime=_read(path)
  if o.get('schema')!='atlas.native-queue-status.v1':raise ValueError('schema')
  h=o['per_host'][host];count=h['queued_eligible'];active=h['active']
  if type(count) is not int or not 0<=count<=32 or not isinstance(active,list) or len(active)>32:raise ValueError('counts')
  queued=o['queued'];claims=o['active']
  if not isinstance(queued,list) or not isinstance(claims,list) or len(queued)>32 or len(claims)>32:raise ValueError('jobs')
  rows=[]
  for job in queued+claims:
   if not isinstance(job,dict):raise ValueError('job')
   if host not in job.get('eligible_hosts',[]):continue
   if job.get('status')!='QUEUED' and job.get('host')!=host:continue
   rows.append({k:job.get(k) for k in ('id','priority','status','host','reason')})
  return {'status':'OBSERVED','queued_eligible':count,'active_requests':len(active),'jobs':rows,'as_of':mtime,
          'scope':'Dispatcher requests only; remote game state comes from native controller receipts.'}
 except FileNotFoundError:return None
 except (OSError,ValueError,KeyError,TypeError):return {'status':'UNAVAILABLE','reason':'Queue snapshot unavailable or invalid'}
