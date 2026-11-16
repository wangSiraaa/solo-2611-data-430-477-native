class Producer:
    def __init__(self,broker,id):
        self.broker=broker;self.id=id;self.epoch=broker.initialize(id);self.sequence=0;self.tx=None
    def begin(self,id):self.tx=id;self.broker.begin(self.id,self.epoch,id)
    def send(self,key,value):
        seq=self.sequence;self.sequence+=1
        return self.broker.append(self.id,self.epoch,seq,self.tx,key,value)
    def retry(self,seq,key,value):return self.broker.append(self.id,self.epoch,seq,self.tx,key,value)
    def commit(self):self.broker.finish(self.id,self.epoch,self.tx,True);self.tx=None
    def abort(self):self.broker.finish(self.id,self.epoch,self.tx,False);self.tx=None
