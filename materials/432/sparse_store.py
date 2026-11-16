import json
from pathlib import Path
class SparseStore:
    def __init__(self,root):
        self.root=Path(root);self.root.mkdir(exist_ok=True);self.meta={'rows':{},'columns':{}};self.reads=[]
    def build(self,triples):
        buckets={}
        for r,c,v in triples:buckets.setdefault((r//8,c//8),[]).append([r,c,v])
        for (br,bc),rows in buckets.items():
            name=f'{br}-{bc}.json';(self.root/name).write_text(json.dumps(rows));self.meta['rows'].setdefault(str(br),[]).append(name);self.meta['columns'].setdefault(str(bc),[]).append(name)
        self.publish()
    def patch(self,row,col,value):
        br,bc=row//8,col//8;name=f'{br}-{bc}.json';path=self.root/name
        cells={(r,c):v for r,c,v in json.loads(path.read_text())} if path.exists() else {}
        if value==0:cells.pop((row,col),None)
        else:cells[(row,col)]=value
        path.write_text(json.dumps([[r,c,v] for (r,c),v in sorted(cells.items())]));self.meta['rows'].setdefault(str(br),[])
        if name not in self.meta['rows'][str(br)]:self.meta['rows'][str(br)].append(name)
        self.publish()
    def publish(self):(self.root/'catalog.json').write_text(json.dumps(self.meta))
    def read(self,name):
        raw=(self.root/name).read_bytes();self.reads.append({'file':name,'bytes':len(raw)});return json.loads(raw)
