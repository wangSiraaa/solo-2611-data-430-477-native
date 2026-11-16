import json
from engine import instant,evaluate
l=[{'labels':{'__name__':'left_metric','job':'a','instance':'x'},'samples':[{'t':0,'seq':0,'v':2},{'t':5,'seq':0,'stale':True}]}]
r=[{'labels':{'__name__':'right_metric','job':'a','source':'one'},'samples':[{'t':5,'seq':0,'v':3}]},{'labels':{'__name__':'right_metric','job':'a','source':'two'},'samples':[{'t':5,'seq':0,'v':9}]}]
print(json.dumps({'stale_actual':instant(l,6,10),'expected_stale':[],'duplicate_right':evaluate(l,r,4,10,['job']),'expected_matching':'error if duplicate right is active','duplicate_at6':evaluate(l,r,6,10,['job'])}))
