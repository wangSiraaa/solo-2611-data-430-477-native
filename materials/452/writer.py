from arbiter import locate
from receipt import result

def apply(db,rows,target='active_email'):
 out=[]
 with db:
  for n,row in enumerate(rows):
   old=locate(db,row,target)
   if old is None:
    c=db.execute('INSERT INTO contacts(tenant,email,active,external_id,n) VALUES(:tenant,:email,:active,:external_id,:n)',row);id=c.lastrowid;action='insert'
   else:
    id=old['id'];db.execute('UPDATE contacts SET n=? WHERE id=?',(row['n'],id));action='update'
   out.append(result(n,id,action,db.execute('SELECT * FROM contacts WHERE id=?',(id,)).fetchone()))
 return out
