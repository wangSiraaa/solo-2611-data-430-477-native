import sqlite3,json
class Ledger:
    def __init__(self,path=':memory:'):
        self.db=sqlite3.connect(path)
        self.db.execute('create table if not exists deliveries(id text primary key,payload text,status text,tag integer,error text)')
    def add(self,id,payload):
        with self.db:self.db.execute('insert into deliveries values(?,?,?,NULL,NULL)',(id,json.dumps(payload,sort_keys=True),'queued'))
    def sent(self,id,tag):
        with self.db:self.db.execute("update deliveries set status='sent',tag=? where id=?",(tag,id))
    def status(self,id,state,error=None):
        with self.db:self.db.execute('update deliveries set status=?,error=? where id=?',(state,error,id))
    def get(self,id):return self.db.execute('select status,error from deliveries where id=?',(id,)).fetchone()
