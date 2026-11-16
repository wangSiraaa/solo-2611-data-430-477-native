import tempfile,json
from column_store import Column
with tempfile.TemporaryDirectory() as d:
 c=Column(d);values=['',None,'x','x','y','y'];c.build(values)
 print(json.dumps({'actual':c.project([0,1,2,4,5]),'expected':['',None,'x','y','y'],'reopened':Column(d).project([2])}))
