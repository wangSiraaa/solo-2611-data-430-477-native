class Transport:
    def __init__(self):self.blocked=set();self.log=[]
    def partition(self,left,right):
        for a in left:
            for b in right:self.blocked.add((a,b));self.blocked.add((b,a))
    def heal(self):self.blocked.clear()
    def deliver(self,source,target,kind,body,callback):
        ok=(source,target) not in self.blocked;self.log.append({'from':source,'to':target,'kind':kind,'delivered':ok,'body':body})
        return callback() if ok else None
