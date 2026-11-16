from pathlib import Path
from node import Node
from transport import Transport
from state_machine import read
class Cluster:
    def __init__(self,root):
        self.nodes={n:Node(str(Path(root)/(n+'.sqlite')),n) for n in ['A','B','C']};self.transport=Transport();self.leader='A'
    def elect(self,name):
        self.leader=name;t=max(n.term() for n in self.nodes.values())+1;self.nodes[name].set_term(t);return {'leader':name,'term':t}
    def put(self,request_id,key,value):
        leader=self.nodes[self.leader];index=len(leader.entries())+1;term=leader.term();leader.append(index,term,key,value);acks=1
        for name,node in self.nodes.items():
            if name==self.leader:continue
            ok=self.transport.deliver(self.leader,name,'append',[index,term,key,value],lambda n=node:n.append(index,term,key,value) or True)
            if ok:acks+=1
        return {'success':True,'index':index,'acks':acks}
    def get(self,key):return read(self.nodes[self.leader],key)
    def close(self):
        for n in self.nodes.values():n.close()
