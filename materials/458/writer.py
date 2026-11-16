import json
from pathlib import Path
from schema import generate
class Store:
 def __init__(self,columns,index):self.columns=columns;self.rows={};self.index=index
 def put(self,id,base):
  row=generate(self.columns,base);self.rows[id]=row;self.index.add(id,row)
 def export(self,path):Path(path).write_text(json.dumps(self.rows))
