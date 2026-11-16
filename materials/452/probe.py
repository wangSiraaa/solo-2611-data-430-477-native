import tempfile,json
from database import create
from writer import apply
with tempfile.TemporaryDirectory() as d:
 db=create(d+'/c.sqlite');apply(db,[dict(tenant='t',email='a@x',active=1,external_id='x1',n=1)])
 normal=apply(db,[dict(tenant='t',email='A@X',active=1,external_id='x2',n=2)])
 wrong=apply(db,[dict(tenant='t',email='b@x',active=1,external_id='x1',n=9)])
 print(json.dumps({'normal':normal,'non_target_conflict':wrong,'expected_non_target':'constraint_error, unchanged n=2'}));db.close()
