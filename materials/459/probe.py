import tempfile,json
from graph_index import GraphIndex
from phrase import search
with tempfile.TemporaryDirectory() as d:
 idx=GraphIndex(d+'/g.sqlite');idx.put('d',['new','york','city'])
 print(json.dumps({'linear_normal':search(idx,['new','york']),'false_phrase':search(idx,['nyc','york']),'missed_phrase':search(idx,['nyc','city']),'expected_false':[],'expected_nyc_city':['d']}));idx.close()
