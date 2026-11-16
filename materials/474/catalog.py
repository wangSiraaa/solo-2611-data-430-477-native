import json,sqlite3
class Catalog:
    def __init__(self,path=':memory:'):
        self.db=sqlite3.connect(path);self.db.execute('create table if not exists schemas(subject text,version integer,body text,primary key(subject,version))')
    def publish(self,subject,body):
        n=self.db.execute('select coalesce(max(version),0)+1 from schemas where subject=?',(subject,)).fetchone()[0]
        with self.db:self.db.execute('insert into schemas values(?,?,?)',(subject,n,json.dumps(body,sort_keys=True)))
        return n
    def get(self,subject,version=None):
        if version is None:row=self.db.execute('select version,body from schemas where subject=? order by version desc limit 1',(subject,)).fetchone()
        else:row=self.db.execute('select version,body from schemas where subject=? and version=?',(subject,version)).fetchone()
        if row is None:raise KeyError((subject,version))
        return row[0],json.loads(row[1])
