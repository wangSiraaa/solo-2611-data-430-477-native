import threading,time
class LockTable:
    def __init__(self):self.cv=threading.Condition();self.holders={};self.waiting={}
    def acquire(self,tx,key,mode,timeout=1):
        end=time.monotonic()+timeout
        with self.cv:
            while True:
                other=[(t,m) for t,m in self.holders.get(key,{}).items() if t!=tx]
                if not other or (mode=='S' and all(m=='S' for _,m in other)):
                    self.holders.setdefault(key,{})[tx]=mode;self.waiting.pop(tx,None);return
                self.waiting[tx]=[t for t,_ in other]
                left=end-time.monotonic()
                if left<=0:raise TimeoutError(tx)
                self.cv.wait(left)
    def release(self,tx):
        with self.cv:
            for key in list(self.holders):
                self.holders[key].pop(tx,None)
                if not self.holders[key]:del self.holders[key]
            self.waiting.pop(tx,None);self.cv.notify_all()
