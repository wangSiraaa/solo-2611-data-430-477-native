import json,hashlib
from pathlib import Path
class Source:
 def __init__(self,root):self.root=Path(root);self.reads=[]
 def catalog(self):return json.loads((self.root/'catalog.json').read_text())
 def page(self,id):
  raw=(self.root/(id+'.json')).read_bytes();self.reads.append({'id':id,'bytes':len(raw)});return json.loads(raw)
