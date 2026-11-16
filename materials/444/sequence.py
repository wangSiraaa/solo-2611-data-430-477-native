class Sequence:
    def __init__(self):self.chars=[]
    def insert(self,event):
        anchor=event['after'];at=0 if anchor is None else next(i for i,x in enumerate(self.chars) if x['id']==anchor)+1
        made=[{'id':f"{event['replica']}:{event['seq']}:{n}",'char':c,'deleted':False} for n,c in enumerate(event['text'])]
        self.chars[at:at]=made
    def delete(self,ids):
        for x in self.chars:
            if x['id'] in ids:x['deleted']=True
    def text(self):return ''.join(x['char'] for x in self.chars if not x['deleted'])
