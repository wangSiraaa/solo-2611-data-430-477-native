import threading
class ControlledTransport:
    def __init__(self):self.pending={};self.started=[];self.lock=threading.Lock()
    def start(self,r,callback):
        with self.lock:self.started.append(r.id);self.pending[r.id]=callback
    def finish(self,id,response=None):
        with self.lock:cb=self.pending.pop(id,None)
        if cb:cb(response or {})
    def cancel(self,id):self.finish(id,{'cancelled':True})
class ThreadTransport(ControlledTransport):
    def __init__(self,fn):super().__init__();self.fn=fn
    def start(self,r,callback):
        super().start(r,callback)
        def work():
            try:result=self.fn(r)
            except Exception as e:result={'error':str(e)}
            self.finish(r.id,result)
        threading.Thread(target=work,daemon=True).start()
