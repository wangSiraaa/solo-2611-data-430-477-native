import json
from logstore import LogStore,root_hash
from proofs import consistency
from witness import Witness
old=[b'a',b'b'];s=LogStore()
for x in old+[b'c']:s.append(x)
w=Witness(2,root_hash(old).hex());assert w.accept(*s.head(),consistency(s,2))
forged=LogStore()
for x in [b'a',b'REPLACED',b'c']:forged.append(x)
w=Witness(2,root_hash(old).hex());accepted=w.accept(*forged.head(),consistency(forged,2))
print(json.dumps({'valid_append':True,'changed_old_prefix_accepted':accepted,'old_root':root_hash(old).hex(),'new_root':forged.head()[1]}))
