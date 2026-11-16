import json,tempfile,sys
from normalizer import normalize
from sparse_store import SparseStore
from reader import select
with tempfile.TemporaryDirectory() as d:
 s=SparseStore(d);triples=[[0,0,2],[1,1,None]]
 if sys.argv[1]=='duplicate':triples+=[[0,0,3]]
 s.build(normalize(triples))
 if sys.argv[1]=='patch':s.patch(9,17,7)
 print(json.dumps({'row':select(s,[0],[0]),'patched_column':select(s,[9],[17],axis='column'),'reads':s.reads,'expected_00':5 if sys.argv[1]=='duplicate' else 2,'expected_9_17':7 if sys.argv[1]=='patch' else 0},sort_keys=True))
