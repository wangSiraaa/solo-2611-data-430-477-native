import tempfile,json,sys
from lexicon import Lexicon
from suggest import suggest
with tempfile.TemporaryDirectory() as d:
 l=Lexicon(d+'/l.sqlite')
 for id,term,f in [('a','cat',10),('b','bat',20),('c','catalog',100)]:l.put(id,term,f)
 if sys.argv[1]=='update':l.put('a','dog',11);l.delete('c')
 print(json.dumps({'prefix_ca':suggest(l,'ca','prefix'),'edit_cat':suggest(l,'cat','edit',max_edits=1),'actual_candidate_reads':l.reads,'expected_edit':['cat','bat'] if sys.argv[1]!='update' else ['bat'],'expected_prefix_ids':[] if sys.argv[1]=='update' else ['c','a']},sort_keys=True));l.close()
