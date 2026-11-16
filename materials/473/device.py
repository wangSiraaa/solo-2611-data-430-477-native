import os,threading
class Device:
    def __init__(self,path):self.fd=os.open(path,os.O_RDONLY);self.started=[];self.pending={};self.guard=threading.Lock();self.active=0;self.peak=0
    def read(self,id,offset,size,callback,gate=None):
        with self.guard:self.active+=1;self.peak=max(self.peak,self.active);self.started.append(id)
        def work():
            if gate:gate.wait()
            data=os.pread(self.fd,size,offset)
            with self.guard:self.active-=1
            callback(id,data)
        thread=threading.Thread(target=work,daemon=True);self.pending[id]=thread;thread.start()
    def join(self):
        for thread in list(self.pending.values()):thread.join(2)
    def close(self):self.join();os.close(self.fd)
