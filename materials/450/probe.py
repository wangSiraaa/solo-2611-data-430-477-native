import json
from pathlib import Path
from bitmap import Bitmap
from planner import combine
from heap_scan import scan
rows=[];a=Bitmap();b=Bitmap()
for p in range(8):
 rs=json.loads(Path(f'heap/{p}.json').read_text());rows+=rs
 for i,r in enumerate(rs):
  if r['a']:
   a.add(p,i);a.fit(128)
  if r['b']:b.add(p,i)
before=a.charge();joined=combine(a,b,'and')
print(json.dumps({'predicate':'a=1 AND b=1','budget':256,'a_after_stream_fit':a.charge(),'all_live_charge':a.charge()+b.charge()+joined.charge(),'lossy_pages':[p for p,x in a.pages.items() if x is None],'result':scan('heap',joined,lambda r:r['a']==1 and r['b']==1),'expected':[r['row_id'] for r in rows if r['a']==1 and r['b']==1]}))
