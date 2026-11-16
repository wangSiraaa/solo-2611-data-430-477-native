import json
from subscription import Subscription
from warehouse import Warehouse
from collector import Collector
s=Subscription(5);w=Warehouse();c=Collector(s,w);a=s.notification([1]);c.receive(a);assert w.sequences()==[1]
b=s.notification([2]);d=s.notification([3]);c.receive(d);c.receive(b)
print(json.dumps({'stored':w.sequences(),'acks':s.acks,'expected':[1,2,3],'republish2_still_available':2 in s.retained}))
