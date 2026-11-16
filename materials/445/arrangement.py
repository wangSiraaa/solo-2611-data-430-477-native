import sqlite3,json
from time_order import leq,complete
class Arrangement:
    def __init__(self,path):
        self.db=sqlite3.connect(path);self.db.executescript('create table if not exists changes(k text,v text,t0 integer,t1 integer,diff integer); create table if not exists meta(k text primary key,v text);')
        self.db.execute("insert or ignore into meta values('since','[[0,0]]')");self.db.execute("insert or ignore into meta values('upper','[[0,0]]')");self.db.commit()
    def frontier(self,name):return json.loads(self.db.execute('select v from meta where k=?',(name,)).fetchone()[0])
    def set_frontier(self,name,frontier):
        with self.db:self.db.execute('update meta set v=? where k=?',(json.dumps(frontier),name))
    def insert(self,k,v,time,diff):
        with self.db:self.db.execute('insert into changes values(?,?,?,?,?)',(k,v,*time,diff))
    def records(self):return list(self.db.execute('select k,v,t0,t1,diff from changes'))
    def query(self,time,key=None):
        if not complete(self.frontier('upper'),time):return {'status':'pending'}
        out={}
        for k,v,t0,t1,d in self.records():
            if (key is None or key==k) and leq((t0,t1),time):out[(k,v)]=out.get((k,v),0)+d
        return {'status':'complete','rows':[[k,v,d] for (k,v),d in sorted(out.items()) if d]}
    def close(self):self.db.close()
