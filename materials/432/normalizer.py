def normalize(triples):
    values={}
    for row,col,value in triples:values[(row,col)]=value
    return [(r,c,v) for (r,c),v in sorted(values.items()) if v!=0]
