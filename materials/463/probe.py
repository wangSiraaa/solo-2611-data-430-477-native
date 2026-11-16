import tempfile,sqlite3,json
from pathlib import Path
from pool import Pool
with tempfile.TemporaryDirectory() as td:
    p=Path(td)
    for t in ('a','b'):
        d=sqlite3.connect(p/t);d.execute('create table data(v)');d.execute('insert into data values(?)',(t,));d.commit();d.close()
    pool=Pool(1);a=pool.acquire('a',p/'a')
    assert a.session.db.execute('select v from tenant.data').fetchone()[0]=='a'
    a.session.db.execute('create temp table prior_secret(v)');a.session.db.execute("insert into prior_secret values('secret')");a.session.db.commit()
    pool.release(a);b=pool.acquire('b',p/'b')
    print(json.dumps({'normal_tenant':b.session.db.execute('select v from tenant.data').fetchone()[0],'leaked_temp_table':bool(b.session.db.execute("select 1 from sqlite_temp_master where name='prior_secret'").fetchone())}))
    pool.release(b);pool.close()
