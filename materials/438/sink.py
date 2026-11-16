import json
from pathlib import Path
class Sink:
    def __init__(self,path):self.path=Path(path);self.rows=[];self.receipts=[]
    def append(self,batch):
        self.rows.extend(batch['rows']);self.receipts.append(batch['batch_no'])
        self.path.write_text(json.dumps({'status':'complete','rows':self.rows,'batches':self.receipts},ensure_ascii=False))
    def inspect(self):return json.loads(self.path.read_text()) if self.path.exists() else None
