class Cache:
    def __init__(self,budget):self.budget=budget;self.data={};self.resident=0;self.peak=0;self.hits=0
    def get(self,oid):
        if oid in self.data:self.hits+=1;return self.data[oid]
    def put(self,oid,value):
        if oid in self.data:self.resident-=len(self.data[oid])
        self.data[oid]=value;self.resident+=len(value);self.peak=max(self.peak,self.resident)
