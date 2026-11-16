import tempfile,json
from lexicon import Lexicon
from suggest import suggest
from benchmark import rows
with tempfile.TemporaryDirectory() as d:
 l=Lexicon(d+'/l.sqlite')
 for row in rows():l.put(**row)
 result=suggest(l,'abcdef123456','edit',k=20,max_edits=2)
 print(json.dumps({'actual':result,'complete_term_records_read':l.reads,'expected_terms':['abcdef123456','abcdef123457','abcdef12345','abcxef123456'],'dictionary_terms':10000},sort_keys=True));l.close()
