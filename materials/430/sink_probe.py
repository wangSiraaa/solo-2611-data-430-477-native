import json
from publisher import Publisher
p=Publisher();v={'sequence':100,'sources':['s100'],'levels':[['ask',11,4],['bid',10,5]]};p.publish(0,'X',v)
try:p.publish(1,'X',{'sequence':101,'sources':['s100','d101'],'levels':[['ask',11,8],['bid',10,7]]},fail_after=1)
except OSError:pass
print(json.dumps(p.read_current('X'),sort_keys=True))
