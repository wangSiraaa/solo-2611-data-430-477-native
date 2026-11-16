import tempfile,json
from bit_slices import Slices
from container_store import save,load
from query import ge,ids,total,not_mask
s=Slices()
for rid,v in [('a',-2),('b',3),('c',None),('d',5)]:s.put(rid,v)
s.delete('d')
with tempfile.TemporaryDirectory() as d:
 save(s,d+'/s.json');s=load(d+'/s.json');m=ge(s,0)
 print(json.dumps({'positive_normal':'b','selected':ids(s,m),'sum':total(s,m),'expected_ids':['b'],'expected_sum':3,'not_selected_live_count':not_mask(s,m).bit_count(),'expected_not_true_count':1}))
