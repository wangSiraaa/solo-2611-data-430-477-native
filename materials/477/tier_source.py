from pathlib import Path
import threading
class NotReady(Exception):pass
class TierSource:
    def __init__(self,root):self.root=Path(root);self.now=0;self.tickets={};self.restored={};self.restore_calls=[];self.reads=[];self.serial=0;self.lock=threading.RLock()
    def restore(self,key,ttl=10):
        with self.lock:
            self.serial+=1;ticket=self.serial;self.tickets[ticket]={'key':key,'state':'pending','ttl':ttl};self.restore_calls.append((key,ticket));return ticket
    def complete(self,ticket,success=True):
        with self.lock:
            t=self.tickets[ticket];t['state']='ready' if success else 'failed'
            if success:self.restored[t['key']]=self.now+t['ttl']
    def status(self,ticket):
        with self.lock:return dict(self.tickets[ticket])
    def advance(self,ms):self.now+=ms
    def range(self,key,offset,length):
        with self.lock:
            if self.restored.get(key,-1)<=self.now:raise NotReady(key)
            self.reads.append((key,offset,length,self.now))
        with (self.root/key).open('rb') as f:f.seek(offset);return f.read(length)
