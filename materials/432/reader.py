def select(store,rows,columns,axis='row'):
    out={}
    directory=store.meta['rows' if axis=='row' else 'columns'];wanted=rows if axis=='row' else columns
    files=set(name for k in wanted for name in directory.get(str(k//8),[]))
    for name in sorted(files):
        for r,c,v in store.read(name):
            if r in rows and c in columns:out[(r,c)]=v
    return [[r,c,out.get((r,c),0)] for r in rows for c in columns]
