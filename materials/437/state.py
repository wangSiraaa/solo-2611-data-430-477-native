class State:
    def __init__(self):self.counts={};self.timers={}
    def add(self,key,delta):self.counts[key]=self.counts.get(key,0)+delta
    def timer(self,timer_id,key,due):self.timers[timer_id]=(key,due)
    def advance(self,watermark):
        out=[]
        for tid,(key,due) in sorted(list(self.timers.items())):
            if due<=watermark:out.append({'timer_id':tid,'key':key,'value':self.counts.get(key,0),'due':due});del self.timers[tid]
        return out
