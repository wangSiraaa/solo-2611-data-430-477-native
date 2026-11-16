import json

def save(sample,path):open(path,'w').write(json.dumps(sample))
def load(path):return json.load(open(path))
def combine(parts):
 out=parts[0].copy();out['pages']=sum((p['pages'] for p in parts),[]);return out
