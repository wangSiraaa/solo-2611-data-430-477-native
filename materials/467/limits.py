from dataclasses import dataclass
@dataclass
class Bucket:
    capacity:float
    per_ms:float
    max_active:int
    tokens:float=0
    updated:int=0
    active:int=0
    frozen_until:int=0
    def refill(self,now):
        self.tokens=min(self.capacity,self.tokens+max(0,now-self.updated)*self.per_ms);self.updated=now
    def available(self,cost,now):
        self.refill(now)
        return now>=self.frozen_until and self.tokens>=cost and self.active<self.max_active
    def reserve(self,cost,now):
        if not self.available(cost,now):return False
        self.tokens-=cost;self.active+=1;return True
    def complete(self):self.active-=1
    def throttle(self,until):self.frozen_until=max(self.frozen_until,until)

class QuotaLedger:
    def __init__(self,buckets):self.buckets=buckets;self.tickets={};self.history=[]
    def begin(self,id,domains,cost,now):
        reserved=[]
        for d in domains:
            if not self.buckets[d].reserve(cost,now):
                self.history.append(('partial',id,list(reserved)));return False
            reserved.append(d)
        self.tickets[id]=(reserved,cost);self.history.append(('sent',id,now));return True
    def finish(self,id):
        item=self.tickets.pop(id,None)
        if item is None:return
        domains,cost=item
        for d in domains:
            self.buckets[d].complete()
            self.buckets[d].tokens=min(self.buckets[d].capacity,self.buckets[d].tokens+cost)
        self.history.append(('settled',id))
