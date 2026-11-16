class Accounting:
    def __init__(self,weights,quantum=4096):self.weights=weights;self.quantum=quantum;self.deficit={t:0 for t in weights};self.served={t:0 for t in weights};self.reserved={}
    def round(self,tenant):self.deficit[tenant]+=self.quantum*self.weights[tenant]
    def can_start(self,tenant,size):return self.deficit[tenant]>=size
    def start(self,id,tenant,size):self.deficit[tenant]-=size;self.reserved[id]=(tenant,size)
    def finish(self,id,data):
        tenant,size=self.reserved.pop(id);self.served[tenant]+=size
    def cancel(self,id):
        item=self.reserved.pop(id,None)
        if item:self.deficit[item[0]]+=item[1]
