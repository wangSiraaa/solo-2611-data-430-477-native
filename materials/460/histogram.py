def validate(h):
 assert -2<=h['scale']<=4
 assert sum(h['positive'].values())+sum(h['negative'].values())+h['zero']==h['count']
 assert all(isinstance(v,int) and v>=0 for v in list(h['positive'].values())+list(h['negative'].values()))
 return h
