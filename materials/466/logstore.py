import sqlite3,hashlib
H=lambda x:hashlib.sha256(x).digest()
def leaf(x):return H(b'\x00'+x)
def parent(a,b):return H(b'\x01'+a+b)
def root_hash(leaves):
    n=len(leaves)
    if not n:return H(b'')
    if n==1:return leaf(leaves[0])
    k=1<<((n-1).bit_length()-1)
    return parent(root_hash(leaves[:k]),root_hash(leaves[k:]))
class LogStore:
    def __init__(self,path=':memory:'):
        self.db=sqlite3.connect(path);self.db.execute('create table if not exists leaves(id integer primary key,body blob)');self.reads=[]
    def append(self,body):
        with self.db:self.db.execute('insert into leaves(body) values(?)',(body,))
    def leaves(self,size=None):
        q='select body from leaves order by id';rows=[r[0] for r in self.db.execute(q)]
        self.reads.append(('raw',len(rows)));return rows if size is None else rows[:size]
    def head(self):
        rows=self.leaves();return len(rows),root_hash(rows).hex()
