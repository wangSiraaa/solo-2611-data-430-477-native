def refresh_totals(db,old,new):
 groups={r[1] for r in old+new}
 for g in groups:
  n=db.execute('SELECT coalesce(sum(n),0) FROM facts WHERE g=?',(g,)).fetchone()[0]
  db.execute('INSERT OR REPLACE INTO totals VALUES(?,?)',(g,n));db.execute('INSERT INTO audit VALUES(?,?)',(g,n))
  if n<0:raise ValueError('negative statement total')
