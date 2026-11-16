import json,sys,hashlib
from ledger import Ledger
from window import estimate
from sketch import Summary
user=hashlib.sha256(b'same').hexdigest()[:32];l=Ledger();l.put('a',1,user);l.put('b',2,user);l.put('c',11,user)
if sys.argv[1]=='delete':l.delete('a')
out={'window_0_10':estimate(l,0,10),'window_0_20':estimate(l,0,20),'expected_both':1}
if sys.argv[1]=='budget':
 users=[hashlib.sha256(f'public-user-{i}'.encode()).hexdigest()[:32] for i in range(5000)];s=Summary(users);out.update({'exact_zlib_bytes':len(s.payload()),'summary_budget':2048,'distinct_users':len(users)})
print(json.dumps(out,sort_keys=True))
