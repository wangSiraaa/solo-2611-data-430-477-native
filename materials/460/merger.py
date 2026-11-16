from histogram import validate

def merge(parts):
 for p in parts:validate(p)
 out={'scale':min(p['scale'] for p in parts),'positive':{},'negative':{},'zero':sum(p['zero'] for p in parts),'count':sum(p['count'] for p in parts),'zero_threshold':max(p['zero_threshold'] for p in parts)}
 for p in parts:
  for side in ('positive','negative'):
   for key,n in p[side].items():out[side][key]=out[side].get(key,0)+n
 return out
