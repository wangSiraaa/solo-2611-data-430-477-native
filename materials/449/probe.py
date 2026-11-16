import json
from pathlib import Path
schemas=json.loads(Path('schemas.json').read_text())
from executor import execute
from reorder import optimize
p={'join':'inner','left':{'join':'left','left':{'scan':'A'},'right':{'scan':'B'},'on':['A.k','B.k'],'right_cols':['B.k']},'right':{'scan':'C'},'on':['B.k','C.k']}
t={'A':[{'occurrence_id':'a1','k':1},{'occurrence_id':'a2','k':2}],'B':[{'occurrence_id':'b1','k':1}],'C':[{'occurrence_id':'c1','k':1}]}
print(json.dumps({'normal':execute(p,t),'rotated':execute(optimize(p),t),'expected_rows':1},ensure_ascii=False))
