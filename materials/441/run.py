import json,sys
from pathlib import Path
from model import Model
from query import QueryEngine
model=Model(json.loads(Path(sys.argv[1]).read_text()))
queries=json.loads(Path(sys.argv[2]).read_text())
engine=QueryEngine(model)
print(json.dumps({'results':[engine.run(q) for q in queries],'audit':model.audit},ensure_ascii=False,sort_keys=True))
