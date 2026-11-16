import os,threading
class Device:
    def __init__(self,path,count=8,size=128):
        self.fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_TRUNC,0o600);self.count=count;self.size=size;os.ftruncate(self.fd,count*size);self.locks=[threading.Lock() for _ in range(count)];self.events=[]
    def read(self,sector):
        with self.locks[sector]:data=os.pread(self.fd,self.size,sector*self.size)
        self.events.append(('read',sector));return data
    def write(self,sector,data):
        if len(data)!=self.size:raise ValueError('whole sector only')
        with self.locks[sector]:n=os.pwrite(self.fd,data,sector*self.size)
        if n!=self.size:raise OSError('short sector write')
        self.events.append(('write',sector))
    def close(self):os.close(self.fd)
