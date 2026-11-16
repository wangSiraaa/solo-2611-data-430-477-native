import tempfile,json
from pathlib import Path
from sst import SST
from reader import get
from compactor import compact
with tempfile.TemporaryDirectory() as td:
 p=Path(td);base=SST(p/'base').write([{'key':'k','seq':1,'op':'put','value':10}]);delta=SST(p/'delta').write([{'key':'k','seq':2,'op':'mul','value':2},{'key':'k','seq':3,'op':'add','value':3}]);assert get([base],'k',1)==10
 before=get([base,delta],'k',3);out=compact([delta],SST(p/'out'));after=get([base,out],'k',3)
 print(json.dumps({'read_chain':before,'after_partial':after,'expected':23}))
