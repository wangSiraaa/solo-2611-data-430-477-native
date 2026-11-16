from index import candidates

def distance(a,b):
    row=list(range(len(b)+1))
    for i,x in enumerate(a,1):
        nxt=[i]
        for j,y in enumerate(b,1):nxt.append(min(nxt[-1]+1,row[j]+1,row[j-1]+(x!=y)))
        row=nxt
    return row[-1]
def suggest(lexicon,query,mode,k=10,max_edits=1):
    out=[]
    for id,term,freq in candidates(lexicon,query)[:k]:
        d=distance(query,term[:len(query)])
        if (term.startswith(query) if mode=='prefix' else d<=max_edits):out.append({'id':id,'term':term,'freq':freq,'distance':d})
    return sorted(out,key=lambda r:(r['distance'],-r['freq'],r['term'],r['id']))[:k]
