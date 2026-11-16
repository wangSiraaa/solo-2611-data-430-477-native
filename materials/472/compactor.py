from reader import apply
def compact(selected,output):
    rows=[r for t in selected for r in t.records()];groups={}
    for r in rows:groups.setdefault(r['key'],[]).append(r)
    result=[]
    for key,rs in groups.items():
        latest=max(rs,key=lambda r:r['seq'])
        if latest['op']=='delete':continue
        result.append({'key':key,'seq':latest['seq'],'op':'put','value':apply(None,latest)})
    return output.write(result)
