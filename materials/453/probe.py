import json
from store import create
from statement import update

db=create();db.executemany('INSERT INTO facts VALUES(?,?,?)',[('a','g',5),('b','g',5)]);db.commit()
update(db,[('a',6)]);normal=list(db.execute('SELECT * FROM totals'))
try:update(db,[('a',-6),('b',17)]);error=None
except Exception as e:error=str(e)
print(json.dumps({'normal':normal,'batch_error':error,'rows':list(db.execute('SELECT * FROM facts')),'expected_batch_total':11,'expected_batch':'success once after whole statement'}));db.close()
