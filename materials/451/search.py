import json
from geo_model import geometry

def search(index,query):
 q=geometry(query);out=[];seen=[]
 for id,body in index.candidates(q.bounds):
  seen.append(id)
  if geometry(json.loads(body)).intersects(q):out.append(id)
 return {'ids':sorted(out),'candidates':seen}
