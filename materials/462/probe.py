import json
from relation import Relation
from locks import LockTable
from transactions import Transaction
r=Relation();l=LockTable();n=Transaction('normal',r,l);n.put(90,'ok');n.commit();assert r.rows[90]=='ok'
a=Transaction('a',r,l);b=Transaction('b',r,l);ar=a.scan(10,20);br=b.scan(10,20)
a.put(11,'a');b.put(12,'b');a.commit();b.commit()
print(json.dumps({'both_saw_empty':not ar and not br,'registrations':r.scan(10,20,{}),'expected_at_most':1}))
