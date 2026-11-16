from collections import deque
class Scheduler:
    def __init__(self,device,accounting,limit=1):self.device=device;self.accounting=accounting;self.limit=limit;self.queue={t:deque() for t in accounting.weights};self.active={};self.results={}
    def submit(self,id,tenant,offset,size,gate=None):self.queue[tenant].append((id,offset,size,gate))
    def tick(self):
        for tenant,q in self.queue.items():
            self.accounting.round(tenant)
            if q and len(self.active)<self.limit:
                id,offset,size,gate=q[0]
                if self.accounting.can_start(tenant,size):
                    q.popleft();self.active[id]=tenant;self.accounting.start(id,tenant,size);self.device.read(id,offset,size,self.complete,gate)
    def complete(self,id,data):
        if id not in self.active:return
        self.accounting.finish(id,data);self.active.pop(id);self.results[id]=data
    def cancel(self,id):
        self.active.pop(id,None);self.accounting.cancel(id)
