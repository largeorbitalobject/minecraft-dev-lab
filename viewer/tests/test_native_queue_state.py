import json,pathlib,tempfile,unittest
from unittest.mock import patch
import helpers
from mcdevlab_viewer import native_queue_state as n,collector
class QueueState(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=pathlib.Path(self.tmp.name)/'status.json'
  self.obj={'schema':'atlas.native-queue-status.v1','per_host':{'fd':{'queued_eligible':1,'active':['b']}},'queued':[{'id':'a','eligible_hosts':['fd','mac'],'status':'QUEUED','priority':'owner'}],'active':[{'id':'b','eligible_hosts':['fd'],'host':'fd','status':'UNCERTAIN','priority':'normal'}]};self.save()
 def save(self):self.path.write_text(json.dumps(self.obj))
 def test_queue_and_uncertain_claim_are_metadata_not_liveness(self):
  o=n.observe('fd',str(self.path));self.assertEqual(o['queued_eligible'],1);self.assertEqual(o['active_requests'],1);self.assertEqual(o['jobs'][1]['status'],'UNCERTAIN');self.assertNotIn('state',o)
 def test_other_hosts_active_requests_not_shown(self):
  self.obj['active'].append({'id':'c','eligible_hosts':['fd','mac'],'host':'mac','status':'RUNNING'});self.save()
  self.assertEqual([x['id'] for x in n.observe('fd',str(self.path))['jobs']],['a','b'])
 def test_missing_and_corrupt_not_zero(self):
  self.path.unlink();self.assertIsNone(n.observe('fd',str(self.path)));self.path.write_text('{');self.assertEqual(n.observe('fd',str(self.path))['status'],'UNAVAILABLE')
 def test_symlink_refused(self):
  p=self.path.with_name('link');p.symlink_to(self.path);self.assertEqual(n.observe('fd',str(p))['status'],'UNAVAILABLE')
 def test_bad_count_not_idle(self):
  for value in (True,-1,33,'1'):
   self.obj['per_host']['fd']['queued_eligible']=value;self.save();self.assertEqual(n.observe('fd',str(self.path))['status'],'UNAVAILABLE')
 def test_actual_collector_attachment_preserves_native_state(self):
  # Call the real collector method with only its host configuration dependency.
  cls=next(v for v in vars(collector).values() if isinstance(v,type) and hasattr(v,'_attach_native_queue'))
  instance=object.__new__(cls);instance.h={'controller_host':'fd'};instance.id='amd'
  state={'state':'running','job':{'id':'real-native-job'}}
  with patch.object(n,'observe',return_value={'status':'OBSERVED','queued_eligible':1}):instance._attach_native_queue(state)
  self.assertEqual(state['state'],'running');self.assertEqual(state['job']['id'],'real-native-job');self.assertEqual(state['native_queue']['queued_eligible'],1)
if __name__=='__main__':unittest.main(verbosity=2)
