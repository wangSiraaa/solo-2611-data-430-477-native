class Remote:
    def __init__(self):
        self.rows=[(1,'ann',2),(2,'anne',10),(3,'bob',3),(4,'ann',None)];self.calls=[];self.active=set();self.serial=0
    def open(self,filters):
        self.serial+=1;cid=self.serial;self.active.add(cid);rows=self.rows
        for column,value in filters:
            if column==0:rows=[r for r in rows if r[0]==value]
            elif column==1:rows=[r for r in rows if r[1].casefold().startswith(str(value).casefold())]
        rows=sorted(rows,key=lambda r:str(r[2]));self.calls.append({'cursor':cid,'filters':filters,'returned':len(rows)})
        return cid,rows
    def close(self,cid):self.active.discard(cid)
