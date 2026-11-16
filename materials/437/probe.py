import tempfile,json,sys
from runtime import Runtime
r=Runtime(2)
try:
 r.submit('add',('k',5));r.submit('timer',('t','k',10));r.drain()
 with tempfile.TemporaryDirectory() as d:
  if sys.argv[1]=='rescale':r.rescale(d+'/save.json',3)
  r.watermark(10)
 print(json.dumps({'outputs':r.outputs,'expected':[{'timer_id':'t','key':'k','value':5,'due':10}]},sort_keys=True))
finally:r.close()
