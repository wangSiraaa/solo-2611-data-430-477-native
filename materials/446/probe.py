import json,tempfile
from pathlib import Path
from ordered_state import partial
from run_pool import Pool
from finalizer import finish
rows=json.loads(Path('rows.json').read_text())
with tempfile.TemporaryDirectory() as d:
 p=Pool(d+'/state.sqlite');p.add(partial(rows[:2],'a'));p.add(partial(rows[2:],'b'))
 print(json.dumps({'one_chunk':partial(rows,'all'),'split_result':list(finish(p)),'expected_text':'甲|乙|丙|丁','truncated':list(finish(p,4))},ensure_ascii=False));p.close()
