import threading
class Sink:
 def __init__(self):self.lock=threading.Lock();self.rows=[]
 def submit(self,worker,rows):
  with self.lock:self.rows.extend((worker,r) for r in rows)
