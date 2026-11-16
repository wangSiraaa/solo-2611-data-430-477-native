from time_order import leq

def compact(arrangement,since):
    target=min(map(tuple,since));rows=arrangement.records();merged={}
    for k,v,t0,t1,diff in rows:
        time=target if leq((t0,t1),target) else (t0,t1);key=(k,v,*time);merged[key]=merged.get(key,0)+diff
    with arrangement.db:
        arrangement.db.execute('delete from changes');arrangement.db.executemany('insert into changes values(?,?,?,?,?)',[(*key,d) for key,d in merged.items() if d])
        arrangement.db.execute("update meta set v=? where k='since'",(__import__('json').dumps(since),))
