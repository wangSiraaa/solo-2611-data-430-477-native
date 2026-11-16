import json
from limits import Bucket
from dispatcher import Dispatcher,Request
from transport import ControlledTransport
b={'global':Bucket(10,1,4,10),'tenant':Bucket(1,1,1,0)};t=ControlledTransport();d=Dispatcher(b,t)
d.submit(Request('r1',('global','tenant')));d.tick(0)
print(json.dumps({'started_with_empty_tenant':t.started,'tenant_active':b['tenant'].active,'global_active':b['global'].active}));t.finish('r1');assert b['global'].active==0

from limits import QuotaLedger
b2={'a':Bucket(1,1,1,1),'b':Bucket(1,1,1,0)};q=QuotaLedger(b2);ok=q.begin('partial',('a','b'),1,0)
b3={'g':Bucket(1,1,1,1)};q3=QuotaLedger(b3);q3.begin('s1',('g',),1,0);q3.finish('s1');again=q3.begin('s2',('g',),1,0)
print(json.dumps({'partial_started':ok,'partial_a_active':b2['a'].active,'token_same_clock_reused_after_finish':again,'expected_a_active':0,'expected_second_start':False}))
