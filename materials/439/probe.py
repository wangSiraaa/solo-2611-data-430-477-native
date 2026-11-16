import json,sys
from stream_operator import run
D=lambda value:{'kind':'data','key':'k','delta':value}
B=lambda n:{'kind':'barrier','id':n}
if sys.argv[1]=='normal':frames={'a':[D(1),B(1),D(2),B(2)],'b':[D(10),B(1),D(20),B(2)]};schedule=['a','b','a','b','a','b','a','b']
else:frames={'a':[D(1),B(1),D(2),B(2)],'b':[D(10),D(20),B(1),B(2)]};schedule=['a','a','b','a','b','b','a','b']
for ch,rows in frames.items():
 for n,frame in enumerate(rows):
  if frame['kind']=='data':frame['source_seq']=n
final,cps=run(frames,schedule)
left={ch:len(rows)-cps[1]['offsets'][ch] for ch,rows in frames.items()};remaining=[]
while any(left.values()):
 for ch in ['a','b']:
  if left[ch]:remaining.append(ch);left[ch]-=1
resumed,_=run(frames,remaining,cps[1]);print(json.dumps({'full':final,'checkpoint1':cps[1],'resumed':resumed,'expected':final},sort_keys=True))
