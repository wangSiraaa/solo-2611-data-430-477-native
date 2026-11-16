import sqlite3,json
class Pool:
    def __init__(self,path):
        self.db=sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS runs(chunk TEXT,g TEXT,payload TEXT,PRIMARY KEY(chunk,g))')
    def add(self,states):
        with self.db:
            for s in states:self.db.execute('INSERT OR REPLACE INTO runs VALUES(?,?,?)',(s['chunk'],s['group'],json.dumps(s)))
    def states(self):
        for row in self.db.execute('SELECT payload FROM runs ORDER BY rowid'):yield json.loads(row[0])
    def close(self):self.db.close()
