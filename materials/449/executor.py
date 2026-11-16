def eq(a,b):return a is not None and b is not None and a==b
def execute(plan,tables,metrics=None):
 if metrics is None:metrics=[]
 if 'scan' in plan:
  alias=plan['scan'];return [{alias+'.'+k:v for k,v in r.items()} for r in tables[alias]]
 l=execute(plan['left'],tables,metrics);r=execute(plan['right'],tables,metrics);out=[]
 cols=set().union(*(x.keys() for x in r)) if r else set(plan.get('right_cols',[]))
 for a in l:
  hits=[]
  for b in r:
   row=a|b;x,y=plan['on']
   if eq(row.get(x),row.get(y)):hits.append(row)
  if hits:out.extend(hits)
  elif plan['join']=='left':out.append(a|{c:None for c in cols})
 metrics.append({'join':plan['join'],'left_rows':len(l),'right_rows':len(r),'out_rows':len(out)})
 return out
