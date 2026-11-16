import json
from pathlib import Path
from writer import Store
from index import Index
cols=json.loads(Path('schema.json').read_text());idx=Index('y');s=Store(cols,idx);s.put('a',{'x':2});first=s.rows['a'].copy();s.put('a',{'x':8})
print(json.dumps({'first':first,'after':s.rows['a'],'old_index_key':idx.find(3),'new_index_key':idx.find(9),'expected_first':{'x':2,'y':3,'z':4},'expected_old_key':[]}))
