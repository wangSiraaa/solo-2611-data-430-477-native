import json,zlib
from pathlib import Path
class PackReader:
    def __init__(self,root):self.root=Path(root);self.index=json.loads((self.root/'index.json').read_text());self.reads=[]
    def entry(self,oid):return self.index[oid]
    def payload(self,entry):
        with (self.root/entry['pack']).open('rb') as f:f.seek(entry['offset']);raw=f.read(entry['length'])
        self.reads.append({'pack':entry['pack'],'offset':entry['offset'],'compressed_bytes':len(raw)})
        return zlib.decompress(raw)
