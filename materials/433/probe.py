import json,sys,threading
from concurrent.futures import ThreadPoolExecutor
from service import Source
from auth import Auth
from requester import Requester
from collector import Collector
s=Source();a=Auth(s)
if sys.argv[1]=='normal':
 c=Collector(Requester(s,a));print(json.dumps({'A':c.collect('A'),'B':c.collect('B'),'refresh_calls':s.refresh_calls}))
else:
 barrier=threading.Barrier(2);used=a.token()
 def work(_):
  try:return {'token':a.refresh_after_401(used,barrier.wait)}
  except Exception as e:return {'error':type(e).__name__}
 with ThreadPoolExecutor(2) as ex:results=list(ex.map(work,range(2)))
 print(json.dumps({'results':results,'refresh_calls':s.refresh_calls,'generation':s.generation}))
