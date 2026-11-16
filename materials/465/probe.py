import tempfile,json
from zones import Zones
from index import Index
from allocator import Store
with tempfile.TemporaryDirectory() as td:
 z=Zones(td,4,64);s=Store(z,Index());s.put('a',b'A'*16);assert s.get('a')==b'A'*16
 for k in range(17):s.put('hot',bytes([66+k])*16)
 print(json.dumps({'a':s.get('a').decode(),'expected':'AAAAAAAAAAAAAAAA','resets':[x for x in z.events if x[0]=='reset']}))
