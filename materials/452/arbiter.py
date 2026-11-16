def locate(db,row,target):
 # Generic upsert preflight: pick any conflicting unique key.
 return db.execute('SELECT * FROM contacts WHERE external_id=? OR (tenant=? AND lower(email)=lower(?) AND active=1) ORDER BY id LIMIT 1',(row['external_id'],row['tenant'],row['email'])).fetchone()
