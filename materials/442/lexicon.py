import sqlite3
class Lexicon:
    def __init__(self,path):
        self.db=sqlite3.connect(path);self.db.executescript('create table if not exists terms(id text primary key,term text,freq integer); create table if not exists buckets(prefix text,id text,term text,freq integer,primary key(prefix,id));');self.reads=0
    def put(self,id,term,freq):
        with self.db:
            self.db.execute('insert or replace into terms values(?,?,?)',(id,term,freq));self.db.execute('insert or replace into buckets values(?,?,?,?)',(term[:2],id,term,freq))
    def delete(self,id):
        with self.db:self.db.execute('delete from terms where id=?',(id,))
    def close(self):self.db.close()
