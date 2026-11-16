import threading
class Relation:
    def __init__(self,rows=()):self.rows=dict(rows);self.guard=threading.RLock()
    def read(self,key,writes):
        if key in writes:return writes[key]
        with self.guard:return self.rows.get(key)
    def scan(self,left,right,writes):
        with self.guard:merged={**self.rows,**writes}
        return [(k,v) for k,v in sorted(merged.items()) if left<=k<right and v is not None]
    def apply(self,writes):
        with self.guard:
            for k,v in writes.items():
                if v is None:self.rows.pop(k,None)
                else:self.rows[k]=v
