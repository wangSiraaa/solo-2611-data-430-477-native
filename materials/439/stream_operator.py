from channels import Sources
from checkpoint import Checkpoint

def run(frames,schedule,resume=None):
    state=dict((resume or {}).get('state',{}));offsets=dict((resume or {}).get('offsets',{'a':0,'b':0}));cp=Checkpoint();source=Sources(frames,offsets)
    try:
        for channel in schedule:
            n,frame=source.receive(channel);offsets[channel]=n+1
            if frame['kind']=='data':
                cp.data(channel,frame);key=frame['key'];state[key]=state.get(key,0)+frame['delta']
            else:cp.barrier(frame['id'],channel,state,offsets)
    finally:source.close()
    return state,cp.completed
