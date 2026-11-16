class Cursor:
 def __init__(self,rows,audit):self.rows=iter(rows);self.audit=audit;self.closed=False;audit.append('open')
 def __iter__(self):return self
 def __next__(self):
  if self.closed:raise StopIteration
  return next(self.rows)
 def close(self):
  if not self.closed:self.closed=True;self.audit.append('close')
class Provider:
 def __init__(self):self.audit=[]
 def open(self,start,count):return Cursor([{'v':start+i} for i in range(count)],self.audit)
