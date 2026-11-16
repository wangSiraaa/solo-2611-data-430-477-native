import json,apsw
from planner import choose
class Module:
    def __init__(self,remote):self.remote=remote
    def Create(self,*args):return 'create table x(id INTEGER,name TEXT,amount INTEGER)',Table(self.remote)
    Connect=Create
class Table:
    def __init__(self,remote):self.remote=remote
    def BestIndexObject(self,info):return choose(info)
    def Open(self):return Cursor(self.remote)
    def Disconnect(self):pass
    Destroy=Disconnect
class Cursor:
    def __init__(self,remote):self.remote=remote;self.cid=None;self.rows=[];self.at=0
    def Filter(self,number,string,values):
        self.cid,self.rows=self.remote.open(list(zip(json.loads(string),values)));self.at=0
    def Eof(self):return self.at>=len(self.rows)
    def Rowid(self):return self.rows[self.at][0]
    def Column(self,col):return self.rows[self.at][col]
    def Next(self):self.at+=1
    def Close(self):
        if self.cid is not None:self.remote.close(self.cid)

def connect(remote):
    db=apsw.Connection(':memory:');db.create_module('source',Module(remote),use_bestindex_object=True,read_only=True);db.execute('create virtual table facts using source');return db
