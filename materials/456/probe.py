import threading,json
from scan_tasks import Tasks
from worker import scan
from sink import Sink
q=Tasks(0,8);sink=Sink();ready=threading.Event();go=threading.Event()
t=threading.Thread(target=scan,args=('source.json',q.claim(),q,sink,'old',ready,go));t.start();ready.wait()
suffix=q.split();scan('source.json',suffix,q,sink,'new');go.set();t.join()
ids=[r['row_no'] for _,r in sink.rows]
print(json.dumps({'ids':ids,'duplicates':sorted({i for i in ids if ids.count(i)>1}),'expected':list(range(8)),'workers':sorted({w for w,_ in sink.rows})}))
