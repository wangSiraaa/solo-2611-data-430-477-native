import json
from pathlib import Path
def export(store,destination):
    out=Path(destination);out.mkdir(exist_ok=True);manifest={}
    for n,path in enumerate(store.paths()):
        node=store.stat(path)
        if node['kind']=='dir':manifest[path]={'kind':'dir'};continue
        body=f'data{n}';(out/body).write_bytes(store.read(path));manifest[path]={'kind':'file','body':body,'inode':str(n)}
    (out/'manifest.json').write_text(json.dumps(manifest,sort_keys=True))
