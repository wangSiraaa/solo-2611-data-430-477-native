import json
from merger import merge
from query import quantile
p={'scale':1,'positive':{'2':1},'negative':{},'zero':0,'count':1,'zero_threshold':0}
q={'scale':0,'positive':{'1':1},'negative':{},'zero':0,'count':1,'zero_threshold':0}
for no,h in enumerate([p,q]):
 h.update(metric='latency',labels={'service':'a'},window=[0,10],chunk_id=str(no))
 h['payload_sha256']=__import__('hashlib').sha256(json.dumps(h,sort_keys=True).encode()).hexdigest()
a=merge([p,q]);n={'scale':0,'positive':{},'negative':{'0':1,'2':1},'zero':0,'count':2,'zero_threshold':0}
print(json.dumps({'merged':a,'expected_positive':{'1':2},'negative_q0':quantile(n,0),'expected_negative_q0':[-8,-4]}))
