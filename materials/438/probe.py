import tempfile,json
from pathlib import Path
from runner import Runner
with tempfile.TemporaryDirectory() as td:
    out={};batches=[['海',None,''],['salt']]
    for name in ('identity','bad_descriptor','second_trap','loop'):
        out[name]=Runner().run(Path(name+'.wat').read_text(),batches,str(Path(td)/(name+'.json')))
    print(json.dumps(out,ensure_ascii=False,sort_keys=True))
