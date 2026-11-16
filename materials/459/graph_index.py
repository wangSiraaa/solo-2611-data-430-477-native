import sqlite3
from analyzer import analyze
class GraphIndex:
 def __init__(self,path):
  self.db=sqlite3.connect(path);self.db.execute('CREATE TABLE IF NOT EXISTS arcs(doc TEXT,start INTEGER,term TEXT)')
 def put(self,doc,tokens):
  with self.db:
   self.db.execute('DELETE FROM arcs WHERE doc=?',(doc,))
   self.db.executemany('INSERT INTO arcs VALUES(?,?,?)',[(doc,a['start'],a['term']) for a in analyze(tokens)])
 def postings(self,term):return list(self.db.execute('SELECT doc,start FROM arcs WHERE term=?',(term,)))
 def close(self):self.db.close()
