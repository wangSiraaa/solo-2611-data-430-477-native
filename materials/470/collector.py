class Collector:
    def __init__(self,subscription,warehouse):self.source=subscription;self.warehouse=warehouse;self.last=0;self.errors=[]
    def receive(self,message):
        seq=message['sequence']
        if seq<=self.last:return
        self.warehouse.commit(message);self.last=seq;self.source.acknowledge(seq)
    def reconnect(self):return self.last
