import tempfile,json
from pathlib import Path
from layer_lookup import Layer,Lookup
from inode_store import InodeStore
with tempfile.TemporaryDirectory() as td:
 p=Path(td);base=p/'base';top=p/'top';base.mkdir();top.mkdir();(base/'body').write_bytes(b'old')
 (base/'manifest.json').write_text(json.dumps({'d':{'kind':'dir'},'d/a':{'kind':'file','inode':'i','body':'body'},'alias':{'kind':'file','inode':'i','body':'body'},'alias2':{'kind':'file','inode':'i','body':'body'}}))
 (top/'manifest.json').write_text(json.dumps({'d':{'kind':'opaque'}}));store=InodeStore(Lookup([Layer(base),Layer(top)]));assert store.read('alias')==b'old'
 hidden='d/a' in store.paths();store.write('alias',b'new')
 print(json.dumps({'opaque_lower_file_visible':hidden,'alias':store.read('alias').decode(),'other_hardlink':store.read('alias2').decode(),'base_unchanged':(base/'body').read_text()}))
