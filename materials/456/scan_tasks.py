import threading
class Tasks:
 def __init__(self,lo,hi):self.lock=threading.Lock();self.task={'lo':lo,'hi':hi,'read':lo,'committed':lo};self.pending=[]
 def claim(self):
  with self.lock:return dict(self.task)
 def observed_read(self,end):
  with self.lock:self.task['read']=end
 def commit(self,end):
  with self.lock:self.task['committed']=end
 def split(self):
  with self.lock:
   pivot=self.task['read'];suffix={'lo':pivot,'hi':self.task['hi'],'read':pivot,'committed':pivot};self.task['hi']=pivot;self.pending.append(suffix);return suffix
