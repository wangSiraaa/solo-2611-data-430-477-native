import json
from cursor import Provider
from apply import lateral
p=Provider();rows=list(lateral([{'occurrence_id':'a','x':4,'n':1},{'occurrence_id':'b','x':4,'n':1}],p,{'parent':'x'},{'parent':'n'},True))
print(json.dumps({'rows':rows,'audit':p.audit,'expected_v':[4,4],'expected_open_close':[2,2]}))
