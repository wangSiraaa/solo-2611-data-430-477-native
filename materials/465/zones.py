from pathlib import Path
import os,threading
class Zones:
    def __init__(self,root,count=4,capacity=64):
        self.root=Path(root);self.root.mkdir(exist_ok=True);self.count=count;self.capacity=capacity;self.wp=[0]*count;self.lock=threading.RLock();self.events=[]
        for z in range(count):(self.root/f'z{z}').write_bytes(b'')
    def append(self,z,data):
        with self.lock:
            if self.wp[z]+len(data)>self.capacity:raise BufferError(z)
            pos=self.wp[z]
            with open(self.root/f'z{z}','ab',buffering=0) as f:f.write(data)
            self.wp[z]+=len(data);self.events.append(('append',z,pos,len(data)));return z,pos,len(data)
    def read(self,loc):
        z,pos,n=loc
        with open(self.root/f'z{z}','rb') as f:f.seek(pos);return f.read(n)
    def reset(self,z):
        with self.lock:(self.root/f'z{z}').write_bytes(b'');self.wp[z]=0;self.events.append(('reset',z))
