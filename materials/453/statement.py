from triggers import refresh_totals

def update(db,changes):
 with db:
  for id,n in changes:
   old=db.execute('SELECT id,g,n FROM facts WHERE id=?',(id,)).fetchone()
   db.execute('UPDATE facts SET n=? WHERE id=?',(n,id));new=db.execute('SELECT id,g,n FROM facts WHERE id=?',(id,)).fetchone()
   refresh_totals(db,[old],[new])
