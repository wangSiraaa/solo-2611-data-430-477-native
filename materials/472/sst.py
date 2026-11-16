import json
from pathlib import Path
class SST:
    def __init__(self,path):self.path=Path(path)
    def write(self,records):self.path.write_text('\n'.join(json.dumps(r,sort_keys=True) for r in sorted(records,key=lambda x:(x['key'],-x['seq'])))+'\n');return self
    def records(self,key=None):
        rows=[json.loads(s) for s in self.path.read_text().splitlines() if s]
        return [r for r in rows if key is None or r['key']==key]
