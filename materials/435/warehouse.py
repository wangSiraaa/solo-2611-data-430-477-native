import json,sys
from code_map import CodeMap
from inline_map import resolve

def process(events):
    directory=CodeMap();facts=[]
    # Current code builds its address directory first, then resolves sample facts.
    for e in events:
        if e['kind']=='load':directory.load(e)
        elif e['kind']=='move':directory.move(e)
        elif e['kind']=='unload':directory.unload(e)
    for e in events:
        if e['kind']!='sample':continue
        frames=[]
        for pc in e['pcs']:
            hit=directory.lookup(pc)
            frames.append({'pc':pc,'stack':resolve(hit[1],hit[2]),'code_id':hit[0]} if hit else {'pc':pc,'stack':[],'reason':'unmapped'})
        facts.append({'sample_id':e['id'],'position':e['position'],'frames':frames})
    return facts
if __name__=='__main__':print(json.dumps(process(json.load(open(sys.argv[1]))),sort_keys=True))
