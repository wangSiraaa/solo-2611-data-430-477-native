import json,sys
from pack_reader import PackReader
from resolver import Resolver,object_id
from cache import Cache
r=PackReader('fixtures');c=Cache(262144);resolver=Resolver(r,c);requests=json.load(open('fixtures/requests.json'))[sys.argv[1]];result=[]
for oid in requests:
 try:
  value=resolver.read(oid);result.append({'oid':oid,'bytes':len(value),'hash_matches':object_id(value)==oid})
 except Exception as e:result.append({'oid':oid,'error':str(e)})
print(json.dumps({'results':result,'cache_peak':c.peak,'budget':c.budget,'reads':r.reads},sort_keys=True))
