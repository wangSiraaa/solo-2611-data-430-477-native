import json
from sequence import Sequence
from merge import Replica
from annotations import Annotations
base={'replica':'R','seq':1,'op':'insert','after':None,'text':'猫狗','context':{},'lamport':1}
a={'replica':'A','seq':1,'op':'insert','after':'R:1:0','text':'甲','context':{'R':1},'lamport':2}
b={'replica':'B','seq':1,'op':'insert','after':'R:1:0','text':'乙','context':{'R':1},'lamport':2}
s1=Sequence();s2=Sequence();r1=Replica(s1);r2=Replica(s2);r1.merge([base]);r2.merge([base]);ann=Annotations();ann.add('label-dog',1,2,'animal');r1.merge([a,b]);r2.merge([b,a])
print(json.dumps({'replica1':s1.text(),'replica2':s2.text(),'annotation_after_insert':ann.export(s1),'expected':'same replica text; label-dog must still quote 狗 using character IDs'},ensure_ascii=False))
