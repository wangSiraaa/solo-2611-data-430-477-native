import json,tempfile,sys
from cluster import Cluster
with tempfile.TemporaryDirectory() as d:
 c=Cluster(d)
 try:
  c.elect('A');first=c.put('r1','x',1);out={'normal':first,'read':c.get('x')}
  if sys.argv[1]=='partition':
   c.transport.partition(['A'],['B','C']);out['isolated_write']=c.put('r9','x',9);c.elect('B');out['new_leader_write']=c.put('r2','x',2);out['new_read']=c.get('x');c.transport.heal();c.elect('A');out['old_leader_read']=c.get('x');out['expected']='isolated9 never success; committed2 survives any valid new leader'
  out['transport']=c.transport.log;print(json.dumps(out,sort_keys=True))
 finally:c.close()
