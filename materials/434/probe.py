import json,sys,sqlite3
from remote import Remote
from virtual import connect
r=Remote();db=connect(r);oracle=sqlite3.connect(':memory:');oracle.execute('create table facts(id INTEGER,name TEXT,amount INTEGER)');oracle.executemany('insert into facts values(?,?,?)',r.rows)
queries={'normal':"select id,name,amount from facts where id=1",'residual':"select id,name,amount from facts where name='ann' order by id",'order':'select id,amount from facts where amount is not null order by amount','reuse':'select o.id,f.id from (select 1 id union all select 3) o join facts f on f.id=o.id'}
sql=queries[sys.argv[1]];got=list(db.execute(sql));expected=list(oracle.execute(sql));print(json.dumps({'sql':sql,'actual':got,'expected':expected,'remote_calls':r.calls,'leaked_cursors':sorted(r.active)},sort_keys=True));db.close()
