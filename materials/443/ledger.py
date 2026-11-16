from sketch import Summary
class Ledger:
    def __init__(self,width=10):self.width=width;self.events={};self.summaries={};self.raw_reads=[]
    def put(self,id,time,user):
        if id in self.events:self.delete(id)
        self.events[id]=(time,user);self.summaries.setdefault(time//self.width,Summary()).add(user)
    def delete(self,id):
        time,user=self.events.pop(id);self.summaries[time//self.width].remove(user)
    def raw(self,start,end):
        rows=[(id,t,u) for id,(t,u) in self.events.items() if start<=t<end];self.raw_reads.append({'range':[start,end],'rows':len(rows)});return rows
