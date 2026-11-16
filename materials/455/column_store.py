import json
from pathlib import Path
from encoder import encode
from decoder import read
class Column:
 def __init__(self,root):self.root=Path(root);self.root.mkdir(exist_ok=True)
 def build(self,values,block_size=4):
  catalog=[]
  for n,start in enumerate(range(0,len(values),block_size)):
   b=encode(values[start:start+block_size]);p=self.root/f'{n}.json';p.write_text(json.dumps(b));catalog.append({'file':p.name,'start':start,'count':min(block_size,len(values)-start)})
  (self.root/'catalog.json').write_text(json.dumps(catalog))
 def project(self,positions):
  catalog=json.loads((self.root/'catalog.json').read_text());out=[]
  for pos in positions:
   b=next(x for x in catalog if x['start']<=pos<x['start']+x['count']);out.extend(read(json.loads((self.root/b['file']).read_text()),[pos-b['start']]))
  return out
