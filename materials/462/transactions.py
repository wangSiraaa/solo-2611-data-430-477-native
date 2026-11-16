class Transaction:
    def __init__(self,id,relation,locks):self.id=id;self.relation=relation;self.locks=locks;self.writes={};self.state='open'
    def scan(self,left,right):
        rows=self.relation.scan(left,right,self.writes)
        for k,_ in rows:self.locks.acquire(self.id,k,'S')
        return rows
    def put(self,key,value):
        self.locks.acquire(self.id,key,'X');self.writes[key]=value
    def move(self,old,new):
        value=self.relation.read(old,self.writes)
        self.put(old,None);self.put(new,value)
    def commit(self):
        self.relation.apply(self.writes);self.state='committed';self.locks.release(self.id)
    def abort(self):self.writes.clear();self.state='aborted';self.locks.release(self.id)
