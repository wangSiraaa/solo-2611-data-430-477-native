import threading
class Index:
    def __init__(self):self.rows={};self.lock=threading.RLock();self.version=0
    def put(self,key,loc):
        with self.lock:self.version+=1;self.rows[key]=(self.version,loc);return self.version
    def get(self,key):
        with self.lock:return self.rows[key]
    def entries(self):
        with self.lock:return list(self.rows.items())
