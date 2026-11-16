import threading
class Fenced(Exception):pass
class Broker:
    def __init__(self):
        self.lock=threading.RLock();self.epochs={};self.log=[];self.transactions={};self.sequences={}
    def initialize(self,producer):
        with self.lock:
            self.epochs[producer]=self.epochs.get(producer,-1)+1
            return self.epochs[producer]
    def begin(self,producer,epoch,tx):
        with self.lock:self.transactions[(producer,tx)]='open'
    def append(self,producer,epoch,sequence,tx,key,value):
        with self.lock:
            identity=(producer,sequence)
            if identity in self.sequences:return self.sequences[identity]
            offset=len(self.log)
            self.log.append(dict(offset=offset,producer=producer,epoch=epoch,sequence=sequence,tx=tx,key=key,value=value))
            self.sequences[identity]=offset;return offset
    def finish(self,producer,epoch,tx,commit):
        with self.lock:self.transactions[(producer,tx)]='committed' if commit else 'aborted'
