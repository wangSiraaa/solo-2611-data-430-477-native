import sqlite3,json
class Publisher:
    def __init__(self):
        self.db=sqlite3.connect(':memory:')
        self.db.executescript('create table levels(symbol text,side text,price integer,qty integer check(qty>0),primary key(symbol,side,price)); create table frontier(symbol text primary key,seq integer,sources text); create table observations(pos integer,symbol text,payload text,primary key(pos,symbol));')
    def publish(self,position,symbol,view,fail_after=None):
        # This importer presently replaces normalized rows one at a time.
        self.db.execute('delete from levels where symbol=?',(symbol,));self.db.commit()
        for n,(side,price,quantity) in enumerate(view['levels'],1):
            self.db.execute('insert into levels values(?,?,?,?)',(symbol,side,price,quantity));self.db.commit()
            if fail_after==n:raise OSError('injected storage write failure')
        self.db.execute('insert or replace into frontier values(?,?,?)',(symbol,view['sequence'],json.dumps(view['sources'])));self.db.commit()
        with self.db:self.db.execute('insert into observations values(?,?,?)',(position,symbol,json.dumps(view,sort_keys=True)))
    def read_current(self,symbol):
        f=self.db.execute('select seq,sources from frontier where symbol=?',(symbol,)).fetchone()
        return {'sequence':f[0] if f else None,'sources':json.loads(f[1]) if f else [],'levels':[list(r) for r in self.db.execute('select side,price,qty from levels where symbol=? order by side,price',(symbol,))]}
    def observations(self):
        return [{'position':p,'symbol':s,**json.loads(v)} for p,s,v in self.db.execute('select pos,symbol,payload from observations order by pos,symbol')]
