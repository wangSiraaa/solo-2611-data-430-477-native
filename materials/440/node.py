import sqlite3
class Node:
    def __init__(self,path,name):
        self.name=name;self.db=sqlite3.connect(path);self.db.executescript('create table if not exists meta(k text primary key,v integer); create table if not exists log(idx integer primary key,term integer,key text,value integer);');self.db.execute("insert or ignore into meta values('term',0)");self.db.commit()
    def term(self):return self.db.execute("select v from meta where k='term'").fetchone()[0]
    def set_term(self,t):
        with self.db:self.db.execute("update meta set v=? where k='term'",(t,))
    def entries(self):return list(self.db.execute('select idx,term,key,value from log order by idx'))
    def append(self,index,term,key,value):
        with self.db:self.db.execute('insert or replace into log values(?,?,?,?)',(index,term,key,value))
    def close(self):self.db.close()
