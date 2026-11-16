import json
from catalog import Catalog
from pruner import keep

def evaluate(expr,row,params):
 op=expr['op']
 if op=='eq':
  a=row[expr['col']];b=params[expr['param']];return None if a is None or b is None else a==b
 if op=='isnull':return row[expr['col']] is None
 if op=='not':
  v=evaluate(expr['arg'],row,params);return None if v is None else not v
 if op in ('and','or'):
  vs=[evaluate(e,row,params) for e in expr['args']]
  return (False if False in vs else None if None in vs else True) if op=='and' else (True if True in vs else None if None in vs else False)
 raise ValueError(op)
def run(root,expr,params,prune=True):
 c=Catalog(root);out=[];opened=[]
 for leaf in c.children():
  if prune and not keep(leaf,expr,params):continue
  opened.append(leaf['name'])
  for line in (c.root/leaf['file']).open():
   r=json.loads(line)
   if evaluate(expr,r,params) is True:out.append(r['id'])
 return {'rows':sorted(out),'opened':opened}
