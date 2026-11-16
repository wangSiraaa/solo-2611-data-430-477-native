import sqlite3,json
class Warehouse:
    def __init__(self,path=':memory:'):
        self.db=sqlite3.connect(path)
        self.db.executescript('create table if not exists messages(seq integer primary key,body text);create table if not exists points(seq integer,position integer,value text,primary key(seq,position));')
    def commit(self,message):
        seq=message['sequence']
        with self.db:
            if self.db.execute('select 1 from messages where seq=?',(seq,)).fetchone():return
            self.db.execute('insert into messages values(?,?)',(seq,json.dumps(message,sort_keys=True)))
            self.db.executemany('insert into points values(?,?,?)',[(seq,n,json.dumps(r)) for n,r in enumerate(message['rows'])])
    def sequences(self):return [r[0] for r in self.db.execute('select seq from messages order by rowid')]
