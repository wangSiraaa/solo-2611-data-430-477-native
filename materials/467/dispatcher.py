from collections import deque
from limits import QuotaLedger
from dataclasses import dataclass,field
@dataclass
class Request:
    id:str
    domains:tuple
    cost:int=1
    state:str='queued'
    reserved:list=field(default_factory=list)
class Dispatcher:
    def __init__(self,buckets,transport):
        self.buckets=buckets;self.ledger=QuotaLedger(buckets);self.transport=transport;self.queue=deque();self.active={};self.events=[]
    def submit(self,request):self.queue.append(request)
    def tick(self,now):
        for _ in range(len(self.queue)):
            r=self.queue.popleft();first=r.domains[0]
            if not self.ledger.begin(r.id,(first,),r.cost,now):self.queue.append(r);continue
            r.reserved=[first];r.state='active';self.active[r.id]=r
            self.events.append(('start',r.id,now));self.transport.start(r,lambda response:self.complete(r.id,response,now))
    def complete(self,request_id,response,now):
        r=self.active.pop(request_id,None)
        if r is None:return
        self.ledger.finish(r.id)
        if response.get('retry_after'):
            self.buckets[r.domains[0]].throttle(now+response['retry_after'])
        r.state='done';self.events.append(('finish',r.id,now))
    def cancel(self,request_id):
        for r in list(self.queue):
            if r.id==request_id:self.queue.remove(r);r.state='cancelled';return
        self.transport.cancel(request_id)
