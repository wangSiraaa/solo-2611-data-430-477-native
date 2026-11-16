import json
from pathlib import Path
class Layer:
    def __init__(self,root):self.root=Path(root);self.entries=json.loads((self.root/'manifest.json').read_text())
class Lookup:
    def __init__(self,layers):self.layers=layers;self.visible={};self.reads=[]
    def build(self):
        for generation,layer in enumerate(self.layers):
            for path,node in layer.entries.items():
                if node['kind'] in ('whiteout','opaque'):continue
                self.visible[path]={**node,'layer':generation,'root':str(layer.root)}
        return self.visible
    def node(self,path):return self.visible[path]
    def read_body(self,node):
        p=Path(node['root'])/node['body'];data=p.read_bytes();self.reads.append((str(p),len(data)));return data
