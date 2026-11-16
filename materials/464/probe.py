import json
from replica import Replica
from quorum import Transport
from register import Register
servers={k:Replica() for k in 'ABC'}
try:
 t=Transport({k:v.start() for k,v in servers.items()});r=Register(t);r.write('normal');assert r.read()=='normal'
 t.rpc('A',{'op':'put','tag':2,'value':'new'})
 first=r.read(['A','B']);second=r.read(['B','C'])
 print(json.dumps({'first':first,'second':second,'reversed':first=='new' and second=='normal'}))
finally:
 for s in servers.values():s.shutdown();s.server_close()
