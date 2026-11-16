import queue,threading
from dataclasses import dataclass
from session import Session
@dataclass
class Lease:
    serial:int
    session:Session
    tenant:str
    closed:bool=False
class Pool:
    def __init__(self,size=2):
        self.size=size;self.serial=0;self.guard=threading.Lock()
        self.idle=queue.Queue();self.sessions=[]
        for _ in range(size):
            s=Session();self.sessions.append(s);self.idle.put(s)
    def acquire(self,tenant,path,timeout=1):
        s=self.idle.get(timeout=timeout)
        with self.guard:self.serial+=1;serial=self.serial
        try:s.bind(tenant,path)
        except BaseException:self.idle.put(s);raise
        return Lease(serial,s,tenant)
    def release(self,lease):
        if lease.closed:return
        lease.session.reset();lease.closed=True;self.idle.put(lease.session)
    def close(self):
        for s in self.sessions:s.close()
