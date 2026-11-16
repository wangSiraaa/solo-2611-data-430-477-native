import json
from pathlib import Path
class Catalog:
 def __init__(self,path):self.root=Path(path);self.leaves=json.loads((self.root/'catalog.json').read_text())
 def children(self):return self.leaves
