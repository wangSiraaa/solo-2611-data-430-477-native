import json
from pathlib import Path

def scan(path,task,tasks,sink,name,after_read=None,proceed=None):
 rows=json.loads(Path(path).read_text());cursor=task['lo']
 while cursor<task['hi']:
  batch=rows[cursor:min(cursor+2,task['hi'])];end=cursor+len(batch);tasks.observed_read(end)
  if after_read is not None:after_read.set();proceed.wait();after_read=None
  sink.submit(name,[{'row_no':r['row_no'],'value':r['value']*3} for r in batch]);tasks.commit(end);cursor=end
