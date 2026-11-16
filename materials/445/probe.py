import tempfile,json,sys
from arrangement import Arrangement
from compactor import compact
from time_order import complete
with tempfile.TemporaryDirectory() as d:
 a=Arrangement(d+'/a.sqlite');a.set_frontier('upper',[])
 if sys.argv[1]=='normal':
  a.insert('k','v',(1,1),2);out={'query':a.query((2,2)),'expected':[['k','v',2]]}
 elif sys.argv[1]=='partial':
  a.insert('k','v',(1,5),2);out={'query_2_0':a.query((2,0)),'expected_rows':[],'frontier_complete_2_0':complete([(1,5)],(2,0)),'expected_complete':True}
 else:
  a.insert('k','v',(0,1),1);q=(3,1)
  def independent():return sum(diff for k,v,t0,t1,diff in a.records() if t0<=q[0] and t1<=q[1])
  before=independent();compact(a,[(1,3),(3,1)]);out={'before':before,'after_by_component_order':independent(),'records':a.records(),'expected_after':before}
 print(json.dumps(out,sort_keys=True));a.close()
