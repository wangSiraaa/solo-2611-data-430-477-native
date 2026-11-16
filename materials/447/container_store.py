import json
from bit_slices import Slices

def save(index,path):
 data={'bits':[hex(b) for b in index.bits],'live':hex(index.live),'nulls':hex(index.nulls),'slots':index.slots,'next':index.next}
 open(path,'w').write(json.dumps(data))
def load(path):
 d=json.load(open(path));s=Slices();s.bits=[int(x,16) for x in d['bits']];s.live=int(d['live'],16);s.nulls=int(d['nulls'],16);s.slots=d['slots'];s.next=d['next'];return s
