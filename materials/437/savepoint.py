import json
from state import State

def save(states,path):
    # Timer heaps were considered execution-local and omitted.
    all_counts={k:v for state in states for k,v in state.counts.items()};open(path,'w').write(json.dumps({'counts':all_counts}))
def restore(path,workers,group):
    d=json.load(open(path));states=[State() for _ in range(workers)]
    for k,v in d['counts'].items():states[group(k)%workers].counts[k]=v
    return states
