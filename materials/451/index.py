import sqlite3,json
from geo_model import geometry
class Index:
 def __init__(self,path):
  self.db=sqlite3.connect(path);self.db.execute('CREATE TABLE IF NOT EXISTS objects(id TEXT PRIMARY KEY,body TEXT,x0 REAL,y0 REAL,x1 REAL,y1 REAL)')
 def put(self,id,obj):
  x0,y0,x1,y1=geometry(obj).bounds
  with self.db:self.db.execute('INSERT OR REPLACE INTO objects VALUES(?,?,?,?,?,?)',(id,json.dumps(obj),x0,y0,x1,y1))
 def candidates(self,bounds):
  x0,y0,x1,y1=bounds
  return self.db.execute('SELECT id,body FROM objects WHERE x0<=? AND x1>=? AND y0<=? AND y1>=?',(x1,x0,y1,y0))
 def close(self):self.db.close()
