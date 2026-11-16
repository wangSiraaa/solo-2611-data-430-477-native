import json,copy
class Checkpoint:
    def __init__(self):self.pending={};self.completed={}
    def barrier(self,cid,channel,state,offsets):
        if cid not in self.pending:self.pending[cid]={'state':copy.deepcopy(state),'seen':set()}
        p=self.pending[cid];p['seen'].add(channel)
        if p['seen']=={'a','b'}:
            self.completed[cid]={'state':p['state'],'offsets':dict(offsets)};del self.pending[cid]
    def data(self,channel,frame):pass
    def write(self,cid,path):open(path,'w').write(json.dumps(self.completed[cid]))
