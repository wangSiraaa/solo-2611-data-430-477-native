def key(labels,on):return tuple((k,labels.get(k,'')) for k in sorted(on))
def match(left,right,on):
 table={key(r['labels'],on):r for r in right};out=[]
 for l in left:
  r=table.get(key(l['labels'],on))
  if r is not None:out.append({'labels':l['labels'],'v':l['v']+r['v']})
 return out
