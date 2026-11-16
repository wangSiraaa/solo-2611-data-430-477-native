import json,tempfile
from pathlib import Path
from index import Index
from search import search

def box(a,b):return {'type':'Polygon','coordinates':[[[a,-1],[b,-1],[b,1],[a,1],[a,-1]]]}
with tempfile.TemporaryDirectory() as d:
 idx=Index(d+'/geo.sqlite');idx.put('cross',json.loads(Path('cross.json').read_text()))
 print(json.dumps({'date_line':search(idx,box(178,179)),'greenwich':search(idx,box(-1,1)),'expected':{'date_line':['cross'],'greenwich':[]}}));idx.close()
