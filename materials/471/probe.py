import tempfile,json
from page_tree import PageTree
from navigator import find
from cursor import Cursor
with tempfile.TemporaryDirectory() as td:
 t=PageTree(td,4)
 for k in range(4):t.insert(k,str(k))
 assert find(t,3)=='3';during=[]
 t.insert(4,'4',split_gate=lambda:during.append(find(t,3)))
 print(json.dumps({'before_parent_install':during,'after_parent_install':find(t,3),'range':Cursor(t,0,5).all(),'expected_during':'3'}))
